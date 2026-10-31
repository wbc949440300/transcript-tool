"""看门狗（⑥）：探活判定 + "活着就不动它" + dry-run 不乱启动。"""
import importlib.util
import pathlib
import socket

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "watchdog", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "watchdog.py")
wd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wd)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                     # 立刻关掉 → 这个端口基本不会有人监听
    return port


@pytest.fixture
def listening_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    yield srv.getsockname()[1]
    srv.close()


def test_alive_detection(listening_port):
    assert wd._alive(listening_port) is True
    assert wd._alive(_closed_port()) is False


def test_alive_does_not_start_anything(listening_port):
    called = []
    status = wd.check_once(listening_port, starter=lambda port: called.append(port) or True)
    assert status == "alive" and called == []


def test_dead_triggers_start():
    called = []
    status = wd.check_once(_closed_port(), starter=lambda port: called.append(port) or True)
    assert status == "restarted" and called


def test_start_failure_reported():
    assert wd.check_once(_closed_port(), starter=lambda port: False) == "start_failed"


def test_dry_run_never_starts():
    called = []
    status = wd.check_once(_closed_port(), dry_run=True,
                           starter=lambda port: called.append(port) or True)
    assert status == "dry_run" and called == []