"""抖音专用提取：用 Playwright 渲染真实浏览器环境绕过抖音风控。

yt-dlp 的 DouyinIE 需要 cookie 且不认 jingxuan/modal 链接；直接请求也被
反爬拦截。本模块利用 Playwright 启动真实 Chromium，正常访问抖音页面并
拦截 aweme_detail API，从而无需登录即拿到标题/作者/描述/视频直链，必要时
下载音频再做 whisper 语音转文字。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

from . import browser
from .utils import (account_key, fetch_subtitle_text, load_netscape_cookies,
                    mark_platform_cooldown, pick_account, pick_proxy_available,
                    platform_cooldown_enabled, platform_cooldown_remaining,
                    platform_of, platform_serialized, proxy_for, record_account_result,
                    throttle_platform)

logger = logging.getLogger("douyin")

# 兼容各种抖音链接：/video/<id>、/jingxuan?modal_id=<id>、短链 v.douyin.com
_PATTERN_STD = re.compile(r"douyin\.com/video/(\d+)")
_PATTERN_MODAL = re.compile(r"(?:modal_id|modalId|item_id)=(\d+)")
_PATTERN_SHORT = re.compile(r"v\.douyin\.com/([\w-]+)")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 只在页面里出现这些才算"被风控"（抖音页面永远有"登录"字样，不能用通用登录特征）
_DOUYIN_RISK_MARKERS = ("验证码", "滑块", "安全验证", "请完成验证", "访问过于频繁",
                        "操作频繁", "请求过于频繁", "异常访问", "网络繁忙", "稍后再试")
# 内容本身没了：不算风控，不该让整个平台停摆
_DOUYIN_GONE_MARKERS = ("作品不存在", "该作品已被删除", "内容不存在", "视频不存在",
                        "作品已删除")

_NOTE_URL_RE = re.compile(r"douyin\.com/note/", re.I)


def is_note_url(url: str) -> bool:
    """是不是抖音图集（图文）作品的 /note/<id> 地址。

    实测（2026-10-31）：/note/ 页面不调用详情接口，匿名 / 带登录 cookie / 分享页
    都拿不到图集数据（页面里只有站点框架，正文 4 个字符、0 张图）——只有 App 能看。
    所以这里要提前给出可执行的提示，而不是落进"页面改版/渲染不完整"的模糊错误。
    """
    return bool(_NOTE_URL_RE.search(url or ""))


def normalize_url(url: str) -> str:
    """把各种抖音链接归一化为标准 /video/<id>，返回规范化后的 URL。"""
    url = (url or "").strip()
    m = _PATTERN_STD.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    m = _PATTERN_MODAL.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return url  # 短链等交给 Playwright 直接访问（会自动跳转）


def _extract_video_id(url: str) -> Optional[str]:
    m = _PATTERN_STD.search(url)
    if m:
        return m.group(1)
    m = _PATTERN_MODAL.search(url)
    if m:
        return m.group(1)
    return None


def _fetch_subtitle_text(url: str) -> str:
    """下载并清理字幕文件（vtt/srt/json），返回纯文本。

    实际实现放在 utils.fetch_subtitle_text（与通用通道共用，含 JSON 解析）。
    """
    return fetch_subtitle_text(url, referer="https://www.douyin.com/")


def extract_subtitles(aweme: dict) -> list[str]:
    """提取抖音视频自带的字幕（自动字幕/CC）。

    注意：作品描述里的 #话题 是 caption（描述），**不是字幕**，不能当口播文案。
    """
    out: list[str] = []
    video = aweme.get("video") or {}
    sub = video.get("subtitle")
    if isinstance(sub, dict):
        for u in (sub.get("subtitle_url_list") or sub.get("url_list") or []):
            t = _fetch_subtitle_text(u)
            if t:
                out.append(t)
    for key in ("caption_subtitle", "cc_subtitle", "video_caption", "sub_title_text"):
        val = aweme.get(key)
        if isinstance(val, dict):
            for u in (val.get("url_list") or []):
                t = _fetch_subtitle_text(u)
                if t:
                    out.append(t)
        elif isinstance(val, str) and val.strip():
            out.append(val.strip())
    return [t for t in out if t.strip()]


# 风控冷却：被拦后一段时间内快速失败。刚被限流时继续请求只会加深 IP/指纹标记，
# 停手等衰减（通常几十分钟到几小时）才是恢复最快的路（2026-10-30 用户实测教训）。
# 冷却状态持久化在 cookies/.cooldown.json，重启服务后仍然有效。
_RISK_COOLDOWN_SECS = 1800


def _profile_name_for(cookie_file: str | None) -> str:
    """账号池里的 cookie 文件 → 该账号专属 profile 名。

    多账号登录态必须互不串：profile_dir_for("douyin_account_1") 与
    login_cookie.py --account account_1 用的是同一个目录名。
    """
    if not cookie_file:
        return "douyin"
    stem = Path(cookie_file).stem
    return f"douyin_{stem}" if stem.startswith("account_") else "douyin"


def _image_post_message() -> str:
    """图集（图文）作品的用户提示：优先引导到"丢进监听目录自动成一条文案"。"""
    watch_dir = ""
    try:
        from .watch_folder import current_dir

        d = current_dir()
        if d is not None:
            watch_dir = str(d)
    except Exception:  # noqa: BLE001
        watch_dir = ""
    if watch_dir:
        return ("这是抖音的图集（图文）作品：网页端拿不到图集数据（实测匿名 / 带账号 cookie / "
                "分享页都拿不到），没法直接解析。可行做法：在抖音 App 里保存这些图片，"
                f"然后全部丢进监听目录：{watch_dir} —— 本工具会把同一批图片自动合成一条文案。")
    return ("这是抖音的图集（图文）作品：网页端拿不到图集数据（实测匿名 / 带账号 cookie / "
            "分享页都拿不到），没法直接解析。可行做法：在抖音 App 里保存图片，"
            "再用本工具的「图片文案提取（OCR）」上传识别。")


@platform_serialized("douyin")
def fetch_douyin(url: str, timeout: int = 35, use_cookie: bool = False) -> dict:
    """用 Playwright 无头访问抖音视频页，抓取详情。

    无 cookie 即可；返回 dict 含 title/author/desc/duration/caption/视频直链等。
    失败抛异常。
    """
    target = normalize_url(url)

    # /note/<id> 一定是图集（图文）作品：不用开浏览器，直接给可执行提示。
    # 短链在 links.normalize 阶段已经还原成 /note/，所以这里能提前拦住，
    # 省掉一次页面访问和约 20 秒等待（实测 2026-10-31）。
    if is_note_url(target):
        raise RuntimeError(_image_post_message())

    result: dict = {}

    # 先定账号、再定出口：**同一个号固定走同一条出口**（账号↔出口绑定），
    # 匿名则用固定 key "anon" —— 避免同一个"匿名机器"一会儿换一个 IP。
    account_ck = pick_account(platform_of(target)) if use_cookie else None
    acct_key = account_key(account_ck) or "anon"
    px, eg = pick_proxy_available("douyin", key=acct_key)

    remain = platform_cooldown_remaining("douyin", egress=eg)
    if remain > 0 and platform_cooldown_enabled("douyin"):
        raise RuntimeError(
            f"抖音风控冷却中（约 {int(remain // 60) + 1} 分钟后自动恢复）。"
            "刚被限流时继续请求会加深标记；建议先提取其他平台，到点自动恢复。")

    # P1：持久 profile + 固定指纹。同一账号始终是同一台"机器"，
    # 而不是每次任务换一个全新无痕浏览器（后者是最明显的机器特征之一）。
    # 账号池存在时按账号隔离 profile（douyin_account_1），与
    # login_cookie.py --account account_1 的目录名一致 → 登录态直接复用。
    profile_name = _profile_name_for(account_ck)
    try:
        pw, ctx, owned = browser.open_profile(
            headless=True, profile_dir=browser.profile_dir_for(profile_name),
            attach_cdp=False, user_agent=_UA, proxy=px)
    except Exception as exc:  # noqa: BLE001
        logger.warning("抖音持久 profile(%s) 启动失败，回退临时上下文: %s", profile_name, exc)
        pw, ctx, owned = browser.open_profile(
            headless=True, persistent=False, attach_cdp=False,
            user_agent=_UA, proxy=px)
    logger.info("抖音持久 profile: %s（%s）", profile_name,
                "账号 cookie" if account_ck else "匿名")
    try:
        # 默认匿名访问；仅在匿名失败后由上层带 cookie 重试（降低账号风控风险）
        if account_ck:
            cs = load_netscape_cookies(account_ck)
            if cs:
                try:
                    ctx.add_cookies(cs)
                    logger.info("抖音通道使用账号 cookie: %s", Path(account_ck).name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("抖音 cookie 注入失败: %s", exc)
        page = ctx.new_page()

        def on_response(resp):
            nonlocal result
            if result.get("__got"):
                return
            rurl = resp.url
            if "aweme/v1/web/aweme/detail" in rurl or "/aweme/detail/" in rurl:
                try:
                    result["__api_status"] = resp.status
                except Exception:  # noqa: BLE001
                    pass
                try:
                    body = resp.json()
                    if not isinstance(body, dict):
                        return
                    aweme = body.get("aweme_detail") or body.get("detail") or body
                    if not isinstance(aweme, dict):
                        return
                    result["__got"] = True
                    desc = aweme.get("desc")
                    result["title"] = desc
                    result["desc"] = desc
                    author = aweme.get("author") or {}
                    result["author"] = author.get("nickname") if isinstance(author, dict) else None
                    video = aweme.get("video") or {}
                    result["duration"] = video.get("duration")
                    # 只认真正的字幕文件；caption 是作品描述/话题，不能当口播文案
                    subs = extract_subtitles(aweme)
                    result["caption"] = "\n".join(subs) if subs else None
                    result["has_real_subtitle"] = bool(subs)
                    # 视频直链：bit_rate[] 每档带码率与自己的 play_addr，play_addr 是默认档。
                    # 全部记下来，供上层"ASR 取最小码率、预览取最大"（2026-10-31 加：
                    # 旧实现只留第一条 URL，长视频做 ASR 时白下几百 MB 高清流）
                    variants: list[dict] = []
                    seen_urls: set[str] = set()

                    def _add_variant(pa, bitrate=None, gear: str = "",
                                     width: int = 0, height: int = 0, source: str = "") -> None:
                        if not isinstance(pa, dict):
                            return
                        for u in (pa.get("url_list") or []):
                            if isinstance(u, str) and u.startswith("http") and u not in seen_urls:
                                seen_urls.add(u)
                                variants.append({"url": u, "bitrate": bitrate, "gear": gear,
                                                 "width": width or 0, "height": height or 0,
                                                 "source": source})
                                return   # 每档只取第一条（同档多 CDN 主机时交给下载端换）

                    for br in (video.get("bit_rate") or []):
                        if isinstance(br, dict):
                            _add_variant(br.get("play_addr"), br.get("bit_rate"),
                                         str(br.get("gear_name") or ""),
                                         int(br.get("width") or 0), int(br.get("height") or 0),
                                         "bit_rate")
                    play = video.get("play_addr") if isinstance(video.get("play_addr"), dict) else None
                    _add_variant(play, None, "default",
                                 int(play.get("width") or 0) if play else 0,
                                 int(play.get("height") or 0) if play else 0,
                                 "play_addr")
                    if variants:
                        result["video_variants"] = variants
                        result["video_url"] = variants[0]["url"]
                    # 封面
                    cover = video.get("cover") or video.get("origin_cover") or {}
                    if isinstance(cover, dict):
                        cl = cover.get("url_list") or []
                        result["cover"] = cl[0] if cl else None
                    # 图集/图文作品：aweme_detail 给的是 images[]，没有视频流
                    imgs = aweme.get("images") or []
                    if isinstance(imgs, list) and imgs:
                        iurls = []
                        for im in imgs:
                            if not isinstance(im, dict):
                                continue
                            for u in (im.get("url_list") or []):
                                if isinstance(u, str) and u.startswith("http"):
                                    iurls.append(u)
                                    break
                        if iurls:
                            result["images"] = iurls[:20]
                    # 音乐
                    mu = aweme.get("music") or {}
                    if isinstance(mu, dict):
                        mup = mu.get("play_url")
                        if isinstance(mup, dict):
                            raw = mup.get("url_list") or mup.get("raw")
                            if isinstance(raw, str):
                                result["audio_url"] = raw
                            elif isinstance(raw, list) and raw:
                                result["audio_url"] = raw[0]
                except Exception:
                    pass

        page.on("response", on_response)
        goto_error = ""
        page_html = ""
        final_url = ""
        try:
            throttle_platform("douyin")
            page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                final_url = page.url or ""
            except Exception:  # noqa: BLE001
                final_url = ""
            # 等待详情 API 返回
            deadline = time.time() + timeout
            while time.time() < deadline and not result.get("__got"):
                page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            goto_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Playwright 访问抖音失败: %s", exc)
            # 尝试用页面标题兜底
            try:
                if not result.get("title"):
                    t = page.title()
                    if t and "抖音" in t:
                        result["title"] = t.replace(" - 抖音", "").strip()
            except Exception:
                pass
    finally:
        # 失败时留一份页面证据，用于区分"真风控 / 内容没了 / 页面没打开"
        try:
            if not result.get("__got"):
                page_html = (page.content() or "")[:200000]
                final_url = final_url or (page.url or "")
        except Exception:  # noqa: BLE001
            page_html = ""
        browser.close(pw, ctx, owned)

    # 提取 id
    vid = _extract_video_id(target)
    if vid:
        result["id"] = vid
    # 图集（图文）作品：网页端拿不到数据 → 给可执行的提示，别落进"页面改版"的模糊错误
    if (not result.get("video_url") and not result.get("images")
            and (is_note_url(target) or is_note_url(final_url))):
        raise RuntimeError(_image_post_message())
    if not result.get("title") and not result.get("video_url"):
        html = page_html or ""
        api_status = result.get("__api_status")
        low_url = (final_url or "").lower()
        # URL 信号比页面文本可靠：抖音页面永远带 captcha SDK 字样，但只有真的
        # 被拦时才会跳到 /verify 或 /passport。
        if not result.get("__got") and any(k in low_url for k in ("/verify", "captcha", "/passport")):
            mark_platform_cooldown("douyin", _RISK_COOLDOWN_SECS, kind="risk",
                                   reason="人机校验/登录墙", egress=eg)
            record_account_result(account_ck, False, "douyin", "人机校验/登录墙")
            logger.warning("抖音跳转到验证/登录页，停手冷却: %s", final_url[:120])
            raise RuntimeError(
                "抖音要求人机验证（滑块/验证码）或登录，已停手并冷却。"
                "请先用普通浏览器打开抖音完成一次验证（或配置 cookies/douyin.txt），"
                "冷却结束后再试；期间反复请求只会加深标记。")
        risk_hit = next((m for m in _DOUYIN_RISK_MARKERS if m in html), "")
        gone_hit = next((m for m in _DOUYIN_GONE_MARKERS if m in html), "")
        if risk_hit or api_status in (401, 403, 429):
            mark_platform_cooldown("douyin", _RISK_COOLDOWN_SECS, kind="risk",
                                   reason=risk_hit or f"HTTP {api_status}", egress=eg)
            record_account_result(account_ck, False, "douyin",
                                  risk_hit or f"HTTP {api_status}")
            logger.warning("抖音风控命中（%s），通道冷却 %d 分钟",
                           risk_hit or f"HTTP {api_status}", _RISK_COOLDOWN_SECS // 60)
            raise RuntimeError(
                f"抖音风控拦截（{risk_hit or 'HTTP ' + str(api_status)}），"
                f"通道已进入 {_RISK_COOLDOWN_SECS // 60} 分钟冷却，到点自动恢复；"
                "期间请求会直接快速失败。建议先提取其他平台。")
        if goto_error and not html.strip():
            # 页面根本没打开 → 网络/超时，**不是风控**，不冷却
            raise RuntimeError(f"访问抖音页面失败（{goto_error[:150]}）。"
                               "这通常是网络或超时问题，稍后重试即可，未触发平台冷却。")
        if gone_hit:
            raise RuntimeError(f"该抖音作品已不可访问（{gone_hit}），无法提取。")
        # 页面打开了、也没命中风控特征：不冷却，交给上层按"未知失败"原样重试一次
        logger.warning("抖音页面已打开但未解析到详情（未命中风控特征，不冷却）")
        raise RuntimeError("抖音页面已打开但未解析到视频详情（可能是页面改版或渲染不完整）。")
    record_account_result(account_ck, True, "douyin")      # 账号健康度（只用于观测）
    return result
