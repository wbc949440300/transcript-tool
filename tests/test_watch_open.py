"""打开监听目录 + 抖音图集提示：图集类链接要引导到"丢进监听目录自动成一条文案"。"""
import json

import pytest
from fastapi.testclient import TestClient

from app import watch_folder

TOKEN = "test-token"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, base_url="http://127.0.0.1:8000")


@pytest.fixture
def watch_dir(tmp_path, monkeypatch):
    """把"记住的监听目录"指到临时目录。"""
    d = tmp_path / "watch"
    d.mkdir()
    f = tmp_path / "watch_remember.json"
    f.write_text(json.dumps({"dir": str(d)}), encoding="utf-8")
    monkeypatch.setenv("TRANSCRIPT_WATCH_REMEMBER_FILE", str(f))
    return d


def test_open_watch_dir(client, watch_dir, monkeypatch):
    opened = {}
    monkeypatch.setattr("os.startfile", lambda p: opened.setdefault("path", p), raising=False)
    r = client.post("/prod-api/watch/open", headers={"X-API-Token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["dir"] == str(watch_dir)
    assert opened.get("path") == str(watch_dir)


def test_open_watch_dir_requires_auth(client, watch_dir):
    assert client.post("/prod-api/watch/open").status_code == 401


def test_current_dir_reads_remembered(watch_dir):
    assert watch_folder.current_dir() == watch_dir


def test_current_dir_create_makes_dir(tmp_path, monkeypatch):
    target = tmp_path / "notyet"
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"dir": str(target)}), encoding="utf-8")
    monkeypatch.setenv("TRANSCRIPT_WATCH_REMEMBER_FILE", str(f))
    assert watch_folder.current_dir(create=True) == target
    assert target.is_dir()


def test_douyin_image_message_points_to_watch_dir(watch_dir):
    from app.douyin import _image_post_message

    msg = _image_post_message()
    assert "监听目录" in msg
    assert str(watch_dir) in msg


def test_douyin_note_url_is_routed_through_video_path(watch_dir):
    """图集 /note/<id> 要改写成 /video/<id> 再渲染。

    实测（2026-11-04）：直接开 /note/ 页面不请求详情接口；开 /video/<id> 时抖音
    会跳回 /note/<id> 并正常请求 aweme/v1/web/aweme/detail，返回 desc + images[]。
    """
    from app.douyin import _extract_video_id, normalize_url

    nid = "7360323126909095219"
    note = f"https://www.douyin.com/note/{nid}"
    assert normalize_url(note) == f"https://www.douyin.com/video/{nid}"
    assert _extract_video_id(note) == nid


def test_douyin_image_message_without_watch_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_WATCH_REMEMBER_FILE", str(tmp_path / "none.json"))
    from app.douyin import _image_post_message

    msg = _image_post_message()
    assert "图集" in msg
    # 没有监听目录时退回"上传 OCR"的指引，而不是给一个不存在的路径
    assert "图片文案提取" in msg