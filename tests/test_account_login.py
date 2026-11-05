"""账号登录（本机桌面）：状态读取 / 启动校验 / 防重复启动 / 验证登录态。

注意：这里**不打桩 subprocess 之外的任何东西**——被测的是"怎么读本地 cookie 目录、
怎么校验参数、怎么避免重复弹窗"这些真实逻辑；真正跑登录（弹浏览器）不在测试里做。
"""
import pathlib

import pytest

from app import account_login as al
from app.utils import COOKIE_DIR


@pytest.fixture(autouse=True)
def _clean_running():
    al._RUNNING.clear()
    yield
    al._RUNNING.clear()


@pytest.fixture
def account_cookie():
    """造一个账号池文件（cookies/平台/account_1.txt），用完删掉。"""
    d = COOKIE_DIR / "douyin"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "account_1.txt"
    f.write_text("# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tFAKE\n",
                 encoding="utf-8")
    yield f
    try:
        f.unlink(missing_ok=True)
        d.rmdir()
    except Exception:  # noqa: BLE001
        pass


def test_status_lists_default_and_accounts(account_cookie):
    st = al.accounts_status()
    assert set(st) == {"douyin", "kuaishou", "xiaohongshu", "bilibili", "yuanbao"}
    rows = st["douyin"]["accounts"]
    assert rows[0]["account"] == "（默认）"
    assert rows[0]["logged_in"] is True            # conftest 造的 cookies/douyin.txt
    assert any(r["account"] == "account_1" and r["logged_in"] for r in rows)
    assert st["douyin"]["logged_in_count"] >= 2
    assert st["kuaishou"]["note"]                  # 快手有"匿名即可"的提示
    assert "视频号" in st["yuanbao"]["note"]        # 元宝：说明它只服务视频号贴链接下载


def test_status_shows_profile_and_login_time(account_cookie):
    row = [r for r in al.accounts_status()["douyin"]["accounts"] if r["account"] == "account_1"][0]
    assert row["profile"].endswith("douyin_account_1")   # 实际是 _browser_profile_douyin_account_1    # 与抓取端同一命名规则
    assert "douyin/account_1.txt" in row["file"]
    assert row["days_ago"] == 0 and row["logged_at"]


def test_start_login_rejects_bad_input(monkeypatch):
    called = []
    monkeypatch.setattr(al.subprocess, "Popen", lambda *a, **k: called.append(a))
    assert al.start_login("youtube")["started"] is False          # 不在支持列表
    assert al.start_login("douyin", "我的小号")["started"] is False  # 标签格式不对
    assert called == []                                          # 一个都没启动


def test_start_login_spawns_with_account(monkeypatch):
    called = []
    monkeypatch.setattr(al.subprocess, "Popen", lambda args, **kw: called.append(args))
    res = al.start_login("douyin", "account_1")
    assert res["started"] is True and "--account" in called[0]
    assert res["profile"].endswith("douyin_account_1")
    assert "account_1.txt" in res["output"]


def test_start_login_does_not_open_twice(monkeypatch):
    called = []
    monkeypatch.setattr(al.subprocess, "Popen", lambda args, **kw: called.append(args))
    al.start_login("kuaishou")
    again = al.start_login("kuaishou")
    assert again.get("already") is True and len(called) == 1      # 不会弹两个窗口


def test_check_login_maps_result(monkeypatch):
    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, b""

    monkeypatch.setattr(al.subprocess, "run", lambda *a, **k: _R(0, b"login ok"))
    assert al.check_login("douyin")["ok"] is True

    monkeypatch.setattr(al.subprocess, "run", lambda *a, **k: _R(1, b"not logged in"))
    res = al.check_login("douyin")
    assert res["ok"] is False and "not logged in" in res["output"]


def test_account_cooldown_visible(monkeypatch):
    """账号冷却要能在状态里看到（否则用户不知道这个号被限了）。"""
    from app import utils

    f = str(COOKIE_DIR / "douyin.txt")
    utils.mark_account_cooldown(f, 600)
    row = [r for r in al.accounts_status()["douyin"]["accounts"] if r["account"] == "（默认）"][0]
    assert row["cooldown"] > 500
    utils._write_cooldown({k: v for k, v in utils._read_cooldown().items() if k != f})