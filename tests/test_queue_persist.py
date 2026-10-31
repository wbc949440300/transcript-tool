"""持久队列（③）：排队/运行中的任务落盘，服务重启后重新入队。"""
import json
import time

import pytest

from app import scheduler, utils
from app.models import TaskDetail, TaskStatus
from app.task_store import store

DY = "https://www.douyin.com/video/7612933474718141747"


@pytest.fixture(autouse=True)
def _clean_queue_file():
    f = scheduler._queue_file()
    f.unlink(missing_ok=True)
    yield
    f.unlink(missing_ok=True)


def _mk(tid: str) -> TaskDetail:
    task = TaskDetail(taskId=tid, taskType="transcript", status=TaskStatus.PENDING,
                      webpageUrl=DY, platform="抖音")
    store.create(task)
    return task


def _jobs_in_file() -> list:
    return scheduler._read_state().get("jobs") or []


def test_submit_writes_queue_file(monkeypatch):
    """入队即落盘：服务在这里被 kill 也不丢。"""
    started = []

    def fake_run(task, url, want_audio, model, ocr_frames=False):
        started.append(task.taskId)
        time.sleep(0.4)                       # 停在"运行中"，方便检查文件
        store.update_status(task.taskId, TaskStatus.SUCCESS, textContent="ok")

    monkeypatch.setattr("app.extractor._run_task", fake_run)
    task = _mk("persist_1")
    assert scheduler.submit(task, DY, False, "tiny")
    jobs = _jobs_in_file()
    assert any(j["task"]["taskId"] == "persist_1" for j in jobs)

    for _ in range(40):                       # 等它跑完 → 应从文件里移除
        if not any(j["task"]["taskId"] == "persist_1" for j in _jobs_in_file()):
            break
        time.sleep(0.1)
    assert not any(j["task"]["taskId"] == "persist_1" for j in _jobs_in_file())


def test_restore_reenqueues_unfinished_jobs(monkeypatch):
    """重启恢复：文件里的任务被重新入队，并真的被跑起来。"""
    ran = []

    def fake_run(task, url, want_audio, model, ocr_frames=False):
        ran.append(task.taskId)
        store.update_status(task.taskId, TaskStatus.SUCCESS, textContent="ok")

    monkeypatch.setattr("app.extractor._run_task", fake_run)
    task = _mk("persist_restore_1")
    payload = {
        "jobs": [{
            "task": task.model_dump(mode="json"),
            "url": DY, "want_audio": False, "model": "tiny",
            "ocr_frames": False, "platform": "douyin", "state": "running",
        }],
        "saved_at": "2026-10-31 00:00:00",
    }
    scheduler._queue_file().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert scheduler.restore() == 1
    assert store.get("persist_restore_1") is not None

    for _ in range(50):                 # 重新入队后应被 worker 跑起来
        if ran:
            break
        time.sleep(0.1)
    assert ran == ["persist_restore_1"]


def test_restore_ignores_broken_entries():
    scheduler._queue_file().write_text(
        json.dumps({"jobs": [{"task": None, "url": DY}, {"url": "x"}]}), encoding="utf-8")
    assert scheduler.restore() == 0


def test_restore_no_file():
    assert scheduler.restore() == 0