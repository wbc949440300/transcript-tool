"""快手专用提取：走移动端分享页 m.gifshow.com/fw/photo/<id>。

yt-dlp 没有快手提取器；www.kuaishou.com 对无 cookie 访问直接返回
{"result":2} 风控 JSON，移动端会跳验证码。但分享页是服务端渲染的，
包含 caption / userName / duration / mp4 直链，无需登录即可读取。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .utils import (cookies_header_for, http_opener, mark_platform_cooldown, pick_account,
                    pick_proxy_available, platform_cooldown_enabled,
                    platform_cooldown_remaining, platform_of, platform_serialized,
                    throttle_platform)

logger = logging.getLogger("kuaishou")


_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

_PHOTO_ID = re.compile(r"(?:short-video|fw/photo)/([0-9a-zA-Z]{6,})")

# 只在"什么都没解析到"时才用于判风控（解析成功就完全不看这些字符串），且只看
# 页面头部：页面 JS 里可能本来就含这类词，误判的代价是整个平台冷却 15 分钟。
_RISK_MARKERS = ("验证码", "滑块", "安全验证", "请完成验证", "访问过于频繁",
                 "操作频繁", "访问频繁")


def _page_fingerprint(html: str, caption: str, author: Optional[str],
                      dur_ms: Optional[float], urls: list[str]) -> str:
    """页面指纹（改版排查用）：只记体量与关键键的命中情况，不记内容。"""
    keys = ",".join(k for k in ("caption", "userName", "duration", "photoType")
                    if f'"{k}"' in html)
    return (f"bytes={len(html)} caption={len(caption)} "
            f"author={'y' if author else 'n'} duration={'y' if dur_ms else 'n'} "
            f"mp4={len(urls)} keys=[{keys}]")


def extract_photo_id(url: str) -> Optional[str]:
    m = _PHOTO_ID.search(url or "")
    return m.group(1) if m else None


@platform_serialized("kuaishou")
def fetch_kuaishou(url: str, timeout: float = 25.0, use_cookie: bool = False) -> dict:
    """抓取快手分享页，返回 title/author/duration/video_url/caption 等。

    use_cookie 默认 False：分享页本身不需要登录，避免无谓暴露账号。
    """
    # 先选出口（没有账号概念 → 按请求轮询池子），冷却也只针对这条出口
    px, eg = pick_proxy_available("kuaishou")
    remain = platform_cooldown_remaining("kuaishou", egress=eg)
    if remain > 0 and platform_cooldown_enabled("kuaishou"):
        raise RuntimeError(
            f"快手风控冷却中（约 {int(remain // 60) + 1} 分钟后自动恢复）。"
            "期间请求会直接快速失败，请先提取其他平台。")

    pid = extract_photo_id(url)
    if not pid:
        raise RuntimeError("无法从链接中识别快手作品 ID")

    share_url = f"https://m.gifshow.com/fw/photo/{pid}"
    headers = {"User-Agent": _MOBILE_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if use_cookie:
        acc = pick_account(platform_of(url))
        ck = cookies_header_for(acc or url)
        if ck:
            headers["Cookie"] = ck
            logger.info("快手分享页使用账号 cookie: %s", Path(acc).name if acc else "(默认)")
    req = urllib.request.Request(share_url, headers=headers)
    throttle_platform("kuaishou")
    try:
        with http_opener("kuaishou", proxy=px).open(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            mark_platform_cooldown("kuaishou", 900, kind="risk", reason=f"HTTP {exc.code}", egress=eg)
            raise RuntimeError(
                f"快手接口被限流（HTTP {exc.code}），已进入 15 分钟冷却；"
                "请降低频率，或稍后再试。") from exc
        raise RuntimeError(f"快手分享页请求失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        # 网络/超时类失败不冷却（旧实现与风控混在一起，一次抖动就让整个平台停摆）
        raise RuntimeError(f"快手分享页网络错误：{exc.reason}") from exc

    # ---- 先解析数据，再决定失败性质 ----
    # 顺序很重要：文本特征只在"什么都没解析到"时才用于判风控。页面 JS 里就可能
    # 含"稍后再试"这类常量，先解析能避免正常页面被误判成风控（误判 = 整个平台
    # 冷却 15 分钟，比失败本身更难查）。
    def jstr(key: str) -> Optional[str]:
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(key), html)
        if not m:
            return None
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)

    def jnum(key: str) -> Optional[float]:
        m = re.search(r'"%s"\s*:\s*(\d+)' % re.escape(key), html)
        return float(m.group(1)) if m else None

    caption = jstr("caption") or ""
    author = jstr("userName")
    dur_ms = jnum("duration")

    # 收集 mp4 直链（去重、优先 kwimgs/ndcimgs）
    raw = re.findall(r'https?://[^"\\\s]+?\.mp4[^"\\\s]*', html)
    urls: list[str] = []
    for u in raw:
        u = u.replace("\\u002F", "/").replace("\\/", "/").strip()
        if u not in urls:
            urls.append(u)
    urls.sort(key=lambda x: (0 if ("kwimgs.com" in x or "ndcimgs.com" in x) else 1))

    if caption or urls:
        if not caption or not dur_ms:
            # 还能出数据，但解析不全 → 多半是页面结构变了。先成功，留指纹方便排查。
            logger.warning("快手分享页解析不完整（可能是改版）：%s",
                           _page_fingerprint(html, caption, author, dur_ms, urls))
        return {
            "id": pid,
            "title": caption,
            "desc": caption,
            "author": author,
            "duration": (dur_ms / 1000.0) if dur_ms else None,
            "video_url": urls[0] if urls else None,
            "video_urls": urls[:6],
            "cover": None,
            "share_url": share_url,
        }

    # 什么都没解析到：这时候才看响应本身是什么（只在页面头部找特征，避免 JS 常量误伤）
    head = html[:60000]
    hit = next((m for m in _RISK_MARKERS if m in head), "")
    result2 = False
    if html.lstrip()[:1] == "{":
        try:
            payload = json.loads(html)
        except Exception:  # noqa: BLE001
            payload = None
        result2 = isinstance(payload, dict) and str(payload.get("result")) == "2"
    if hit or result2:
        reason = hit or "result:2"
        mark_platform_cooldown("kuaishou", 900, kind="risk", reason=reason, egress=eg)
        raise RuntimeError(f"快手命中风控特征（{reason}），已进入 15 分钟冷却；"
                           "期间请求会直接快速失败。请降低频率或稍后再试。")
    if len(html) < 5000:
        # 页面过短但没有风控特征：多为链接失效/改版，**不冷却**，交给上层重试一次
        raise RuntimeError(f"快手分享页内容异常（仅 {len(html)} 字节），未能解析到作品信息。")
    if "atlas" in html.lower() or '"phototype"' in html.lower():
        raise RuntimeError("这是快手的图集/图文作品，没有视频流，暂不支持视频转写。")
    logger.warning("快手分享页未解析到内容：%s", _page_fingerprint(html, caption, author, dur_ms, urls))
    raise RuntimeError("快手分享页未解析到内容（链接可能已失效）")
