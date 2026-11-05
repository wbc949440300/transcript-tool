"""画面文字识别：视频抽帧 → OCR → 按「频率 + 位置 + 字号」分类去重。

用户需求（2026-10-30）：把"画面里的文字"与"口播文案"分开提取，且区分
创作者内容（硬字幕/说明文字）与噪音（水印/台标/界面元素）。

分类规则（对所有平台统一，无需按平台特判）：
- 临时出现（<60% 帧出现）                          → 硬字幕/花字   → 画面文字 ✓
- 持续出现（≥60% 帧）+ 角落 + 小字                 → 水印/台标/UI  → 丢弃 ✗
- 持续出现但居中大字（如视频号"告示牌"型视频）      → 正文          → 画面文字 ✓
（"角"= 水平与垂直都靠边；"小"= 字高 < 6% 画面高）

输出：blocks（带首次出现时间）+ 合并文本。OCR 引擎复用 ocr.py 的单例。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .subproc import hidden

logger = logging.getLogger("frame_ocr")

_MIN_LINE_SCORE = 0.45      # OCR 置信度下限
_PERSISTENT_RATIO = 0.6     # 出现帧占比 ≥ 该值视为"常驻"
_CORNER_X = 0.25            # 水平靠边阈值（距左右边缘 25% 内）
_CORNER_Y = 0.16            # 垂直靠边阈值（距上下边缘 16% 内）
_SMALL_H = 0.06             # 小字阈值（字高/画面高）
_MIN_TEXT_LEN = 1
# 作者展示文案 vs 画面杂字（2026-11-02 实测：作者主文案水平正中 cx≈0.50、
# 字号约为杂字的 2 倍；背景招牌/贴纸靠右 cx≈0.70~0.92 且字号小）
_MAIN_CX_TOL = 0.20         # 水平中心容差（0.30~0.70 视为居中）
_MAIN_H_RATIO = 0.55        # 字号 ≥ 全画面最大字号的该比例才算主文案
_MAIN_ABS_MIN = 0.035       # 且字号 ≥ 画面高 3.5%（纯小字视频不硬造主文案）


def _video_duration(path: str) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30, **hidden(),
        )
        return float((r.stdout or "").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def _video_size(path: str) -> tuple[int, int]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30, **hidden(),
        )
        parts = (r.stdout or "").strip().split(",")
        return int(parts[0]), int(parts[1])
    except Exception:  # noqa: BLE001
        return 0, 0


def _norm_key(text: str) -> str:
    """分组键：去掉空白/标点/大小写差异，剩下"内容骨架"。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text).lower()


def _ocr_lines(img: Path, W: int, H: int) -> list[tuple[str, float, float, float]]:
    """单帧 OCR，返回 [(text, cx, cy, h)]，坐标均为 0~1 归一化。"""
    from . import ocr as ocr_mod

    engine = ocr_mod._get_engine()
    result = engine(str(img))
    out: list[tuple[str, float, float, float]] = []
    for item in (result[0] if result and result[0] else []):
        try:
            box, text, score = item[0], item[1], item[2]
        except Exception:  # noqa: BLE001
            continue
        if not text or not text.strip() or float(score or 0) < _MIN_LINE_SCORE:
            continue
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        cx = ((min(xs) + max(xs)) / 2) / max(W, 1)
        cy = ((min(ys) + max(ys)) / 2) / max(H, 1)
        h = (max(ys) - min(ys)) / max(H, 1)
        out.append((text.strip(), cx, cy, h))
    return out


def _is_corner(cx: float, cy: float) -> bool:
    return (cx < _CORNER_X or cx > 1 - _CORNER_X) and (cy < _CORNER_Y or cy > 1 - _CORNER_Y)


def _ocr_workers() -> int:
    # 默认串行：服务进程里并行会与 onnxruntime 内部线程抢占，实测反而更慢
    # （4 帧密集文字：串行 ~19s，4 线程 ~26s）。需要时可用环境变量开启。
    try:
        return max(1, min(8, int(os.getenv("TRANSCRIPT_FRAME_OCR_WORKERS", "1") or 1)))
    except Exception:  # noqa: BLE001
        return 1


_DUP_HAMMING = 8          # dHash 汉明距离 ≤ 该值视为同一画面，跳过重复 OCR


def _frame_hash(path: Path) -> str | None:
    """16x16 灰度 dHash（廉价的画面相似度指纹）；cv2 不可用时返回 None。"""
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return None
    try:
        img = cv2.imread(str(path))
        if img is None:
            return None
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (17, 16))
        return "".join("1" if g[y, x] > g[y, x + 1] else "0"
                       for y in range(16) for x in range(16))
    except Exception:  # noqa: BLE001
        return None


