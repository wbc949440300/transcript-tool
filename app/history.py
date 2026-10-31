"""历史记录：SQLite 持久化已完成的任务。

任务在达成 SUCCESS / FAILURE 时写入 SQLite，重启后仍可查看。
提供按时间分页、按关键词搜索、删除、详情等能力。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional
from pathlib import Path

logger = logging.getLogger("history")

_DB_NAME = os.getenv("TRANSCRIPT_DB", "transcript_history.db")

# 历史记录保留条数：只留最近 N 条（含其本地预览视频等任务文件），更早的自动删除。
# 0 = 不限制。默认 20，防止 tasks/<id>/video.mp4 长期堆积占满磁盘。
def history_keep() -> int:
    try:
        return int(os.getenv("TRANSCRIPT_HISTORY_KEEP", "20"))
    except ValueError:
        return 20


def history_keep_days() -> int:
    """按天数保留（P2）：>0 时与条数上限同时生效；0 = 不按天删。

    文案归档（文案输出/）不受这里影响，它才是长期存档。
    """
    try:
        return max(0, int(os.getenv("TRANSCRIPT_HISTORY_KEEP_DAYS", "0")))
    except ValueError:
        return 0


def media_keep_days() -> int:
    """任务目录里的媒体文件保留天数（默认 14，0 = 不清理）。"""
    try:
        return max(0, int(os.getenv("TRANSCRIPT_MEDIA_KEEP_DAYS", "14")))
    except ValueError:
        return 14


_MEDIA_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv",
                   ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma")


def _cleanup_task_files(task_id: str) -> None:
    """删除任务工作目录（tasks/<id>/，含本地预览视频 video.mp4 等）。尽力而为不抛异常。"""
    import shutil

    if not task_id:
        return
    tasks_dir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks"))
    p = tasks_dir / task_id
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理任务文件失败 %s: %s", task_id, exc)


def sweep_old_media(days: Optional[int] = None) -> int:
    """只清理任务目录里的过期媒体文件，**保留历史记录与文案**（P2）。

    与 trim() 的区别：trim 删"记录 + 整个任务目录"；这里只删大文件，
    记录还在（预览会 404，但文案/导出/归档完全不受影响）。
    """
    days = media_keep_days() if days is None else int(days)
    if days <= 0:
        return 0
    tasks_dir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks"))
    if not tasks_dir.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for p in tasks_dir.iterdir():
        if not p.is_dir():
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            for f in p.iterdir():
                if f.is_file() and f.suffix.lower() in _MEDIA_SUFFIXES:
                    f.unlink(missing_ok=True)
                    removed += 1
            if not any(p.iterdir()):
                p.rmdir()
        except Exception:  # noqa: BLE001
            pass
    if removed:
        logger.info("清理过期媒体文件 %d 个（文案与历史记录保留）", removed)
    return removed


def sweep_orphan_task_files() -> int:
    """清理 tasks/ 下已无历史记录的孤儿目录（旧版删除不清理文件/任务崩溃遗留）。

    只在启动时调用：此刻内存中没有运行中任务，凡是不在历史里的目录都可删。
    """
    import shutil

    tasks_dir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks"))
    if not tasks_dir.is_dir():
        return 0
    try:
        c = _conn()
        ids = {r["task_id"] for r in c.execute("SELECT task_id FROM history").fetchall()}
        c.close()
    except Exception:  # noqa: BLE001
        return 0
    removed = 0
    for p in tasks_dir.iterdir():
        if not p.is_dir() or p.name in ids:
            continue
        try:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        except Exception:  # noqa: BLE001
            pass
    if removed:
        logger.info("启动清理：删除 %d 个无记录的孤儿任务目录", removed)
    return removed


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            task_id      TEXT PRIMARY KEY,
            task_type    TEXT,
            status       TEXT,
            title        TEXT,
            content      TEXT,
            text_content TEXT,
            cover        TEXT,
            platform     TEXT,
            duration     REAL,
            uploader     TEXT,
            webpage_url  TEXT,
            error_msg    TEXT,
            extra        TEXT,
            created_at   TEXT,
            updated_at   TEXT
        )
    """)
    conn.commit()
    return conn


class HistoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = _conn()

    def save(self, task) -> None:
        """保存/更新一条历史记录（task 为 TaskDetail 或 TaskNode）。"""
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO history
                       (task_id, task_type, status, title, content, text_content,
                        cover, platform, duration, uploader, webpage_url,
                        error_msg, extra, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                    (
                        task.taskId,
                        getattr(task, "taskType", "transcript"),
                        task.status.value if hasattr(task.status, "value") else str(task.status),
                        task.title,
                        task.content,
                        task.textContent,
                        task.cover,
                        task.platform,
                        task.duration,
                        task.uploader,
                        task.webpageUrl,
                        task.errorMessage,
                        json.dumps({
                            "frameText": getattr(task, "frameText", None),
                            "frameBlocks": getattr(task, "frameBlocks", None),
                            "videoUrlList": getattr(task, "videoUrlList", None),
                            "audioUrl": getattr(task, "audioUrl", None),
                            "sourceText": getattr(task, "sourceText", None),
                            "batchId": getattr(task, "batchId", None),
                            "index": getattr(task, "index", None),
                            "url": getattr(task, "url", None),
                            "segments": getattr(task, "segments", None),
                        }, ensure_ascii=False),
                        getattr(task, "createTime", None),
                    ),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("历史记录保存失败: %s", exc)

    def list(self, keyword: Optional[str] = None, status: Optional[str] = None,
             limit: int = 30, offset: int = 0) -> list[dict]:
        with self._lock:
            q = "SELECT * FROM history WHERE 1=1"
            args = []
            if keyword:
                q += " AND (title LIKE ? OR text_content LIKE ? OR webpage_url LIKE ? OR platform LIKE ?)"
                like = f"%{keyword}%"
                args += [like, like, like, like]
            if status:
                q += " AND status = ?"
                args.append(status)
            q += " ORDER BY CAST(created_at AS TEXT) DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            try:
                rows = self._conn.execute(q, args).fetchall()
                return [dict(r) for r in rows]
            except Exception as exc:  # noqa: BLE001
                logger.error("历史记录查询失败: %s", exc)
                return []

    def count(self, keyword: Optional[str] = None, status: Optional[str] = None) -> int:
        with self._lock:
            q = "SELECT COUNT(*) AS n FROM history WHERE 1=1"
            args = []
            if keyword:
                q += " AND (title LIKE ? OR text_content LIKE ? OR webpage_url LIKE ? OR platform LIKE ?)"
                like = f"%{keyword}%"
                args += [like, like, like, like]
            if status:
                q += " AND status = ?"
                args.append(status)
            try:
                return self._conn.execute(q, args).fetchone()["n"]
            except Exception:
                return 0

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM history WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def update_fields(self, task_id: str, patch: dict) -> bool:
        """更新若干列（白名单），用于"刷新直链"时同步封面/时长等易失字段。"""
        allowed = ("title", "cover", "duration", "uploader", "content", "error_msg")
        cols = {k: v for k, v in (patch or {}).items()
                if k in allowed and v is not None}
        if not cols:
            return False
        with self._lock:
            try:
                sets = ", ".join(f"{k}=?" for k in cols)
                self._conn.execute(
                    f"UPDATE history SET {sets}, updated_at=datetime('now','localtime') "
                    "WHERE task_id=?",
                    (*cols.values(), task_id))
                self._conn.commit()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("历史字段更新失败: %s", exc)
                return False

    def update_extra(self, task_id: str, patch: dict) -> bool:
        """合并写入 extra JSON（用于保存 AI 纠错结果等附加数据）。"""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT extra FROM history WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    return False
                try:
                    extra = json.loads(row["extra"] or "{}")
                except Exception:
                    extra = {}
                extra.update(patch or {})
                self._conn.execute(
                    "UPDATE history SET extra=?, updated_at=datetime('now','localtime') WHERE task_id=?",
                    (json.dumps(extra, ensure_ascii=False), task_id))
                self._conn.commit()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("历史 extra 更新失败: %s", exc)
                return False

    def delete(self, task_id: str) -> bool:
        ok = False
        with self._lock:
            try:
                c = self._conn.execute("DELETE FROM history WHERE task_id=?", (task_id,))
                self._conn.commit()
                ok = c.rowcount > 0
            except Exception:
                ok = False
        if ok:
            _cleanup_task_files(task_id)  # 记录删了，磁盘上的预览视频等也一并清掉
        return ok

    def clear(self) -> int:
        with self._lock:
            try:
                ids = [r["task_id"] for r in
                       self._conn.execute("SELECT task_id FROM history").fetchall()]
                c = self._conn.execute("DELETE FROM history")
                self._conn.commit()
                n = c.rowcount
            except Exception:
                return 0
        for tid in ids:
            _cleanup_task_files(tid)
        return n

    def trim(self, keep: int, keep_days: int = 0) -> int:
        """按保留策略清理记录（P2）。

        - keep > 0：只保留最近 keep 条
        - keep_days > 0：再删掉早于 N 天的记录（与条数上限同时生效）
        删记录会连带删除 tasks/<id>/；文案归档（文案输出/）不受影响。
        """
        if keep <= 0 and keep_days <= 0:
            return 0
        ids: list[str] = []
        with self._lock:
            try:
                if keep > 0:
                    rows = self._conn.execute(
                        "SELECT task_id FROM history "
                        "ORDER BY CAST(created_at AS TEXT) DESC, rowid DESC LIMIT -1 OFFSET ?",
                        (keep,)).fetchall()
                    ids.extend(r["task_id"] for r in rows)
                if keep_days > 0:
                    rows = self._conn.execute(
                        "SELECT task_id FROM history WHERE created_at < datetime('now','localtime', ?)",
                        (f"-{int(keep_days)} days",)).fetchall()
                    for r in rows:
                        if r["task_id"] not in ids:
                            ids.append(r["task_id"])
                if ids:
                    self._conn.executemany(
                        "DELETE FROM history WHERE task_id=?", [(i,) for i in ids])
                    self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("历史记录清理失败: %s", exc)
                return 0
        for tid in ids:
            _cleanup_task_files(tid)
        if ids:
            logger.info("历史记录超出保留策略，已清理 %d 条（含任务文件）", len(ids))
        return len(ids)


history_store = HistoryStore()
