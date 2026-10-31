"""小红书图文笔记：用已登录浏览器渲染笔记页，取标题/正文/图片，再交给 OCR。

背景：小红书图文笔记没有视频流，yt-dlp 会报 "No video formats found"；
图片列表是客户端异步加载的（SSR 的 __INITIAL_STATE__ 里没有），所以必须真实渲染。
"""
from __future__ import annotations

import logging
import re
import time

from . import browser
from .utils import proxy_for

logger = logging.getLogger("xiaohongshu")

_NOTE_ID = re.compile(r"/explore/([0-9a-f]{24})|/discovery/item/([0-9a-f]{24})")


def is_xhs(url: str) -> bool:
    low = (url or "").lower()
    return "xiaohongshu.com" in low or "xhslink.com" in low


def note_id(url: str) -> str:
    m = _NOTE_ID.search(url or "")
    return (m.group(1) or m.group(2)) if m else ""


def _collect(page) -> dict:
    """从渲染后的页面收集标题、正文、图片 URL。

    图片范围是关键：笔记页除了正文图，还挂着"相关推荐"的封面（实测同一个 img
    选择器能抓到 40+ 张，其中正文图只有轮播里的那几张）。旧实现在整页扫，结果
    OCR 出来的文案里混进了别的笔记内容（2026-10-31 实测踩坑）。
    """
    return page.evaluate(
        """() => {
            const pick = (sels) => {
                for (const s of sels) {
                    const el = document.querySelector(s);
                    const t = el && (el.innerText || el.textContent || '').trim();
                    if (t) return t;
                }
                return '';
            };
            let title = pick(['#detail-title', '.note-content .title', '.title', 'h1']);
            if (!title) {
                // document.title 形如 "标题 - 小红书"
                title = (document.title || '').split(' - ')[0].trim();
            }
            const desc = pick(['#detail-desc', '.note-content .desc', '.desc', '.note-text']);

            const srcOf = (img) => (img.currentSrc || img.src || '');
            const want = (src) => /sns-webpic|sns-img/.test(src) && !/avatar|fe-static|emoji/i.test(src);
            const images = [];
            const push = (u) => {
                if (!u) return;
                if (/^https?:/i.test(u) && !images.includes(u)) images.push(u);
            };

            // 1) 正文图：轮播/swiper 容器内（页面其它位置的图基本是推荐流封面）
            document.querySelectorAll(
                '.swiper-slide img, [class*=swiper] img, [class*=slider] img, [class*=carousel] img'
            ).forEach(img => { const s = srcOf(img); if (want(s)) push(s); });

            // 2) 兜底：整页扫描，但排除推荐流（a.cover / feed / recommend）
            if (!images.length) {
                document.querySelectorAll('img').forEach(img => {
                    const s = srcOf(img);
                    if (!want(s)) return;
                    if (img.closest('a.cover, [class*=feed], [class*=recommend]')) return;
                    push(s);
                });
            }

            // 3) 兜底：轮播容器里的背景图
            if (!images.length) {
                document.querySelectorAll(
                    '[class*=swiper] [style*="background-image"], [class*=slider] [style*="background-image"]'
                ).forEach(el => {
                    const m = (el.getAttribute('style') || '').match(/url\\(["']?(https?:[^"')]+)/);
                    if (m && want(m[1])) push(m[1]);
                });
            }
            return { title: title, desc: desc, images: images };
        }"""
    )


def extract_note(url: str, timeout_ms: int = 45000) -> dict | None:
    """渲染笔记页并返回 {title, desc, images, noteId}；失败返回 None。"""
    try:
        pw, ctx, owned = browser.open_profile(headless=True, proxy=proxy_for("xiaohongshu"))
    except Exception as exc:
        logger.warning("浏览器启动失败: %s", exc)
        return None

    page = None
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # 302 到 404 说明笔记不可访问（多数情况是链接缺 xsec_token 或笔记已删除）
        if "/404" in (page.url or ""):
            logger.warning("笔记不可访问（可能缺少 xsec_token 或已删除）: %s", page.url[:120])
            return None
        # 等图片懒加载
        for _ in range(3):
            page.evaluate("window.scrollBy(0, 900)")
            time.sleep(1.2)
        time.sleep(1.5)
        data = _collect(page)
        data["noteId"] = note_id(url)
        logger.info("图文笔记取到 %d 张图片（%s）", len(data.get("images") or []), data.get("title", "")[:20])
        return data
    except Exception as exc:
        logger.warning("渲染笔记失败: %s", exc)
        return None
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        browser.close(pw, ctx, owned)
