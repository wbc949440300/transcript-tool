"""保留策略：按条数/按天清理记录，媒体单独清理，归档永不删除。"""
import os
import time

from app import archive
from app.history import history_store, media_keep_days, sweep_old_media
from app.models import TaskDetail, TaskStatus


def _task(tid: str, title: str = "测试") -> TaskDetail:
    task = TaskDetail(taskId=tid, taskType="transcript", status=TaskStatus.SUCCESS,
                      title=title, textContent="正文")
    task.createTime = time.strftime("%Y-%m-%d %H:%M:%S")
    return task


def test_trim_by_count():
    history_store.clear()
    for i in range(3):
        history_store.save(_task(f"trim{i}"))
    removed = history_store.trim(keep=1)
    rows = history_store.list(limit=100)
    assert removed == 2
    assert len(rows) == 1


def test_trim_by_days():
    history_store.clear()
    history_store.save(_task("old1"))
    history_store._conn.execute(
        "UPDATE history SET created_at=datetime('now','localtime','-100 days') "
        "WHERE task_id='old1'")
    history_store._conn.commit()
    history_store.save(_task("new1"))

    removed = history_store.trim(keep=0, keep_days=30)
    rows = history_store.list(limit=100)
    assert removed == 1
    assert [r["task_id"] for r in rows] == ["new1"]


def test_trim_keeps_archive_files():
    history_store.clear()
    history_store.save(_task("keep1"))
    f = archive.archive_dir() / "长期存档.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("长期内容", encoding="utf-8")

    history_store.trim(keep=0, keep_days=1)   # 新记录不会被删
    assert f.exists()


def test_sweep_old_media_removes_only_media(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_TASKS_DIR", str(tmp_path))

    old_dir = tmp_path / "oldtask"
    old_dir.mkdir()
    (old_dir / "video.mp4").write_bytes(b"x")
    (old_dir / "note.txt").write_text("keep", encoding="utf-8")
    past = time.time() - 30 * 86400
    os.utime(old_dir, (past, past))

    fresh_dir = tmp_path / "freshtask"
    fresh_dir.mkdir()
    (fresh_dir / "video.mp4").write_bytes(b"x")

    removed = sweep_old_media(7)
    assert removed == 1
    assert not (old_dir / "video.mp4").exists()
    assert (old_dir / "note.txt").exists(), "非媒体文件不能删"
    assert (fresh_dir / "video.mp4").exists(), "未过期的媒体不能删"


def test_media_keep_days_default(monkeypatch):
    monkeypatch.delenv("TRANSCRIPT_MEDIA_KEEP_DAYS", raising=False)
    assert media_keep_days() == 14
    monkeypatch.setenv("TRANSCRIPT_MEDIA_KEEP_DAYS", "0")
    assert media_keep_days() == 0
