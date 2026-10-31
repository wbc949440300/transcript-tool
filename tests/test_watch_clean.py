"""视频号监听链路的两处优化：processed/ 占用与清理 + 转写完成通知。"""
import json
import os
import time

import pytest

from app import alerts, watch_folder


@pytest.fixture
def watch_dir(tmp_path, monkeypatch):
    """把"记住的监听目录"指到临时目录，并放 2 个待处理 + 2 个已处理文件。"""
    d = tmp_path / "watch"
    (d / "processed").mkdir(parents=True)
    (d / "a.mp4").write_bytes(b"x" * 100)
    (d / "b.mp4").write_bytes(b"y" * 50)
    (d / "processed" / "old.mp4").write_bytes(b"z" * 200)
    (d / "processed" / "new.mp4").write_bytes(b"w" * 10)
    (d / "processed" / "note.txt").write_bytes(b"not-media")
    past = time.time() - 40 * 86400
    os.utime(d / "processed" / "old.mp4", (past, past))
    f = tmp_path / "remember.json"
    f.write_text(json.dumps({"dir": str(d)}), encoding="utf-8")
    monkeypatch.setenv("TRANSCRIPT_WATCH_REMEMBER_FILE", str(f))
    return d


def test_dir_usage_counts_both(watch_dir):
    u = watch_folder.dir_usage()
    assert u["inboxFiles"] == 2 and u["inboxBytes"] == 150
    assert u["processedFiles"] == 3 and u["processedBytes"] == 200 + 10 + len(b"not-media")


def test_status_includes_usage(watch_dir):
    st = watch_folder.status()
    assert st["usage"]["inboxFiles"] == 2


def test_sweep_is_off_by_default(watch_dir, monkeypatch):
    monkeypatch.delenv("TRANSCRIPT_WATCH_KEEP_DAYS", raising=False)
    assert watch_folder.sweep_processed() == 0
    assert (watch_dir / "processed" / "old.mp4").exists()      # 默认不替用户删素材


def test_sweep_removes_only_old_media(watch_dir, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_WATCH_KEEP_DAYS", "30")
    assert watch_folder.sweep_processed() == 1
    assert not (watch_dir / "processed" / "old.mp4").exists()
    assert (watch_dir / "processed" / "new.mp4").exists()      # 新的不动
    assert (watch_dir / "processed" / "note.txt").exists()     # 非媒体不动


def test_clean_all_and_partial(watch_dir):
    res = watch_folder.clean_processed(30)                     # 只删 40 天前那个
    assert res["removed"] == 1 and res["freedBytes"] == 200
    res2 = watch_folder.clean_processed(0)                     # 手动全清
    assert res2["removed"] == 2 and res2["freedBytes"] == 10 + len(b"not-media")
    assert not any((watch_dir / "processed").iterdir())
    assert (watch_dir / "a.mp4").exists()                      # 待处理目录不动


def test_clean_without_watch_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_WATCH_REMEMBER_FILE", str(tmp_path / "none.json"))
    assert watch_folder.clean_processed(0)["removed"] == 0


# ---------- 完成通知 ----------

def test_task_done_notify_switch_off(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_WATCH_NOTIFY", "0")
    assert alerts.notify_task_done("标题", True, chars=100) is False


def test_task_done_notify_ignores_quiet_hours_by_default(monkeypatch):
    """完成通知默认**不受**静默时段限制（"你刚下的视频跑完了"该告诉你）。"""
    calls = []
    monkeypatch.setenv("TRANSCRIPT_WATCH_NOTIFY", "1")
    monkeypatch.delenv("TRANSCRIPT_WATCH_NOTIFY_QUIET", raising=False)
    monkeypatch.setattr(alerts, "_quiet_now", lambda now=None: True)
    monkeypatch.setattr(alerts.subprocess, "Popen",
                        lambda cmd, **kw: calls.append(cmd) or None)
    assert alerts.notify_task_done("夜里的视频", True, chars=10) is True
    assert calls


def test_task_done_notify_can_opt_into_quiet_hours(monkeypatch):
    """想让它也守静默时段：TRANSCRIPT_WATCH_NOTIFY_QUIET=1。"""
    monkeypatch.setenv("TRANSCRIPT_WATCH_NOTIFY", "1")
    monkeypatch.setenv("TRANSCRIPT_WATCH_NOTIFY_QUIET", "1")
    monkeypatch.setattr(alerts, "_quiet_now", lambda now=None: True)
    assert alerts.notify_task_done("夜里的视频", True, chars=10) is False


def test_task_done_notify_pops_balloon(monkeypatch):
    calls = []
    monkeypatch.setenv("TRANSCRIPT_WATCH_NOTIFY", "1")
    monkeypatch.setattr(alerts, "_quiet_now", lambda now=None: False)
    monkeypatch.setattr(alerts.subprocess, "Popen",
                        lambda cmd, **kw: calls.append(cmd) or None)
    assert alerts.notify_task_done("某条视频", True, chars=1234) is True
    assert calls and "ShowBalloonTip" in calls[0][-1]
    assert "播放" not in calls[0][-1]          # 不该混入其它内容
    assert "1234" in calls[0][-1]


def test_task_done_notify_failure_message(monkeypatch):
    calls = []
    monkeypatch.setenv("TRANSCRIPT_WATCH_NOTIFY", "1")
    monkeypatch.setattr(alerts, "_quiet_now", lambda now=None: False)
    monkeypatch.setattr(alerts.subprocess, "Popen",
                        lambda cmd, **kw: calls.append(cmd) or None)
    assert alerts.notify_task_done("某条视频", False, note="没有可识别的音视频流") is True
    assert "提取失败" in calls[0][-1] and "音视频流" in calls[0][-1]