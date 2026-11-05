"""平台账号登录（本机桌面用）：看状态 / 启动登录 / 验证登录态 / 退出登录。

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
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .utils import COOKIE_DIR, account_cooldown_remaining, list_accounts

logger = logging.getLogger("account_login")

ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT_RE = re.compile(r"^account_[0-9]{1,2}$")
# 顺序即账号页展示顺序：视频号扫码授权放第一（「贴链接直接下载」要用它换凭证，先看到它）
PLATFORMS: tuple[tuple[str, str], ...] = (
    ("yuanbao", "视频号"),
    ("douyin", "抖音"), ("kuaishou", "快手"), ("xiaohongshu", "小红书"), ("bilibili", "B站"),
)
_LOCK = threading.Lock()
_RUNNING: dict[str, float] = {}          # "platform/account" → 启动时间戳
_PROCS: dict[str, object] = {}           # 同上 → 登录子进程（用于发现"窗口被关掉"）


def _reap(key: str) -> None:
    """登录子进程已经退出 → 立刻清掉"登录中"状态。

    2026-11-03 修：此前 `_RUNNING` 只在**启动失败**时清理，进程正常结束
    （典型场景：用户直接把弹出的浏览器窗口关掉、没登录）后状态一直挂着，
    界面要等满 5 分钟超时才恢复。现在按子进程是否已退出来判定。
    """
    with _LOCK:
        proc = _PROCS.get(key)
        started = _RUNNING.get(key)
        if proc is None and started is None:
            return
        dead = False
        if proc is not None:
            try:
                dead = proc.poll() is not None   # 进程已结束
            except Exception:  # noqa: BLE001
                dead = True
        if not dead and started and time.time() - started >= _login_timeout():
            dead = True                          # 兜底：超时（进程可能卡住）
        if dead:
            _PROCS.pop(key, None)
            _RUNNING.pop(key, None)


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


def _cookie_file_usable(path: Path) -> bool:
    """cookie 文件存在、有内容，且至少有一条未过期的 cookie。

    此前只看 `st_size > 0`：一个半年前导出的、cookie 全部过期的文件，
    状态页也会显示"已登录"（2026-11-02 实测：状态与 /login/check 结论相反）。
    """
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        now = time.time()
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            try:
                exp = int(float(parts[4]))
            except Exception:  # noqa: BLE001
                continue
            if exp == 0 or exp > now:      # 0 = 会话 cookie
                return True
        return False
    except Exception:  # noqa: BLE001
        try:
            return path.is_file() and path.stat().st_size > 0
        except Exception:  # noqa: BLE001
            return False


def _cookie_expiries(path: Path) -> dict:
    """Netscape cookie 文件里的过期时间（name → unix 时间戳；会话 cookie 记 0）。

    ★ 2026-11-05：用户质疑界面上「长期有效」说得太满（hy_token 实际 60 天就到期）。
    与其在文案里写猜的数字，不如直接从 cookie 文件读真实过期时间并显示。
    """
    out: dict = {}
    try:
        for line in path.read_text("utf-8", "replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            f = s.split("\t")
            if len(f) < 7:
                continue
            try:
                exp = int(f[4])
            except ValueError:
                exp = 0
            out[f[5]] = exp
    except Exception:  # noqa: BLE001
        return {}
    return out


def _row(platform: str, account: str) -> dict:
    path = _cookie_path(platform, account)
    prof = _profile_dir(platform, account)
    row: dict = {
        "account": account or "（默认）",
        "file": (path.relative_to(COOKIE_DIR).as_posix()
                 if path.is_relative_to(COOKIE_DIR) else path.name),
        "logged_in": _cookie_file_usable(path),
        "profile": prof.name,
        "profile_exists": prof.is_dir(),
        "cooldown": int(max(0.0, account_cooldown_remaining(str(path)))),
    }
    if row["logged_in"]:
        st = path.stat()
        row["size"] = st.st_size
        row["logged_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        age = time.time() - st.st_mtime
        row["days_ago"] = int(age // 86400)
        # 登录凭证的真实过期时间：元宝看 hy_token/hy_user（≈60 天），其它平台取最早的一个。
        expiries = _cookie_expiries(path)
        names = ("hy_token", "hy_user") if platform == "yuanbao" else ()
        now = time.time()
        live = [v for k, v in expiries.items() if v > now and (not names or k in names)]
        if live:
            ts = min(live)
            row["expires_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            row["days_left"] = int((ts - now) // 86400)
            row["expiring"] = row["days_left"] <= 7
        # 友好相对时间（2026-11-03）：1 小时内按分钟、1 天内按小时、超过 1 天才按天。
        # 原来只给天数，刚登录完也显示"0 天前"，没有信息量。
        if age < 60:
            row["ago"] = "刚刚"
        elif age < 3600:
            row["ago"] = f"{max(1, int(age // 60))} 分钟前"
        elif age < 86400:
            row["ago"] = f"{int(age // 3600)} 小时前"
        else:
            row["ago"] = f"{int(age // 86400)} 天前"
    key = f"{platform}/{account or '-'}"
    _reap(key)                               # 窗口被关掉就立刻恢复，不等超时
    started = _RUNNING.get(key)
    row["logging_in"] = bool(started and time.time() - started < _login_timeout())

    # ★ 2026-11-03：显示该号**稳定绑定的出口**。一号一 IP 到底有没有生效，
    # 以前只能翻代码或看日志猜，现在页面上直接看得见 —— 这是配代理池时最该确认的一件事。
    try:
        from .utils import account_key, proxy_for

        px = proxy_for(platform, account_key(str(path)))
        row["egress"] = px or ""
        row["egress_label"] = px or "直连"
        row["_akey"] = account_key(str(path))
    except Exception:  # noqa: BLE001
        row["egress"] = ""
        row["egress_label"] = ""
        row["_akey"] = ""
    return row


def accounts_status() -> dict:
    """各平台账号状态（纯读本地文件，不联网、不开浏览器）。"""
    from .utils import account_pool_snapshot

    out: dict = {}
    for key, label in PLATFORMS:
        # 池内每个号的健康度（今日用量 / 成功失败 / 最近失败原因）
        health = {h.get("name"): h for h in account_pool_snapshot(key)}
        rows = [_row(key, "")]
        for f in list_accounts(key):                 # cookies/<平台>/account_*.txt
            rows.append(_row(key, Path(f).stem))
        for r in rows:
            h = health.get(r.get("_akey") or "") or {}
            r["ok"] = h.get("ok")
            r["fail"] = h.get("fail")
            r["used_today"] = h.get("usedToday")
            r["daily_max"] = h.get("dailyMax")
            r["capped"] = h.get("capped")
            r["success_rate"] = h.get("successRate")
            r["last_fail"] = h.get("lastFail") or ""
            r.pop("_akey", None)
        used = [r for r in rows if r["logged_in"]]
        out[key] = {
            "label": label,
            "accounts": rows,
            "logged_in_count": len(used),
            "note": ("匿名即可，无需登录" if key == "kuaishou"
                     else "视频号下载用它换凭证：点登录会打开元宝网页，微信扫码一次，凭证有效期约 60 天（腾讯定的），到期会提示重扫" if key == "yuanbao"
                     else ""),
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
        proc = subprocess.Popen(args, cwd=str(ROOT), **_no_window_flags())
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _RUNNING.pop(key, None)
            _PROCS.pop(key, None)
        return {"started": False, "error": f"拉起登录脚本失败：{exc}"}
    with _LOCK:
        _PROCS[key] = proc
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


def logout_account(platform: str, account: str = "") -> dict:
    """退出登录：删除该账号的 cookie 文件与浏览器 profile。

    - `account` 为空 = 默认账号（`cookies/<平台>.txt` + `cookies/_browser_profile_<平台>`）
    - 删的是**登录态**，不是历史记录；重新登录即可恢复
    - 浏览器窗口还开着时会占用 profile 目录 → 删不干净的部分如实回报，不假装成功

    2026-11-03 新增：此前只有「登录/验证」，没有退出通道 —— 被标记或废弃的号
    无法从界面清掉，会一直留在池子里被反复选中。
    """
    plat = (platform or "").strip().lower()
    labels = dict(PLATFORMS)
    if plat not in labels:
        return {"ok": False, "msg": f"不支持的平台：{platform}"}
    acct = (account or "").strip()
    if acct and not _ACCOUNT_RE.match(acct):
        return {"ok": False, "msg": f"账号名不合法：{acct}"}

    cookie = _cookie_path(plat, acct)
    profile = _profile_dir(plat, acct)
    done: list[str] = []
    failed: list[str] = []

    # ① cookie 文件（决定"还算不算登录着"）
    try:
        if cookie.is_file():
            cookie.unlink()
            done.append(f"已删除 {cookie.name}")
        else:
            done.append("cookie 文件本就不存在")
    except Exception as exc:  # noqa: BLE001
        failed.append(f"删 cookie 失败：{exc}")

    # ② 浏览器 profile（登录态 + 被平台打上的标记都在这里）
    try:
        if profile.exists():
            shutil.rmtree(profile)
            done.append(f"已删除浏览器 profile {profile.name}")
        else:
            done.append("浏览器 profile 本就不存在")
    except Exception as exc:  # noqa: BLE001
        failed.append(f"删 profile 失败（浏览器窗口可能还开着，关掉后重试）：{exc}")

    # ③ 顺手清掉该账号的用量/成功失败记账，免得状态页还显示旧数据
    try:
        from .utils import _read_accounts, _write_accounts, account_key
        data = _read_accounts()
        key = account_key(str(cookie))
        if (data.get(plat) or {}).pop(key, None) is not None:
            _write_accounts(data)
            done.append("已清除该账号的用量记录")
    except Exception:  # noqa: BLE001
        pass

    parts = done + failed
    if cookie.is_file() or profile.exists():
        # 还有残留 → 不算成功
        return {"ok": False, "msg": "；".join(parts) or "删除未完成"}
    return {"ok": not failed, "msg": "；".join(parts) or "无变化"}