"""代理自检：配了坏代理要立刻看出来，而不是把任务失败误判成"平台风控"。"""
import pytest

from app import alerts, utils


class _Boom:
    def open(self, *a, **k):
        raise AssertionError("没配代理时不该发起任何请求")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    utils._PROXY_CHECKS.clear()
    for k in ("TRANSCRIPT_PROXY_POOL", "TRANSCRIPT_PROXY_POOL_DOUYIN",
              "TRANSCRIPT_PROXY_POOL_KUAISHOU", "TRANSCRIPT_PROXY_DOUYIN",
              "TRANSCRIPT_PROXY_KUAISHOU"):
        monkeypatch.delenv(k, raising=False)
    yield
    utils._PROXY_CHECKS.clear()


def test_direct_when_no_proxy_and_no_request(monkeypatch):
    monkeypatch.setattr(utils, "http_opener", lambda *a, **k: _Boom())
    res = utils.proxy_check("douyin")
    assert res["direct"] is True and res["ok"] is True
    assert "直连" in res["detail"]


def test_bad_proxy_reports_failure(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_DOUYIN", "http://127.0.0.1:9")
    res = utils.proxy_check("douyin", timeout=3)
    assert res["ok"] is False and res["direct"] is False
    assert res.get("pool") == 1
    # 结果里不应出现代理地址本身（避免把凭据写进状态/日志）
    assert "127.0.0.1:9" not in res["detail"]


def test_check_result_is_cached(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_KUAISHOU", "http://127.0.0.1:9")
    utils.proxy_check("kuaishou", timeout=3)
    assert "kuaishou" in utils.proxy_checks()
    assert utils.proxy_checks()["kuaishou"]["ok"] is False


def test_alert_surfaces_dead_proxy(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_DOUYIN", "http://127.0.0.1:9")
    utils.proxy_check("douyin", timeout=3)
    items = alerts._proxy_alert()
    assert any(i["code"] == "proxy_dead" for i in items)
    assert any("douyin" in i["message"] for i in items)


def test_alert_silent_for_direct_platforms():
    utils.proxy_check("kuaishou")            # 没配代理 → direct
    assert alerts._proxy_alert() == []