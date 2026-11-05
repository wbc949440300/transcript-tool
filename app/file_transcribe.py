"""上传文件提取：本地视频/录音转文字。

用 faster-whisper 转写用户上传的音视频文件内容。
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

from .models import TaskDetail, TaskStatus
from .subproc import hidden
from .task_store import store

logger = logging.getLogger("file_transcribe")

_ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4a", ".mp3",
                ".wav", ".aac", ".ogg", ".opus", ".flac", ".wma", ".m4v"}


def is_allowed_ext(filename: str) -> bool:
    return Path(filename).suffix.lower() in _ALLOWED_EXT


def _probe_has_stream(path: Path, spec: str) -> bool:
    """ffprobe 检查是否含指定流（spec 如 'v:0' / 'a:0'）。探测失败按 False 处理。"""
    import subprocess

    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", spec,
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20, **hidden(),
        )
        return spec[0] in (r.stdout or "")
    except Exception:  # noqa: BLE001
        return False


_BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}


def _video_codec(path: Path) -> str:
    import subprocess

    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20, **hidden(),
        )
        return (r.stdout or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _ensure_browser_preview(path: Path) -> Path:
    """HEVC/H.265 等浏览器不能解码的视频 → 转成 H.264 预览。

    实测 8.7 秒 720x1280 HEVC 转 H.264 仅 0.3 秒；不转的话 Chrome
    能读出时长但 videoWidth=0，播放器就是纯黑（用户 2026-11-02 反馈）。
    """
    import subprocess

    codec = _video_codec(path)
    if codec in _BROWSER_VIDEO_CODECS or not codec:
        return path
    tmp = path.with_name("video_h264.tmp.mp4")
    out = path.with_name("video_h264.mp4")
    base = ["ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-movflags", "+faststart"]
    for audio_args in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "128k"]):
        try:
            if tmp.exists():
                tmp.unlink()
            r = subprocess.run(base + audio_args + [str(tmp)],
                               capture_output=True, text=True, timeout=120, **hidden())
            if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                target = path if path.suffix.lower() == ".mp4" else out
                if target.exists() and target != tmp:
                    target.unlink()
                tmp.replace(target)
                logger.info("视频编码 %s 浏览器不支持，已转 H.264 预览: %s", codec, target.name)
                return target
        except Exception as exc:  # noqa: BLE001
            logger.warning("视频转 H.264 失败(%s): %s", audio_args, exc)
    try:
        tmp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    logger.warning("视频编码 %s 无法转为 H.264，浏览器预览可能黑屏", codec)
    return path


def _transcribe_file(path: Path, model_name: str = "tiny", on_progress=None) -> str:
    """转写本地文件，复用链接任务同一套 ASR 参数。

    2026-11-02 修：此前直接 `model.transcribe(path)`，等于走 whisper 全默认参数——
    丢掉了 language="zh"、简体中文 initial_prompt、vad_filter 与
    condition_on_previous_text=False。后果是上传文件与监视目录（视频号主链路）
    实测输出繁体、静音段易出幻觉文本。

    现改为委托 extractor._transcribe：
      · 设备探测、模型缓存、全局 ASR 锁都在它内部（锁不可重复获取，故此处不再加锁）
      · 语言预检会把明显非中文的音频自动回退到自动识别，不会误伤英文素材
      · 顺带获得 ffmpeg 16k 单声道 + 高通滤波的音频增强
    """
    from .extractor import _transcribe

    logger.info("文件转写: 模型=%s（走 extractor._transcribe 统一参数）", model_name)
    text, _segs = _transcribe(path, model_name, language="zh", on_progress=on_progress)
    return text


def _resolve_auto_model(model_name: str, media: Path) -> str:
    """「auto」自适应（2026-11-05 用户定的策略）：短片（≤ TRANSCRIPT_AUTO_MEDIUM_MAX_SEC，
    默认 240 秒 = 4 分钟，用户定的基准）用 medium 换人名/数字准确率；长视频自动降 tiny
    控制耗时。时长探测失败按长视频处理（速度优先）。用户显式选了模型则原样返回。"""
    if model_name != "auto":
        return model_name
    try:
        limit = float(os.getenv("TRANSCRIPT_AUTO_MEDIUM_MAX_SEC", "240"))
    except ValueError:
        limit = 120.0
    from .frame_ocr import _probe_duration

    dur = _probe_duration(str(media))
    pick = "medium" if 0 < dur <= limit else "tiny"
    logger.info("转写模型自适应: 时长 %.0f 秒（阈值 %.0f 秒）→ %s", dur, limit, pick)
    return pick


def _run_file_task(task: TaskDetail, file_path: Path, model_name: str, src_name: str,
                   ocr_frames: bool = False, notify: bool = False,
                   platform: str = "本地文件") -> None:
    try:
        store.update_status(task.taskId, TaskStatus.RUNNING)

        has_audio = _probe_has_stream(file_path, "a:0")
        has_video = _probe_has_stream(file_path, "v:0")
        if not has_audio and not has_video:
            raise RuntimeError("该文件没有可识别的音视频流（文件可能损坏或为空）")

        # 含视频流 → 视频保留进任务目录（历史详情可预览/截图）+ 画面文字，与 ASR
        # **并行**跑（2026-11-05 优化：ASR 占 GPU、抽帧 OCR 占 CPU，串行时两边纯排队）。
        # 视频副本用 copy 不用 move —— 转写线程此刻可能还在读原文件的音轨。
        from .extractor import _has_video_stream

        worker = None
        if has_video or _has_video_stream(file_path):
            def _keep_and_ocr():
                try:
                    dest_dir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks")) / task.taskId
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / ("video" + file_path.suffix.lower())
                    shutil.copyfile(str(file_path), str(dest))
                    kept = _ensure_browser_preview(dest)
                    task.videoUrl = "file://" + kept.name
                    task.videoUrlList = [task.videoUrl]
                    if not ocr_frames:
                        return
                    from .frame_ocr import extract_frame_text

                    fr = extract_frame_text(str(kept))
                    if fr.get("text"):
                        task.frameText = fr["text"]
                        task.frameBlocks = fr.get("blocks") or []
                        task.frameMainText = fr.get("main_text") or None
                        task.frameSceneText = fr.get("scene_text") or None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("预览视频/画面文字处理失败（不影响转写结果）: %s", exc)

            if has_audio:
                worker = threading.Thread(target=_keep_and_ocr, daemon=True,
                                          name=f"keepocr-{task.taskId}")
                worker.start()
            else:
                _keep_and_ocr()

        # 无音轨（纯画面视频，视频号"告示牌"类常见）→ 跳过语音转写，走画面文字
        text = ""
        if has_audio:
            from .extractor import _progress_reporter

            model_use = _resolve_auto_model(model_name, file_path)
            text = _transcribe_file(file_path, model_use, on_progress=_progress_reporter(task))
        else:
            logger.info("文件无音轨，跳过语音转写: %s", src_name)
        if worker is not None:
            worker.join()          # 画面文字没算完不算完成（结果字段要齐）

        task.title = src_name
        task.textContent = text
        task.content = ""

        from .extractor import _empty_text_note

        store.update_status(
            task.taskId, TaskStatus.SUCCESS,
            title=task.title, textContent=task.textContent,
            content=task.content, errorMessage=_empty_text_note(task, asr=True),
            platform=platform,
            videoUrl=getattr(task, "videoUrl", None),
            videoUrlList=getattr(task, "videoUrlList", None),
            frameText=getattr(task, "frameText", None),
        )
        if notify:
            _notify_done(True, src_name, len((task.textContent or "").strip()))
    except Exception as exc:  # noqa: BLE001
        logger.error("文件转写任务 %s 失败: %s", task.taskId, exc)
        store.update_status(task.taskId, TaskStatus.FAILURE, errorMessage=str(exc))
        if notify:
            _notify_done(False, src_name, 0, str(exc))
    finally:
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass


def _notify_done(ok: bool, title: str, chars: int = 0, note: str = "") -> None:
    """完成气泡（监听目录/视频号流程专用；失败不影响主流程）。"""
    try:
        from .alerts import notify_task_done

        notify_task_done(title, ok, chars=chars, note=note)
    except Exception:  # noqa: BLE001
        pass


def start_file_task(task: TaskDetail, tmp_file: Path, model_name: str, src_name: str,
                    ocr_frames: bool = False, notify: bool = False,
                    platform: str = "本地文件") -> None:
    import threading
    th = threading.Thread(
        target=_run_file_task,
        args=(task, tmp_file, model_name, src_name),
        kwargs={"ocr_frames": ocr_frames, "notify": notify, "platform": platform},
        daemon=True,
    )
    th.start()
