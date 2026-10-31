"""每日请求预算（P1）。

设计原则：
- 预算按"任务"计，不按 HTTP 请求计 —— 用户能理解"今天这个平台还能提 20 条"，
  而内部一次任务到底发几个请求由各平台通道自己控制（见 utils.throttle）。
- 超预算直接拒绝，连浏览器都不打开（比"失败了再重试"省得多）。
- 状态持久化在 cookies/.budget.json，服务重启后仍然生效；按自然日滚动。
- 熔断（连续风控 → 冷却翻倍）复用 utils 的 .cooldown.json，本模块只读状态，
  不重复实现，避免两套真相。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .utils import COOKIE_DIR, platform_cooldown_info

logger = logging.getLogger("risk")

DEFAULT_DAILY_BUDGET = 200
_ENV_ALIAS = {
    "douyin": "DOUYIN",
    "kuaishou": "KUAISHOU",
    "xiaohongshu": "XIAOHONGSHU",
    "bilibili": "BILIBILI",
    "tiktok": "TIKTOK",
    "default": "DEFAULT",
}


def _budget_file() -> Path:
    return COOKIE_DIR / ".budget.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load() -> dict:
    f = _budget_file()
    if not f.is_file():
        return {"date": _today(), "platforms": {}}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bad budget file")
    except Exception:
        return {"date": _today(), "platforms": {}}
    if data.get("date") != _today():
        # 跨天：用量清零；冷却/熔断在 .cooldown.json 里独立延续
        data = {"date": _today(), "platforms": {}}
    data.setdefault("platforms", {})
    return data


def _save(data: dict) -> None:
    try:
        f = _budget_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("预算状态写入失败: %s", exc)


def daily_limit(platform: str) -> int:
    """该平台每天允许的任务数；0 = 不限。

    优先级：TRANSCRIPT_<PLATFORM>_DAILY_BUDGET > TRANSCRIPT_DAILY_BUDGET
            > 平台适配器里的默认值（如抖音 150 / 小红书 100）> 200
    """
    key = (platform or "default").lower()
    alias = _ENV_ALIAS.get(key, key.upper())
    raw = os.getenv(f"TRANSCRIPT_{alias}_DAILY_BUDGET")
    if raw is None:
        raw = os.getenv("TRANSCRIPT_DAILY_BUDGET")
    if raw is None:
        try:
            from .platforms import adapter_by_key

            return max(0, int(adapter_by_key(key).daily_budget or DEFAULT_DAILY_BUDGET))
        except Exception:  # noqa: BLE001
            return DEFAULT_DAILY_BUDGET
    try:
        return max(0, int(raw))
    except Exception:
        return DEFAULT_DAILY_BUDGET


def budget_state(platform: str) -> dict:
    key = (platform or "default").lower()
    used = int((_load().get("platforms", {}).get(key) or {}).get("used") or 0)
    limit = daily_limit(key)
    return {
        "date": _today(),
        "used": used,
        "limit": limit,
        "remaining": (limit - used) if limit > 0 else -1,
        "unlimited": limit <= 0,
    }


def allow(platform: str) -> tuple[bool, dict]:
    """是否还有预算。返回 (allowed, state)。"""
    st = budget_state(platform)
    if st["unlimited"] or st["remaining"] > 0:
        return True, st
    return False, st


def consume(platform: str) -> dict:
    """记一条任务用量（在真正开始抓取前调用）。"""
    key = (platform or "default").lower()
    data = _load()
    entry = data["platforms"].setdefault(key, {})
    entry["used"] = int(entry.get("used") or 0) + 1
    _save(data)
    return budget_state(key)


def reset(platform: str | None = None) -> None:
    data = _load()
    if platform:
        data["platforms"].pop((platform or "").lower(), None)
    else:
        data["platforms"] = {}
    _save(data)


def breaker_state(platform: str) -> dict:
    """熔断状态（读 utils 的冷却记录）：连续命中次数 + 当前冷却等级。"""
    info = platform_cooldown_info(platform)
    return {
        "open": info.get("remaining", 0) > 0,
        "remaining": int(max(0, info.get("remaining") or 0)),
        "strikes": int(info.get("strikes") or 0),
        "level": int(info.get("level") or 0),
        "reason": info.get("reason") or "",
    }


def stats() -> dict:
    """所有平台的预算 + 熔断概览（供 /risk/status）。"""
    data = _load()
    keys = set(data.get("platforms", {}).keys()) | set(_ENV_ALIAS.keys())
    out = {}
    for k in sorted(keys):
        out[k] = {"budget": budget_state(k), "breaker": breaker_state(k)}
    return out
