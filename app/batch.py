"""批量提取：把多条链接交给平台队列（P1）。

以前是"起一条、睡 3 秒、再起一条"，但任务本身要跑几十秒 → 实际是并发的。
现在统一进 scheduler 的每平台队列：同一平台严格串行，跨平台并行，
批量之间不需要再手工 sleep（队列本身就是节流）。
"""
from __future__ import annotations

import logging
import threading

from .extractor import start_async
from .links import LinkError, normalize
from .models import TaskDetail, TaskStatus
from .task_store import store
from .utils import detect_platform, gen_task_id

logger = logging.getLogger("batch")

_BATCH_THREADS: set[threading.Thread] = set()


def start_batch(batch_id: str, urls: list[str], whisper_model: str = "base") -> None:
    """为每条 URL 先创建 PENDING 子任务并入 store，然后后台逐个入队。"""
    items: list[TaskDetail] = []
    for idx, url in enumerate(urls):
        url = (url or "").strip()
        task = TaskDetail(
            taskId=gen_task_id(),
            taskType="transcript",
            status=TaskStatus.PENDING,
            errorMessage="排队中...",
            platform=detect_platform(url),
            webpageUrl=url or None,
            batchId=batch_id,
            index=idx,
            total=len(urls),
        )
        items.append((task, url))
    # 一次性登记整批（子任务 + batch 元数据）
    store.create_batch(batch_id, [t for t, _ in items])

    def _worker():
        for task, url in items:
            if not url:
                store.update_status(task.taskId, TaskStatus.FAILURE, errorMessage="空链接")
                continue
            # 逐条归一化（短链跳转在后台完成，不阻塞接口）
            try:
                fixed, _ = normalize(url)
            except LinkError as exc:
                store.update_status(task.taskId, TaskStatus.FAILURE, errorMessage=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001
                store.update_status(task.taskId, TaskStatus.FAILURE,
                                    errorMessage=f"链接解析失败: {exc}")
                continue
            store.update_status(task.taskId, TaskStatus.PENDING, errorMessage="排队中...",
                                webpageUrl=fixed, platform=detect_platform(fixed))
            from .platforms import frame_ocr_default_for

            if not start_async(task, fixed, False, whisper_model,
                               ocr_frames=frame_ocr_default_for(fixed)):
                store.update_status(
                    task.taskId, TaskStatus.FAILURE,
                    errorMessage="该平台排队已满（见 TRANSCRIPT_QUEUE_MAX），请稍后再试。")
        # 子任务由平台队列逐个推进状态

    th = threading.Thread(target=_worker, daemon=True, name="batch-enqueue")
    th.start()
    _BATCH_THREADS.add(th)
