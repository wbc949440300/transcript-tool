"""核心提取逻辑：yt-dlp 抓取信息/字幕/音频，faster-whisper 语音转文字。

尊重并遵守：
- 仅提取用户主动粘贴、有权访问的公开链接的元数据/字幕。
- 严格遵守目标平台的服务条款与版权；本项目用于技术演示与自身内容。
"""
from __future__ import annotations

import logging
import os
import os as _os
import random
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from .models import TaskDetail, TaskStatus
from .dual_asr import dual_asr_correct, vosk_available
from .douyin import fetch_douyin, normalize_url
from .kuaishou import fetch_kuaishou
from .platforms import adapter_for
from .robust_media import _referer_for, download_media
from .task_store import store
from .utils import (account_key, classify_failure, cookie_available_for, cookie_file_for,
                   detect_platform, fetch_subtitle_text, mark_account_cooldown,
                   mark_platform_cooldown, pick_account, pick_proxy_available,
                   platform_cooldown_enabled, platform_cooldown_remaining,
                   platform_of, proxy_for, record_account_result, should_use_cookie,
                   throttle)

logger = logging.getLogger("transcript")

WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_WHISPER = "medium"
# 口播短于这个字数就认为"没有有效口播"，改用画面字幕作正文（字幕优先，2026-10-31）
_SUBTITLE_AS_MAIN_MIN = 15
_WHISPER_LOCK = threading.Lock()


def _whisper_device() -> str:
    """选择 whisper 推理设备。

    faster-whisper 的 device=\"auto\" 会在 CUDA 缺 cuBLAS DLL 时延迟到 encode
    阶段才报错。这里默认强制 CPU（最稳）；需要 GPU 的用户自行设置
    TRANSCRIPT_USE_CUDA=1 并确保已安装可用的 CUDA / cuBLAS 运行库。
    """
    if os.getenv("TRANSCRIPT_USE_CUDA", "") == "1":
        return "cuda"
    return "cpu"


# ---------- yt-dlp 文案抓取 ----------

def _host_of(u: str) -> str:
    m = re.match(r"https?://([^/?#]+)", u or "", re.I)
    return m.group(1).lower() if m else ""


def _extract_info(url: str) -> dict:
    """返回 (dict) 元数据；失败抛异常。

    重试策略（2026-10-31 改）：
      - 默认匿名；**只有判定为"缺登录态"（login）的失败才在下次带上 cookie**
      - 风控（risk）→ 立刻停手并按平台冷却（带出口），不再消耗请求
      - 内容不存在/不支持（permanent）→ 直接失败，不做无意义重试
      - 网络/超时（transient/unknown）→ 退避后**原样（匿名）**重试
    旧实现是"失败就带 cookie 重试"，等于把账号反复推到平台面前，且一条失效
    链接要换 3 次脸面 —— 请求放大的主因。
    """
    import yt_dlp

    plat = platform_of(url)
    adapter = adapter_for(url)
    # 冷却闸门现在放在每次尝试里（按本次要用的出口判定），见下面的 pick_proxy_available
    last_exc: Exception | None = None
    login_required = False
    for attempt in range(1, 3):        # 最多 2 次：yt-dlp 内部已有 5 次网络重试
        throttle(_host_of(url), platform=plat)
        used_ck = None
        px, eg = None, "direct"        # 本次尝试用的出口（可能没走到赋值就抛异常）
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "extract_flat": False,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["zh-CN", "zh-Hans", "zh", "en"],
                "youtubedl_search_match_filter": None,
                "socket_timeout": 30,
                "retries": 5,
            }
            if adapter.needs_login and should_use_cookie(url, attempt, login_required=login_required):
                ck = pick_account(plat) or cookie_file_for(url)
                if ck:
                    ydl_opts["cookiefile"] = ck
                    used_ck = ck
                    logger.info("元数据抓取带 cookie（需要登录态）: %s", Path(ck).name)
            # 出口：有账号时绑定到该账号（同一个号固定一条出口），匿名轮询
            px, eg = pick_proxy_available(plat, key=account_key(used_ck))
            if px:
                ydl_opts["proxy"] = px
            remain = platform_cooldown_remaining(plat, egress=eg)
            if remain > 0 and platform_cooldown_enabled(plat):
                raise RuntimeError(
                    f"{detect_platform(url)} 风控冷却中（约 {int(remain // 60) + 1} 分钟后自动恢复）。"
                    "刚被限流时继续请求会加深标记；建议先提取其他平台，到点自动恢复。")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    if used_ck:
                        record_account_result(used_ck, True, plat)
                    return info
            raise RuntimeError("extract_info 返回空")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            kind = classify_failure(exc)
            logger.warning("元数据抓取尝试 %d/2 失败（%s）: %s", attempt, kind, exc)
            if kind == "risk":
                if used_ck:
                    mark_account_cooldown(used_ck, 900)      # 只有真的用了账号才罚它
                    record_account_result(used_ck, False, plat, str(exc)[:120])
                if platform_cooldown_enabled(plat):
                    mark_platform_cooldown(plat, adapter.cooldown_base,
                                           kind="risk", reason=str(exc)[:120], egress=eg)
                raise
            if kind == "permanent":
                raise
            if kind == "login":
                login_required = True
            if attempt >= 2:
                break
            time.sleep(2 ** attempt + random.uniform(0, 1.5))
    raise last_exc if last_exc else RuntimeError("元数据抓取失败")


