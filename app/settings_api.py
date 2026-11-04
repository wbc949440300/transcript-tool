"""可视化配置：把常用 .env 项开放到前端，改完**立即生效**（不需要重启）。

设计要点
--------
1. **白名单**：只有本文件 `SCHEMA` 里列出的键可读可写。LLM key、Server酱 key
   这类敏感项**绝不**出现在这里，也不会有任何通用"读任意环境变量"的接口。
2. **立即生效**：代码里读配置都是调用时 `os.getenv(...)`，所以只改 `os.environ`
   就能生效；同时回写 `.env`，重启后依然保留。
3. **不破坏 .env**：只替换/追加白名单那几行，注释和其它配置原样保留。
   行内注释（`KEY=值   # 说明`）也会被保留。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# (分组, 键, 标签, 提示, 类型)
# 类型：int / enum:a,b,c / text / proxy / proxy_list
# 刻意只暴露"需要人改"的两类：
#   1) 出口代理 —— 换代理是日常操作，必须能改
#   2) Cookie 策略 —— 排查风控时要在 fallback / always 之间切
# 每日预算、每账号上限、节流毫秒、冷却闸门等**一律走 .env 默认值**，
# 不放到前端：这些是安全网参数，误改会直接放大风控风险（2026-11-03 按需求收敛）。
SCHEMA: list[tuple[str, str, str, str, str]] = [
    ("出口代理", "TRANSCRIPT_PROXY_POOL_DOUYIN", "抖音出口池",
     "一条一行；配几条就能同时挂几个号。留空=直连", "proxy_list"),
    ("出口代理", "TRANSCRIPT_PROXY_POOL", "全局出口池",
     "其它平台共享的池（抖音有独立池时不会用到这个）", "proxy_list"),
    ("出口代理", "TRANSCRIPT_PROXY_POOL_KUAISHOU", "快手出口池",
     "留空=用全局池", "proxy_list"),

    ("账号", "TRANSCRIPT_COOKIE_MODE", "Cookie 策略",
     "fallback=匿名优先（推荐）；always=每次都带号；off=从不带号", "enum:off,fallback,always"),
]

_BY_KEY = {row[1]: row for row in SCHEMA}


def _current(key: str) -> str:
    return (os.getenv(key, "") or "").strip()


def _validate(key: str, value: str) -> tuple[bool, str]:
    """按类型校验。返回 (是否合法, 错误说明)。"""
    kind = _BY_KEY[key][4]
    v = (value or "").strip()
    if kind == "int":
        if v == "":
            return True, ""
        if not re.fullmatch(r"\d{1,9}", v):
            return False, "只能填数字（留空=用默认值）"
        return True, ""
    if kind.startswith("enum:"):
        allowed = kind.split(":", 1)[1].split(",")
        if v == "" or v not in allowed:
            return False, "只能是：" + " / ".join(allowed)
        return True, ""
    if kind == "proxy_list":
        if not v:
            return True, ""
        items = [x for x in re.split(r"[,\s]+", v) if x]
        for it in items:
            ok, why = _validate_proxy(it)
            if not ok:
                return False, f"「{it}」{why}"
        return True, ""
    if kind == "proxy":
        if not v:
            return True, ""
        return _validate_proxy(v)
    return True, ""  # text


def _validate_proxy(item: str) -> tuple[bool, str]:
    """代理写法：host:port / http://host:port / socks5://host:port

    也支持带认证的 user:pass@host:port —— 住宅代理基本都是这种，
    之前这条正则把它们漏了（2026-11-03 修）。
    """
    s = item.strip()
    if not re.fullmatch(
            r"(?:(?:https?|socks5h?|socks4)://)?"
            r"(?:[^\s:@/]+:[^\s@/]*@)?"      # 可选：user:pass@
            r"[^\s/:@]+:\d{1,5}", s):
        return False, "格式应为 host:port，可带认证 user:pass@host:port"
    return True, ""


def read_settings() -> list[dict[str, Any]]:
    """按分组返回全部白名单项的当前值（取自进程环境 = 实际生效值）。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for group, key, label, hint, kind in SCHEMA:
        groups.setdefault(group, []).append({
            "key": key, "label": label, "hint": hint, "kind": kind,
            "value": _current(key),
            "env": True,
        })
    return [{"group": g, "items": items} for g, items in groups.items()]


def _rewrite_env(updates: dict[str, str]) -> None:
    """把白名单项写回 .env：替换已有行（保留行内注释），缺失的追加到末尾。"""
    lines: list[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", ln)
        key = m.group(1) if m else None
        if key and key in updates:
            seen.add(key)
            # 保留 `KEY=值   # 说明` 里的行内注释
            comment = ""
            cut = ln.find("  #")
            if cut == -1:
                cut = ln.find("\t#")
            if cut > 0:
                comment = ln[cut:]
            out.append(f"{key}={updates[key]}{comment}")
        else:
            out.append(ln)

    missing = [k for k in updates if k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# ── 由设置页写入（2026-11-03）─────────────────────────────")
        for k in missing:
            out.append(f"{k}={updates[k]}")

    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    """校验 + 立即生效 + 持久化。返回 {applied: [...], errors: {key: why}}。"""
    applied: list[str] = []
    errors: dict[str, str] = {}
    clean: dict[str, str] = {}

    for key, raw in (values or {}).items():
        if key not in _BY_KEY:
            continue  # 白名单之外直接忽略，不做任何写入
        val = str(raw if raw is not None else "").strip().replace("\r", "").replace("\n", ",")
        ok, why = _validate(key, val)
        if not ok:
            errors[key] = why
            continue
        clean[key] = val

    if clean:
        # 1) 立即生效
        for key, val in clean.items():
            if val:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        # 2) 持久化
        try:
            _rewrite_env(clean)
        except Exception as exc:  # noqa: BLE001
            return {"applied": applied, "errors": errors,
                    "warn": f"已生效但写回 .env 失败（重启后会丢）：{exc}"}
        applied = sorted(clean)

    return {"applied": applied, "errors": errors}
