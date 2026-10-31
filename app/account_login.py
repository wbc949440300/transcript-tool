"""平台账号登录（本机桌面用）：看状态 / 启动登录 / 验证登录态。

设计边界（重要）：
- **必须人工**：拉起 login_cookie.py 后要在浏览器里扫码或输密码 —— 这里不做、也不该做"自动登录"
- **必须本机**：登录会弹浏览器窗口，所以只在"有桌面的本机"上有意义（因此不设计导入凭据的通道）
- **命名规则与抓取端共用**（`login_cookie._profile_for` ↔ `browser.profile_dir_for`），
  避免重演"登录用 A profile、抓取读 B profile → 登录态白登"那个坑
- 只读本地文件判断状态；真要验证登录态是否还有效，走 `check_login()`（会开一次无头浏览器）
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .utils import COOKIE_DIR, account_cooldown_remaining, list_accounts

logger = logging.getLogger("account_login")

ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT_RE = re.compile(r"^account_[0-9]{1,2}$")
PLATFORMS: tuple[tuple[str, str], ...] = (
    ("douyin", "抖音"), ("kuaishou", "快手"), ("xiaohongshu", "小红书"), ("bilibili", "B站"),
)
_LOCK = threading.Lock()
_RUNNING: dict[str, float] = {}          # "platform/account" → 启动时间戳


def _login_timeout() -> int:
    try:
        return max(60, int(os.getenv("TRANSCRIPT_LOGIN_TIMEOUT", "300") or 300))
    except Exception:  # noqa: BLE001
        return 300


def _no_window_flags() -> dict:
    """拉起登录脚本时的进程参数：隐藏控制台（浏览器窗口照常出现），POSIX 下新开会话。"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _profile_dir(platform: str, account: str) -> Path:
    """与 login_cookie.py 完全一致的 profile 目录（单一事实来源）。"""
    try:
        from login_cookie import _profile_for

        return _profile_for(platform, account)
    except Exception:  # noqa: BLE001
        from .browser import profile_dir_for

        return profile_dir_for(f"{platform}_{account}" if account else platform)


def _cookie_path(platform: str, account: str) -> Path:
    return (COOKIE_DIR / platform / f"{account}.txt") if account else (COOKIE_DIR / f"{platform}.txt")


def _row(platform: str, account: str) -> dict:
    path = _cookie_path(platform, account)
    prof = _profile_dir(platform, account)
    row: dict = {
        "account": account or "（默认）",
        "file": (path.relative_to(COOKIE_DIR).as_posix()
                 if path.is_relative_to(COOKIE_DIR) else path.name),
        "logged_in": path.is_file() and path.stat().st_size > 0,
        "profile": prof.name,
        "profile_exists": prof.is_dir(),
        "cooldown": int(max(0.0, account_cooldown_remaining(str(path)))),
    }
    if row["logged_in"]:
        st = path.stat()
        row["size"] = st.st_size
        row["logged_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        row["days_ago"] = int((time.time() - st.st_mtime) // 86400)
    key = f"{platform}/{account or '-'}"
    started = _RUNNING.get(key)
    row["logging_in"] = bool(started and time.time() - started < _login_timeout())
    return row


def accounts_status() -> dict:
    """各平台账号状态（纯读本地文件，不联网、不开浏览器）。"""
    out: dict = {}
    for key, label in PLATFORMS:
        rows = [_row(key, "")]
        for f in list_accounts(key):                 # cookies/<平台>/account_*.txt
            rows.append(_row(key, Path(f).stem))
        used = [r for r in rows if r["logged_in"]]
        out[key] = {
            "label": label,
            "accounts": rows,
            "logged_in_count": len(used),
            "note": "匿名即可，无需登录" if key == "kuaishou" else "",
        }
    return out


def _validate(platform: str, account: str) -> tuple[bool, str]:
    if platform not in [p for p, _ in PLATFORMS]:
        return False, f"不支持的平台：{platform}"
    if account and not _ACCOUNT_RE.match(account):
        return False, "账号标签格式不对（应为 account_1、account_2 这种）"
    return True, ""


def start_login(platform: str, account: str = "") -> dict:
    """拉起登录脚本（会弹出浏览器窗口）。同一账号不会重复启动。"""
    ok, err = _validate(platform, account)
    if not ok:
        return {"started": False, "error": err}
    key = f"{platform}/{account or '-'}"
    with _LOCK:
        started = _RUNNING.get(key)
        if started and time.time() - started < _login_timeout():
            return {"started": False, "already": True,
                    "error": "这个账号的登录窗口已经开着，请先在浏览器里完成登录"}
        _RUNNING[key] = time.time()
    args = [sys.executable, str(ROOT / "login_cookie.py"), platform,
            "--timeout", str(_login_timeout())]
    if account:
        args += ["--account", account]
    try:
        subprocess.Popen(args, cwd=str(ROOT), **_no_window_flags())
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _RUNNING.pop(key, None)
        return {"started": False, "error": f"拉起登录脚本失败：{exc}"}
    return {
        "started": True,
        "profile": str(_profile_dir(platform, account)),
        "output": str(_cookie_path(platform, account)),
        "hint": f"浏览器窗口已打开：登录完成后本页会显示「已登录」（最多等 {_login_timeout() // 60} 分钟）。"
                "登录态会写进与抓取端**同一个** profile，不会白登。",
    }


def check_login(platform: str, account: str = "") -> dict:
    """验证登录态是否还有效（跑 login_cookie.py --check：无窗口，会刷新 cookie）。"""
    ok, err = _validate(platform, account)
    if not ok:
        return {"ok": False, "error": err}
    args = [sys.executable, str(ROOT / "login_cookie.py"), platform, "--check"]
    if account:
        args += ["--account", account]
    try:
        r = subprocess.run(args, cwd=str(ROOT), capture_output=True,
                           timeout=int(os.getenv("TRANSCRIPT_LOGIN_CHECK_TIMEOUT", "120")))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "验证超时（浏览器启动较慢时可能发生，可稍后再试）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"验证失败：{exc}"}
    out = (r.stdout or b"").decode("utf-8", "ignore")
    err_out = (r.stderr or b"").decode("utf-8", "ignore")
    tail = "\n".join((out + "\n" + err_out).strip().splitlines()[-12:])
    return {"ok": r.returncode == 0, "rc": r.returncode, "output": tail[:1200]}


def open_cookies_dir() -> str:
    """返回 cookies 目录路径（由调用方负责打开）。"""
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    return str(COOKIE_DIR)