# -*- coding: utf-8 -*-
"""B站访客态采集：关键词搜索 / 热搜（无需账号、无需代理、不用浏览器）。

2026-11-04 实测（同一家宽出口、零 cookie、零代理）：
- 访客指纹 GET /x/frontend/finger/spi 直接发 buvid3/buvid4，不需要渲染页面；
- wbi 密钥在未登录时也能从 /x/web-interface/nav 拿到（code=-101 但 wbi_img 有值），
  所以 wbi 签名（w_rid + wts）纯 Python 就能算；
- 搜索 /x/web-interface/wbi/search/type → 20 条/页、numResults=1000 可翻页；
- 热搜 /x/web-interface/wbi/search/square → 10 条。

已知边界（同为实测）：评论接口只给 3 条最热、翻不到第二页；短视频字幕接口返回空。
所以本模块只负责"发现链接"，正文仍走原有的 yt-dlp + ASR 链路。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

from .utils import http_opener

logger = logging.getLogger("bilibili")

_API = "https://api.bilibili.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
_HEADERS = {
    "user-agent": _UA,
    "referer": "https://www.bilibili.com/",
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "origin": "https://www.bilibili.com",
}

# wbi 混淆表（官方已知的 mixinKeyEncTab）
_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
              5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
              24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
              6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

_FP_TTL = 6 * 3600       # buvid3 有效期内复用
_WBI_TTL = 3600          # wbi 密钥官方每天轮换，1 小时刷新一次足够
_CACHE_TTL = 180         # 同一关键词的搜索结果缓存，避免重复点"搜索"就重新打接口

_fp = {"buvid3": "", "buvid4": "", "at": 0.0}
_wbi = {"mixin": "", "at": 0.0}
_cache: dict[str, tuple[float, dict]] = {}


class BiliError(RuntimeError):
    """B站接口失败（带可读原因，上层直接展示给用户）。"""


def _cookie() -> str:
    if not _fp.get("buvid3"):
        return ""
    return "buvid3=%s; buvid4=%s" % (_fp["buvid3"], _fp.get("buvid4") or "")


def _request(url: str, timeout: float = 15.0) -> dict:
    """单次 GET → JSON。失败抛 BiliError（不吞异常，让上层决定提示）。"""
    req = urllib.request.Request(url, headers=dict(_HEADERS))
    ck = _cookie()
    if ck:
        req.add_header("Cookie", ck)
    try:
        with http_opener("bilibili").open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001  连接/超时/HTTPError
        raise BiliError("请求 B站 失败：%s" % exc) from exc
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise BiliError("B站 返回了非 JSON 内容（可能被限流）") from exc


def _ensure_fingerprint() -> None:
    """访客指纹：buvid3/buvid4。纯 HTTP，一支请求，不需要浏览器。"""
    if _fp["buvid3"] and time.time() - _fp["at"] < _FP_TTL:
        return
    j = _request(_API + "/x/frontend/finger/spi")
    data = j.get("data") or {}
    b3, b4 = data.get("b_3"), data.get("b_4")
    if not b3:
        raise BiliError("B站没有下发访客指纹（code=%s）" % j.get("code"))
    _fp.update(buvid3=b3, buvid4=b4 or "", at=time.time())
    logger.info("B站访客指纹已获取: buvid3 长度=%d", len(b3))


def _ensure_mixin() -> str:
    """wbi mixin key：nav 未登录也返回 wbi_img，取两段文件名按表重排取前 32 位。"""
    if _wbi["mixin"] and time.time() - _wbi["at"] < _WBI_TTL:
        return _wbi["mixin"]
    _ensure_fingerprint()
    j = _request(_API + "/x/web-interface/nav")
    wi = (j.get("data") or {}).get("wbi_img") or {}
    img = (wi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if len(img) + len(sub) != 64:
        raise BiliError("B站没有返回 wbi 密钥（img=%d sub=%d）" % (len(img), len(sub)))
    raw = img + sub
    _wbi.update(mixin="".join(raw[i] for i in _MIXIN_TAB)[:32], at=time.time())
    logger.info("B站 wbi 密钥已刷新")
    return _wbi["mixin"]


def _signed(params: dict[str, Any]) -> dict[str, str]:
    """加 wts + w_rid（md5(query + mixin)）；值里的 !'()* 按官方规则剔除。"""
    mixin = _ensure_mixin()
    p = {k: str(v) for k, v in params.items()}
    p["wts"] = str(int(time.time()))
    items = sorted(p.items())
    q = urllib.parse.urlencode(
        [(k, "".join(ch for ch in v if ch not in "!'()*")) for k, v in items])
    p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
    return p


