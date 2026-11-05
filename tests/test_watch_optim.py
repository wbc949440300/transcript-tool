"""视频号链路三件优化：零拷贝暂存、导入失败可见、下载器空闲自动停。"""
import os
import time

import pytest

from app import alerts, watch_folder


# ---------- #3 零拷贝暂存 ----------

def test_stage_file_prefers_hardlink(tmp_path):
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x" * 4096)
    dst = tmp_path / "staged.mp4"
    watch_folder._stage_file(src, dst)
    assert dst.read_bytes() == src.read_bytes()
    # 硬链接：同一份数据（inode 相同），没有额外占用
    assert os.stat(src).st_ino == os.stat(dst).st_ino


def test_stage_file_falls_back_to_copy(tmp_path, monkeypatch):
    src = tmp_path / "big.mp4"
    src.write_bytes(b"y" * 2048)
    dst = tmp_path / "staged2.mp4"

    def boom(*a, **k):
        raise OSError(18, "cross-device link")      # 模拟跨分区

    monkeypatch.setattr(watch_folder.os, "link", boom)
    watch_folder._stage_file(src, dst)
    assert dst.read_bytes() == src.read_bytes()
    assert os.stat(src).st_ino != os.stat(dst).st_ino


# ---------- #6 导入失败可见 ----------

@pytest.fixture
def watch_dir(tmp_path, monkeypatch):
    d = tmp_path / "watch"
    d.mkdir()
    import json

    f = tmp_path / "remember.json"
    f.write_text(json.dumps({"dir": str(d)}), encoding="utf-8")
    monkeypatch.setenv("TRANSCRIPT_WATCH_REMEMBER_FILE", str(f))
    monkeypatch.chdir(tmp_path)
    return d


def test_record_fail_visible_in_status(watch_dir):
    watch_folder._record_fail("某视频.mp4", "磁盘空间不足")
    st = watch_folder.status()
    assert st["failed"] and st["failed"][0]["file"] == "某视频.mp4"
    assert "磁盘空间不足" in st["failed"][0]["error"]


def test_enqueue_failure_is_recorded(watch_dir, monkeypatch):
    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(watch_folder, "_stage_file", boom)
    src = watch_dir / "video.mp4"
    src.write_bytes(b"z" * 100)
    watch_folder._enqueue(src, "tiny", move_done=True)
    st = watch_folder.status()
    assert st["failed"] and st["failed"][0]["file"] == "video.mp4"


def test_watch_fail_alert_needs_two_within_30min(watch_dir, monkeypatch):
    monkeypatch.setattr(alerts, "_fail_rate_alert", lambda: [])
    monkeypatch.setattr(alerts, "_queue_alert", lambda: [])
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with watch_folder._lock:
        watch_folder._state["failed"] = [{"file": "a.mp4", "error": "x", "at": now}]
    assert "watch_fail" not in [i["code"] for i in alerts._watch_alert()]
    with watch_folder._lock:
        watch_folder._state["failed"] = [{"file": "a.mp4", "error": "x", "at": now},
                                         {"file": "b.mp4", "error": "y", "at": now}]
    hits = alerts._watch_alert()
    assert hits and hits[0]["code"] == "watch_fail" and "2 次" in hits[0]["message"]
    with watch_folder._lock:
        watch_folder._state["failed"] = []


def test_old_failures_do_not_alert(watch_dir):
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 7200))
    with watch_folder._lock:
        watch_folder._state["failed"] = [{"file": "a", "error": "x", "at": old},
                                         {"file": "b", "error": "y", "at": old}]
    assert alerts._watch_alert() == []
    with watch_folder._lock:
        watch_folder._state["failed"] = []


# ---------- 默认监听目录（2026-11-05 下载器移除后新增） ----------

def test_default_dir_uses_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSCRIPT_WATCH_DIR", str(tmp_path / "w"))
    assert watch_folder.default_dir() == tmp_path / "w"
    d = watch_folder.default_dir(create=True)
    assert d.is_dir()


def test_default_dir_falls_back_to_wx_downloads(monkeypatch):
    monkeypatch.delenv("TRANSCRIPT_WATCH_DIR", raising=False)
    assert watch_folder.default_dir().name == "wx_downloads"