def _hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def _ocr_frames(frames: list[Path], W: int, H: int) -> list[list]:
    """OCR 多帧：近似重复的画面只识别一次，其余复用同一份结果。

    实测 12.9 秒“免责声明”类视频 9 帧里只有约 3 个不同画面，重复 OCR
    17 帧要 107 秒；跳过近似帧后只识别 3 帧。
    默认串行；TRANSCRIPT_FRAME_OCR_WORKERS 可开并行（服务内通常更慢）。
    """
    def one(p: Path):
        try:
            return _ocr_lines(p, W, H)
        except Exception as exc:  # noqa: BLE001
            logger.warning("单帧 OCR 失败(%s): %s", p, exc)
            return []

    hashes = [_frame_hash(p) for p in frames]
    # 连续相似帧归为一组，取每组**最后一帧**识别：
    # 文字往往是逐步显示/滚动出现的，组内最后一帧内容最完整（比取第一帧更不容易漏字）。
    group_last: list[int] = []
    for i, h in enumerate(hashes):
        if (group_last and h and hashes[group_last[-1]]
                and _hamming(h, hashes[group_last[-1]]) <= _DUP_HAMMING):
            group_last[-1] = i
        else:
            group_last.append(i)

    # 组代表帧之间再做一次相似去重（处理 A→B→A 这类回到原画面）
    picked: list[int] = []
    reuse_pick: dict[int, int] = {}
    cache: list[tuple[str, int]] = []
    for idx in group_last:
        h = hashes[idx]
        src = None
        if h:
            for ph, pi in cache:
                if _hamming(h, ph) <= _DUP_HAMMING:
                    src = pi
                    break
        if src is None:
            picked.append(idx)
            if h:
                cache.append((h, idx))
        else:
            reuse_pick[idx] = src

    workers = _ocr_workers()
    if workers <= 1 or len(picked) <= 1:
        uniq_lines = [one(frames[i]) for i in picked]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(workers, len(picked))) as ex:
            uniq_lines = list(ex.map(lambda i: one(frames[i]), picked))

    by_idx = dict(zip(picked, uniq_lines))
    # 每帧 → 所属组的代表帧
    rep_of = [0] * len(frames)
    prev = -1
    for idx in group_last:
        for i in range(prev + 1, idx + 1):
            rep_of[i] = idx
        prev = idx
    results: list[list] = []
    for i in range(len(frames)):
        rep = rep_of[i]
        results.append(by_idx[reuse_pick[rep]] if rep in reuse_pick else by_idx[rep])
    skipped = len(frames) - len(picked)
    if skipped:
        logger.info("画面文字: %d 帧近似重复/同组，跳过重复 OCR（实际识别 %d 帧）",
                    skipped, len(picked))
    return results


def _frame_cmd(source: str, referer: str, interval: float, max_frames: int,
               out_pattern: str, scale_w: int = 0,
               keyframes_only: bool = False) -> list[str]:
    """构造抽帧命令。

    - 只有 http(s) 源才加 `-headers`：对**本地文件**加这个选项 ffmpeg 会直接报
      "Option headers not found" 而整条抽帧失败（2026-10-31 实测踩到）。
    - scale_w > 0 时把帧缩到该宽度：1080x1920 每帧 OCR 要 ~6 秒，缩到 720 宽
      字幕照样清楚、整体快一倍以上（2026-10-31 实测）。
    """
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if keyframes_only:
        # ★ 2026-11-04：只解关键帧。抖音长视频按 fps 过滤要顺序解到第 90 秒，
        #   实测 54.8s 才出 3 张（CDN 中途截断：Stream ends prematurely），
        #   加这个开关后同一片 3.4s 出满 5 张。
        cmd += ["-skip_frame", "nokey"]
    if referer and str(source).lower().startswith(("http://", "https://")):
        cmd += ["-headers", f"Referer: {referer}\r\nUser-Agent: Mozilla/5.0\r\n"]
    vf = f"fps=1/{interval:.2f}"
    if scale_w > 0:
        vf += f",scale={int(scale_w)}:-2"
    # ★ 2026-11-04：输出侧加 `-strict unofficial`（=-2）。
    #   MJPEG 素材（视频号/微信下来的片子常见）是 full-range YUV，ffmpeg 的
    #   **mjpeg 编码器**默认会拒绝："Non full-range YUV is non-standard"，
    #   结果一张都不出（浏览器却能正常播）。所以放宽的是写 jpg 这一侧。
    cmd += ["-i", source, "-vf", vf, "-frames:v",
            str(max_frames), "-q:v", "4", "-strict", "unofficial", out_pattern]
    return cmd