def _clean_flags(raw: str) -> str:
    """去掉字幕行内的时间戳/序号等噪音，只保留纯文本。"""
    if not raw:
        return ""
    text = raw
    # 移掉 SRT/VTT 行号与时间轴
    text = re.sub(r"^\d+\s*$", "", text, flags=re.M)
    text = re.sub(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->.*$", "", text, flags=re.M)
    # 移除 `<...>` 标签
    text = re.sub(r"<[^>]+>", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln and ln.strip()]
    # 去重连续重复（部分平台字幕分段重复行）
    out: list[str] = []
    for ln in lines:
        if not out or ln != out[-1]:
            out.append(ln)
    return "\n".join(out)


_SUBTITLE_LANGS = ("zh-CN", "zh-Hans", "zh", "zh-Hant", "zh-TW", "en", "en-US")
# 这些键不是字幕：B站会把弹幕(danmaku)塞进 subtitles，YouTube 直播聊天同理
_NOT_SUBTITLE_KEYS = ("danmaku", "live_chat", "comments", "chat", "rechat")
_LANG_KEY = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")


def _subtitle_keys(subs: dict) -> list[str]:
    """挑出真正像"语言"的键：首选语言在前，其它语言在后，弹幕等非字幕项跳过。"""
    keys = [str(k) for k in subs.keys() if str(k).lower() not in _NOT_SUBTITLE_KEYS]
    preferred = [k for k in _SUBTITLE_LANGS if k in keys]
    others = [k for k in keys if k not in preferred and _LANG_KEY.match(k)]
    return preferred + others


def _subtitle_entry_text(entry, budget: list, referer: str = "") -> str:
    """一个语言条目的正文。

    yt-dlp 的两种形状都要认：
      - requested_subtitles: {lang: {ext, url}}（skip_download 下没有 data）
      - subtitles/automatic_captions: {lang: [ {ext, url}, ... ]}（值是 list）
    budget 只允许有限的几次 URL 下载，避免为了字幕把平台请求数放大。
    """
    if isinstance(entry, list):          # 规范形状：值为多个格式，末尾通常是最佳
        for item in reversed(entry):
            t = _subtitle_entry_text(item, budget, referer)
            if t:
                return t
        return ""
    if isinstance(entry, str):
        return _clean_flags(entry)
    if not isinstance(entry, dict):
        return ""
    data = entry.get("data")             # 已下载的正文（某些调用路径会给）
    if data:
        from .utils import subtitle_body_to_text

        txt = data if isinstance(data, str) else str(data)
        # VTT/SRT/ASS/JSON 一律先按字幕格式清洗（data 也可能是 JSON 文本）
        return subtitle_body_to_text(txt) or _clean_flags(txt)
    url = entry.get("url")               # 只有地址 → 自己下载（绝不把 URL 当文案）
    if url and budget and budget[0] > 0:
        budget[0] -= 1
        return _clean_flags(fetch_subtitle_text(url, referer=referer))
    return ""


def _pick_subtitle(info: dict, referer: str = "") -> str:
    """从字幕/自动字幕中取正文（中文优先，其次平台给的其它语言）。

    三点都是 2026-10-31 实测踩过的坑：
      1. yt-dlp 在 skip_download=True 下只填字幕 URL 不填正文 → 这里自己下载；
         拿不到就返回空串交给 ASR，**绝不把 URL 或乱码当文案**。
      2. `subtitles` 里可能根本没有字幕（B站给的是 danmaku 弹幕 XML，且 CDN 用
         压缩返回）→ 只认像语言代码的键，下载结果还要过 looks_like_text 校验。
      3. 值可能是 list（多格式），旧实现按 dict 取会抛 AttributeError。
    """
    from .utils import looks_like_text

    budget = [2]     # 单任务最多为字幕额外发 2 次请求
    for kind in ("requested_subtitles", "subtitles", "automatic_captions"):
        subs = info.get(kind)
        if not isinstance(subs, dict) or not subs:
            continue
        for lang in _subtitle_keys(subs):
            text = _subtitle_entry_text(subs.get(lang), budget, referer)
            if text and looks_like_text(text):
                logger.info("使用平台字幕（%s，%d 字符）", lang, len(text))
                return text
    return ""


def _pick_video_urls(formats) -> list[str]:
    """从 yt-dlp formats 里挑浏览器能播的视频流（排除纯音频）。

    B站 DASH 的音频流（30216/30232/30280）如果混进 videoUrlList 并被预览选中，
    播放器会只有黑屏和时间轴；这里只保留 vcodec != none 的流。
    优先级：AVC(h264) > HEVC/AV1；带音轨的合并流 > 纯视频；分辨率优先 720p 以内。
    """
    scored: list[tuple[float, str]] = []
    for f in formats or []:
        u = f.get("url") or f.get("manifest_url")
        if not u:
            continue
        proto = f.get("protocol") or ""
        if not (isinstance(proto, str) and "http" in proto):
            continue
        vcodec = str(f.get("vcodec") or "").lower()
        if vcodec in ("", "none"):
            continue
        acodec = str(f.get("acodec") or "").lower()
        ext = str(f.get("ext") or "").lower()
        height = f.get("height") or 0
        score = 0.0
        if vcodec.startswith(("avc", "h264")):
            score += 100
        elif vcodec.startswith(("hev", "hvc")):
            score += 30
        elif vcodec.startswith("av01"):
            score += 10
        if acodec not in ("", "none"):
            score += 20
        if ext == "mp4":
            score += 5
        score += min(height, 720) / 100.0
        if height > 720:
            score -= (height - 720) / 10000.0
        scored.append((score, u))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for _, u in scored:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ---------- 语音转文字（ASR） ----------

# ---------- GPU 加速（pip 安装的 CUDA 运行库） ----------

_CUDA_DLL_READY: bool | None = None
_MODEL_CACHE: dict[tuple[str, str, str], object] = {}


def _enable_cuda_dlls() -> bool:
    """把 pip 装的 cuBLAS/cuDNN DLL 目录加入搜索路径。

    Windows 上 ctranslate2 不会自动找 site-packages 里的 nvidia/*/bin，
    需要 add_dll_directory + PATH 双保险。
    """
    global _CUDA_DLL_READY
    if _CUDA_DLL_READY is not None:
        return _CUDA_DLL_READY
    ok = False
    try:
        import sys
        base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        found = []
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            for d in base.glob(f"{sub}/bin"):
                try:
                    os.add_dll_directory(str(d))
                except Exception:
                    pass
                found.append(str(d))
        if found:
            os.environ["PATH"] = ";".join(found) + ";" + os.environ.get("PATH", "")
            ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("CUDA DLL 注入失败: %s", exc)
    _CUDA_DLL_READY = ok
    return ok


def _pick_device() -> tuple[str, str]:
    """返回 (device, compute_type)。优先 GPU，失败回退 CPU。"""
    if os.getenv("TRANSCRIPT_FORCE_CPU", "0") in ("1", "true", "yes"):
        return "cpu", "int8"
    if not _enable_cuda_dlls():
        return "cpu", "int8"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _get_model(model_name: str, device: str, compute_type: str):
    """按 (模型, 设备, 精度) 缓存模型实例，避免每次任务重复加载。"""
    key = (model_name, device, compute_type)
    model = _MODEL_CACHE.get(key)
    if model is None:
        import faster_whisper
        logger.info("加载 whisper 模型: %s (%s/%s)", model_name, device, compute_type)
        # 显存有限（如 6GB），只保留最近 N 个模型，避免 OOM
        try:
            max_models = max(1, int(os.getenv("TRANSCRIPT_MODEL_CACHE", "2")))
        except Exception:
            max_models = 2
        while len(_MODEL_CACHE) >= max_models:
            old_key = next(iter(_MODEL_CACHE))
            _MODEL_CACHE.pop(old_key, None)
            logger.info("释放旧模型缓存: %s", old_key[0])
        model = faster_whisper.WhisperModel(
            model_name, device=device, compute_type=compute_type,
            cpu_threads=int(os.getenv("TRANSCRIPT_CPU_THREADS", "0")) or 0,
        )
        _MODEL_CACHE[key] = model
    return model


def _fmt_mmss(sec: float) -> str:
    s = int(max(0.0, sec))
    return f"{s // 60}:{s % 60:02d}"


def _progress_reporter(task: TaskDetail):
    """转写进度回调：长视频要几分钟，得让用户看到"还要多久"（2026-10-31 加）。"""
    def report(done: float, total: float, eta: float) -> None:
        left = "不到 1 分钟" if eta < 60 else f"约 {int(eta // 60) + 1} 分钟"
        try:
            store.update_status(task.taskId, TaskStatus.RUNNING,
                                errorMessage=f"正在转写：{_fmt_mmss(done)} / {_fmt_mmss(total)}"
                                             f"（预计还需 {left}）")
        except Exception:  # noqa: BLE001
            pass
    return report


def _asr_batch_size() -> int:
    """批处理推理的 batch_size（0 = 关闭，走逐段推理）。

    **默认关闭**：实测批处理快 3.4 倍，但文本会比逐段少约 9% 的字（丢掉一些短衬词），
    且字幕段落更粗。日常精度优先就关着；跑量/云端想要吞吐时开
    （`TRANSCRIPT_ASR_BATCH=8`，6GB 显存实测 medium+batch8 不 OOM）。
    任何异常都会自动回落到逐段推理，不会让任务失败。
    """
    try:
        return max(0, int(os.getenv("TRANSCRIPT_ASR_BATCH", "0") or 0))
    except Exception:  # noqa: BLE001
        return 0


class _SplitSegment:
    """轻量分段（只带 SRT 需要的三样），用于把批处理的长段切开。"""
    __slots__ = ("start", "end", "text")

    def __init__(self, start: float, end: float, text: str):
        self.start, self.end, self.text = start, end, text


def _seg_max_sec() -> float:
    try:
        return max(3.0, float(os.getenv("TRANSCRIPT_SEG_MAX_SEC", "8") or 8))
    except Exception:  # noqa: BLE001
        return 8.0


def _split_long_segments(segs, max_sec: float) -> list:
    """把批处理推理出来的**长段**按词级时间戳切回正常粒度。

    为什么需要：批处理会把整段 VAD 区间合成一个 segment（实测 7.8 分钟音频只有 19 段），
    SRT 导出会变成"每 25 秒一行"的糊状字幕。词级时间戳本来就有，切开即可，
    不用回到慢 3 倍的逐段推理。
    """
    out: list = []
    for s in segs or []:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        words = getattr(s, "words", None) or []
        try:
            span = float(s.end) - float(s.start)
        except Exception:  # noqa: BLE001
            span = 0.0
        if not words or span <= max_sec:
            out.append(s)
            continue
        cur: list = []
        hard_sec = max_sec * 1.5          # 等不到标点也要硬切，别让一行拖太久
        for w in words:
            cur.append(w)
            piece = "".join(str(getattr(x, "word", "") or "") for x in cur).strip()
            try:
                cur_span = float(cur[-1].end) - float(cur[0].start)
            except Exception:  # noqa: BLE001
                cur_span = 0.0
            soft_cut = cur_span >= max_sec and piece and piece[-1] in "。！？!?；;，,"
            if piece and (soft_cut or cur_span >= hard_sec):
                out.append(_SplitSegment(float(cur[0].start), float(cur[-1].end), piece))
                cur = []
        if cur:
            piece = "".join(str(getattr(x, "word", "") or "") for x in cur).strip()
            if piece:
                out.append(_SplitSegment(float(cur[0].start), float(cur[-1].end), piece))
    return out or list(segs or [])


def _transcribe_stream(model, audio_path: Path, kwargs: dict):
    """跑一次转写，返回 (segments, info)。

    优先用 faster-whisper 的**批处理推理**（BatchedInferencePipeline，吞吐通常高数倍）：
      - 同时要 `word_timestamps`，用它把长段切回正常粒度（否则 SRT 会糊）
      - 不可用（老版本/显存不够）就回落逐段推理，kwargs 完全一致
    """
    batch = _asr_batch_size()
    if batch > 0:
        try:
            from faster_whisper import BatchedInferencePipeline

            pipe = BatchedInferencePipeline(model=model)
            segments, info = pipe.transcribe(str(audio_path), batch_size=batch,
                                             word_timestamps=True, **kwargs)
            segments = list(segments)
            try:
                segments = _split_long_segments(segments, _seg_max_sec())
            except Exception as exc:  # noqa: BLE001
                logger.warning("长段切分失败（保留原始分段）: %s", exc)
            logger.info("ASR 批处理推理（batch_size=%d）", batch)
            return segments, info
        except Exception as exc:  # noqa: BLE001
            logger.warning("批处理推理不可用（%s），回落到逐段推理", exc)
    return model.transcribe(str(audio_path), **kwargs)


def _transcribe(video_path: Path, model_name: str = DEFAULT_WHISPER,
                language: Optional[str] = None, vad: bool = True,
                on_progress=None) -> tuple[str, list[dict]]:
    """用 faster-whisper 转写本地视频/音频为文字。

    language 指定语种（中文平台传 "zh"，可显著减少误判和同音错字）；
    vad_filter 跳过静音段，避免纯 BGM 段落产生幻觉文本。
    initial_prompt 注入领域词，帮助模型输出常用书面词（射击/射程/力矩等）。
    先用 ffmpeg 重采样为 16k 单声道并做高通滤波（去低频隆隆声）。
    实测对比：不加 loudnorm 响度归一更好——含枪声/爆炸声的视频里，
    loudnorm 会把人声压弱反而伤害识别。

    返回 (全文, 带时间戳分段)；分段用于 SRT 字幕导出。
    """
    audio_path = _enhance_audio(video_path)
    # 音频增强失败（返回原文件）时先确认它真有可解码音轨：否则 whisper 会抛出
    # 看不懂的 IndexError（实测：抖音某些档的直链下到的文件 ffmpeg 认不出任何流）
    if audio_path == video_path and not _has_audio_stream(video_path):
        raise RuntimeError(
            "下载/导入的媒体没有可解码音轨（文件可能不完整或不是媒体文件），无法转写。")

    if model_name not in WHISPER_MODELS:
        model_name = DEFAULT_WHISPER
    device, compute_type = _pick_device()
    logger.info("whisper 使用设备: %s/%s（模型 %s）", device, compute_type, model_name)
    with _WHISPER_LOCK:  # 串行推理，避免并发占用过多显存/内存
        model = _get_model(model_name, device, compute_type)
        # 语言预检：强制 zh 时，若音频其实是英文/其它语种，whisper 会输出"中文伪翻译"
        # （实测 B站英文 MV 被译成中文）。检测到明显非中文就改用自动语种。
        if language == "zh":
            try:
                from faster_whisper import decode_audio

                # 注意：detect_language 只接受 ndarray（传路径会 AttributeError）
                _samples = decode_audio(str(audio_path), sampling_rate=16000)
                detected, prob, _all = model.detect_language(_samples)
                if detected and detected != "zh" and float(prob) >= 0.4:
                    logger.info("语种预检：检测到 %s（置信度 %.2f），不强制中文", detected, prob)
                    language = None
            except Exception as exc:  # noqa: BLE001
                logger.debug("语种预检失败（忽略）: %s", exc)
        kwargs = {"vad_filter": bool(vad), "beam_size": 5,
                  "condition_on_previous_text": False}
        if language:
            kwargs["language"] = language
        if language == "zh":
            kwargs["initial_prompt"] = (
                "以下是普通话视频的口播转写，用简体中文书面语，"
                "常见词：射击、射程、弹头、尾锥、圆柱部、力矩。"
            )
        segments, _info = _transcribe_stream(model, audio_path, kwargs)
        texts: list[str] = []
        segs: list[dict] = []
        total_sec = float(getattr(_info, "duration", 0) or 0)
        t_start = time.time()
        last_report = 0.0
        for seg in segments:
            t = (seg.text or "").strip()
            if not t:
                continue
            texts.append(t)
            segs.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": t,
            })
            if on_progress and total_sec > 0:
                now = time.time()
                if now - last_report >= 5:          # 每 5 秒更新一次，别刷屏
                    last_report = now
                    done = float(getattr(seg, "end", 0) or 0)
                    rate = done / max(0.1, now - t_start)
                    eta = (total_sec - done) / rate if rate > 0 else 0.0
                    try:
                        on_progress(done, total_sec, eta)
                    except Exception:  # noqa: BLE001
                        pass
    full = "\n".join(texts)
    # 双 ASR 只做"分歧提示"，默认不自动替换：
    # 实测 Vosk-small 中文能力弱于 whisper-small，自动替换会把正确文字改坏。
    if full.strip() and _dual_asr_enabled() and vosk_available():
        try:
            _fixed, fixes, status = dual_asr_correct(audio_path, full)
            if fixes:
                logger.info("双 ASR 发现 %d 处分歧（仅供参考，未改动）", len(fixes))
        except Exception as exc:  # noqa: BLE001
            logger.warning("双 ASR 失败: %s", exc)
    return full, segs


