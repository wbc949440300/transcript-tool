"""上传文件提取：本地视频/录音转文字。

用 faster-whisper 转写用户上传的音视频文件内容。
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

from .models import TaskDetail, TaskStatus
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
            capture_output=True, text=True, timeout=20,
        )
        return spec[0] in (r.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def _transcribe_file(path: Path, model_name: str = "tiny", on_progress=None) -> str:
    # 复用 extractor 的设备探测（GPU 优先，自动回退 CPU）与模型缓存——
    # 旧实现写死 CPU，24 分钟视频要转半小时（2026-10-30 实测踩坑）
    # P1：锁也用 extractor 的**全局** ASR 锁 —— 否则"链接任务 + 文件任务"会各拿
    # 一把锁同时跑两个模型，6GB 显存直接 OOM。
    from .extractor import _WHISPER_LOCK, _get_model, _pick_device

    device, compute_type = _pick_device()
    logger.info("文件转写: 设备=%s compute=%s 模型=%s", device, compute_type, model_name)
    with _WHISPER_LOCK:
        model = _get_model(model_name, device, compute_type)
        segments, _info = model.transcribe(str(path))
        total_sec = float(getattr(_info, "duration", 0) or 0)
        t_start = time.time()
        last_report = 0.0
        texts: list[str] = []
        for seg in segments:
            t = (seg.text or "").strip()
            if not t:
                continue
            texts.append(t)
            # 长视频（视频号常见 20~30 分钟）要能看到"还要多久"
            if on_progress and total_sec > 0:
                now = time.time()
                if now - last_report >= 5:
                    last_report = now
                    done = float(getattr(seg, "end", 0) or 0)
                    rate = done / max(0.1, now - t_start)
                    eta = (total_sec - done) / rate if rate > 0 else 0.0
                    try:
                        on_progress(done, total_sec, eta)
                    except Exception:  # noqa: BLE001
                        pass
    return "\n".join(texts)


def _run_file_task(task: TaskDetail, file_path: Path, model_name: str, src_name: str,
                   ocr_frames: bool = False, notify: bool = False) -> None:
    kept_video: Path | None = None
    try:
        store.update_status(task.taskId, TaskStatus.RUNNING)

        has_audio = _probe_has_stream(file_path, "a:0")
        has_video = _probe_has_stream(file_path, "v:0")
        if not has_audio and not has_video:
            raise RuntimeError("该文件没有可识别的音视频流（文件可能损坏或为空）")

        # 无音轨（纯画面视频，视频号"告示牌"类常见）→ 跳过语音转写，走画面文字
        text = ""
        if has_audio:
            from .extractor import _progress_reporter

            text = _transcribe_file(file_path, model_name, on_progress=_progress_reporter(task))
        else:
            logger.info("文件无音轨，跳过语音转写: %s", src_name)

        task.title = src_name
        task.textContent = text
        task.content = ""

        # 含视频流 → 把视频保留进任务目录，历史详情即可像链接任务一样预览/截图
        from .extractor import _has_video_stream

        if _has_video_stream(file_path):
            try:
                dest_dir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks")) / task.taskId
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / ("video" + file_path.suffix.lower())
                shutil.move(str(file_path), str(dest))
                kept_video = dest
                task.videoUrl = "file://" + dest.name
                task.videoUrlList = [task.videoUrl]
            except Exception as exc:  # noqa: BLE001
                logger.warning("保留预览视频失败（不影响转写结果）: %s", exc)

        # 画面文字（可选）：视频号等平台的文字往往写在画面里
        if ocr_frames and (kept_video is not None or has_video):
            try:
                from .frame_ocr import extract_frame_text

                src = str(kept_video or file_path)
                fr = extract_frame_text(src)
                if fr.get("text"):
                    task.frameText = fr["text"]
            except Exception as exc:  # noqa: BLE001
                logger.warning("画面文字识别失败（不影响转写结果）: %s", exc)

        from .extractor import _empty_text_note

        store.update_status(
            task.taskId, TaskStatus.SUCCESS,
            title=task.title, textContent=task.textContent,
            content=task.content, errorMessage=_empty_text_note(task, asr=True),
            platform="本地文件",
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
                    ocr_frames: bool = False, notify: bool = False) -> None:
    import threading
    th = threading.Thread(
        target=_run_file_task,
        args=(task, tmp_file, model_name, src_name),
        kwargs={"ocr_frames": ocr_frames, "notify": notify},
        daemon=True,
    )
    th.start()