_PROBE_FRAMES = 8          # 探针帧数：先扫这几帧，没有"正文类文字"就跳过全扫
_PROBE_MIN_LINES = 1       # 探针至少命中几条"非水印"文字才算有画面文字


def _scan(source: str, referer: str, interval: float, frames_n: int, W: int, H: int,
          tmp: Path, tag: str):
    """抽 frames_n 帧 → OCR → 按频率/位置/字号聚合。

    返回 (blocks, dropped, 实际帧数, 原始行)；原始行供探针判定用。
    """
    cmd = _frame_cmd(source, referer, interval, frames_n,
                     str(tmp / f"{tag}_%03d.jpg"), scale_w=W)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, **hidden())
    frames = sorted(tmp.glob(f"{tag}_*.jpg"))
    if not frames:
        logger.warning("抽帧失败(%s): %s", tag, (r.stderr or "")[:150])
        return [], 0, 0, []

    total = len(frames)
    agg: dict[str, dict] = {}
    order: list[str] = []
    raw: list[tuple[str, float, float, float]] = []
    frame_lines = _ocr_frames(frames, W, H)
    for idx, lines in enumerate(frame_lines):
        t = round(idx * interval, 1)
        for text, cx, cy, h in lines:
            if len(text.strip()) < _MIN_TEXT_LEN:
                continue
            k = _norm_key(text)
            if not k:
                continue
            raw.append((text, cx, cy, h))
            rec = agg.get(k)
            if rec is None:
                rec = {"count": 0, "first_t": t, "samples": {text: 0},
                       "corner": 0, "small": 0,
                       "sum_cx": 0.0, "sum_cy": 0.0, "sum_h": 0.0}
                agg[k] = rec
                order.append(k)
            rec["count"] += 1
            rec["samples"][text] = rec["samples"].get(text, 0) + 1
            rec["sum_cx"] += cx
            rec["sum_cy"] += cy
            rec["sum_h"] += h
            if _is_corner(cx, cy):
                rec["corner"] += 1
            if h < _SMALL_H:
                rec["small"] += 1

    survivors: list[dict] = []
    dropped = 0
    for k in order:
        rec = agg[k]
        persistent = rec["count"] >= max(3, _PERSISTENT_RATIO * total)
        corner = rec["corner"] >= rec["count"] * 0.6
        small = rec["small"] >= rec["count"] * 0.6
        if persistent and corner and small:
            dropped += 1  # 水印/台标/界面元素
            continue
        rec["avg_cx"] = rec["sum_cx"] / rec["count"]
        rec["avg_h"] = rec["sum_h"] / rec["count"]
        survivors.append(rec)
    max_h = max((r["avg_h"] for r in survivors), default=0.0)
    blocks: list[dict] = []
    for rec in survivors:
        text = max(rec["samples"].items(), key=lambda x: x[1])[0]
        # 作者展示文案：水平居中 + 字号够大（相对全画面最大字号 + 绝对下限）
        is_main = (max_h >= _MAIN_ABS_MIN
                   and rec["avg_h"] >= _MAIN_H_RATIO * max_h
                   and abs(rec["avg_cx"] - 0.5) <= _MAIN_CX_TOL)
        blocks.append({"t": rec["first_t"], "text": text,
                       "kind": "main" if is_main else "scene"})
    blocks.sort(key=lambda b: b["t"])
    return blocks, dropped, total, raw


def _probe_has_text(raw: list[tuple[str, float, float, float]]) -> bool:
    """探针判定：画面里有没有"像正文/字幕"的文字。

    单帧看不出"是否常驻"，这里只排除"既靠边又很小"的行（水印/台标/UI 的典型特征），
    其余的（居中、或字够大）都算有内容 → 值得跑全扫。
    """
    hits = 0
    for text, cx, cy, h in raw:
        if len(_norm_key(text)) < 2:
            continue
        if _is_corner(cx, cy) and h < _SMALL_H:
            continue
        hits += 1
        if hits >= _PROBE_MIN_LINES:
            return True
    return False


