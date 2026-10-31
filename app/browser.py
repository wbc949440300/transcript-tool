"""浏览器会话管理：优先接管已打开的登录窗口，否则用已登录 profile 起无头实例。

供图文笔记取图、抖音抓取等需要真实渲染的场景使用。
- 小红书沿用 `cookies/_browser_profile`（与 login_cookie.py 共用登录态）
- 抖音用独立 profile（`cookies/_browser_profile_douyin`）：同一个账号始终是
  "同一台机器、同一个浏览器"，比每次全新无痕实例更像真人（P1 指纹一致性）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = Path(os.getenv("TRANSCRIPT_BROWSER_PROFILE", str(ROOT / "cookies" / "_browser_profile")))
CDP_PORT = int(os.getenv("TRANSCRIPT_LOGIN_CDP_PORT", "9333"))

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]


def profile_dir_for(name: str) -> Path:
    """按用途取 profile 目录（可用 TRANSCRIPT_BROWSER_PROFILE_<NAME> 覆盖）。"""
    key = (name or "default").upper()
    return Path(os.getenv(f"TRANSCRIPT_BROWSER_PROFILE_{key}",
                          str(ROOT / "cookies" / f"_browser_profile_{name}")))


def cdp_alive(port: int = CDP_PORT) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def open_profile(headless: bool = True, profile_dir=None, *,
                 attach_cdp: bool = True, persistent: bool = True,
                 user_agent: str | None = None, proxy: str | None = None,
                 locale: str = "zh-CN", timezone_id: str = "Asia/Shanghai",
                 viewport: dict | None = None, stealth: bool = True):
    """打开浏览器，返回 (playwright, context, owned)。

    owned=True 表示 context 是我们启动的，调用方用 close(pw, ctx, owned) 收尾。
    persistent=True 时使用磁盘 profile（登录态与指纹都持久）；
    传 persistent=False 可得到一次性上下文（profile 被占用/损坏时的回退路径）。
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    if attach_cdp and persistent and cdp_alive(CDP_PORT):
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            return pw, ctx, False
        except Exception:
            pass

    args = list(_STEALTH_ARGS) if stealth else []
    ctx_kwargs: dict = {
        "locale": locale,
        "timezone_id": timezone_id,
        "viewport": viewport or {"width": 1280, "height": 900},
    }
    if user_agent:
        ctx_kwargs["user_agent"] = user_agent
    if proxy:
        ctx_kwargs["proxy"] = {"server": proxy}

    if persistent:
        kwargs = dict(ctx_kwargs)
        kwargs.update({
            "user_data_dir": str(profile_dir or PROFILE_DIR),
            "headless": headless,
            "args": args,
        })
        if any(Path(p).exists() for p in EDGE_PATHS):
            kwargs["channel"] = "msedge"
        ctx = pw.chromium.launch_persistent_context(**kwargs)
        return pw, ctx, True

    browser = pw.chromium.launch(headless=headless, args=args)
    ctx = browser.new_context(**ctx_kwargs)
    return pw, ctx, True


def close(pw, ctx, owned: bool) -> None:
    """owned=True 时关闭 context（自己启动的），否则只断开连接。"""
    try:
        if owned:
            ctx.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
