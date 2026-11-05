"""进行中任务找回（2026-11-05）：页面关掉重开后能续上进度，而不是只能翻历史。

前端原来把进行中任务存在 sessionStorage（仅本标签页），关掉标签页就丢；
这里测服务端兜底接口 /transcript/active：只返回未结束的任务、批量按 batchId 汇总。
"""
import pytest
from fastapi.testclient import TestClient

from app.models import TaskDetail, TaskStatus
from app.task_store import store

TOKEN = "test-token"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, base_url="http://127.0.0.1:8000")


@pytest.fixture(autouse=True)
def _clean_store():
    """store 是全局内存表：每个用例前后清干净，避免互相看到彼此的任务。"""
    with store._lock:
        store._tasks.clear()
        store._batches.clear()
    yield
    with store._lock:
        store._tasks.clear()
        store._batches.clear()


def _auth() -> dict:
    return {"X-API-Token": TOKEN}


def _mk(tid: str, status: TaskStatus, platform="抖音", task_type="transcript", batch_id=None):
    t = TaskDetail(taskId=tid, taskType=task_type, status=status, platform=platform,
                   webpageUrl="https://x/1", title=tid, batchId=batch_id)
    store.create(t)
    return t


def test_active_lists_only_unfinished(client):
    _mk("act_run", TaskStatus.RUNNING)
    _mk("act_pending", TaskStatus.PENDING, platform="视频号", task_type="file_transcript")
    _mk("act_done", TaskStatus.SUCCESS)   # 已结束的不该再出现在"进行中"里

    j = client.get("/prod-api/transcript/active", headers=_auth()).json()
    ids = {t["taskId"] for t in j["data"]["tasks"]}

    assert {"act_run", "act_pending"} <= ids
    assert "act_done" not in ids
    pending = next(t for t in j["data"]["tasks"] if t["taskId"] == "act_pending")
    assert pending["platform"] == "视频号" and pending["taskType"] == "file_transcript"


def test_active_reports_running_batch_and_hides_children(client):
    items = [_mk("b1", TaskStatus.RUNNING, batch_id="batch_x"),
             _mk("b2", TaskStatus.PENDING, batch_id="batch_x")]
    store.create_batch("batch_x", items)

    j = client.get("/prod-api/transcript/active", headers=_auth()).json()
    data = j["data"]

    assert [b["batchId"] for b in data["batches"]] == ["batch_x"]
    assert data["batches"][0]["total"] == 2 and data["batches"][0]["done"] == 0
    # 子任务不单列：前端只用一条 batch 入口恢复整批进度
    assert not {t["taskId"] for t in data["tasks"]} & {"b1", "b2"}


def test_active_finished_batch_not_reported(client):
    items = [_mk("f1", TaskStatus.SUCCESS, batch_id="batch_done")]
    store.create_batch("batch_done", items)

    j = client.get("/prod-api/transcript/active", headers=_auth()).json()
    assert [b["batchId"] for b in j["data"]["batches"]] == []


def test_history_delete_also_discards_memory(client):
    """删除历史后，内存任务也要消失（否则结果页 URL 到重启前都还能打开）。"""
    from app.history import history_store

    t = _mk("del_mem", TaskStatus.SUCCESS)
    history_store.save(t)

    r = client.delete("/prod-api/history/delete", params={"taskId": "del_mem"}, headers=_auth())
    assert r.status_code == 200
    assert store.get("del_mem") is None
    assert client.get("/prod-api/transcript/queryTask",
                      params={"taskId": "del_mem"}, headers=_auth()).status_code == 404


def test_history_clear_keeps_running_task(client):
    """清空历史只清已结束的；正在跑的任务不能被打断。"""
    from app.history import history_store

    _mk("clear_run", TaskStatus.RUNNING)
    done = _mk("clear_done", TaskStatus.SUCCESS)
    history_store.save(done)

    assert client.delete("/prod-api/history/clear", headers=_auth()).status_code == 200
    assert store.get("clear_run") is not None
    assert store.get("clear_done") is None
