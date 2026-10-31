"""平台适配器注册表（P2）。

目的：把"每个平台怎么抓、要不要登录、几次请求、冷却多久、每天几条、要不要浏览器
指纹"集中声明在一处。主流程只问注册表，不再到处写 if douyin / if kuaishou。

注意：这是**渐进式**适配器 —— 专用通道的抓取实现仍留在 douyin.py / kuaishou.py，
这里只做声明与分发；等以后迁 Go 插件时，这些声明就是插件清单的雏形。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import douyin, kuaishou


@dataclass(frozen=True)
class PlatformAdapter:
    key: str                      # 平台 key（与 utils.platform_of 一致）
    label: str                    # 中文名
    hosts: tuple[str, ...]        # 域名特征
    run_kind: str = "generic"     # 任务执行方式：douyin / kuaishou / generic
    fetch: Optional[Callable] = None   # 专用抓取入口（声明用，便于未来插件化）
    needs_login: bool = False     # 是否可能用到登录态（决定是否允许 cookie 升级）
    supports_images: bool = False # 是否支持图文/图集作品
    fingerprint: str = "none"     # "profile" = 用持久浏览器 profile
    frame_ocr_default: bool = False  # 平台通常没有字幕文件 → 默认开画面文字 OCR
    cooldown_base: int = 1800     # 首次风控冷却秒数（之后按 strikes 翻倍）
    daily_budget: int = 0         # 0 = 用 TRANSCRIPT_DAILY_BUDGET


ADAPTERS: tuple[PlatformAdapter, ...] = (
    PlatformAdapter(
        key="douyin", label="抖音",
        hosts=("douyin.com", "iesdouyin.com"),
        run_kind="douyin", fetch=douyin.fetch_douyin,
        needs_login=True, supports_images=True, fingerprint="profile",
        frame_ocr_default=True,
        cooldown_base=1800, daily_budget=150,
    ),
    PlatformAdapter(
        key="kuaishou", label="快手",
        hosts=("kuaishou.com", "gifshow.com"),
        run_kind="kuaishou", fetch=kuaishou.fetch_kuaishou,
        needs_login=False, supports_images=True, fingerprint="none",
        frame_ocr_default=True,
        cooldown_base=900, daily_budget=200,
    ),
    PlatformAdapter(
        key="xiaohongshu", label="小红书",
        hosts=("xiaohongshu.com", "xhslink.com"),
        needs_login=True, supports_images=True, fingerprint="profile",
        frame_ocr_default=True,
        cooldown_base=900, daily_budget=100,
    ),
    PlatformAdapter(
        key="bilibili", label="B站",
        hosts=("bilibili.com", "b23.tv"),
        needs_login=True,
        cooldown_base=900, daily_budget=200,
    ),
    PlatformAdapter(
        key="tiktok", label="TikTok",
        hosts=("tiktok.com",),
        needs_login=True,
        cooldown_base=900, daily_budget=100,
    ),
)

GENERIC = PlatformAdapter(
    key="default", label="通用",
    hosts=(),
    cooldown_base=1800, daily_budget=200,
)

_BY_KEY = {a.key: a for a in ADAPTERS}


def adapter_for(url: str) -> PlatformAdapter:
    """按 URL 找平台适配器；不认识就返回通用适配器。"""
    low = (url or "").lower()
    for a in ADAPTERS:
        if any(h in low for h in a.hosts):
            return a
    return GENERIC


def adapter_by_key(key: str) -> PlatformAdapter:
    return _BY_KEY.get((key or "").lower(), GENERIC)


def frame_ocr_default_for(url: str) -> bool:
    """该 URL 是否默认开启画面文字 OCR。

    没有平台字幕文件的平台（抖音/快手/小红书）默认开：实测画面里的硬字幕
    比 ASR 准得多（用户实测："肥水不流外人田"被 ASR 听成"非说也没有留到外人天地里去"）。
    B站/TikTok 等可能有 CC 字幕的，保持默认关，避免白等一次抽帧 OCR。
    """
    return bool(adapter_for(url).frame_ocr_default)


def all_adapters() -> tuple[PlatformAdapter, ...]:
    return ADAPTERS + (GENERIC,)
