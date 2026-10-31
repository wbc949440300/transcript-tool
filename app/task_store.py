"""任务存储 + 自动历史落库。

内存中管理运行中的任务；任务达到 SUCCESS/FAILURE 时自动写入 SQLite 历史。
"""
from __future__ import annotations

import threading
from typing import Optional

from .history import history_keep, history_keep_days, history_store
from .models import TaskDetail, TaskStatus
from .utils import now_str


class TaskStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskDetail] = {}
        self._batches: dict[str, dict] = {}

    def create(self, task: TaskDetail) -> None:
        with self._lock:
            task.createTime = now_str()
            self._tasks[task.taskId] = task

    def get(self, task_id: str) -> Optional[TaskDetail]:
        with self._lock:
            return self._tasks.get(task_id)

    def update_status(self, task_id: str, status: TaskStatus, **fields) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = status
            for key, val in fields.items():
                setattr(t, key, val)
            # 任务终结时自动写入历史（SUCCESS / FAILURE）
            if status in (TaskStatus.SUCCESS, TaskStatus.FAILURE):
                try:
                    history_store.save(t)
                    history_store.trim(history_keep(), history_keep_days())
                    if status == TaskStatus.SUCCESS:
                        from .archive import save_text_file
                        save_text_file(t)  # 文案自动存入 文案输出/ 文件夹
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 批量任务 ----------
    def create_batch(self, batch_id: str, items: list[TaskDetail]) -> None:
        with self._lock:
            for it in items:
                it.createTime = now_str()
                self._tasks[it.taskId] = it
            self._batches[batch_id] = {"items": items}

    def get_batch(self, batch_id: str) -> Optional[list[TaskDetail]]:
        with self._lock:
            b = self._batches.get(batch_id)
            return list(b["items"]) if b else None


store = TaskStore()
