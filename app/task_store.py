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

    def discard(self, task_id: str) -> None:
        """从内存表移除一个任务（删除历史记录时调用）。

        2026-11-05 修：此前只删历史库，内存里那份还在 —— 删完记录后结果页 URL
        仍能打开，直到服务重启才消失。顺带把它从所属批次的条目里摘掉。
        """
        with self._lock:
            self._tasks.pop(task_id, None)
            for b in self._batches.values():
                b["items"] = [it for it in b["items"] if it.taskId != task_id]

    def discard_finished(self) -> int:
        """清空历史时移除所有已结束任务（运行中的保留，别打断正在跑的任务）。"""
        with self._lock:
            done = {tid for tid, t in self._tasks.items()
                    if t.status in (TaskStatus.SUCCESS, TaskStatus.FAILURE)}
            for tid in done:
                self._tasks.pop(tid, None)
            for b in self._batches.values():
                b["items"] = [it for it in b["items"]
                              if it.status not in (TaskStatus.SUCCESS, TaskStatus.FAILURE)]
            return len(done)

    def list_active(self) -> list[TaskDetail]:
        """还没结束的任务（PENDING/RUNNING），给页面重开后「找回进行中界面」用。

        2026-11-05 加：进行中任务原来只存在浏览器的 sessionStorage 里，关掉标签页
        就丢了，重开页面看不到进度、只能去历史记录翻。服务端本来就有这份内存表，
        直接暴露出来，前端重开时据此续上轮询。
        """
        with self._lock:
            return [t for t in self._tasks.values()
                    if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)]

    def list_active_batches(self) -> list[str]:
        """还有未结束子任务的批量 id（批量入口重开后恢复进度用）。"""
        with self._lock:
            return [bid for bid, b in self._batches.items()
                    if any(it.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                           for it in b["items"])]

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
