# -*- coding: utf-8 -*-
"""发现页两条访客通道的离线测试：wbi 签名 / 结果裁剪 / 参数校验。

不打真实接口 —— 真实链路由人工验证（2026-11-04：B站搜索 1000 条可翻页、
抖音一级评论 298 条可翻页）覆盖，这里只锁住容易改坏的计算与边界。
"""
import hashlib
import urllib.parse

import pytest

from app import bilibili
from app.douyin_comments import CommentError, fetch_comments


def test_wbi_sign_is_md5_of_sorted_query_plus_mixin(monkeypatch):
    monkeypatch.setattr(bilibili, "_ensure_mixin", lambda: "M" * 32)
    signed = bilibili._signed({"keyword": "a b", "page": 1})
    q = urllib.parse.urlencode([("keyword", "a b"), ("page", "1"), ("wts", signed["wts"])])
    assert signed["w_rid"] == hashlib.md5((q + "M" * 32).encode()).hexdigest()


def test_wbi_removes_forbidden_chars_from_values(monkeypatch):
    monkeypatch.setattr(bilibili, "_ensure_mixin", lambda: "M" * 32)
    signed = bilibili._signed({"keyword": "a!b(c)*d", "page": 2})
    q = urllib.parse.urlencode([("keyword", "abcd"), ("page", "2"), ("wts", signed["wts"])])
    assert signed["w_rid"] == hashlib.md5((q + "M" * 32).encode()).hexdigest()
    assert signed["keyword"] == "a!b(c)*d"   # 原值保留，只影响参与签名的串


def test_duration_parsing():
    assert bilibili._duration_secs("12:34") == 754
    assert bilibili._duration_secs("1:02:03") == 3723
    assert bilibili._duration_secs(95) == 95
    assert bilibili._duration_secs("") == 0


def test_search_item_trims_fields_and_fixes_cover():
    row = {"bvid": "BV1xx", "aid": 1, "title": "  标题<em>高亮</em> ",
           "author": "UP", "play": 12345, "duration": "3:07",
           "pic": "//i0.hdslb.com/a.jpg", "pubdate": 1700000000}
    it = bilibili._item(row)
    assert it["url"] == "https://www.bilibili.com/video/BV1xx"
    assert it["title"] == "标题高亮"
    assert it["duration"] == 187
    assert it["cover"] == "https://i0.hdslb.com/a.jpg"


def test_search_requires_keyword():
    with pytest.raises(bilibili.BiliError):
        bilibili.search_videos("   ")


def test_comments_reject_non_douyin_url_without_launching_browser():
    # 不认识的链接必须在起浏览器之前就报错（失败要快，别把用户晾 30 秒）
    with pytest.raises(CommentError):
        fetch_comments("https://example.com/video/123")