def _dual_asr_enabled() -> bool:
    return _os.getenv("TRANSCRIPT_DUAL_ASR", "0") not in ("0", "false", "off")


def _enhance_audio(src: Path) -> Path:
    """把输入音轨重采样为 whisper 的 16k 单声道，并做降噪级预处理。

    - highpass 80Hz 去低频隆隆声
    失败时回退原始文件，不阻塞任务。
    """
    import subprocess
    out = src.with_suffix(".enh16k.wav")
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn",
             "-af", "highpass=f=80",
             "-ac", "1", "-ar", "16000", str(out)],
            check=True, timeout=600,
        )
        if out.exists() and out.stat().st_size > 0:
            logger.info("音频增强完成: %s", out.name)
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("音频增强失败（用原始文件继续）: %s", exc)
    return src


def _fetch_platform(fetch_fn, url: str, **kwargs) -> dict:
    """先匿名抓取；**只有判定"缺登录态"时才带 cookie 重试一次**。

    其它失败按分类处理：风控/内容不存在 → 直接抛（不再多发请求）；
    网络/超时/未知 → 原样（匿名）重试一次，退避 1.5s。
    目的：默认不暴露账号；把"失败即升级 cookie"改成"确认需要登录态才动用账号"。
    """
    last_exc = None
    login_required = False
    for attempt in (1, 2):
        use_ck = should_use_cookie(url, attempt, login_required=login_required)
        try:
            data = fetch_fn(url, use_cookie=use_ck, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            kind = classify_failure(exc)
            logger.info("通道抓取尝试 %d/2 失败（%s）: %s", attempt, kind, exc)
            if attempt >= 2 or kind in ("risk", "permanent"):
                raise
            if kind == "login" and cookie_available_for(url):
                login_required = True
                logger.info("判定需要登录态，带 cookie 重试")
            else:
                logger.info("原样（匿名）重试一次")
            time.sleep(1.5)
            continue
        if data and (data.get("title") or data.get("video_url") or data.get("caption")):
            return data
        if attempt == 1:
            logger.info("匿名抓取无有效数据，原样（匿名）重试一次")
            time.sleep(1.5)
            continue
        return data or {}
    if last_exc:
        raise last_exc
    return {}


# ---------- 任务执行 ----------



_DOUYIN_LOW_GEAR_BAD = False   # 实测复现：抖音低码率档的直链下到的文件 ffprobe 读不出流


def _douyin_stream_urls(d: dict) -> tuple[list[str], list[str]]:
    """抖音直链分流：返回 (ASR 用 URL 列表, 预览用 URL 列表)。

    aweme_detail 的 bit_rate[] 有多档码率（每档有自己的 play_addr）：
      - ASR 只需要语音 → 取**最小码率**那档（长视频能省 60~80% 流量与下载时间）
      - 预览给浏览器 → 取**最大码率**那档
    码率未知的档（只有 play_addr 的默认档）排在各自列表末尾，仍可用。
    """
    variants = [v for v in (d.get("video_variants") or [])
                if isinstance(v, dict) and str(v.get("url") or "").startswith("http")]

    def _rate(v: dict):
        b = v.get("bitrate")
        return float(b) if isinstance(b, (int, float)) and b > 0 else None

    if not variants:
        u = (d.get("video_url") or "").strip()
        return ([u] if u else []), ([u] if u else [])

    known = sorted([v for v in variants if _rate(v) is not None], key=lambda v: _rate(v))
    unknown = [v for v in variants if _rate(v) is None]
    asr = list(dict.fromkeys([v["url"] for v in known] + [v["url"] for v in unknown]))
    preview = list(dict.fromkeys([v["url"] for v in reversed(known)] + [v["url"] for v in unknown]))
    return asr, preview


def _run_douyin_task(task: TaskDetail, work_url: str, want_audio: bool,
                     whisper_model: str, work_dir: Path) -> None:
    """抖音专用：Playwright 渲染抓取 aweme_detail，再对整条视频做语音转写。

    兼容 /video/、/jingxuan?modal_id=、v.douyin.com 短链。
    标题/作者/描述/封面/时长来自详情 API；**文案一律来自全片 ASR**，
    不再用作品描述（话题标签）冒充口播文案。
    """
    from .douyin import fetch_douyin

    d = _fetch_platform(fetch_douyin, work_url)
    task.title = d.get("title")
    task.uploader = d.get("author")
    # 抖音 aweme_detail 的 duration 单位是毫秒，统一换算成秒（与 yt-dlp 一致）
    raw_dur = d.get("duration")
    try:
        task.duration = float(raw_dur) / 1000.0 if raw_dur else None
    except Exception:
        task.duration = None
    task.cover = d.get("cover")
    task.webpageUrl = work_url
    # 直链分流：预览用最高码率档（可切档的备用地址一并带上），ASR 用最低档
    asr_urls, preview_urls = _douyin_stream_urls(d)
    if preview_urls:
        task.videoUrlList = preview_urls[:8]
        task.videoUrl = preview_urls[0]
    else:
        task.videoUrl = d.get("video_url")
        if d.get("video_url"):
            task.videoUrlList = [d.get("video_url")]
    task.content = d.get("desc")

    # 图集/图文作品：aweme_detail 给的是 images[]，没有视频流。
    # 旧实现会落到"抖音视频下载失败"，对用户是误导；这里直接走图片 OCR（P2）。
    images = d.get("images") or []
    if not d.get("video_url") and images:
        logger.info("抖音任务 %s 是图集作品（%d 张图），走按需 OCR", task.taskId, len(images))
        store.update_status(task.taskId, TaskStatus.RUNNING,
                            errorMessage="正在识别图集文字（OCR）...")
        text = _ocr_images(images, referer="https://www.douyin.com/", platform="douyin")
        if not text.strip():
            raise RuntimeError("这是抖音图文/图集作品，但图片文字识别失败（图片可能已过期）。")
        task.sourceText = text
        task.textContent = text
        task.segments = None
        return

    # 优先用视频自带的真实字幕（自动字幕/CC，比 ASR 更准）
    real_sub = (d.get("caption") or "").strip()
    if real_sub:
        logger.info("抖音任务 %s 使用视频自带字幕（%d 字符）", task.taskId, len(real_sub))
        task.sourceText = real_sub
        task.textContent = real_sub
        return

    # 无字幕：下载整条视频做全片 ASR（作品描述只作"作品简介"）
    logger.info("抖音任务 %s 无自带字幕，下载视频做全片 ASR（%d 档直链，取最小码率）",
                task.taskId, len(asr_urls))
    chosen = None
    candidates = asr_urls[:2]                          # 先试最小的两档（省流量）
    if d.get("video_url"):
        candidates.append(d["video_url"])              # 默认档兜底（实测最稳）
    candidates = list(dict.fromkeys(u for u in candidates
                                    if isinstance(u, str) and u.startswith("http")))[:3]
    if _DOUYIN_LOW_GEAR_BAD and d.get("video_url"):
        # 本进程已经证实低码率档不可解码 → 后面几条直接走默认档，别再白下几 MB
        candidates = [d["video_url"]]
    if not candidates and d.get("audio_url"):
        candidates = [d["audio_url"]]
    seg_text, segs = "", []
    chosen = None
    transcribe_error: Exception | None = None
    for idx, cand in enumerate(candidates, 1):
        logger.info("抖音 ASR 下载候选 %d/%d（优先最小码率）", idx, len(candidates))
        got = _download_http_media(cand, work_dir, referer="https://www.douyin.com/",
                                   platform="douyin")
        if got is None:
            continue
        if not _has_audio_stream(got):
            # 低码率档有时给的不是可真解码的流 → 删掉换下一档（别硬丢给 whisper）
            logger.warning("候选 %d 下到的文件没有音轨（%.1f MB），换下一档",
                           idx, got.stat().st_size / 1048576)
            got.unlink(missing_ok=True)
            if idx < len(candidates) and cand != d.get("video_url"):
                globals()["_DOUYIN_LOW_GEAR_BAD"] = True   # 记住：低码率档不可用
            continue
        store.update_status(task.taskId, TaskStatus.RUNNING,
                            errorMessage="正在转写全片口播（视频较长时会久一点）...")
        try:
            seg_text, segs = _transcribe(got, whisper_model, language="zh",
                                on_progress=_progress_reporter(task))
        except Exception as exc:  # noqa: BLE001
            # 实测存在"ffprobe 说有音轨、解码/转写仍失败"的档（2026-10-31 一条任务死在这）
            # → 换下一档候选，而不是让整条任务失败
            transcribe_error = exc
            logger.warning("候选 %d 转写失败（%s: %s），换下一档候选",
                           idx, type(exc).__name__, exc)
            try:
                got.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            if idx < len(candidates) and cand != d.get("video_url"):
                globals()["_DOUYIN_LOW_GEAR_BAD"] = True   # 这档连解码都不稳
            continue
        chosen = got
        break

    if chosen is None and not (seg_text or "").strip():
        # 直链候选全军覆没 → 回到 yt-dlp 多源（自带格式选择与重试）
        fallback = download_media(work_url, work_dir, media_type="video", max_attempts=3,
                                  on_attempt=lambda a, m, _m: store.update_status(
                                      task.taskId, TaskStatus.RUNNING, errorMessage=_m))
        if fallback is not None:
            store.update_status(task.taskId, TaskStatus.RUNNING,
                                errorMessage="正在转写全片口播（视频较长时会久一点）...")
            seg_text, segs = _transcribe(fallback, whisper_model, language="zh",
                                       on_progress=_progress_reporter(task))
        elif transcribe_error is not None:
            raise RuntimeError(
                f"抖音视频转写失败：所有直链档都无法解码（{type(transcribe_error).__name__}: "
                f"{transcribe_error}），多源重试也没取到可用视频。") from transcribe_error
        else:
            raise RuntimeError("抖音视频下载失败（多源重试耗尽）")

    task.sourceText = seg_text
    task.textContent = seg_text
    task.segments = segs
    if not seg_text.strip():
        logger.info("抖音任务 %s 未识别到语音（可能是纯音乐视频）", task.taskId)


_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_KS_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _download_http_media(url: str, work_dir: Path, referer: str = "",
                         platform: str = "") -> Optional[Path]:
    """直接 http 下载一个媒体 URL 到工作目录，返回文件路径或 None。

    P1：显式 opener（默认直连，可用 TRANSCRIPT_PROXY_MEDIA 配代理），
    并按平台给 UA/Referer —— 快手 CDN 对桌面 UA / 无 Referer 会挑。
    """
    import urllib.request

    from .utils import http_opener

    out = work_dir / f"audio_{int(time.time()*1000)}.mp4"
    ua = _KS_MOBILE_UA if platform == "kuaishou" else _DESKTOP_UA
    headers = {"User-Agent": ua, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with http_opener(media=True).open(req, timeout=30) as resp, open(out, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("直接下载媒体失败: %s", exc)
    try:
        out.unlink(missing_ok=True)
    except Exception:
        pass
    return None

# 成功但没有任何文字时的提示（纯音乐 / 无旁白视频）。
# 放在 errorMessage 里是为了让历史列表、结果页都能看到一句人话，而不是"成功 + 空白"。
NO_SPEECH_NOTE = ("没有识别到口播内容（可能是纯音乐 / 无旁白视频）。"
                  "如果是画面文字类内容，可勾选「识别画面文字（OCR）」重新提取；"
                  "也可以换更大的 whisper 模型再试一次。")

# 极短文字几乎不是视频的真正内容，而是 whisper 在纯音乐/静音上"幻听"出来的
MIN_MEANINGFUL_CHARS = 5
SHORT_ASR_NOTE = ("只识别到极少文字（「{text}」）——很可能是纯音乐 / 环境音被模型幻听出来的，"
                  "请核对后再用；也可以换更大的 whisper 模型再试一次。")


def _empty_text_note(task, *, asr: Optional[bool] = None) -> Optional[str]:
    """成功但没有有效文字时给出提示，否则 None。

    - 口播与画面文字都为空 → 提示"没有识别到口播"
    - 只有 ASR 产出且短得离谱（<5 字，如静音上的 "You"）→ 提示可能是幻听
    - 有画面文字 / 有像样的文字 → 不打扰
    asr=None 时按 task.segments 推断（ASR 路径会写 segments，字幕/图片路径不会）。
    """
    if (getattr(task, "frameText", None) or "").strip():
        return None                                   # 画面文字就是内容
    text = (getattr(task, "textContent", None) or "").strip()
    if not text:
        return NO_SPEECH_NOTE
    if asr is None:
        asr = getattr(task, "segments", None) is not None
    if asr and len(text) < MIN_MEANINGFUL_CHARS:
        return SHORT_ASR_NOTE.format(text=text[:20])
    return None


def _mark_success(task: TaskDetail) -> None:
    """统一写入 SUCCESS 状态与全部结果字段。"""
    store.update_status(
        task.taskId, TaskStatus.SUCCESS,
        title=task.title, content=task.content, textContent=task.textContent,
        sourceText=task.sourceText, cover=task.cover,
        videoUrl=task.videoUrl, videoUrlList=task.videoUrlList,
        platform=task.platform, duration=task.duration,
        uploader=task.uploader, webpageUrl=task.webpageUrl,
        segments=getattr(task, "segments", None),
        frameText=getattr(task, "frameText", None),
        frameBlocks=getattr(task, "frameBlocks", None),
        errorMessage=_empty_text_note(task),
    )


def _run_kuaishou_task(task: TaskDetail, work_url: str, want_audio: bool,
                       whisper_model: str, work_dir: Path) -> None:
    """快手专用：移动端分享页拿标题/作者/直链，再下载视频做 ASR。"""
    d = _fetch_platform(fetch_kuaishou, work_url)
    task.title = d.get("title")
    task.content = d.get("desc")
    task.uploader = d.get("author")
    task.duration = d.get("duration")
    task.videoUrl = d.get("video_url")
    vlist = d.get("video_urls") or []
    task.videoUrlList = vlist or None

    chosen = None
    # 快手 CDN 一次给多条直链（kwimgs/ndcimgs 优先）：一条不通就换下一条，
    # 别直接落到 yt-dlp 的 3 次重试（那等于重新请求平台页面）。
    candidates = list(dict.fromkeys(u for u in ([task.videoUrl] + list(vlist)) if u))
    for cand in candidates[:3]:
        chosen = _download_http_media(cand, work_dir,
                                      referer="https://m.gifshow.com/", platform="kuaishou")
        if chosen is not None:
            break
    if chosen is None:
        chosen = download_media(
            work_url, work_dir, media_type="video", max_attempts=3,
            on_attempt=lambda a, m, _m: store.update_status(
                task.taskId, TaskStatus.RUNNING, errorMessage=_m),
        )
    if chosen is None:
        raise RuntimeError("快手视频下载失败（多源重试耗尽）")

    seg_text, segs = _transcribe(chosen, whisper_model, language="zh", on_progress=_progress_reporter(task))
    task.sourceText = seg_text
    task.textContent = seg_text
    task.segments = segs


def _humanize_error(exc: Exception, platform: str = "") -> str:
    """把常见原始报错翻译成用户能看懂的话（保留原始信息在日志里）。"""
    msg = str(exc or "")
    low = msg.lower()

    # 小红书图文笔记：yt-dlp 只能找视频流，图文笔记必然报 No video formats
    if "no video formats" in low and ("小红书" in platform or "xiaohongshu" in low or "xhs" in low):
        return ("这条笔记是「图文笔记」，没有视频流，所以无法按视频提取。"
                "请改用「小红书图文提取」：上传图片，或填写图片直链后再识别。")

    if "no video formats" in low:
        return "该链接没有可提取的视频流（可能是图文/纯音频内容，或链接不是视频页）。"

    # 小红书访问受限（未登录 / 已删除 / 需要 xsec_token）
    if "300031" in msg or "笔记暂时无法浏览" in msg:
        return ("笔记无法访问：可能需要登录态（在 cookies/xiaohongshu.txt 放登录 cookie），"
                "或笔记已删除/仅作者可见。")

    # 冷却/限流：保留后端给出的剩余时间和处理建议，不要压成通用 403 文案
    if "冷却" in msg or "限流" in msg:
        return msg

    # 风控 / 频率
    if "403" in msg or "风控" in msg or "forbidden" in low:
        return "平台风控拦截（403）：请降低频率、稍后重试；或配置 cookies/<平台>.txt 提升成功率。"

    # 已删除 / 地区限制
    if "deleted" in low or "geo-restrict" in low or "已删除" in msg or "地区" in msg:
        return "该内容已删除或存在地区限制，无法提取。"

    return msg

def _ocr_images(urls, referer: str = "", platform: str = "") -> str:
    """按需 OCR 一组图片（P2）：先认前 N 张，文字够用就停。

    N / 上限 / 最小字数可用 TRANSCRIPT_IMAGE_OCR_FIRST / _MAX / _MIN_CHARS 调整。
    典型收益：小红书图文从"20 张全扫"降到"3 张够用就停"，更快、请求更少。
    """
    import urllib.request
    from urllib.parse import urlparse

    from .ocr import ocr_image
    from .utils import http_opener, throttle

    try:
        first = max(1, int(os.getenv("TRANSCRIPT_IMAGE_OCR_FIRST", "3")))
        maxn = max(first, int(os.getenv("TRANSCRIPT_IMAGE_OCR_MAX", "20")))
        quiet = max(0, int(os.getenv("TRANSCRIPT_IMAGE_OCR_QUIET", "5")))
    except Exception:  # noqa: BLE001
        first, maxn, quiet = 3, 20, 5
    texts: list[str] = []
    added: list[int] = []
    for i, u in enumerate((urls or [])[:maxn]):
        got = ""
        try:
            throttle(urlparse(u).netloc or "img", min_interval=0.5)
            headers = {"User-Agent": _DESKTOP_UA, "Accept": "image/*,*/*"}
            if referer:
                headers["Referer"] = referer
            req = urllib.request.Request(u, headers=headers)
            with http_opener("", media=True).open(req, timeout=30) as r:
                data = r.read()
            got = (ocr_image(data) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("第 %d 张图 OCR 失败: %s", i + 1, exc)
        if got:
            texts.append(got)
        added.append(len(got))
        done = i + 1
        # 自适应停止：至少 FIRST 张；之后若**连续三张**几乎没新增文字（照片/封面），才停。
        # 阈值要小：短句风格的图文笔记每张也就十几个字，不能用大阈值（实测踩过：
        # 20 字阈值让一条 12 图笔记只认了前 3 张、只拿到 37 字）。
        # QUIET=0 表示不提前结束（一路扫到 MAX）。
        if done >= first and len(added) >= 3 and all(a < quiet for a in added[-3:]):
            logger.info("图文 OCR 提前结束：%d/%d 张（最近三张只加了 %s 字）",
                        done, len(urls or []), added[-3:])
            break
    return "\n\n".join(texts)


def _run_xhs_image_note(task: TaskDetail, work_url: str, note: dict | None = None) -> bool:
    """小红书图文笔记：用已渲染的页面数据取图 → 按需 OCR。成功返回 True。"""
    from . import xiaohongshu

    if note is None:
        note = xiaohongshu.extract_note(work_url)
    if not note or not (note.get("images") or []):
        logger.warning("图文笔记未取到图片: %s", work_url[:80])
        return False

    text = _ocr_images(note["images"], referer="https://www.xiaohongshu.com/",
                       platform="xiaohongshu")
    if not text.strip():
        logger.warning("图文笔记 %d 张图均未识别到文字", len(note["images"]))
        return False

    task.title = ((note.get("title") or task.title or "").strip() or None)
    task.content = ((note.get("desc") or "").strip() or None)
    task.sourceText = text
    task.textContent = text
    task.segments = None
    task.platform = "小红书"
    task.webpageUrl = work_url
    logger.info("小红书图文笔记：%d 张图 → %d 字", len(note["images"]), len(text))
    return True
def _external_fallback(work_url: str, work_dir: Path, media_url: str = "") -> Optional[Path]:
    """直链兜底：本地下载媒体后交回本项目转写。

    media_url 优先；没有媒体直链时把页面链接交给本地下载器，它会拒绝 HTML，
    不会误把网页存成视频。不依赖 wx_channels_download 在线。
    """
    from . import external_dl

    target_url = (media_url or work_url or "").strip()
    if not target_url:
        return None
    logger.info("尝试直链兜底下载: %s", target_url[:100])
    try:
        return external_dl.download_and_wait(target_url, work_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("直链兜底失败: %s", exc)
        return None


# ---------- 本地预览视频（平台只发 HLS / 无浏览器可播直链时的兜底） ----------

_LOCAL_PREVIEW_FILE = "video.mp4"


def _has_audio_stream(path: Path) -> bool:
    """用 ffprobe 判断文件是否真的含可解码音轨。

    下载来的东西未必是媒体：抖音低码率档的直链实测会返回一个 ffmpeg 认不出任何
    流的文件（2026-10-31：最小档 151kbps 下到的文件 "Output file does not
    contain any stream"），此时必须换下一档，而不是硬丢给 whisper。
    """
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return "audio" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def _has_video_stream(path: Path) -> bool:
    """用 ffprobe 判断文件是否真的含视频流（外部下载器产物也可能是纯音频）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return "video" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def _ensure_mp4(path: Path, work_dir: Path) -> Optional[Path]:
    """把输入视频落实为任务目录里的 video.mp4（流复制，不重编码）。

    非 mp4 容器（HLS/TS/webm/mkv）→ ffmpeg 转封装；mp4 但名字不同 → 直接改名。
    aac_adtstoasc 只适用于 AAC 输入（TS/HLS），webm/mkv 等会报错 → 失败去掉重试。
    """
    if path is None or not path.is_file():
        return None
    out = work_dir / _LOCAL_PREVIEW_FILE
    if path.suffix.lower() == ".mp4":
        if path == out:
            return path
        try:
            path.replace(out)  # 同目录改名，零拷贝
            return out if out.is_file() and out.stat().st_size > 0 else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("预览视频改名失败: %s", exc)
            return None
    import subprocess

    for extra in (["-bsf:a", "aac_adtstoasc"], []):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(path), "-c", "copy",
                 "-movflags", "+faststart", *extra, str(out)],
                capture_output=True, timeout=600, check=True,
            )
            if out.is_file() and out.stat().st_size > 0:
                return out
        except Exception:  # noqa: BLE001
            continue
    logger.warning("转封装 mp4 失败: %s", path.name)
    return None


def _pick_hls_url(formats) -> Optional[str]:
    """从 yt-dlp formats 里挑最高画质的 HLS 播放列表地址（给 ffmpeg 直接拉流）。"""
    best, best_h = None, -1
    for f in formats or []:
        u = f.get("url") or ""
        proto = (f.get("protocol") or "").lower()
        if not u:
            continue
        if "m3u8" in proto or u.lower().split("?")[0].endswith(".m3u8"):
            h = f.get("height") or 0
            if h >= best_h:
                best, best_h = u, h
    return best


def _ffmpeg_pull_hls(hls_url: str, referer: str, out: Path, timeout: int = 900) -> Optional[Path]:
    """ffmpeg 直接拉 HLS → mp4（流复制）。不再重复请求平台页面，降低风控压力。"""
    import subprocess

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    headers = f"User-Agent: {ua}\r\n"
    if referer:
        headers += f"Referer: {referer}\r\n"
    cmd = ["ffmpeg", "-y", "-v", "error", "-headers", headers, "-i", hls_url,
           "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffmpeg 直拉 HLS 失败: %s", exc)
        return None
    return out if out.is_file() and out.stat().st_size > 0 else None


def _download_local_preview(task: TaskDetail, work_url: str, work_dir: Path,
                            ready_media: Optional[Path] = None,
                            info: Optional[dict] = None) -> None:
    """任务没有浏览器可播直链时，弄一份本地 mp4 放进任务目录，
    并把 videoUrlList 标记为 file:// 供 /media/proxy 服务（预览/截图/保存全走它）。

    典型场景：小红书 CDN 本次只发 HLS（m3u8），<video> 标签无法直接播放，
    于是前端整块"视频预览"消失（2026-10-30 实测）。
    优先级：ready_media（现成文件）> info 里的 m3u8（ffmpeg 直拉，不再二次请求平台）
    > download_media 兜底（会重新提取，尽量少走）。
    """
    if (_os.getenv("TRANSCRIPT_HLS_PREVIEW", "1").strip().lower() in ("0", "false", "off")):
        return
    try:
        final: Optional[Path] = None
        if ready_media is not None and ready_media.is_file() and _has_video_stream(ready_media):
            final = _ensure_mp4(ready_media, work_dir)
            if final is not None and final != ready_media:
                ready_media.unlink(missing_ok=True)  # 转封装成功，删掉原容器
        if final is None and info:
            hls = _pick_hls_url(info.get("formats") or [])
            if hls:
                store.update_status(task.taskId, TaskStatus.RUNNING,
                                    errorMessage="平台本次未提供可播直链，正在拉取视频用于预览...")
                out = work_dir / _LOCAL_PREVIEW_FILE
                out.unlink(missing_ok=True)
                final = _ffmpeg_pull_hls(hls, _referer_for(work_url), out)
                if final is not None and not _has_video_stream(final):
                    final = None
        if final is None:
            store.update_status(task.taskId, TaskStatus.RUNNING,
                                errorMessage="平台本次未提供可播直链，正在下载视频用于预览...")
            local = download_media(work_url, work_dir, media_type="video", max_attempts=2)
            if local is not None and _has_video_stream(local):
                final = _ensure_mp4(local, work_dir)
        if final is None:
            logger.warning("任务 %s 未能生成本地预览视频（不影响文案）", task.taskId)
            return
        final = _ensure_mp4(final, work_dir) or final
        task.videoUrlList = ["file://" + _LOCAL_PREVIEW_FILE]
        task.videoUrl = task.videoUrlList[0]
        logger.info("任务 %s 已生成本地预览视频 (%.1f MB)",
                    task.taskId, final.stat().st_size / 1048576)
    except Exception as exc:  # noqa: BLE001
        logger.warning("本地预览视频生成失败（不影响文案）: %s", exc)


def _run_frame_ocr(task: TaskDetail, work_url: str, work_dir: Path) -> None:
    """画面文字（硬字幕/花字）OCR，与口播文案分开存放。

    抖音 / 快手 / 通用三条收尾路径都会调用 —— 旧实现只挂在通用路径上，导致
    抖音/快手勾了"识别画面文字"也不会执行（2026-10-31 修）。

    为什么重要：画面硬字幕往往比 ASR 准。用户实测同一句，截图是"肥水不流外人田"，
    ASR 听成"非说也没有留到外人天地里去"。

    视频源优先级：本地预览 video.mp4 → 任务目录里的媒体 → 任务直链（http / file://）。
    """
    src = ""
    keep = work_dir / _LOCAL_PREVIEW_FILE
    if keep.is_file():
        src = str(keep)
    if not src:
        for f in (sorted(work_dir.glob("*.mp4")) + sorted(work_dir.glob("*.mkv"))
                  + sorted(work_dir.glob("*.webm")) + sorted(work_dir.glob("*.mov"))):
            if f.is_file() and f.stat().st_size > 0 and f != keep:
                src = str(f)
                break
    if not src:
        for u in (task.videoUrlList or []):
            if not isinstance(u, str):
                continue
            if u.startswith("file://"):
                p = work_dir / u[len("file://"):]
                if p.is_file():
                    src = str(p)
                    break
            elif u.startswith("http"):
                src = u
                break
    if not src:
        logger.info("任务 %s 没有可用于画面文字识别的视频源，跳过 OCR", task.taskId)
        return
    store.update_status(task.taskId, TaskStatus.RUNNING,
                        errorMessage="正在识别画面文字（OCR）...")
    try:
        from .frame_ocr import extract_frame_text

        fr = extract_frame_text(src, duration=task.duration, referer=_referer_for(work_url))
        if fr.get("text"):
            task.frameText = fr["text"]
            task.frameBlocks = fr.get("blocks") or []
            logger.info("任务 %s 画面文字 %d 字", task.taskId, len(fr["text"]))
            # 口播为空/极短（纯音乐、无旁白）→ 画面字幕就是正文（字幕优先）
            body = (task.textContent or "").strip()
            if len(body) < _SUBTITLE_AS_MAIN_MIN:
                if body:
                    task.sourceText = task.sourceText or body
                task.textContent = fr["text"]
                logger.info("任务 %s 无有效口播（%d 字），改用画面文字作为正文", task.taskId, len(body))
    except Exception as exc:  # noqa: BLE001
        logger.warning("画面文字识别失败（不影响口播文案）: %s", exc)


def _run_task(task: TaskDetail, work_url: str, want_audio: bool, whisper_model: str,
              ocr_frames: bool = False) -> None:
    work_dir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks")) / task.taskId
    work_dir.mkdir(parents=True, exist_ok=True)
    adapter = adapter_for(work_url)
    try:
        store.update_status(task.taskId, TaskStatus.RUNNING)
        # 平台专用通道由适配器声明（douyin/kuaishou），其余走 yt-dlp 通用通道
        if adapter.run_kind == "douyin":
            _run_douyin_task(task, work_url, want_audio, whisper_model, work_dir)
            if ocr_frames:
                _run_frame_ocr(task, work_url, work_dir)
            task.platform = adapter.label
            task.webpageUrl = normalize_url(work_url)
            _mark_success(task)
            return
        # 快手专用通道：移动端分享页（yt-dlp 无快手提取器）
        if adapter.run_kind == "kuaishou":
            try:
                _run_kuaishou_task(task, work_url, want_audio, whisper_model, work_dir)
            except Exception:
                # 专用通道失败时尝试直链本地下载兜底
                ext_media = _external_fallback(work_url, work_dir, task.videoUrl or "")
                if ext_media is None:
                    raise
                logger.info("快手通道失败，已用直链下载取到媒体: %s", ext_media.name)
                task.title = task.title or ext_media.stem
                seg_text, segs = _transcribe(ext_media, whisper_model, language="zh",
                                       on_progress=_progress_reporter(task))
                task.sourceText = seg_text
                task.textContent = seg_text
                task.segments = segs
            if ocr_frames:
                _run_frame_ocr(task, work_url, work_dir)
            task.platform = adapter.label
            task.webpageUrl = work_url
            _mark_success(task)
            return
        ext_media: Optional[Path] = None   # 直链兜底产物（若有）
        try:
            info = _extract_info(work_url)
        except Exception as exc:
            # 小红书图文笔记没有视频流（yt-dlp 报 No video formats）→ 自动改走"取图 + OCR"
            if "no video formats" in str(exc).lower():
                from . import xiaohongshu

                if xiaohongshu.is_xhs(work_url):
                    note = xiaohongshu.extract_note(work_url)
                    if note is None:
                        raise RuntimeError(
                            "笔记无法访问：链接可能缺少 xsec_token 或笔记已删除/受限。"
                            "请在小红书 App 里重新「分享 → 复制链接」后再试。"
                        ) from exc
                    if _run_xhs_image_note(task, work_url, note):
                        _mark_success(task)
                        return
            # 兜底：直链本地下载——视频号/快手/B站等的最后一条路
            ext_media = _external_fallback(work_url, work_dir)
            if ext_media is None:
                raise
            logger.info("已用直链下载取到媒体，改为直接转写: %s", ext_media.name)
            info = {"title": ext_media.stem, "webpage_url": work_url}

        task.title = info.get("title")
        task.uploader = (info.get("uploader") or info.get("channel") or info.get("creator"))
        task.cover = info.get("thumbnail")
        task.webpageUrl = info.get("webpage_url") or work_url
        task.platform = adapter.label
        dur = info.get("duration")
        if isinstance(dur, (int, float)):
            task.duration = float(dur)
        task.content = info.get("description")
        # 可播放的视频直链（列表）：只收视频流，排除纯音频（否则 B站 预览黑屏）
        urls = _pick_video_urls(info.get("formats") or [])
        if not urls:
            u = info.get("url") or ""
            vcodec = str(info.get("vcodec") or "").lower()
            if u and vcodec not in ("", "none"):
                urls = [u]
        task.videoUrlList = urls[:8] or None
        task.videoUrl = urls[0] if urls else None

        # 1) 先尝试现成字幕
        caption = _pick_subtitle(info, referer=_referer_for(work_url))

        # 2) 若没有字幕且允许解析音频，则下载音频做 ASR
        if not caption and not want_audio:
            # 仍尝试取 "热词" 型 description 作为兜底？不，无字幕则转写音频
            pass

        if caption:
            task.sourceText = caption
            task.textContent = caption
            logger.info("任务 %s 使用现成字幕", task.taskId)
        else:
            logger.info("任务 %s 无现成字幕，下载音频进行 ASR", task.taskId)
            if ext_media is not None:
                chosen = ext_media
            else:
                chosen = download_media(
                    work_url, work_dir, media_type="audio", max_attempts=4,
                    on_attempt=lambda a, m, _m: store.update_status(
                        task.taskId, TaskStatus.RUNNING,
                        errorMessage=_m,
                    ),
                )
                if chosen is None:
                    # 兜底：常规下载失败 → 试直链本地下载（有直链才传，页面链接会被拒绝）
                    fallback_media = task.videoUrl if task.platform in ("抖音", "快手") else ""
                    chosen = _external_fallback(work_url, work_dir, fallback_media)
                if chosen is None:
                    raise RuntimeError("音频下载失败（多源+多轮重试耗尽；直链下载亦不可用）")
            # 中文平台强制 zh，避免被误判成其他语种
            zh_platforms = ("抖音", "小红书", "快手", "B站", "视频号")
            lang = "zh" if task.platform in zh_platforms else None
            seg_text, segs = _transcribe(chosen, whisper_model, language=lang, on_progress=_progress_reporter(task))
            # 回退1：VAD 有时会把整段语音都过滤掉（实测 B站 MV 会 0 字）→ 关闭 VAD 重试
            if not (seg_text or "").strip():
                logger.warning("任务 %s ASR 为空（VAD 可能误过滤，language=%s），关闭 VAD 重试",
                               task.taskId, lang)
                seg_text, segs = _transcribe(chosen, whisper_model, language=lang, vad=False,
                                        on_progress=_progress_reporter(task))
            # 回退2：强制 zh 时英文/纯音乐可能仍为空 → 自动语种 + 关 VAD
            if not (seg_text or "").strip():
                logger.warning("任务 %s 仍为空，改用自动语种 + 关VAD 重试", task.taskId)
                seg_text, segs = _transcribe(chosen, whisper_model, language=None, vad=False,
                                        on_progress=_progress_reporter(task))
            if not (seg_text or "").strip():
                logger.warning("任务 %s 三次尝试均无语音内容（可能是纯音乐/无口播）", task.taskId)
            task.sourceText = seg_text
            task.textContent = seg_text
            task.segments = segs

        # 无浏览器可播直链（典型：小红书本次只发 HLS；或走了直链下载兜底）
        # → 落一份本地 mp4 进任务目录，保住前端"视频预览/截图/保存"
        if not task.videoUrlList:
            _download_local_preview(task, work_url, work_dir, ready_media=ext_media, info=info)

        # 画面文字（可选）：识别视频内嵌文字（硬字幕/说明文字），与口播文案分开
        if ocr_frames:
            _run_frame_ocr(task, work_url, work_dir)

        task.status = TaskStatus.SUCCESS
        store.update_status(
            task.taskId, TaskStatus.SUCCESS,
            title=task.title, content=task.content, textContent=task.textContent,
            sourceText=task.sourceText, cover=task.cover,
            videoUrl=task.videoUrl, videoUrlList=task.videoUrlList,
            platform=task.platform, duration=task.duration,
            uploader=task.uploader, webpageUrl=task.webpageUrl,
            frameText=getattr(task, "frameText", None),
            frameBlocks=getattr(task, "frameBlocks", None),
            errorMessage=_empty_text_note(task),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("任务 %s 失败: %s\n%s", task.taskId, exc, traceback.format_exc())
        store.update_status(
            task.taskId, TaskStatus.FAILURE,
            errorMessage=_humanize_error(exc, getattr(task, "platform", "") or ""),
        )
    finally:
        # 尽量清理临时文件（保留本地预览视频 video.mp4，供 /media/proxy 长期服务）
        try:
            keep = work_dir / _LOCAL_PREVIEW_FILE
            has_keep = keep.is_file() and keep.stat().st_size > 0
            for f in work_dir.iterdir():
                if has_keep and f == keep:
                    continue
                f.unlink(missing_ok=True)
            if not has_keep:
                work_dir.rmdir()
        except Exception:
            pass


def start_async(task: TaskDetail, work_url: str, want_audio: bool,
                whisper_model: str, ocr_frames: bool = False) -> bool:
    """把任务交给平台队列（每平台串行，P1）。返回 False = 队列已满。"""
    from .scheduler import submit

    return submit(task, work_url, want_audio, whisper_model, ocr_frames=ocr_frames)
