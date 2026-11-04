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
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from . import browser
from .utils import (account_key, fetch_subtitle_text, http_opener,
                    load_netscape_cookies, mark_platform_cooldown, pick_account,
                    pick_proxy_available, platform_cooldown_enabled,
                    platform_cooldown_remaining, platform_of, platform_serialized,
                    proxy_for, record_account_result, throttle_platform)

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
# 图集（图文）作品 id：/note/<id> —— 取数时改走 /video/<id>，详见 normalize_url
_PATTERN_NOTE = re.compile(r"douyin\.com/note/(\d+)", re.I)

# ---------------- 公开路径优先（2026-11-02 实测后加） ----------------
#
# 实测结论（同一个作品、同一时刻、同一条出口）：
#   · 纯 HTTP 直连接口      → 403 "Blocked by ArgusSecurityPlugin Uifid Not Found"（缺浏览器指纹）
#   · 移动分享页 SSR        → _ROUTER_DATA 里只有页面/AB 配置，**没有任何作品字段**
#   · 真实浏览器 + 全新上下文（Chromium 或 Edge 都行、带 stealth 参数）
#                           → 详情接口 200，标题/描述/封面/多码率直链全拿到，**不需要任何 cookie**
#   · 项目的持久 profile(cookies/_browser_profile_douyin)
#                           → 一个数据字段都没有，页面标题就是四个字「验证码中间页」
# 所以抖音的取数瓶颈是"这个浏览器实例有没有被标记"，不是"有没有号"。
# 本模块因此坚持：公开路径优先（零浏览器分享页 → 单条作品页渲染 → 最后才用账号 cookie），
# 并且把"验证码中间页"识别成 **profile 被标记**（换干净上下文重试），而不是平台风控
# （那会让整个抖音通道白停 30 分钟，还会被上层反复自我升级）。
_SHARE_URL = "https://www.iesdouyin.com/share/video/{aid}/"
_SHARE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
_CAPTCHA_INTERSTITIAL_MARKS = ("验证码中间页", "安全验证", "请完成验证", "滑块验证")


class _CaptchaInterstitial(RuntimeError):
    """抖音返回「验证码中间页」：被标记的是这个浏览器 profile，不是出口 IP。"""


def _polluted_path() -> Path:
    return Path(__file__).resolve().parent.parent / "cookies" / ".douyin_profile_polluted.json"


def profile_polluted(profile_name: str) -> Optional[dict]:
    """该 profile 是否被抖音标记过（记在 cookies/.douyin_profile_polluted.json）。"""
    try:
        data = json.loads(_polluted_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    rec = data.get(profile_name) if isinstance(data, dict) else None
    return rec if isinstance(rec, dict) else None


def _mark_profile_polluted(profile_name: str, reason: str) -> None:
    """记下这个 profile 已被抖音标记：下次直接换干净上下文，不再白等一次验证码页。"""
    p = _polluted_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    data[profile_name] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason[:200]}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("写入抖音 profile 标记失败: %s", exc)


def _is_captcha_interstitial(title: str, html: str) -> str:
    """是不是抖音的「验证码中间页」（标题/正文只有验证码字样、没有任何作品数据）。"""
    t = (title or "").strip()
    for mark in _CAPTCHA_INTERSTITIAL_MARKS:
        if mark in t:
            return mark
    if "验证码中间页" in (html or ""):
        return "验证码中间页"
    return ""


def try_share_page(aid: str, timeout: float = 6.0) -> Optional[dict]:
    """零浏览器快路径：读抖音移动分享页 SSR 里的 _ROUTER_DATA。

    实测（2026-11-02）：这类页面里只有页面配置/AB 参数，没有作品数据，
    所以正常返回 None（成本约 0.4~0.6 秒）。保留它是因为：一旦抖音对 SSR
    或爬虫 UA 放开数据，这条路径就能在完全不碰浏览器的情况下拿到标题/封面/直链，
    风控面为零。命中时返回与 fetch_douyin 同构的 dict。
    """
    aid = (aid or "").strip()
    if not aid.isdigit():
        return None
    try:
        req = urllib.request.Request(_SHARE_URL.format(aid=aid), headers={
            "User-Agent": _SHARE_UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.iesdouyin.com/",
        })
        with http_opener("douyin").open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            html = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        logger.debug("抖音分享页快路径失败（按实测属正常，继续走渲染）: %s", exc)
        return None
    m = re.search(r"_ROUTER_DATA\s*=\s*", html)
    if not m:
        logger.debug("抖音分享页无 _ROUTER_DATA（按实测属正常）")
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except Exception:  # noqa: BLE001
        return None
    item = _find_share_item(obj)
    if not item:
        logger.debug("抖音分享页 _ROUTER_DATA 内无作品数据（实测预期结果）")
        return None
    return _share_item_to_result(item)


