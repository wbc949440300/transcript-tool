"""刷新直链：只重新取一次详情、不重跑转写，并把新直链写回历史。"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.history import history_store
from app.models import TaskDetail, TaskStatus

TOKEN = "test-token"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, base_url="http://127.0.0.1:8000")


def _auth() -> dict:
    return {"X-API-Token": TOKEN}


def _save_task(tid: str = "refresh1", url: str = "https://www.douyin.com/video/123"):
    history_store.clear()
    task = TaskDetail(taskId=tid, taskType="transcript", status=TaskStatus.SUCCESS,
                      title="测试", platform="抖音", webpageUrl=url, textContent="正文")
    task.createTime = "2026-10-31 10:00:00"
    task.videoUrlList = ["https://old/1.mp4"]
    history_store.save(task)
    return task


def _fake_douyin_fetch(url, **kwargs):
    return {
        "title": "新标题",
        "cover": "https://new/cover.jpg",
        "duration": 60000,
        "video_url": "https://new/default.mp4",
        "video_variants": [
            {"url": "https://new/low.mp4", "bitrate": 480},
            {"url": "https://new/high.mp4", "bitrate": 1080},
        ],
    }


def _use_fake_douyin(monkeypatch):
    fake = dataclasses.replace(platforms.adapter_for("https://www.douyin.com/x"),
                               fetch=_fake_douyin_fetch)
    monkeypatch.setattr(
        platforms, "ADAPTERS",
        tuple(fake if a.key == "douyin" else a for a in platforms.ADAPTERS))


def test_refresh_douyin_writes_new_urls(monkeypatch, client):
    _save_task()
    _use_fake_douyin(monkeypatch)

    r = client.post("/prod-api/transcript/refreshMedia",
                    json={"taskId": "refresh1"}, headers=_auth())
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # 预览取最高码率档
    assert data["videoUrlList"][0] == "https://new/high.mp4"
    assert "https://new/low.mp4" in data["videoUrlList"]
    assert data["duration"] == 60.0

    row = history_store.get("refresh1")
    assert "https://new/high.mp4" in (row["extra"] or "")
    assert row["cover"] == "https://new/cover.jpg"


def test_refresh_rejects_unsupported_platform(client):
    _save_task(url="https://www.bilibili.com/video/BV1xx411c7mD")
    r = client.post("/prod-api/transcript/refreshMedia",
                    json={"taskId": "refresh1"}, headers=_auth())
    assert r.status_code == 400
    assert "只有抖音/快手" in r.json()["msg"]


def test_refresh_missing_task(client):
    history_store.clear()
    r = client.post("/prod-api/transcript/refreshMedia",
                    json={"taskId": "nope"}, headers=_auth())
    assert r.status_code == 404


def test_refresh_requires_auth(client):
    r = client.post("/prod-api/transcript/refreshMedia", json={"taskId": "refresh1"})
    assert r.status_code == 401
