# -*- coding: utf-8 -*-
"""抖音一级评论采集（访客态，无需账号/代理/签名）。

2026-11-04 实测结论（本机家宽出口、零 cookie）：
- 纯 Python 直接打 /aweme/v1/web/comment/list/ 只回**空 body**（TLS/JS 指纹被挡），
  即使把浏览器里拿到的全套 cookie 抄过去也一样 —— 所以必须借页面上下文发请求；
- 在页面里 fetch 时 **a_bogus 不是必需的**（带不带都返回 200、status_code=0），
  关键是同源 + 浏览器指纹 + 页面自己种下的 ttwid/UIFID；
- count 可以自己定（5/20/50 都返回对应条数），cursor 随便翻 —— 只给 count=20 时
  一次就能拿 20 条，比"滚动加载"快得多（滚动一次只有 5 条）。

因此本模块只做一件事：起一个干净上下文打开作品页（顺便把 cookie 种上），
然后在页面里按 cursor 翻页取评论，不滚页面、不点按钮、不注入签名脚本。
已知边界：只做一级评论（回复需登录/更高风控），与项目原有能力边界一致。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from . import browser
from .douyin import _UA, _extract_video_id, _is_captcha_interstitial, normalize_url

logger = logging.getLogger("douyin_comments")

# 页面里发请求：同源、带 cookie，返回精简结构（别把整包 60KB 搬回 Python 再筛）
_JS_FETCH = """
async ([api, awemeId, cursor, count]) => {
  const q = new URLSearchParams({
    device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
    aweme_id: awemeId, cursor: String(cursor), count: String(count), item_type: '0'
  });
  const r = await fetch(api + '?' + q.toString(), {credentials: 'include'});
  const t = await r.text();
  let j = null;
  try { j = JSON.parse(t); } catch (e) { return {http: r.status, len: t.length, parse: false}; }
  const list = (j.comments || []).map(c => {
    const u = c.user || {};
    return {
      cid: c.cid, text: c.text || '', digg: c.digg_count || 0,
      ctime: c.create_time || 0, ip: c.ip_label || '',
      nickname: u.nickname || '', uid: u.uid || '',
      replyCount: c.reply_comment_total || 0, hot: !!c.is_hot
    };
  });
  return {
    http: r.status, len: t.length, parse: true,
    sc: j.status_code === undefined ? 0 : j.status_code,
    msg: j.status_msg || '', cursor: j.cursor || 0,
    more: !!j.has_more, total: j.total || 0, comments: list
  };
}
"""

_COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"


class CommentError(RuntimeError):
    """评论采集失败（带可读原因，上层直接展示）。"""


def _wait_ttwid(ctx, timeout: float = 15.0) -> bool:
    """等页面把 ttwid 种下来再发接口 —— 没有它评论接口只会回空 body。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            names = {c.get("name") for c in ctx.cookies()}
        except Exception:  # noqa: BLE001
            names = set()
        if "ttwid" in names:
            return True
        time.sleep(0.4)
    return False


def fetch_comments(url: str, max_count: int = 100, page_size: int = 20,
                   timeout: float = 90.0) -> dict:
    """按作品链接取一级评论（访客态）。

    max_count: 最多取多少条；page_size: 每次请求条数（实测 50 也能一次给满）。
    返回 {awemeId, url, total, fetched, pages, hasMore, elapsed, comments[...]}。
    """
    target = normalize_url(url)
    aweme_id = _extract_video_id(target)
    if not aweme_id:
        raise CommentError("没能从链接里认出抖音作品 ID：%s" % url)
    max_count = max(1, min(2000, int(max_count or 100)))
    page_size = max(1, min(50, int(page_size or 20)))

    started = time.time()
    pw, ctx, owned = browser.open_profile(
        headless=True, persistent=False, attach_cdp=False, user_agent=_UA)
    try:
        page = ctx.new_page()
        page.goto("https://www.douyin.com/video/%s" % aweme_id,
                  wait_until="domcontentloaded", timeout=45000)
        title = ""
        try:
            title = page.title() or ""
        except Exception:  # noqa: BLE001
            pass
        if _is_captcha_interstitial(title, ""):
            raise CommentError("抖音返回验证码中间页（%s）——这条通道先停手，过段时间或换出口再试"
                               % title[:40])
        if not _wait_ttwid(ctx):
            logger.warning("抖音评论：等待 ttwid 超时，仍尝试发一次请求")

        comments: list[dict] = []
        cursor = 0
        pages = 0
        total = 0
        has_more = True
        while has_more and len(comments) < max_count and pages < 40:
            try:
                res = page.evaluate(_JS_FETCH, [_COMMENT_API, aweme_id, cursor, page_size])
            except Exception as exc:  # noqa: BLE001  页面被关/导航走了
                raise CommentError("页面内请求评论失败：%s" % exc) from exc
            if not res or not res.get("parse"):
                raise CommentError("评论接口返回了非 JSON（http=%s, %s 字节）——通常是被风控了"
                                   % (res.get("http") if res else "?", res.get("len") if res else "?"))
            if res.get("sc") not in (0, None):
                raise CommentError("评论接口 status_code=%s：%s"
                                   % (res.get("sc"), res.get("msg") or "平台拒绝"))
            batch = res.get("comments") or []
            total = total or int(res.get("total") or 0)
            pages += 1
            if not batch:
                break
            comments.extend(batch)
            has_more = bool(res.get("more"))
            nxt = int(res.get("cursor") or 0)
            if nxt <= cursor:      # 服务端没推进 cursor：别原地打转
                break
            cursor = nxt
            time.sleep(0.35)       # 人工节奏，别把评论接口当压测
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        if owned:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

    comments = comments[:max_count]
    elapsed = round(time.time() - started, 1)
    logger.info("抖音评论 %s：%d 条 / %d 页 / %s 秒（匿名）",
                aweme_id, len(comments), pages, elapsed)
    return {
        "awemeId": aweme_id,
        "url": "https://www.douyin.com/video/%s" % aweme_id,
        "total": total,
        "fetched": len(comments),
        "pages": pages,
        "hasMore": bool(has_more),
        "elapsed": elapsed,
        "comments": comments,
    }