def extract_frame_text(source: str, duration: float | None = None,
                       max_frames: int = 48, referer: str = "") -> dict:
    """主入口。source 可为本地文件或 http 直链（ffmpeg 直接拉流）。

    两段式（2026-10-31 提速）：先扫少量**探针帧**；没有画面文字就直接返回，
    不再跑完整的 48 帧（大量视频没有硬字幕，这一刀省掉大部分时间）。

    返回 {"blocks": [{"t": 秒, "text": str}], "text": "按时间合并的文本",
          "dropped": 丢弃的水印条数, "probed": 探针帧数, "skipped": 是否跳过全扫}
    """
    from . import ocr as ocr_mod  # 确保引擎预热计入调用方计时

    max_frames = max(1, int(max_frames))
    dur = duration or _video_duration(source)
    W, H = _video_size(source)
    if not W or not H:
        W, H = 720, 1280
    scale_w = min(720, W)
    if scale_w < W:
        H = max(1, int(round(H * scale_w / W)))
    W = scale_w
    try:
        probe_n = int(os.getenv("TRANSCRIPT_FRAME_OCR_PROBE", str(_PROBE_FRAMES)) or _PROBE_FRAMES)
    except Exception:  # noqa: BLE001
        probe_n = _PROBE_FRAMES
    probe_n = max(1, min(probe_n, max_frames))

    # 短视频：探针覆盖已接近全扫（差 1~2 帧），不值得再重扫一遍；
    # 直接复用探针结果（12.9 秒视频：探针 8 帧、全扫 9 帧，重扫纯属浪费）。
    if dur and dur > 0:
        full_interval = max(1.5, dur / max_frames)
        full_n = int(dur / full_interval) + 1
        if full_n <= probe_n + 2:
            max_frames = probe_n

    tmp = Path(tempfile.mkdtemp(prefix="frames_"))
    try:
        # 1) 探针：帧数少，但间隔按整条视频均分（覆盖率不缩水）
        p_interval = 1.5 if not dur else max(1.5, dur / probe_n)
        p_blocks, p_dropped, p_total, raw = _scan(source, referer, p_interval, probe_n,
                                                  W, H, tmp, "p")
        if not _probe_has_text(raw):
            logger.info("画面文字探针: %d 帧未见字幕/正文文字 → 跳过全扫", p_total)
            return {"blocks": [], "text": "", "main_text": "", "scene_text": "",
                    "dropped": 0, "probed": p_total, "skipped": True}

        if probe_n >= max_frames:
            blocks, dropped, total = p_blocks, p_dropped, p_total
        else:
            # 2) 全扫：覆盖整条视频（间隔按总时长均分，不封顶）
            interval = 1.5 if not dur else max(1.5, dur / max_frames)
            blocks, dropped, total, _raw = _scan(source, referer, interval, max_frames,
                                                 W, H, tmp, "f")
        merged = "\n".join(b["text"] for b in blocks)
        main_text = "\n".join(b["text"] for b in blocks if b.get("kind") == "main")
        scene_text = "\n".join(b["text"] for b in blocks if b.get("kind") != "main")
        logger.info("画面文字: 探针 %d 帧命中 → 全扫 %d 帧 → %d 条"
                    "（作者文案 %d 条 / 画面其他 %d 条，丢弃水印 %d）",
                    p_total, total, len(blocks),
                    sum(1 for b in blocks if b.get("kind") == "main"),
                    sum(1 for b in blocks if b.get("kind") != "main"), dropped)
        return {"blocks": blocks, "text": merged,
                "main_text": main_text, "scene_text": scene_text,
                "dropped": dropped, "probed": p_total, "skipped": False}
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def _probe_duration(source: str, referer: str = "", timeout: float = 20.0) -> float:
    """ffprobe 读总时长，失败返回 0（调用方据此按固定间隔抽）。"""
    cmd = ["ffprobe", "-v", "error"]
    if referer and str(source).lower().startswith(("http://", "https://")):
        cmd += ["-headers", f"Referer: {referer}\r\nUser-Agent: Mozilla/5.0\r\n"]
    cmd += ["-show_entries", "format=duration", "-of", "csv=p=0", source]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **hidden())
        line = (r.stdout or "").strip().splitlines()
        return float(line[0]) if line else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def grab_thumbnails(source: str, duration: float | None = None, referer: str = "",
                    n: int = 5, out_dir: Path | None = None,
                    width: int = 960) -> list[Path]:
    """从视频源抓 n 张均匀分布的缩略图（**服务端 ffmpeg 抽帧**）。

    ★ 2026-11-03 加：结果页的"视频截图"改用它。
    以前是浏览器另开一个 video 元素、seek 到 5 个时间点再 drawImage —— 那要求
    "第二路流能及时缓冲到目标位置"，而抖音/B站 CDN 对同一条直链的并发流本来就慢，
    实测就是 seek 超时、截图全黑（截图失败率极高）。ffmpeg 直接从 URL 抽帧
    没有这个问题 —— OCR 那条路一直就是这么干的，这里复用它。

    返回按时间顺序排列的 jpg 路径列表；失败返回 []（调用方据此走兜底）。
    """
    src = (source or "").strip()
    if not src:
        return []
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="thumbs_"))
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("thumb_*.jpg"):          # 重抽前清掉旧的，避免张数对不上
        try:
            f.unlink()
        except Exception:  # noqa: BLE001
            pass

    n = max(1, min(int(n or 5), 12))
    dur = float(duration) if isinstance(duration, (int, float)) and duration > 0 else 0.0
    is_http = src.lower().startswith(("http://", "https://"))
    if dur <= 0:
        # 本地文件任务的历史记录里没存时长 → 用 ffprobe 补，否则只能按固定 2 秒
        # 间隔瞎抽，长片会全挤在前几秒。
        dur = _probe_duration(src, referer)
    # 均匀取样：跳过开头和结尾各一点点，避免黑场/片尾
    interval = max(dur / (n + 1), 0.3) if dur else 2.0
    w = max(0, int(width))
    if not is_http and dur > 0:
        # 本地文件：5 路并发逐点 seek 最快（毫秒级），也不受「关键帧标记不可靠」影响
        # —— MJPEG 素材加 -skip_frame nokey 实测一张都抽不出来。
        return _seek_thumbnails(src, referer, dur, n, out, w)
    cmd = _frame_cmd(src, referer, interval, n, str(out / "thumb_%03d.jpg"),
                     scale_w=w, keyframes_only=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, **hidden())
    except Exception as exc:  # noqa: BLE001
        logger.warning("抽缩略图异常(%s): %s", src[:70], exc)
        return []
    frames = sorted(out.glob("thumb_*.jpg"))
    if len(frames) < n and dur > 0:
        # 关键帧太少/为空（或 CDN 中途截断）→ 逐点 seek 兜底：每个点只读几十 KB，
        # 并发跑 5 个实测 2~3 秒，比"顺序解到第 N 秒再抽"快一个数量级。
        filled = _seek_thumbnails(src, referer, dur, n, out, w)
        if len(filled) > len(frames):
            frames = filled
    if not frames:
        logger.warning("抽缩略图无输出(%s): %s", src[:70], (r.stderr or "")[:150])
    return frames


