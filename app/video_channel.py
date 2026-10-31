"""视频号（微信）元数据：用 finder-preview 的 get_feed_info 拿标题/作者/封面。

背景：视频号没有可抓的网页接口，视频流只能靠抓包/下载器；但**元数据**有一个公开预览
接口可用（交接文档实测）：

    POST https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info
    body {"baseReq":{"generalToken":""},"shortUri":"<id>"}

拿到后做两件事（2026-10-31 加）：
1. 用户在页面贴视频号链接时，把标题/作者/封面**带回去给他看**，而不是只丢一句"不能解析"；
2. 按标题**记住**这些元数据 —— 文件经下载器落到监听目录后，用它替换"文件名当标题"
   （重复下载去重也用它提高命中率）。

注意：解析响应用的是容错遍历（接口字段名在不同版本里变过），所以即使字段改名也大概率能取到；
取不到就安静放弃，不影响主流程。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from .utils import http_opener, throttle

logger = logging.getLogger("video_channel")

INFO_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
MAX_KEEP = 30          # 记住多少条元数据
_MIN_MATCH_LEN = 6     # 标题匹配的最短长度（太短容易误匹配）

_SHORT_URI_PATTERNS = (
    re.compile(r"/sph/([A-Za-z0-9_-]{6,})"),
    re.compile(r"[?&]shortUri=([A-Za-z0-9_-]{6,})"),
    re.compile(r"/finder-preview/[^/]*?([A-Za-z0-9_-]{8,})"),
)


def short_uri_from(url: str) -> str:
    """从视频号链接里取 shortUri（取不到返回空串）。"""
    raw = (url or "").strip()
    for pat in _SHORT_URI_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1)
    return ""


def _walk_find(obj, keys: tuple[str, ...], depth: int = 0):
    """在嵌套 JSON 里找第一个命中的 key（容错：接口字段名变过）。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            got = _walk_find(v, keys, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:8]:
            got = _walk_find(v, keys, depth + 1)
            if got:
                return got
    return None


def parse_feed_info(payload: dict) -> dict:
    """从 get_feed_info 的响应里取 标题/作者/封面/点赞（取不到就留空）。"""
    if not isinstance(payload, dict):
        return {}
    title = _walk_find(payload, ("desc", "title", "objectDesc", "feedDesc", "content"))
    author = _walk_find(payload, ("nickname", "authorName", "username", "nickName"))
    cover = _walk_find(payload, ("coverUrl", "cover_url", "coverImgUrl", "thumbUrl", "cover"))
    likes = _walk_find(payload, ("likeCount", "like_count", "likes", "favCount"))
    out = {"title": title or "", "author": author or "", "cover": cover or "",
           "likes": likes or ""}
    return out if (out["title"] or out["author"] or out["cover"]) else {}


def fetch_feed_info(short_uri: str, timeout: float = 20.0) -> dict:
    """取视频号元数据；失败返回 {}（调用方按"没有元数据"处理）。"""
    uri = (short_uri or "").strip()
    if not uri:
        return {}
    body = json.dumps({"baseReq": {"generalToken": ""}, "shortUri": uri}).encode("utf-8")
    import urllib.request

    req = urllib.request.Request(INFO_API, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://channels.weixin.qq.com/",
    })
    throttle("channels.weixin.qq.com")
    try:
        with http_opener("").open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:  # noqa: BLE001
        logger.info("视频号元数据获取失败: %s", exc)
        return {}
    meta = parse_feed_info(data)
    if meta:
        logger.info("视频号元数据: 标题=%s 作者=%s 封面=%s",
                    meta["title"][:20], meta["author"][:12], "有" if meta["cover"] else "无")
    return meta


# ---------- 记住元数据（供监听目录把"文件名"换成真标题） ----------

def _meta_file() -> Path:
    return Path(os.getenv("TRANSCRIPT_VC_META", "video_channel_meta.json"))


def _load() -> list[dict]:
    f = _meta_file()
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        return [x for x in (items or []) if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        return []


def remember(meta: dict) -> None:
    """记一条视频号元数据（按标题去重、只留最近 MAX_KEEP 条）。"""
    if not meta or not (meta.get("title") or meta.get("short_uri")):
        return
    items = _load()
    title = (meta.get("title") or "").strip()
    items = [x for x in items if (x.get("title") or "").strip() != title]
    items.insert(0, {
        "title": title,
        "author": (meta.get("author") or "").strip(),
        "cover": (meta.get("cover") or "").strip(),
        "short_uri": (meta.get("short_uri") or "").strip(),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    try:
        f = _meta_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"items": items[:MAX_KEEP]}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("视频号元数据写入失败: %s", exc)


def _norm(text: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "", (text or "").lower())


def match(filename: str) -> Optional[dict]:
    """按文件名找记住的元数据（文件名通常就是下载器按标题命名的）。

    匹配规则：去掉扩展名与标点后，一方包含另一方且长度 ≥ 6 字。
    命中返回元数据 dict，否则 None。
    """
    stem = _norm(Path(filename or "").stem)
    if len(stem) < 4:
        return None
    for item in _load():
        title = _norm(item.get("title") or "")
        if not title:
            continue
        if stem == title:                     # 完全一致：长短都认
            logger.info("文件名命中视频号元数据: %s", filename[:30])
            return item
        # 包含关系要两边都够长，否则容易误匹配（下载器会在标题前后加 id/日期）
        if min(len(stem), len(title)) >= _MIN_MATCH_LEN and (stem in title or title in stem):
            logger.info("文件名命中视频号元数据: %s → %s", filename[:30], item.get("title", "")[:24])
            return item
    return None