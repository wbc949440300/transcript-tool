"""平台适配器：URL → 平台声明（队列键、预算、冷却、执行方式都靠它）。"""
import pytest

from app.links import LinkError, normalize
from app.platforms import GENERIC, adapter_by_key, adapter_for, all_adapters


def test_douyin_urls():
    for url in ("https://www.douyin.com/video/123",
                "https://v.douyin.com/abc/",
                "https://www.iesdouyin.com/share/video/1/"):
        a = adapter_for(url)
        assert a.key == "douyin" and a.label == "抖音"
        assert a.run_kind == "douyin"
        assert a.needs_login is True
        assert a.fingerprint == "profile"


def test_kuaishou_urls():
    for url in ("https://www.kuaishou.com/short-video/3x5gu3rc5sqd9ni",
                "https://m.gifshow.com/fw/photo/3x5gu3rc5sqd9ni",
                "https://v.kuaishou.com/abc"):
        a = adapter_for(url)
        assert a.key == "kuaishou" and a.run_kind == "kuaishou"
        # 快手分享页匿名即可，不应该把账号 cookie 带出去
        assert a.needs_login is False


def test_other_platforms():
    assert adapter_for("https://www.xiaohongshu.com/explore/abc").key == "xiaohongshu"
    assert adapter_for("https://xhslink.com/a/b").key == "xiaohongshu"
    assert adapter_for("https://www.bilibili.com/video/BV1xx411c7mD").key == "bilibili"
    assert adapter_for("https://b23.tv/abc").key == "bilibili"
    assert adapter_for("https://www.tiktok.com/@a/video/1").key == "tiktok"


def test_youtube_is_disabled():
    """YouTube 已按需求关闭：链接应被明确拒绝，而不是落到通用通道。"""
    with pytest.raises(LinkError) as ei:
        normalize("https://youtu.be/abc")
    assert "暂不支持 YouTube" in str(ei.value)
    with pytest.raises(LinkError):
        normalize("https://www.youtube.com/watch?v=abc")


def test_unknown_is_generic():
    a = adapter_for("https://example.com/video/1")
    assert a is GENERIC and a.key == "default"
    assert a.run_kind == "generic"


def test_lookup_by_key_and_catalog():
    assert adapter_by_key("douyin").label == "抖音"
    assert adapter_by_key("nope") is GENERIC
    keys = {a.key for a in all_adapters()}
    assert {"douyin", "kuaishou", "xiaohongshu", "bilibili", "tiktok", "default"} <= keys
    assert "youtube" not in keys


def test_budget_and_cooldown_declared():
    for a in all_adapters():
        assert a.cooldown_base >= 60
        assert a.daily_budget >= 0