def _api_get(path: str, params: dict[str, Any], timeout: float = 15.0) -> dict:
    url = _API + path + "?" + urllib.parse.urlencode(_signed(params))
    j = _request(url, timeout=timeout)
    if j.get("code") not in (0, None):
        raise BiliError("B站接口返回 code=%s：%s" % (j.get("code"), j.get("message") or ""))
    return j


def _clean_title(s: str) -> str:
    """搜索结果标题带 em 高亮标签，去掉标签、保留文字。"""
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _duration_secs(v: Any) -> int:
    """搜索结果的 duration 是 "12:34" / "1:02:03" 字符串，换成秒。"""
    if isinstance(v, (int, float)):
        return int(v)
    parts = str(v or "").split(":")
    try:
        nums = [int(p) for p in parts if p != ""]
    except ValueError:
        return 0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def _item(row: dict) -> dict:
    """把搜索结果裁成前端要的字段（少传无用字段，列表才轻）。"""
    bvid = row.get("bvid") or ""
    pic = str(row.get("pic") or "")
    return {
        "bvid": bvid,
        "aid": row.get("aid"),
        "url": ("https://www.bilibili.com/video/%s" % bvid) if bvid else "",
        "title": _clean_title(row.get("title")),
        "description": (row.get("description") or "").strip(),
        "author": row.get("author") or "",
        "play": row.get("play") or 0,
        "duration": _duration_secs(row.get("duration")),
        "pubdate": row.get("pubdate") or 0,
        "cover": ("https:" + pic) if pic.startswith("//") else pic,
    }


def search_videos(keyword: str, page: int = 1, page_size: int = 20) -> dict:
    """按关键词搜视频（访客态）。返回 items 直接可喂给批处理。

    实测：匿名 20 条/页、numResults=1000、无验证码；同一出口连续多次未触发风控。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise BiliError("请先输入搜索关键词")
    page = max(1, int(page or 1))
    page_size = min(50, max(1, int(page_size or 20)))
    key = "s:%s:%s:%s" % (keyword, page, page_size)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    j = _api_get("/x/web-interface/wbi/search/type", {
        "search_type": "video",
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
    })
    data = j.get("data") or {}
    rows = data.get("result") or []
    items = [_item(r) for r in rows if isinstance(r, dict) and r.get("bvid")]
    out = {
        "keyword": keyword,
        "page": page,
        "pageSize": page_size,
        "total": int(data.get("numResults") or data.get("numPages") or len(items)),
        "items": items,
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    logger.info("B站搜索「%s」第 %d 页：%d 条（匿名）", keyword, page, len(items))
    _cache[key] = (time.time(), out)
    return out


def hot_search(limit: int = 10) -> dict:
    """B站热搜词（访客态）。给"不知道搜什么"的用户一个起点。"""
    limit = min(30, max(1, int(limit or 10)))
    key = "hot:%d" % limit
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    j = _api_get("/x/web-interface/wbi/search/square", {"limit": limit, "platform": "web"})
    lst = (((j.get("data") or {}).get("trending") or {}).get("list")) or []
    out = {
        "items": [{"keyword": x.get("keyword") or x.get("show_name") or "",
                   "icon": x.get("icon") or ""} for x in lst if isinstance(x, dict)],
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _cache[key] = (time.time(), out)
    return out
