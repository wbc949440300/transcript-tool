"""通用链接处理：从分享文案中提取 URL、跟随短链跳转、按平台归一化。

各平台分享出来的往往不是干净的视频地址：
- 抖音/快手/小红书/B站：短链（v.douyin.com、v.kuaishou.com、xhslink.com、b23.tv）
- App 复制的是整段文案，链接夹在中文/emoji 中间
- 抖音还有 /jingxuan?modal_id=<id> 这种合集页形式

本模块把这些统一转成可提取的标准地址；无法支持的平台给出明确中文提示，
而不是把 yt-dlp 的 Unsupported URL 直接抛给用户。
"""
from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit
from typing import Optional

from .utils import http_opener, platform_of

logger = logging.getLogger("links")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 从任意文本中抓第一个链接（兼容中文括号/emoji 包在两边）
_URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、；：（）【】《》]+", re.I)

_SHORT_HOSTS = ("v.douyin.com", "xhslink.com", "v.kuaishou.com", "b23.tv", "b23.com")

# 快手 App「分享→复制链接」给的是 www.kuaishou.com/f/<shareCode>，
# 它 302 到 /short-video/<photoId>，不属于固定短链域名，只能按路径识别。
# 分享码可能包含连字符/下划线（如 X-9mK3qPXX1HVF9k），不能只匹配字母数字。
_KS_SHARE_PATH = re.compile(r"^/f/[A-Za-z0-9_-]{4,}")


class LinkError(ValueError):
    """链接无法处理（面向用户的中文提示）。"""


def extract_url(text: str) -> Optional[str]:
    """从分享文案里取第一个 URL。"""
    if not text:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,;)")


def _host(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def _is_ks_share(url: str) -> bool:
    return _host(url) in ("www.kuaishou.com", "kuaishou.com") and bool(
        _KS_SHARE_PATH.match(urlsplit(url).path or "")
    )


def is_short(url: str) -> bool:
    h = _host(url)
    if any(h == s or h.endswith("." + s) for s in _SHORT_HOSTS):
        return True
    return _is_ks_share(url)


def resolve_short(url: str, timeout: float = 8.0) -> str:
    """跟随短链跳转拿到最终地址；失败则原样返回（交给下游再试）。"""
    if not is_short(url):
        return url
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": _UA, "Accept": "*/*"})
            with http_opener(platform_of(url)).open(req, timeout=timeout) as resp:
                final = resp.geturl()
                if final and final != url:
                    return final
        except urllib.error.HTTPError as exc:
            # 某些站点 HEAD 不允许，但会给出 30x 后的地址
            try:
                if exc.url and exc.url != url:
                    return exc.url
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("短链解析失败 %s: %s", url, exc)
    return url


# ---------- 各平台归一化 ----------
_DOUYIN_MODAL = re.compile(r"(?:modal_id|modalId|item_id)=(\d+)")
_DOUYIN_VIDEO = re.compile(r"douyin\.com/video/(\d+)")
_XHS_NOTE = re.compile(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-zA-Z]+)")
_KS_VIDEO = re.compile(r"kuaishou\.com/(?:short-video|fw/photo)/([0-9a-zA-Z]+)")
_BILI_BV = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]+)")


def _norm_douyin(url: str) -> str:
    m = _DOUYIN_VIDEO.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    m = _DOUYIN_MODAL.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return url


def _norm_xhs(url: str) -> str:
    m = _XHS_NOTE.search(url)
    if not m:
        return url
    base = f"https://www.xiaohongshu.com/explore/{m.group(1)}"
    # 小红书很多笔记必须带 xsec_token 才能访问，不能当作跟踪参数丢掉
    keep = {k: v for k, v in parse_qsl(url.split("?", 1)[1] if "?" in url else "")
            if k in ("xsec_token", "xsec_source")}
    return base + ("?" + urlencode(keep) if keep else "")


def _norm_kuaishou(url: str, from_share: bool = False) -> str:
    m = _KS_VIDEO.search(url)
    if m:
        return f"https://www.kuaishou.com/short-video/{m.group(1)}"
    if from_share or _KS_SHARE_PATH.match(urlsplit(url).path or ""):
        raise LinkError(
            "快手分享短链没有指向具体作品（可能已失效）。"
            "请在快手 App 里重新「分享 → 复制链接」后再试。"
        )
    return url


def _norm_bilibili(url: str) -> str:
    m = _BILI_BV.search(url)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    return url


def normalize(raw: str) -> tuple[str, str]:
    """把用户输入（可能是分享文案/短链/合集页）规整为可提取地址。

    返回 (规范化后的 URL, 平台说明)。无法支持时抛 LinkError（中文原因）。
    """
    text = (raw or "").strip()
    if not text:
        raise LinkError("请输入视频链接")
    # 微信小程序协议链接：只能由微信客户端打开，服务端无法解析
    if text.startswith("#小程序://") or "小程序://" in text[:12]:
        raise LinkError(
            "这是微信小程序链接（#小程序://…），只能在微信里打开，网页端无法解析。"
            "如果它能把视频存到手机，请导出视频后用「上传文件提取 → 视频转文字」。"
        )
    url = extract_url(text) or text
    if not re.match(r"https?://", url, re.I):
        raise LinkError("链接格式不正确，请粘贴以 http(s):// 开头的链接")

    host = _host(url)
    ks_share = _is_ks_share(url)

    # 短链先跟随跳转
    if is_short(url):
        resolved = resolve_short(url)
        if resolved and resolved != url:
            logger.info("短链跳转: %s -> %s", url, resolved)
            url = resolved
            host = _host(url)

    # 视频号（微信）目前 yt-dlp 不支持
    if "weixin.qq.com" in host or "channels.weixin.qq.com" in host:
        raise LinkError(
            "视频号暂不支持直接解析：微信未开放网页可读接口，视频流需要微信客户端环境。"
            "可行做法：用微信/小程序把视频保存到本机，再用「上传文件提取 → 视频转文字」转写。"
        )

    if "douyin.com" in host or "iesdouyin.com" in host:
        return _norm_douyin(url), "抖音"
    if "xiaohongshu.com" in host:
        return _norm_xhs(url), "小红书"
    if "kuaishou.com" in host:
        # 跳到了直播/个人页说明拿不到具体作品
        if "live.kuaishou.com" in host or "/profile/" in url:
            raise LinkError(
                "这个快手链接跳转到了直播/个人主页，不是具体作品。"
                "请在快手 App 里打开视频后，用「分享 → 复制链接」再试。"
            )
        return _norm_kuaishou(url, from_share=ks_share), "快手"
    if "bilibili.com" in host or "b23.tv" in host:
        return _norm_bilibili(url), "B站"
    if "youtube.com" in host or "youtu.be" in host:
        # 2026-10-31：按需求暂时关闭 YouTube 支持（不再维护/验证）。
        raise LinkError(
            "暂不支持 YouTube（已按需求关闭）。"
            "可以先把视频下载到本机，再用「上传文件提取 → 视频转文字」。")
    # 2026-11-03：按需求移除 TikTok 支持。
    if "tiktok.com" in host:
        raise LinkError(
            "暂不支持 TikTok（已按需求移除）。"
            "可以先把视频下载到本机，再用「上传文件提取 → 视频转文字」。")
    return url, "通用"
