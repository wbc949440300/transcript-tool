"""tmp_uploads 启动清理：只删够旧的副本，别误伤刚上传/排队中的任务。"""
import os
import time

from app.history import sweep_tmp_uploads


def _age(path, hours):
    past = time.time() - hours * 3600
    os.utime(path, (past, past))


def test_sweep_removes_only_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tmp = tmp_path / "tmp_uploads"
    tmp.mkdir()
    stale = tmp / "中断任务留下的.wav"
    stale.write_bytes(b"x")
    _age(stale, 48)
    fresh = tmp / "刚上传还在排队的.wav"
    fresh.write_bytes(b"x")

    removed = sweep_tmp_uploads()
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists(), "新上传 / 排队中的副本不能删"


def test_sweep_keeps_subdirs_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tmp = tmp_path / "tmp_uploads"
    tmp.mkdir()
    (tmp / "sub").mkdir()
    assert sweep_tmp_uploads() == 0
    assert (tmp / "sub").is_dir()


def test_sweep_without_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sweep_tmp_uploads() == 0


def test_keep_hours_default_is_24(tmp_path, monkeypatch):
    """默认阈值 24 小时：2 小时前的不算陈旧。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRANSCRIPT_TMP_KEEP_HOURS", raising=False)
    tmp = tmp_path / "tmp_uploads"
    tmp.mkdir()
    f = tmp / "a.wav"
    f.write_bytes(b"x")
    _age(f, 2)
    assert sweep_tmp_uploads() == 0
    assert f.exists()


def test_keep_hours_env_knob(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRANSCRIPT_TMP_KEEP_HOURS", "1")
    tmp = tmp_path / "tmp_uploads"
    tmp.mkdir()
    f = tmp / "a.wav"
    f.write_bytes(b"x")
    _age(f, 2)
    assert sweep_tmp_uploads() == 1
    assert not f.exists()


def test_explicit_threshold_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRANSCRIPT_TMP_KEEP_HOURS", "999")
    tmp = tmp_path / "tmp_uploads"
    tmp.mkdir()
    f = tmp / "a.wav"
    f.write_bytes(b"x")
    _age(f, 2)
    assert sweep_tmp_uploads(max_age_hours=1) == 1