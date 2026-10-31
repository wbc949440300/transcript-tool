"""重试策略：用假 YDL 数真实请求次数（不联网）。

规则（P0/P1）：
- 风控(risk) → 只发 1 次，立刻停手并冷却
- 内容不存在(permanent) → 只发 1 次
- 网络超时(transient/unknown) → 2 次，且全程匿名
- 缺登录态(login) → 2 次，第 2 次才带 cookie
"""
import pytest
import yt_dlp

import app.extractor as ex
from app import utils


class FakeYDL:
    behavior = None
    calls: list = []
    opts: list = []

    def __init__(self, opts):
        FakeYDL.opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.calls.append(url)
        return FakeYDL.behavior(url)


URL = "https://www.bilibili.com/video/BV1xx411c7mD"


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    FakeYDL.calls = []
    FakeYDL.opts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    utils.clear_platform_cooldown("bilibili")
    yield
    utils.clear_platform_cooldown("bilibili")


def _raise(msg):
    def f(url):
        raise RuntimeError(msg)
    return f


def test_risk_stops_after_one_request():
    FakeYDL.behavior = _raise("HTTP Error 403: Forbidden")
    with pytest.raises(RuntimeError):
        ex._extract_info(URL)
    assert len(FakeYDL.calls) == 1
    info = utils.platform_cooldown_info("bilibili")
    assert info["remaining"] > 60 and info["kind"] == "risk"
    assert info["strikes"] == 1


def test_cooldown_blocks_without_requests():
    FakeYDL.behavior = _raise("HTTP Error 403: Forbidden")
    with pytest.raises(RuntimeError):
        ex._extract_info(URL)
    FakeYDL.calls.clear()
    with pytest.raises(RuntimeError):
        ex._extract_info(URL)
    assert FakeYDL.calls == []


def test_permanent_does_not_retry():
    FakeYDL.behavior = _raise("作品已被删除")
    with pytest.raises(RuntimeError):
        ex._extract_info(URL)
    assert len(FakeYDL.calls) == 1


def test_transient_retries_anonymously():
    FakeYDL.behavior = _raise("socket timeout: read timed out")
    with pytest.raises(RuntimeError):
        ex._extract_info(URL)
    assert len(FakeYDL.calls) == 2
    assert all(not o.get("cookiefile") for o in FakeYDL.opts)


def test_login_escalates_to_cookie():
    def behavior(url):
        if len(FakeYDL.calls) == 1:
            raise RuntimeError("需要登录后才能查看")
        return {"title": "ok"}

    FakeYDL.behavior = behavior
    info = ex._extract_info(URL)
    assert info["title"] == "ok"
    ck = [o.get("cookiefile") for o in FakeYDL.opts]
    assert ck[0] is None
    assert str(ck[1] or "").endswith("bilibili.txt")


def test_proxy_option_is_passed(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_BILIBILI", "http://127.0.0.1:7890")
    FakeYDL.behavior = lambda url: {"title": "ok"}
    ex._extract_info(URL)
    assert FakeYDL.opts[0].get("proxy") == "http://127.0.0.1:7890"


def test_fetch_platform_policy():
    calls = []

    def risk_fetch(url, use_cookie=False, **kw):
        calls.append(use_cookie)
        raise RuntimeError("访问过于频繁")

    with pytest.raises(RuntimeError):
        ex._fetch_platform(risk_fetch, "https://www.douyin.com/video/1")
    assert calls == [False]

    calls.clear()

    def login_fetch(url, use_cookie=False, **kw):
        calls.append(use_cookie)
        if len(calls) == 1:
            raise RuntimeError("需要登录后才能查看")
        return {"title": "ok"}

    assert ex._fetch_platform(login_fetch, "https://www.douyin.com/video/1")["title"] == "ok"
    assert calls == [False, True]

    calls.clear()

    def timeout_fetch(url, use_cookie=False, **kw):
        calls.append(use_cookie)
        raise RuntimeError("socket timeout")

    with pytest.raises(RuntimeError):
        ex._fetch_platform(timeout_fetch, "https://www.douyin.com/video/1")
    assert calls == [False, False]


def test_kuaishou_never_sends_cookie(monkeypatch):
    """快手分享页匿名即可：needs_login=False → 即使出现 login 信号也不带 cookie。"""
    from app.platforms import adapter_for

    monkeypatch.setenv("TRANSCRIPT_COOKIE_MODE", "always")
    assert adapter_for("https://m.gifshow.com/fw/photo/1").needs_login is False
    assert adapter_for("https://www.douyin.com/video/1").needs_login is True