def _find_share_item(obj) -> Optional[dict]:
    """在分享页 router data 里找出 item_list[0] / aweme_detail（通常不存在）。"""
    if isinstance(obj, dict):
        for key in ("item_list", "aweme_list"):
            v = obj.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]
        if isinstance(obj.get("aweme_detail"), dict):
            return obj["aweme_detail"]
        for v in obj.values():
            got = _find_share_item(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:8]:
            got = _find_share_item(v)
            if got:
                return got
    return None


def _share_item_to_result(item: dict) -> dict:
    """把分享页里的作品对象整理成 fetch_douyin 的返回结构（与详情接口同构）。"""
    out: dict = {}
    desc = item.get("desc")
    if isinstance(desc, str) and desc.strip():
        out["title"] = desc
        out["desc"] = desc
    author = item.get("author") or {}
    if isinstance(author, dict) and author.get("nickname"):
        out["author"] = author.get("nickname")
    video = item.get("video") or {}
    if isinstance(video, dict):
        if video.get("duration"):
            out["duration"] = video.get("duration")
        variants: list[dict] = []
        for src, tag in ((video.get("play_addr"), "share_page"),
                         (video.get("download_addr"), "share_page_download")):
            if isinstance(src, dict):
                for u in (src.get("url_list") or []):
                    if isinstance(u, str) and u.startswith("http"):
                        variants.append({"url": u, "bitrate": None, "gear": "default",
                                         "width": int(src.get("width") or 0),
                                         "height": int(src.get("height") or 0),
                                         "source": tag})
                        break
        for br in (video.get("bit_rate") or []):
            if isinstance(br, dict) and isinstance(br.get("play_addr"), dict):
                pa = br["play_addr"]
                for u in (pa.get("url_list") or []):
                    if isinstance(u, str) and u.startswith("http"):
                        variants.append({"url": u, "bitrate": br.get("bit_rate"),
                                         "gear": str(br.get("gear_name") or ""),
                                         "width": int(br.get("width") or 0),
                                         "height": int(br.get("height") or 0),
                                         "source": "share_page_bit_rate"})
                        break
        if variants:
            out["video_variants"] = variants
            out["video_url"] = variants[0]["url"]
        cover = video.get("cover") or video.get("origin_cover") or {}
        if isinstance(cover, dict) and (cover.get("url_list") or []):
            out["cover"] = cover["url_list"][0]
    imgs = item.get("images")
    if isinstance(imgs, list) and imgs:
        iurls = []
        for im in imgs:
            if isinstance(im, dict):
                for u in (im.get("url_list") or []):
                    if isinstance(u, str) and u.startswith("http"):
                        iurls.append(u)
                        break
        if iurls:
            out["images"] = iurls[:20]
    out["source"] = "share_page"
    return out


def is_note_url(url: str) -> bool:
    """是不是抖音图集（图文）作品的 /note/<id> 地址。

    实测（2026-10-31）：直接开 /note/<id> 页面不调用详情接口（页面里只有站点框架，
    正文 4 个字符、0 张图）——所以不能拿 /note/ 当渲染入口。
    实测（2026-11-04）：同一作品开 /video/<id> 时抖音会跳回 /note/<id> 并正常请求
    aweme/v1/web/aweme/detail，返回的 aweme_detail 带 desc + images[]。取数入口已在
    normalize_url 里改写成 /video/<id>；本函数只留作"渲染完仍没拿到图片"时的兜底判定。
    """
    return bool(_NOTE_URL_RE.search(url or ""))


def normalize_url(url: str) -> str:
    """把各种抖音链接归一化为标准 /video/<id>，返回规范化后的 URL。

    图集（图文）作品的 /note/<id> **也归一化成 /video/<id>**：实测（2026-11-04，
    匿名、无代理、全新浏览器上下文，2 条图集）——直接开 /note/<id> 页面从不请求
    aweme/v1/web/aweme/detail（页面正文只有站点框架），而开 /video/<id> 时抖音自己
    会跳回 /note/<id> 并**正常请求详情接口**，返回的 aweme_detail 带 desc + images[]
    （5 张 / 1 张），status_code=0。所以改一个入口即可拿到图集数据，不必依赖 App。
    """
    url = (url or "").strip()
    m = _PATTERN_NOTE.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    m = _PATTERN_STD.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    m = _PATTERN_MODAL.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return url  # 短链等交给 Playwright 直接访问（会自动跳转）


