"""账号池运营能力 + 代理池（账号↔出口绑定、出口级冷却）。

对应"长期跑量"要做的事：
- 账号级计量（今天用了多少）+ 健康度（成功/失败）→ 选择策略：冷却 > 用量 > 成功率
- 代理池：同一个号固定走同一条出口；无账号的（快手）按请求轮换；
  冷却/限流按**出口**隔离，一条出口被限流不让整个平台停摆
"""
import pytest

from app import utils


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """每个用例自己的账号池目录，并清掉可能残留的代理配置。"""
    for k in ("TRANSCRIPT_PROXY", "TRANSCRIPT_PROXY_DOUYIN", "TRANSCRIPT_PROXY_KUAISHOU",
              "TRANSCRIPT_PROXY_POOL", "TRANSCRIPT_PROXY_POOL_DOUYIN",
              "TRANSCRIPT_PROXY_POOL_KUAISHOU"):
        monkeypatch.delenv(k, raising=False)
    root = utils.COOKIE_DIR
    pool = root / "douyin"
    pool.mkdir(parents=True, exist_ok=True)
    (pool / "account_1.txt").write_text("# Netscape\n.douyin.com\tTRUE\t/\tTRUE\t0\ts\t1\n",
                                        encoding="utf-8")
    (pool / "account_2.txt").write_text("# Netscape\n.douyin.com\tTRUE\t/\tTRUE\t0\ts\t2\n",
                                        encoding="utf-8")
    (root / ".accounts.json").unlink(missing_ok=True)
    (root / ".cooldown.json").unlink(missing_ok=True)
    yield
    # 收尾：把池子里的假账号清掉，避免影响别的测试（conftest 的 cookies/douyin.txt 必须留着）
    for name in ("account_1.txt", "account_2.txt"):
        (pool / name).unlink(missing_ok=True)
    try:
        pool.rmdir()
    except OSError:
        pass
    (root / ".accounts.json").unlink(missing_ok=True)
    (root / ".cooldown.json").unlink(missing_ok=True)


def _clear_usage():
    (utils.COOKIE_DIR / ".accounts.json").unlink(missing_ok=True)


# ---------- 账号池 ----------

def test_pick_balances_usage():
    """没有任何历史时也应该把量摊平（选中即记账，下次自然轮到另一个）。"""
    picks = [utils.pick_account("douyin") for _ in range(4)]
    names = [utils.account_key(p) for p in picks]
    assert sorted(names) == ["account_1", "account_1", "account_2", "account_2"] or \
        sorted(set(names)) == ["account_1", "account_2"], names
    snap = {a["name"]: a["usedToday"] for a in utils.account_pool_snapshot("douyin")}
    assert set(snap) == {"account_1", "account_2"}
    assert sum(snap.values()) == 4


def test_cooled_account_is_skipped():
    utils.mark_account_cooldown(str(utils.COOKIE_DIR / "douyin" / "account_1.txt"), 600)
    picks = {utils.account_key(utils.pick_account("douyin")) for _ in range(5)}
    assert picks == {"account_2"}


def test_unhealthy_account_is_deprioritized():
    """用量一样时，失败率高的号排后面（健康度参与排序）。"""
    import json
    import time

    today = time.strftime("%Y-%m-%d")
    (utils.COOKIE_DIR / ".accounts.json").write_text(json.dumps({
        "douyin": {
            "account_1": {"date": today, "used_today": 5, "ok": 0, "fail": 3},
            "account_2": {"date": today, "used_today": 5, "ok": 4, "fail": 0},
        }
    }, ensure_ascii=False), encoding="utf-8")
    assert utils.account_key(utils.pick_account("douyin")) == "account_2"


def test_snapshot_reports_health_and_usage():
    a1 = str(utils.COOKIE_DIR / "douyin" / "account_1.txt")
    a2 = str(utils.COOKIE_DIR / "douyin" / "account_2.txt")
    utils.record_account_result(a1, True, "douyin")
    utils.record_account_result(a1, True, "douyin")
    utils.record_account_result(a1, False, "douyin", "验证码")
    picked = utils.account_key(utils.pick_account("douyin"))     # 产生一次今日用量
    snap = {a["name"]: a for a in utils.account_pool_snapshot("douyin")}

    assert snap[picked]["usedToday"] >= 1
    row = snap["account_1"]
    assert row["ok"] == 2 and row["fail"] == 1
    assert row["successRate"] == 0.67
    assert row["lastFail"] == "验证码"
    assert row["cooling"] is False
    assert set(snap) == {"account_1", "account_2"}


