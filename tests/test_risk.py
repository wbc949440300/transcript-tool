"""失败分类 / cookie 策略 / 冷却熔断 / 每日预算 / 出口隔离。"""
import pytest

from app import risk, utils


@pytest.mark.parametrize("text,kind", [
    ("HTTP Error 403: Forbidden", "risk"),
    ("访问过于频繁，请稍后再试", "risk"),
    ("请求失败：300011 Account exception", "risk"),
    ("抖音风控冷却中（约 30 分钟后自动恢复）", "risk"),
    ("需要登录后才能查看", "login"),
    ("HTTP Error 401: Unauthorized", "login"),
    ("笔记暂时无法浏览", "login"),
    ("作品已被删除", "permanent"),
    ("该视频已被删除，无法播放", "permanent"),
    ("HTTP Error 404: Not Found", "permanent"),
    # 内容类型本身做不了：不该被当成"未知失败"去重试（会白多一次页面访问）
    ("这是抖音的图集（图文）作品：网页端拿不到图集数据……", "permanent"),
    ("暂不支持 YouTube（已按需求关闭）。", "permanent"),
    ("socket timeout: read timed out", "transient"),
    ("URLError: <urlopen error [WinError 10054] 连接被重置>", "transient"),
    ("抖音页面已打开但未解析到视频详情（可能是页面改版或渲染不完整）。", "unknown"),
])
def test_classify_failure(text, kind):
    assert utils.classify_failure(RuntimeError(text)) == kind


def test_classify_handles_none_and_objects():
    assert utils.classify_failure(None) == "unknown"
    assert utils.classify_failure(TimeoutError("timed out")) == "transient"


def test_cookie_only_on_login_signal():
    url = "https://www.douyin.com/video/1"
    assert utils.should_use_cookie(url, 2) is False          # 普通失败：匿名
    assert utils.should_use_cookie(url, 1, login_required=True) is True


def test_cookie_mode_off(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_COOKIE_MODE", "off")
    assert utils.should_use_cookie("https://www.douyin.com/video/1", 1,
                                   login_required=True) is False


def test_cooldown_escalates_then_clears():
    utils.clear_platform_cooldown("douyin")
    utils.mark_platform_cooldown("douyin", 900, kind="risk", reason="HTTP 403")
    i1 = utils.platform_cooldown_info("douyin")
    assert (i1["strikes"], i1["level"]) == (1, 1)
    assert 880 < i1["remaining"] <= 900

    utils.mark_platform_cooldown("douyin", 900, kind="risk", reason="HTTP 403")
    i2 = utils.platform_cooldown_info("douyin")
    assert (i2["strikes"], i2["level"]) == (2, 2)
    assert 1750 < i2["remaining"] <= 1800

    utils.mark_platform_cooldown("douyin", 900, kind="risk", reason="HTTP 403")
    i3 = utils.platform_cooldown_info("douyin")
    assert i3["level"] == 3
    assert 3550 < i3["remaining"] <= 3600

    utils.clear_platform_cooldown("douyin")
    assert utils.platform_cooldown_info("douyin")["remaining"] == 0


def test_cooldown_legacy_numeric_format():
    """旧版本 .cooldown.json 里是纯数字时间戳，必须仍然认得。"""
    import json
    import time

    utils.clear_platform_cooldown("kuaishou")
    path = utils.COOKIE_DIR / ".cooldown.json"
    data = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["platform:kuaishou"] = time.time() + 300
    path.write_text(json.dumps(data), encoding="utf-8")
    assert utils.platform_cooldown_info("kuaishou")["remaining"] > 290
    utils.clear_platform_cooldown("kuaishou")


def test_budget_limit_and_reset(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_TIKTOK_DAILY_BUDGET", "2")
    risk.reset("tiktok")
    ok, st = risk.allow("tiktok")
    assert ok and st["limit"] == 2 and st["used"] == 0

    risk.consume("tiktok")
    risk.consume("tiktok")
    ok, st = risk.allow("tiktok")
    assert not ok and st["used"] == 2 and st["remaining"] == 0

    risk.reset("tiktok")
    assert risk.allow("tiktok")[0]


def test_budget_unlimited(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_BILIBILI_DAILY_BUDGET", "0")
    risk.reset("bilibili")
    for _ in range(3):
        risk.consume("bilibili")
    ok, st = risk.allow("bilibili")
    assert ok and st["unlimited"] is True
    risk.reset("bilibili")


def test_budget_defaults_come_from_adapter(monkeypatch):
    """没配 env 时，额度应来自平台适配器的声明值。

    注意：项目 .env 里可能已经按目标量级设了额度（如抖音 600），
    所以这里先清掉相关变量再断言——测的是"回退到适配器默认"，不是某个具体数字。
    """
    for key in ("TRANSCRIPT_DAILY_BUDGET", "TRANSCRIPT_DOUYIN_DAILY_BUDGET",
                "TRANSCRIPT_KUAISHOU_DAILY_BUDGET", "TRANSCRIPT_XIAOHONGSHU_DAILY_BUDGET",
                "TRANSCRIPT_BILIBILI_DAILY_BUDGET", "TRANSCRIPT_TIKTOK_DAILY_BUDGET"):
        monkeypatch.delenv(key, raising=False)
    assert risk.daily_limit("douyin") == 150
    assert risk.daily_limit("xiaohongshu") == 100
    assert risk.daily_limit("kuaishou") == 200


def test_egress_isolation(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY_DOUYIN", "http://127.0.0.1:7890")
    assert utils.proxy_for("douyin") == "http://127.0.0.1:7890"
    assert utils.egress_id("douyin") == "proxy:http://127.0.0.1:7890"
    assert utils._platform_cooldown_key("douyin").endswith("@proxy:http://127.0.0.1:7890")
    # 其它平台不受影响
    assert utils.egress_id("kuaishou") == "direct"


def test_proxy_global_fallback(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_PROXY", "socks5://127.0.0.1:1080")
    assert utils.proxy_for("bilibili") == "socks5://127.0.0.1:1080"
    assert utils.proxy_for_media() is None
    monkeypatch.setenv("TRANSCRIPT_PROXY_MEDIA", "http://127.0.0.1:8080")
    assert utils.proxy_for_media() == "http://127.0.0.1:8080"
