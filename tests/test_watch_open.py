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


def test_douyin_note_url_short_circuits_without_browser(watch_dir):
    """/note/ 链接必须在开浏览器之前就返回提示（省一次页面访问 + 约 20 秒）。"""
    from app.douyin import fetch_douyin

    with pytest.raises(RuntimeError) as ei:
        fetch_douyin("https://www.douyin.com/note/7360323126909095219")
    msg = str(ei.value)
    assert "图集" in msg and "监听目录" in msg


def test_douyin_image_message_without_watch_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_WATCH_REMEMBER_FILE", str(tmp_path / "none.json"))
    from app.douyin import _image_post_message

    msg = _image_post_message()
    assert "图集" in msg
    # 没有监听目录时退回"上传 OCR"的指引，而不是给一个不存在的路径
    assert "图片文案提取" in msg