def test_single_file_fallback_still_works():
    """池子为空时回退 cookies/<平台>.txt（回归保护）。

    注意：cookies/douyin.txt 是 conftest 给整个测试会话准备的，**不能删** ——
    别的用例（cookie 策略）也依赖它存在。
    """
    (utils.COOKIE_DIR / "douyin" / "account_1.txt").unlink()
    (utils.COOKIE_DIR / "douyin" / "account_2.txt").unlink()
    single = utils.COOKIE_DIR / "douyin.txt"
    assert single.is_file() and single.stat().st_size > 0
    assert utils.pick_account("douyin") == str(single)
    # 单文件也要记"今天用了多少"（这条曾经漏掉过，所以单独钉一个断言）
    snap = utils.account_pool_snapshot("douyin")
    assert snap and snap[0]["name"] == "douyin" and snap[0]["usedToday"] >= 1


# ---------- 代理池 ----------

def test_pool_single_proxy_behaves_like_before(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_KUAISHOU", "http://p1:8080")
    assert utils.proxy_pool("kuaishou") == ["http://p1:8080"]
    assert utils.proxy_for("kuaishou") == "http://p1:8080"
    assert utils.egress_id("kuaishou") == "proxy:http://p1:8080"


def test_pool_binds_account_to_one_egress(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_DOUYIN", "http://a:1,http://b:2,http://c:3")
    first = utils.proxy_for("douyin", key="account_1")
    assert first in ("http://a:1", "http://b:2", "http://c:3")
    for _ in range(5):                   # 同一个号始终同一条出口
        assert utils.proxy_for("douyin", key="account_1") == first


def test_pool_rotates_without_key(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_KUAISHOU", "http://a:1,http://b:2")
    seq = [utils.proxy_for("kuaishou") for _ in range(4)]
    assert set(seq) == {"http://a:1", "http://b:2"}, seq
    assert seq[0] != seq[1]


def test_platform_pool_overrides_global(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL", "http://global:1")
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_DOUYIN", "http://douyin:1")
    assert utils.proxy_pool("douyin") == ["http://douyin:1"]
    assert utils.proxy_pool("bilibili") == ["http://global:1"]


def test_pick_proxy_skips_cooling_egress(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_KUAISHOU", "http://a:1,http://b:2")
    utils.mark_platform_cooldown("kuaishou", 600, kind="risk", reason="HTTP 403",
                                 egress="proxy:http://a:1")
    chosen = set()
    for _ in range(6):
        px, eg = utils.pick_proxy_available("kuaishou")
        chosen.add(px)
    assert chosen == {"http://b:2"}, chosen


def test_cooldown_is_isolated_per_egress(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_POOL_DOUYIN", "http://a:1,http://b:2")
    utils.mark_platform_cooldown("douyin", 600, kind="risk", reason="HTTP 403",
                                 egress="proxy:http://a:1")
    assert utils.platform_cooldown_remaining("douyin", egress="proxy:http://a:1") > 0
    assert utils.platform_cooldown_remaining("douyin", egress="proxy:http://b:2") == 0
    utils.clear_platform_cooldown("douyin")          # 不给 egress → 清掉该平台所有出口
    assert utils.platform_cooldown_remaining("douyin", egress="proxy:http://a:1") == 0


def test_mask_proxy_hides_credentials():
    assert utils.mask_proxy("http://user:pass@1.2.3.4:8080") == "http://***@1.2.3.4:8080"
    assert utils.mask_proxy("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert utils.mask_proxy("") == ""


def test_no_pool_means_direct():
    assert utils.proxy_pool("douyin") == []
    assert utils.proxy_for("douyin") is None
    assert utils.egress_id("douyin") == "direct"
    px, eg = utils.pick_proxy_available("douyin")
    assert px is None and eg == "direct"