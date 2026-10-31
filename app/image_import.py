"""本地图片导入：一组图片 → OCR → 一条文案（图集工作流）。

场景：抖音图集（图文）网页端拿不到数据，用户只能把图片存到本地。
把图片丢进监听目录，这里把**同一批**图片合成**一条**任务（一次图集 = 一份文案），
而不是每张图一个人任务。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .models import TaskDetail, TaskStatus
from .ocr import ocr_image
from .task_store import store

logger = logging.getLogger("image_import")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
              ".heic", ".avif")

MAX_IMAGES = 30


def is_image_ext(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTS


def _notify_done(ok: bool, title: str, chars: int = 0, note: str = "") -> None:
    try:
        from .alerts import notify_task_done

        notify_task_done(title, ok, chars=chars, note=note)
    except Exception:  # noqa: BLE001
        pass


def _run_image_task(task: TaskDetail, paths: list[Path], names: list[str],
                    notify: bool = False) -> None:
    try:
        store.update_status(task.taskId, TaskStatus.RUNNING,
                            errorMessage=f"正在识别图片文字（共 {len(paths)} 张）...")
        texts: list[str] = []
        for i, p in enumerate(paths, 1):
            store.update_status(task.taskId, TaskStatus.RUNNING,
                                errorMessage=f"正在识别图片文字（OCR）{i}/{len(paths)}...")
            try:
                t = (ocr_image(p.read_bytes()) or "").strip()
                if t:
                    texts.append(t)
            except Exception as exc:  # noqa: BLE001
                logger.warning("第 %d 张图 OCR 失败: %s", i, exc)
        text = "\n\n".join(texts)
        if not text.strip():
            raise RuntimeError("这批图片都没识别到文字（可能是纯图片或截图无文字）。")
        task.textContent = text
        task.content = ""
        task.platform = "图片OCR"
        store.update_status(task.taskId, TaskStatus.SUCCESS, title=task.title,
                            content="", textContent=text, platform="图片OCR",
                            errorMessage=None)
        logger.info("图片集 OCR 完成：%d 张 → %d 字（%s）",
                    len(paths), len(text), (names[0] if names else "")[:30])
        if notify:
            _notify_done(True, task.title or "图片集", len(text))
    except Exception as exc:  # noqa: BLE001
        logger.error("图片集任务 %s 失败: %s", task.taskId, exc)
        store.update_status(task.taskId, TaskStatus.FAILURE, errorMessage=str(exc))
        if notify:
            _notify_done(False, task.title or "图片集", 0, str(exc))
    finally:
        for p in paths:                      # 清掉 tmp_uploads 里的副本
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


def start_image_task(task: TaskDetail, paths: list[Path], names: list[str],
                     notify: bool = False) -> None:
    """起一个后台线程做这组图片的 OCR（不占用 ASR 锁，也不进平台队列）。"""
    th = threading.Thread(target=_run_image_task, args=(task, paths, names),
                          kwargs={"notify": notify},
                          daemon=True, name=f"image-ocr-{task.taskId}")
    th.start()