def _seek_thumbnails(source: str, referer: str, duration: float, n: int,
                     out: Path, width: int = 960) -> list[Path]:
    """按时间点逐帧 seek 抽图（并发），返回按时间排序的路径列表。

    只在"关键帧过滤拿不满 n 张"时兜底用：每次 seek 由 ffmpeg 用 Range 直接跳到
    目标位置附近的 I 帧，不会像顺序解码那样把整条流读一遍。
    """
    import subprocess as _sp
    from concurrent.futures import ThreadPoolExecutor

    interval = max(duration / (n + 1), 0.3)
    for f in out.glob("thumb_*.jpg"):
        try:
            f.unlink()
        except Exception:  # noqa: BLE001
            pass

    def _one(k: int) -> Path | None:
        t = interval * (k + 1)
        dst = out / ("thumb_%03d.jpg" % (k + 1))
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if referer and str(source).lower().startswith(("http://", "https://")):
            cmd += ["-headers", f"Referer: {referer}\r\nUser-Agent: Mozilla/5.0\r\n"]
        cmd += ["-ss", "%.2f" % t, "-i", source]
        if width > 0:
            cmd += ["-vf", "scale=%d:-2" % width]
        cmd += ["-frames:v", "1", "-q:v", "4", "-strict", "unofficial", str(dst)]
        try:
            _sp.run(cmd, capture_output=True, text=True, timeout=60, **hidden())
        except Exception:  # noqa: BLE001
            return None
        return dst if dst.is_file() and dst.stat().st_size > 0 else None

    try:
        with ThreadPoolExecutor(max_workers=min(5, n)) as pool:
            got = [x for x in pool.map(_one, range(n)) if x]
    except Exception as exc:  # noqa: BLE001
        logger.warning("逐点抽帧异常(%s): %s", source[:70], exc)
        return []
    logger.info("逐点 seek 抽帧: %d/%d 张", len(got), n)
    return sorted(got)