def _extract_video_id(url: str) -> Optional[str]:
    for pat in (_PATTERN_STD, _PATTERN_MODAL, _PATTERN_NOTE):
        m = pat.search(url)
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
        return ("这是抖音的图集（图文）作品，本次没能解析到图片数据。可行做法：在抖音 App 里"
                "保存这些图片，"
                f"然后全部丢进监听目录：{watch_dir} —— 本工具会把同一批图片自动合成一条文案。")
    return ("这是抖音的图集（图文）作品，本次没能解析到图片数据。可行做法：在抖音 App 里保存图片，"
            "再用本工具的「图片文案提取（OCR）」上传识别。")


@platform_serialized("douyin")
def fetch_douyin(url: str, timeout: int = 35, use_cookie: bool = False) -> dict:
    """抓取抖音视频详情：**公开路径优先 → 单条渲染 → 最后才动用账号 cookie**。

    顺序（2026-11-02 起）：
      ① 图集（/note/）先改写成 /video/ 再走同一条渲染路径（2026-11-04 实测：直接开
         /note/ 页面从不请求详情接口；开 /video/ 时抖音自己跳回 /note/ 并正常返回
         desc + images[]），所以"图集"不是特例分支，而是同一条路；
      ② 零浏览器快路径：分享页 SSR（实测通常没有数据，成本≈0.5 秒，命中即省掉整个浏览器）；
      ③ 渲染路径：真实 Chromium/Edge 打开**这一条**作品页，拦截
         aweme/v1/web/aweme/detail —— 不滚动、不进推荐流、不调列表接口。
         抖音的风控主要由"批量/列表"类请求触发，单条详情页是风险最低的取数方式。
      ④ 账号 cookie：只有 use_cookie=True（上层判定"匿名失败且原因是缺登录态"）才注入。

    返回 dict 含 title/desc/author/cover/duration/video_variants/video_url 等；失败抛异常。

    实测（2026-11-02，同一作品、同一时刻、同一出口）：全新上下文不需要任何 cookie
    就能拿到全部字段；而项目原来的持久 profile 只返回「验证码中间页」——所以本函数
    把验证码中间页判定为**该 profile 被标记**，自动换干净上下文重试一次，而不是把
    平台/出口判成风控（后者会让整个抖音通道白停 30 分钟并被上层反复自我升级）。
    """
    target = normalize_url(url)

    # 注意：normalize_url 已把 /note/<id> 改写成 /video/<id>（2026-11-04），
    # 所以走到这里的 target 不会是 /note/ —— 图集与视频共用同一条渲染路径，
    # 由 extractor 看 aweme_detail 里有没有 images[] 决定走 OCR 还是 ASR。
    # 旧的 is_note_url 提前报错分支已删除：它会让图集永远拿不到数据（缺陷 D1）。

    # ① 分享页快路径：零浏览器、零风控面。实测多为空，命中就直接返回。
    aid = _extract_video_id(target)
    if aid:
        quick = try_share_page(aid)
        if quick and (quick.get("title") or quick.get("video_url")):
            logger.info("抖音分享页快路径命中（未启动浏览器）: %s", aid)
            return quick

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

    # ② 该 profile 已经被标记过 → 别再花十几秒等一张验证码页，直接用干净上下文。
    persistent = True
    mark = profile_polluted(profile_name)
    if mark and not account_ck:
        logger.warning("抖音 profile(%s) 先前被风控标记（%s @ %s）→ 本次直接用干净上下文",
                       profile_name, mark.get("reason", ""), mark.get("at", ""))
        persistent = False

    def _blocked(msg: str) -> RuntimeError:
        """干净上下文也被拦：这才算真的平台风控（按出口冷却）。"""
        if platform_cooldown_enabled("douyin"):
            mark_platform_cooldown("douyin", _RISK_COOLDOWN_SECS, kind="risk",
                                   reason=msg[:120], egress=eg)
        if account_ck:
            record_account_result(account_ck, False, "douyin", msg[:120])
        logger.warning("抖音干净上下文也被验证码拦下（%s）→ 通道冷却 %d 分钟",
                       msg[:60], _RISK_COOLDOWN_SECS // 60)
        return RuntimeError(
            f"抖音返回验证码中间页（{msg[:60]}），换用干净浏览器上下文重试仍被拦。"
            f"通道已冷却 {_RISK_COOLDOWN_SECS // 60} 分钟；这通常是出口 IP 被限，"
            "建议换出口（TRANSCRIPT_PROXY_DOUYIN）或到点再试。")

    try:
        return _fetch_douyin_once(target, timeout=timeout, account_ck=account_ck,
                                  px=px, eg=eg, profile_name=profile_name,
                                  persistent=persistent)
    except _CaptchaInterstitial as exc:
        if not persistent:
            raise _blocked(str(exc)) from exc
        _mark_profile_polluted(profile_name, str(exc))
        logger.warning("抖音 profile(%s) 撞上验证码中间页（%s）→ 已记下该 profile，"
                       "改用干净上下文重试一次", profile_name, exc)
        try:
            return _fetch_douyin_once(target, timeout=timeout, account_ck=account_ck,
                                      px=px, eg=eg, profile_name=profile_name,
                                      persistent=False)
        except _CaptchaInterstitial as exc2:
            raise _blocked(str(exc2)) from exc2


def _fetch_douyin_once(target: str, *, timeout: int, account_ck, px, eg: str,
                       profile_name: str, persistent: bool = True) -> dict:
    """单次渲染抓取：一次页面加载、一个浏览器上下文。"""
    result: dict = {}
    try:
        pw, ctx, owned = browser.open_profile(
            headless=True,
            profile_dir=browser.profile_dir_for(profile_name) if persistent else None,
            attach_cdp=False, persistent=persistent, user_agent=_UA, proxy=px)
    except Exception as exc:  # noqa: BLE001
        logger.warning("抖音 profile(%s) 启动失败，回退临时上下文: %s", profile_name, exc)
        pw, ctx, owned = browser.open_profile(
            headless=True, persistent=False, attach_cdp=False,
            user_agent=_UA, proxy=px)
    logger.info("抖音通道: profile=%s（%s，%s）", profile_name,
                "持久" if persistent else "干净上下文",
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
        page_title = ""
        try:
            throttle_platform("douyin")
            try:
                # 用 "commit" 而不是 "domcontentloaded"：详情数据来自 XHR，不需要等首屏资源。
                # 实测（2026-11-02，6 条视频）：其中 2 条 20 s 都到不了 domcontentloaded
                # （goto 抛超时），但接口数据其实早就回来了 —— 改成 commit + 轮询 __got，
                # 这两条从 21 s 降到与其它条同为 5 s 量级。
                page.goto(target, wait_until="commit", timeout=min(timeout, 10) * 1000)
            except Exception as exc:  # noqa: BLE001
                goto_error = f"{type(exc).__name__}: {exc}"
                logger.info("抖音首屏 commit 超时/失败（接口拦截不受影响，继续等数据）: %s", exc)
            try:
                final_url = page.url or ""
            except Exception:  # noqa: BLE001
                final_url = ""
            try:
                page_title = (page.title() or "").strip()[:200]
            except Exception:  # noqa: BLE001
                page_title = ""
            # 等待详情 API 返回
            deadline = time.time() + timeout
            while time.time() < deadline and not result.get("__got"):
                page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            goto_error = goto_error or f"{type(exc).__name__}: {exc}"
            logger.warning("Playwright 访问抖音失败: %s", exc)
            # 尝试用页面标题兜底
            try:
                if not result.get("title"):
                    t = page.title() or ""
                    page_title = t.strip()[:200]
                    # 验证码中间页的标题也带"抖音"字样（"验证码中间页"本身不带，
                    # 但不同变体会带）→ 先排除，别让它冒充作品标题、掩盖真风控。
                    if t and "抖音" in t and not _is_captcha_interstitial(page_title, ""):
                        result["title"] = t.replace(" - 抖音", "").strip()
            except Exception:  # noqa: BLE001
                pass
    finally:
        # 失败时留一份页面证据，用于区分"真风控 / 内容没了 / 页面没打开"
        try:
            if not result.get("__got"):
                page_html = (page.content() or "")[:200000]
                final_url = final_url or (page.url or "")
                try:
                    page_title = page_title or (page.title() or "").strip()[:200]
                except Exception:  # noqa: BLE001
                    pass
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
        # 验证码中间页：URL 正常、标题就是「验证码中间页」、正文≈4 个字、一条数据都没有。
        # 实测（2026-11-02）这是**这个浏览器 profile 被标记**，不是出口 IP 被限 —— 交给
        # 上层换干净上下文重试一次，而不是把整个抖音通道冷却 30 分钟
        # （那正是原实现"冷却自我升级"的入口之一）。
        interstitial = _is_captcha_interstitial(page_title, html)
        if interstitial:
            logger.warning("抖音验证码中间页（%s）→ 该 profile 疑似被标记: title=%r url=%s",
                           interstitial, page_title, (final_url or "")[:100])
            raise _CaptchaInterstitial(
                f"{interstitial}｜页面标题: {page_title or '(空)'}")
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
