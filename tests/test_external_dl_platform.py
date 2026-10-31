"""外部下载器（视频号）在非 Windows 上必须优雅降级 —— 云上跑不能因此崩服务。

真实场景：本机 Windows 负责抓视频（微信 + MITM 下载器），
云端 Linux 只负责转写 —— 所以云端会 import 到 external_dl，但那些
PowerShell/taskkill/winreg 路径一次都不能被执行。
"""
import pytest

from app import external_dl as ed


@pytest.fixture
def non_windows(monkeypatch):
    monkeypatch.setattr(ed, "IS_WINDOWS", False)


def test_probe_degrades_gracefully(non_windows):
    out = ed.probe()
    assert out["online"] is False
    assert out["platform_supported"] is False
    assert "仅支持 Windows" in (out["error"] or "")


def test_install_returns_error_not_exception(non_windows):
    out = ed.install()
    assert out["installed"] is False and out["platform_supported"] is False


def test_start_returns_error_not_exception(non_windows):
    out = ed.start()
    assert out["started"] is False and out["platform_supported"] is False


def test_stop_is_a_noop_success(non_windows):
    """云端本来就没有这个进程，"已停止"才不会让退出钩子误报。"""
    out = ed.stop()
    assert out["stopped"] is True and out["already"] is True


def test_proc_running_is_false(non_windows):
    assert ed._proc_running() is False


def test_windows_path_still_works(monkeypatch):
    """守卫不能误伤 Windows：IS_WINDOWS=True 时不该走降级分支。"""
    monkeypatch.setattr(ed, "IS_WINDOWS", True)
    out = ed.probe()
    assert out["platform_supported"] is True