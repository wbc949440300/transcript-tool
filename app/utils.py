"""通用工具：任务 ID、时间格式化、平台识别。"""
from __future__ import annotations

import logging
import re
import time
import uuid
import urllib.error
import functools as _functools
import random as _random

logger = logging.getLogger("utils")


def gen_task_id() -> str:
    ts = int(time.time() * 1000) << 22
    rand = uuid.uuid4().int & ((1 << 22) - 1)
    return str(ts | rand)


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_PLATFORM_MAP = [
    ("douyin.com", "抖音"), ("iesdouyin.com", "抖音"),
    ("xiaohongshu.com", "小红书"), ("xhslink.com", "小红书"),
    ("kuaishou.com", "快手"),
    ("bilibili.com", "B站"), ("b23.tv", "B站"),
    ("weixin.qq.com/xr", "视频号"), ("weixin.qq.com", "微信"),
]


def detect_platform(url: str) -> str:
    """URL → 平台中文名（优先用平台适配器的 label，保持单一事实来源）。"""
    try:
        from .platforms import adapter_for

        return adapter_for(url).label
    except Exception:  # noqa: BLE001
        u = (url or "").lower()
        for host, name in _PLATFORM_MAP:
            if host in u:
                return name
        return "通用"


# ---------- Cookie 支持（Netscape cookies.txt） ----------

import json as _json
import threading as _threading
import time as _time
import os as _os
from pathlib import Path as _Path

COOKIE_DIR = _Path(_os.getenv("TRANSCRIPT_COOKIE_DIR", "cookies"))

# 域名特征 -> cookies 目录下的文件名
_PLATFORM_COOKIE_FILE = [
    ("douyin.com", "douyin.txt"),
    ("iesdouyin.com", "douyin.txt"),
    ("xiaohongshu.com", "xiaohongshu.txt"),
    ("xhslink.com", "xiaohongshu.txt"),
    ("bilibili.com", "bilibili.txt"),
    ("b23.tv", "bilibili.txt"),
    ("kuaishou.com", "kuaishou.txt"),
    ("gifshow.com", "kuaishou.txt"),
]


def cookie_file_for(url: str) -> str | None:
    """按 URL 找对应的 cookies/<平台>.txt；没有则回退 cookies/default.txt。

    只在文件存在时返回路径，方便直接交给 yt-dlp 的 cookiefile。
    """
    return _find_cookie_file(url)


def cookie_available_for(url: str) -> bool:
    """是否存在可用的平台 cookie 文件。"""
    return _find_cookie_file(url) is not None


def cookie_mode() -> str:
    """Cookie 使用策略（保护账号，降低风控风险）。

    off      : 完全不用 cookie（最安全，公开内容够用）
    fallback : 默认。匿名优先，只有失败时才带 cookie 重试
    always   : 每次都带 cookie（不推荐，等于全程暴露账号）
    """
    mode = (_os.getenv("TRANSCRIPT_COOKIE_MODE", "fallback") or "fallback").strip().lower()
    return mode if mode in ("off", "fallback", "always") else "fallback"


def should_use_cookie(url: str, attempt: int = 1, login_required: bool = False) -> bool:
    """本次请求是否应该携带 cookie。

    off      : 永远 False
    always   : 永远 True
    fallback : **默认匿名**；只有调用方判定"缺登录态"（login_required=True，
               来自 classify_failure 的 login 结论）时才带 cookie。

    旧实现是 attempt >= 2 就带 cookie —— 等于"只要失败就把账号推到前台"，
    在风控场景下是最坏策略（2026-10-31 改）。attempt 参数保留只为兼容调用点。
    """
    mode = cookie_mode()
    if mode == "off":
        return False
    if mode == "always":
        return True
    return bool(login_required) and cookie_available_for(url)


def _find_cookie_file(url: str) -> str | None:
    u = (url or "").lower()
    for host, fname in _PLATFORM_COOKIE_FILE:
        if host in u:
            p = COOKIE_DIR / fname
            if p.is_file() and p.stat().st_size > 0:
                return str(p)
            break
    default = COOKIE_DIR / "default.txt"
    if default.is_file() and default.stat().st_size > 0:
        return str(default)
    return None


def load_netscape_cookies(path: str) -> list[dict]:
    """把 Netscape cookies.txt 解析成 Playwright 可用的 cookie 列表。"""
    out: list[dict] = []
    # 只取该文件"主域名"的 cookie，避免整份文件里混着其它站点的凭证被一并泄露
    primary = _primary_domain(_Path(path).stem)
    try:
        for raw in _Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, cpath, secure, expires, name, value = parts[:7]
            if primary and primary not in domain:
                continue
            item = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cpath or "/",
                "secure": str(secure).upper() == "TRUE",
            }
            try:
                exp = int(float(expires))
                if exp > 0:
                    item["expires"] = exp
            except Exception:
                pass
            out.append(item)
    except Exception:
        return []
    return out


def cookies_header_for(url: str) -> str | None:
    """把 cookie 文件拼成 HTTP Cookie 头（供非 Playwright 的直连请求用）。"""
    path = cookie_file_for(url)
    if not path:
        return None
    # 只发送目标域相关的 cookie（最小必要原则，避免整份文件全部外带）
    target = _host_of_url(url)
    pairs = []
    for c in load_netscape_cookies(path):
        dom = (c.get("domain") or "").lstrip(".").lower()
        if target and dom and target not in dom and dom not in target:
            continue
        pairs.append(f"{c['name']}={c['value']}")
    return "; ".join(pairs) if pairs else None


def _host_of_url(u: str) -> str:
    m = re.match(r"https?://([^/?#]+)", (u or "").lower())
    return m.group(1) if m else ""


def _primary_domain(stem: str) -> str:
    """从 cookie 文件名推主域名（douyin -> douyin.com）。"""
    stem = _Path(str(stem)).stem if "." in str(stem) else str(stem)
    stem = (stem or "").strip().lower()
    if stem in ("default", ""):
        return ""
    # account_1.txt 这类文件名属于账号池，无法推断域名，交给调用方按 URL 过滤
    if stem.startswith("account_"):
        return ""
    return stem.replace("_", ".")


# ---------- 账号池（多 cookie 轮换，避免单账号高频） ----------

_ACCOUNT_IDX: dict[str, int] = {}
_POOL_LOCK = _threading.Lock()


def cookie_pool_dir() -> _Path:
    """账号池目录：cookies/<平台>/account_*.txt"""
    return COOKIE_DIR


def list_accounts(platform: str) -> list[str]:
    """列出某平台账号池里的 cookie 文件（按文件名排序）。"""
    d = cookie_pool_dir() / platform.lower()
    if not d.is_dir():
        return []
    files = sorted(d.glob("account_*.txt"))
    return [str(f) for f in files if f.stat().st_size > 0]


def _accounts_file() -> _Path:
    return COOKIE_DIR / ".accounts.json"


def _read_accounts() -> dict:
    f = _accounts_file()
    if not f.is_file():
        return {}
    try:
        data = _json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_accounts(data: dict) -> None:
    try:
        f = _accounts_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def account_key(cookie_file: str | None) -> str:
    """账号标签：池内用 `account_1`，单文件 cookie 用 `douyin`。

    与 `login_cookie.py --account account_1`、`douyin.py::_profile_name_for` 保持同一套命名。
    """
    if not cookie_file:
        return ""
    return _Path(cookie_file).stem


def _platform_from_cookie_path(cookie_file: str) -> str:
    """cookies/douyin/account_1.txt → douyin；cookies/douyin.txt → douyin。"""
    try:
        p = _Path(cookie_file)
        if p.parent and p.parent.name and p.parent.name != COOKIE_DIR.name:
            return p.parent.name
        return p.stem
    except Exception:  # noqa: BLE001
        return ""


def record_account_result(cookie_file: str | None, ok: bool,
                          platform: str = "", reason: str = "") -> None:
    """给某个账号记一次成功/失败（只用于观测：选择策略 + 页面展示）。"""
    if not cookie_file:
        return
    plat = (platform or _platform_from_cookie_path(cookie_file) or "").lower()
    if not plat:
        return
    try:
        with _POOL_LOCK:
            data = _read_accounts()
            plat_rec = data.setdefault(plat, {})
            rec = plat_rec.setdefault(account_key(cookie_file), {})
            field = "ok" if ok else "fail"
            rec[field] = int(rec.get(field) or 0) + 1
            if not ok:
                rec["last_fail"] = (reason or "")[:120]
                rec["last_fail_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _write_accounts(data)
    except Exception:  # noqa: BLE001
        pass


def _bump_used_today(platform: str, cookie_file: str) -> None:
    """选中即记一次"今天用了" → 下一次自然轮到别的号（自带均衡）。"""
    try:
        today = time.strftime("%Y-%m-%d")
        with _POOL_LOCK:
            data = _read_accounts()
            plat_rec = data.setdefault(platform.lower(), {})
            rec = plat_rec.setdefault(account_key(cookie_file), {})
            if rec.get("date") != today:
                rec["date"] = today
                rec["used_today"] = 0
            rec["used_today"] = int(rec.get("used_today") or 0) + 1
            _write_accounts(data)
    except Exception:  # noqa: BLE001
        pass


def account_pool_snapshot(platform: str) -> list[dict]:
    """账号池概览（供 /risk/status）：今日用量、成功/失败、是否冷却。"""
    plat = (platform or "").lower()
    pool = list_accounts(plat)
    if not pool:
        single = COOKIE_DIR / f"{plat}.txt"
        pool = [str(single)] if single.is_file() and single.stat().st_size > 0 else []
    data = _read_accounts().get(plat, {}) or {}
    cooled = _account_cooldown()
    today = time.strftime("%Y-%m-%d")
    cap = account_daily_max()
    out: list[dict] = []
    for f in pool:
        rec = data.get(account_key(f)) or {}
        ok = int(rec.get("ok") or 0)
        fail = int(rec.get("fail") or 0)
        used = int(rec.get("used_today") or 0) if rec.get("date") == today else 0
        out.append({
            "name": account_key(f),
            "usedToday": used,
            "dailyMax": cap,
            "capped": bool(cap > 0 and used >= cap),
            "ok": ok,
            "fail": fail,
            "successRate": round(ok / (ok + fail), 2) if (ok + fail) else None,
            "cooling": f in cooled,
            "lastFail": rec.get("last_fail") or "",
        })
    return out


def account_daily_max() -> int:
    """每个账号每天最多跑多少条（0 = 不限制，默认 30）。

    20~25 个号的池子里，单号安全日量按 30 条算；达到上限的号当天不再参与挑选，
    避免"某个号被反复挑中"——这正是多号分摊想要的效果。
    """
    try:
        return max(0, int(_os.getenv("TRANSCRIPT_ACCOUNT_DAILY_MAX", "30") or 30))
    except Exception:  # noqa: BLE001
        return 30


_CAP_WARN_AT = 0.0


def pick_account(platform: str) -> str | None:
    """选一个账号 cookie：**跳过冷却与已达每日上限 → 今天用得最少 → 成功率最高**。

    池子为空则回退到 cookies/<平台>.txt。选中后立刻记一次今日用量，
    所以即使所有指标都一样，也会自动在两个号之间来回（不需要轮询索引）。

    `TRANSCRIPT_ACCOUNT_FORCE=<cookie 文件路径>` 可强制只用某个号（巡检脚本用）。
    """
    platform = platform.lower()
    forced = (_os.getenv("TRANSCRIPT_ACCOUNT_FORCE", "") or "").strip()
    if forced and _Path(forced).is_file():
        return forced
    pool = list_accounts(platform)
    if not pool:
        single = COOKIE_DIR / f"{platform}.txt"
        if single.is_file() and single.stat().st_size > 0:
            _bump_used_today(platform, str(single))   # 单文件也要记账（否则"今天用了多少"永远是 0）
            return str(single)
        return None
    cap = account_daily_max()
    with _POOL_LOCK:
        cooled = _account_cooldown()
        alive = [f for f in pool if f not in cooled] or pool
        data = _read_accounts().get(platform, {}) or {}
        today = time.strftime("%Y-%m-%d")

        def used_of(f: str) -> int:
            rec = data.get(account_key(f)) or {}
            return int(rec.get("used_today") or 0) if rec.get("date") == today else 0

        def rank(f: str):
            rec = data.get(account_key(f)) or {}
            ok = int(rec.get("ok") or 0)
            fail = int(rec.get("fail") or 0)
            ratio = (fail / (ok + fail)) if (ok + fail) else 0.0
            return (used_of(f), ratio, account_key(f))

        if cap > 0:
            under = [f for f in alive if used_of(f) < cap]
            if not under:
                # 全都到上限了：仍挑最少用的那个（否则任务直接停摆），但要让人知道
                global _CAP_WARN_AT
                now = time.monotonic()
                if now - _CAP_WARN_AT > 600:      # 10 分钟最多报一次，别刷日志
                    _CAP_WARN_AT = now
                    logger.warning("%s 账号池全部达到每日上限(%d)，临时放宽；"
                                   "要跑更多请加号或调 TRANSCRIPT_ACCOUNT_DAILY_MAX", platform, cap)
                under = alive
            alive = under
        chosen = min(alive, key=rank)
    _bump_used_today(platform, chosen)
    return chosen


def platform_of(url: str) -> str:
    """从 URL 判断平台 key（用于账号池目录）。"""
    u = (url or "").lower()
    for host, name in (("douyin", "douyin"), ("iesdouyin", "douyin"),
                       ("xiaohongshu", "xiaohongshu"), ("xhslink", "xiaohongshu"),
                       ("bilibili", "bilibili"), ("b23.tv", "bilibili"),
                       ("kuaishou", "kuaishou"), ("gifshow", "kuaishou")):
        if host in u:
            return name
    return "default"


def mark_account_cooldown(cookie_file: str, seconds: int = 900) -> None:
    """把某账号标记为冷却（例如连续失败/被风控时）。"""
    if not cookie_file:
        return
    try:
        with _POOL_LOCK:
            data = _read_cooldown()
            data[cookie_file] = time.time() + seconds
            _write_cooldown(data)
    except Exception:
        pass


def _account_cooldown() -> set:
    try:
        return {k for k, v in _read_cooldown().items() if v > time.time()}
    except Exception:
        return set()


def account_cooldown_remaining(cookie_file: str) -> float:
    """某个账号 cookie 文件还剩多少秒冷却（0 = 没在冷却）。

    账号冷却和平台冷却共用 cookies/.cooldown.json，但键不同：
    账号用 cookie 文件路径、平台用 `platform:<平台>@<出口>`。
    """
    if not cookie_file:
        return 0.0
    try:
        raw = _read_cooldown().get(str(cookie_file))
        return max(0.0, float(raw) - time.time()) if raw else 0.0
    except Exception:
        return 0.0


def _read_cooldown() -> dict:
    f = COOKIE_DIR / ".cooldown.json"
    if not f.is_file():
        return {}
    try:
        return _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cooldown(data: dict) -> None:
    f = COOKIE_DIR / ".cooldown.json"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------- 平台级串行锁 + 持久化冷却 ----------

_PLATFORM_LOCKS: dict[str, _threading.Lock] = {}
_PLATFORM_LOCKS_GUARD = _threading.Lock()


def platform_lock(platform: str) -> _threading.Lock:
    """同一平台同一时刻只允许一个抓取请求，避免批量/多标签并发触发风控。"""
    key = (platform or "default").lower()
    with _PLATFORM_LOCKS_GUARD:
        return _PLATFORM_LOCKS.setdefault(key, _threading.Lock())


def platform_serialized(platform: str):
    """装饰同步抓取函数，按平台串行执行。"""
    def deco(fn):
        @_functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with platform_lock(platform):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def _platform_cooldown_key(platform: str, egress: str | None = None) -> str:
    """冷却键 = 平台 + 出口。给了 egress 就用它，否则用当前默认出口。"""
    eg = egress if egress is not None else egress_id(platform)
    return f"platform:{(platform or 'default').lower()}@{eg}"


_PROXY_IDX: dict[str, int] = {}
_PROXY_LOCK = _threading.Lock()


def _single_proxy(platform: str = "") -> str | None:
    """单条代理（平台专用 > 全局）。"""
    key = f"TRANSCRIPT_PROXY_{(platform or 'default').upper()}"
    raw = (_os.getenv(key, "") or "").strip() or (_os.getenv("TRANSCRIPT_PROXY", "") or "").strip()
    return raw or None


def proxy_pool(platform: str = "") -> list[str]:
    """该平台的代理池（优先级：平台池 > 全局池 > 单条平台代理 > 单条全局代理）。

    示例：
      TRANSCRIPT_PROXY_POOL=host1:8080,host2:8080           # 所有平台共享的池
      TRANSCRIPT_PROXY_POOL_KUAISHOU=host1:8080,host2:8080  # 快手单独一个池
      TRANSCRIPT_PROXY_DOUYIN=socks5://127.0.0.1:1080       # 单条（等效于只有一条的池）
    """
    for key in (f"TRANSCRIPT_PROXY_POOL_{(platform or 'default').upper()}",
                "TRANSCRIPT_PROXY_POOL"):
        raw = (_os.getenv(key, "") or "").strip()
        if raw:
            urls = [u.strip() for u in re.split(r"[,\s]+", raw) if u.strip()]
            if urls:
                return urls
    single = _single_proxy(platform)
    return [single] if single else []


def _stable_hash(text: str) -> int:
    """跨进程稳定的哈希（内置 hash 带随机种子，重启后结果会变，不能用来做绑定）。"""
    import zlib

    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def proxy_for(platform: str = "", key: str = "") -> str | None:
    """该平台（可选：某个账号/某个目标）使用的代理。

    - key 非空 → **稳定绑定**到池内某一条：同一个号（或同一条视频）始终走同一条出口，
      指纹与出口一致，不会"这个号一会儿从 A 出口、一会儿从 B 出口"
    - key 为空 → 按平台轮询，把请求摊到多条出口上（快手这类无账号场景）
    """
    pool = proxy_pool(platform)
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    if key:
        return pool[_stable_hash(key) % len(pool)]
    with _PROXY_LOCK:
        plat = (platform or "default").lower()
        idx = _PROXY_IDX.get(plat, 0)
        _PROXY_IDX[plat] = idx + 1
    return pool[idx % len(pool)]


def proxy_for_media() -> str | None:
    """媒体 CDN 下载用的代理（默认直连，避免把大流量都塞进代理）。"""
    return (_os.getenv("TRANSCRIPT_PROXY_MEDIA", "") or "").strip() or None


def egress_of(proxy: str | None) -> str:
    """出口标识（冷却/预算按它隔离）。"""
    return ("proxy:" + proxy) if proxy else "direct"


def egress_id(platform: str = "", key: str = "") -> str:
    """当前抓取出口标识（平台 + 代理）。"""
    return egress_of(proxy_for(platform, key))


def pick_proxy_available(platform: str, key: str = "",
                         tries: int = 4) -> tuple[str | None, str]:
    """选一条**没在冷却**的出口；全在冷却就退回第一条。

    返回 (proxy, egress)。调用方把 egress 一并传给冷却记录/查询，
    这样"哪条出口被限流"才能被精确记住，而不是整个平台一起停摆。
    """
    pool = proxy_pool(platform)
    if not pool:
        return None, "direct"
    if len(pool) == 1 or key:
        px = proxy_for(platform, key)
        return px, egress_of(px)
    plat = (platform or "default").lower()
    with _PROXY_LOCK:
        start = _PROXY_IDX.get(plat, 0)
        _PROXY_IDX[plat] = start + len(pool)
    n = len(pool)
    for i in range(min(max(1, tries), n)):
        px = pool[(start + i) % n]
        eg = egress_of(px)
        if platform_cooldown_remaining(platform, egress=eg) <= 0:
            return px, eg
    px = pool[start % n]
    return px, egress_of(px)


def mask_proxy(url: str) -> str:
    """展示用：抹掉代理里的账号密码，只留 协议://***@host:port。"""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit

        p = urlsplit(url)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
        scheme = p.scheme or "http"
        if p.username or p.password:
            return f"{scheme}://***@{host}{port}"
        return f"{scheme}://{host}{port}"
    except Exception:  # noqa: BLE001
        return "***"


_PROBE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_PROBE_URL = {
    "douyin": "https://www.douyin.com/",
    "kuaishou": "https://m.gifshow.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "bilibili": "https://www.bilibili.com/",
    "default": "https://www.baidu.com/",
}
_PROXY_CHECKS: dict[str, dict] = {}


# ── 出口 IP 探测：判断这条代理是「静态」还是「轮换」─────────────────────────
#
# 为什么需要：轮换型代理（每个请求换 IP）会让两个机制**静默失效** ——
#   1) "一号一 IP 稳定绑定"：IP 一直在变，根本绑不住
#   2) "冷却按出口隔离"：记不住是哪条出口被限，最后变成整平台停摆
# 用户在界面上完全看不出来，只会事后发现"效果莫名变差"。所以自检时主动探一次。
_IP_ECHO_URLS = (
    "http://members.3322.org/dyndns/getip",
    "http://myip.ipip.net",
    "https://api.ipify.org",
)
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_GEO_URL = ("http://ip-api.com/json/{ip}"
            "?lang=zh-CN&fields=status,country,regionName,city,isp,org,as,query")
# 机房/云厂商关键词：命中即提示"疑似机房 IP，建议换住宅线路"
_DC_HINTS = ("阿里云", "腾讯云", "华为云", "百度云", "金山云", "ucloud", "amazon", "aws",
             "azure", "microsoft", "google", "cloud", "hosting", "datacenter", "idc",
             "vps", "digitalocean", "vultr", "linode", "ovh", "hetzner", "oracle")


def _exit_ip_once(px: str, timeout: float = 6.0) -> str | None:
    """经指定出口取一次公网 IP（多个回显源依次试）。全失败返回 None。"""
    import urllib.request

    for url in _IP_ECHO_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _PROBE_UA})
            with http_opener("", proxy=px).open(req, timeout=timeout) as resp:
                body = resp.read(4096).decode("utf-8", "replace")
            m = _IP_RE.search(body)
            if m:
                return m.group(1)
        except Exception:  # noqa: BLE001
            continue
    return None


def _ip_geo(ip: str, timeout: float = 5.0) -> dict:
    """查出口 IP 的归属地与运营商。best-effort：失败返回 {}，不影响主流程。"""
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(_GEO_URL.format(ip=ip), timeout=timeout) as resp:
            d = _json.loads(resp.read(2048).decode("utf-8", "replace"))
        if isinstance(d, dict) and d.get("status") == "success":
            return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def _judge_exit(px: str, *, rounds: int = 3, timeout: float = 6.0) -> dict:
    """连续取多次出口 IP：判断是否稳定，并顺带拿归属地/运营商。"""
    ips: list[str] = []
    for _ in range(max(2, rounds)):
        got = _exit_ip_once(px, timeout)
        if not got:
            break
        ips.append(got)
    out: dict = {"exit_ips": ips}
    if len(ips) >= 2:
        out["stable"] = len(set(ips)) == 1
    geo = _ip_geo(ips[0]) if ips else {}
    if geo:
        out["geo"] = {"country": geo.get("country") or "",
                      "region": geo.get("regionName") or "",
                      "city": geo.get("city") or "",
                      "isp": geo.get("isp") or "",
                      "as": geo.get("as") or ""}
        blob = " ".join(str(geo.get(k) or "") for k in ("isp", "org", "as")).lower()
        out["datacenter"] = any(h in blob for h in _DC_HINTS)
    return out


def proxy_checks() -> dict:
    """各平台最近一次代理自检结果（供 /risk/status 与告警）。"""
    return dict(_PROXY_CHECKS)


def proxy_check(platform: str, timeout: float = 7.0) -> dict:
    """用该平台**池里的每一条**出口各探一次，逐条给出通/不通。

    2026-11-03 改：以前只测 `proxy_for(platform)` 选中的那一条 —— 池里 5 条坏 1 条
    根本测不出来，等真跑任务才一条条失败，还容易误判成"平台风控"。
    现在全测 + 并发（20 条也就十几秒）。

    注意：**返回 403/412 也算通** —— 请求确实经代理到了平台，只是被反爬挡了；
    只有连接被拒 / 超时 / 解析失败才算"不通"。
    """
    import concurrent.futures
    import urllib.request

    pool = proxy_pool(platform)
    if not pool:
        res = {"ok": True, "direct": True, "detail": "未配置代理（直连）",
               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _PROXY_CHECKS[platform] = res
        return res

    url = _PROBE_URL.get((platform or "").lower()) or _PROBE_URL["default"]

    def probe(px: str) -> dict:
        t0 = time.time()
        item = {"proxy": mask_proxy(px), "ok": True, "detail": ""}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _PROBE_UA, "Accept": "*/*"})
            with http_opener(platform, proxy=px).open(req, timeout=timeout) as resp:
                item["detail"] = f"HTTP {getattr(resp, 'status', 200)}"
        except urllib.error.HTTPError as exc:
            item["detail"] = f"HTTP {exc.code}（通了，被平台反爬挡）"
        except Exception as exc:  # noqa: BLE001
            item["ok"] = False
            item["detail"] = f"{type(exc).__name__}: {str(exc)[:70]}"
        item["ms"] = int((time.time() - t0) * 1000)
        if item["ok"]:
            # 通了才探出口稳定性（不通就没必要再探）
            try:
                item.update(_judge_exit(px, rounds=2, timeout=min(timeout, 5.0)))
            except Exception:  # noqa: BLE001
                pass
        return item

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, max(1, len(pool)))) as ex:
        items = list(ex.map(probe, pool))

    # 把"能用但有问题"的情况整理成给人看的问题清单（前端据此弹窗）
    problems: list[str] = []
    for it in items:
        label = it.get("proxy") or "?"
        ips = it.get("exit_ips") or []
        if it.get("stable") is True and ips:
            it["detail"] = f"{it.get('detail', '')} · 出口 {ips[0]}（静态）"
        elif it.get("stable") is False and len(ips) >= 2:
            it["detail"] = f"{it.get('detail', '')} · 出口 IP 会变"
            problems.append(
                f"{label} 的出口 IP 会变（{' → '.join(ips)}）——这是轮换型代理。"
                "「一号一 IP 稳定绑定」和「冷却按出口隔离」都会失效，建议改用静态 IP。")
        g = it.get("geo") or {}
        if g and g.get("country") and "中国" not in str(g.get("country")):
            problems.append(
                f"{label} 的出口在境外（{g.get('country')} {g.get('city') or ''}）——"
                "目标平台对境外 IP 更敏感，建议换国内线路。")
        if it.get("datacenter"):
            who = g.get("isp") or g.get("as") or "未知运营商"
            problems.append(
                f"{label} 疑似机房 IP（{who}）——机房/云厂商 IP 更容易被平台识别，"
                "建议换住宅线路。")

    bad = [i for i in items if not i["ok"]]
    good = len(items) - len(bad)
    if bad:
        problems.insert(0, f"有 {len(bad)} 条出口连不上，检查地址/端口/账号密码是否正确。")
    res = {"ok": not bad, "direct": False, "pool": len(pool),
           "ok_count": good, "bad_count": len(bad), "items": items,
           "problems": problems,
           "detail": f"{good}/{len(items)} 条可用" + (f"，不通 {len(bad)} 条" if bad else ""),
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _PROXY_CHECKS[platform] = res
    return res


def http_opener(platform: str = "", *, media: bool = False, proxy: str | None = None):
    """urllib opener：**显式**决定代理（proxy 参数 > 媒体配置 > 平台配置），不读系统代理。

    为什么要显式：系统代理可能被视频号下载器的 MITM 改到 127.0.0.1:2023，
    让普通平台请求也被绕进去（实测会丢 query / 变慢）。这里一律自己说了算。
    """
    import urllib.request

    px = proxy if proxy is not None else (proxy_for_media() if media else proxy_for(platform))
    if px:
        handler = urllib.request.ProxyHandler({"http": px, "https": px})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def http_get_bytes(req, *, media: bool = True, timeout: float = 30,
                   attempts: int = 3, backoff: float = 0.8,
                   platform: str = "") -> tuple[bytes, dict]:
    """取整段响应体，带退避重试。

    ★ 2026-11-03 加：排查发现封面、图片 OCR 这几处都是"只请求一次，失败即返回"，
    而平台 CDN 的瞬时 5xx 很常见（实测同一 URL 连续两次，一次 502、一次 1.6MB/s 正常）。
    一次抖动就判死，用户体验上就是"莫名其妙失败"。

    **只适合拿完整响应体**（图片、封面、小文件）。视频预览是流式转发，
    不能在这里重试 —— 那条路径要"换下一条直链"，见 api.py 的 _media_all_urls。
    """
    import time

    import urllib.error

    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            with http_opener(platform, media=media).open(req, timeout=timeout) as resp:
                return resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code < 500:      # 4xx 重试无意义（过期 / 无权限 / 不存在）
                raise
        except Exception as exc:    # noqa: BLE001  连接重置 / 超时 / URLError
            last = exc
        if i + 1 < attempts:
            time.sleep(backoff * (i + 1))
    raise last if last is not None else RuntimeError("请求失败")


# ---------- 失败分类：决定"退避 / 冷却 / 换策略"，而不是一味重试 ----------
#
# risk      : 平台风控/限流（403/429/验证码/访问频繁/账号异常）→ 停手冷却
# login     : 缺登录态（登录墙/401/未登录）→ 只有这类失败才值得下次带上 cookie
# transient : 网络/超时/5xx → 退避后原样（匿名）重试
# permanent : 内容不存在/已删除/不支持 → 直接失败，不再消耗请求
# unknown   : 其它 → 允许一次原样重试

_RISK_MARKERS = ("风控", "验证码", "滑块", "安全验证", "请完成验证", "访问过于频繁",
                 "访问频繁", "操作频繁", "请求过于频繁", "异常访问", "限流", "冷却中",
                 "account exception", "rate limit", "too many requests",
                 "temporarily blocked", "forbidden", "403", "429")
_LOGIN_MARKERS = ("登录", "未登录", "需要登录", "请先登录", "login", "sign in",
                  "not logged", "unauthorized", "401", "需要验证身份",
                  "300031", "笔记暂时无法浏览")
_PERMANENT_MARKERS = ("已删除", "已被删除", "被删除", "已下架", "已移除", "不存在",
                      "作品不存在", "not found", "404", "deleted", "removed",
                      "video unavailable", "geo-restrict", "地区限制",
                      "unsupported url", "不支持", "图集", "无法识别", "没有可提取", "已失效")
_TRANSIENT_MARKERS = ("timeout", "timed out", "超时", "connection", "连接", "network",
                      "网络", "temporarily", "ssl", "urlopen error", "reset by peer",
                      "502", "503", "504", "proxy", "远程主机")


def classify_failure(error, default: str = "unknown") -> str:
    """把异常/错误文本归成 risk / login / transient / permanent / unknown。

    调用方据此决定：风控就停手，缺登录态才动 cookie，网络问题原样退避，
    内容不存在就别再试了（旧实现是所有失败都先匿名重试、再带 cookie 重试，
    一条失效链接要换 3 次脸面，是请求放大的主因之一）。
    """
    text = ""
    if error is not None:
        text = str(getattr(error, "reason", "") or "") + " " + str(error)
    text = text.lower()
    if not text.strip():
        return default
    for markers, kind in ((_RISK_MARKERS, "risk"), (_LOGIN_MARKERS, "login"),
                          (_PERMANENT_MARKERS, "permanent"),
                          (_TRANSIENT_MARKERS, "transient")):
        for m in markers:
            if m.lower() in text:
                return kind
    return default


def _cooldown_entry(raw) -> tuple[float, str, str]:
    """兼容旧格式（纯数字=距今时间戳）与新格式（含 kind/reason 的 dict）。"""
    if isinstance(raw, dict):
        try:
            return float(raw.get("until") or 0), str(raw.get("kind") or "risk"), str(raw.get("reason") or "")
        except Exception:
            return 0.0, "risk", ""
    try:
        return float(raw), "risk", ""
    except Exception:
        return 0.0, "risk", ""


def _cooldown_meta(raw) -> tuple[int, int]:
    """取记录里的 (strikes, level)；旧格式没有就是 0。"""
    if isinstance(raw, dict):
        try:
            return int(raw.get("strikes") or 0), int(raw.get("level") or 0)
        except Exception:
            return 0, 0
    return 0, 0


def _platform_cooldown_entries(platform: str,
                               egress: str | None = None) -> list[tuple[str, float, str, str]]:
    """冷却记录。给了 egress 只看那一条出口；否则看当前出口 + 旧格式键。"""
    keys = [_platform_cooldown_key(platform, egress)]
    if egress is None:
        keys.append(f"platform:{(platform or 'default').lower()}")
    out = []
    try:
        data = _read_cooldown()
    except Exception:
        return out
    for k in keys:
        if k in data:
            until, kind, reason = _cooldown_entry(data[k])
            out.append((k, until, kind, reason))
    return out


def platform_cooldown_remaining(platform: str, egress: str | None = None) -> float:
    """该平台（可选：指定出口）剩余的冷却秒数（0 = 未冷却）。"""
    try:
        return max([max(0.0, until - time.time())
                    for _, until, _, _ in _platform_cooldown_entries(platform, egress)] or [0.0])
    except Exception:
        return 0.0


def platform_cooldown_info(platform: str, egress: str | None = None) -> dict:
    """冷却详情（供 /risk/status 展示：剩多久、为什么、连续命中几次、第几级）。"""
    best = {"remaining": 0.0, "until": 0, "kind": "", "reason": "", "strikes": 0, "level": 0}
    try:
        data = _read_cooldown()
    except Exception:
        data = {}
    for k, until, kind, reason in _platform_cooldown_entries(platform, egress):
        remain = max(0.0, until - time.time())
        if remain > best["remaining"]:
            strikes, level = _cooldown_meta(data.get(k))
            best = {"remaining": remain, "until": until, "kind": kind, "reason": reason,
                    "strikes": strikes, "level": level}
    return best


def mark_platform_cooldown(platform: str, seconds: int = 1800, kind: str = "risk",
                           reason: str = "", escalate: bool = True,
                           egress: str | None = None) -> None:
    """把平台（某条出口）标记为冷却，写入 cookies/.cooldown.json，服务重启后仍生效。

    只对 risk 类失败调用（transient/permanent 不该让整个平台停摆）。
    egress 指定时只罚那一条出口（代理池场景：一条出口被限流，其他出口照常用）。
    熔断升级：连续命中时冷却翻倍（30m → 60m → 120m，封顶 2 小时）。
    """
    try:
        with _POOL_LOCK:
            data = _read_cooldown()
            key = _platform_cooldown_key(platform, egress)
            old = data.get(key)
            old_until, old_kind, _ = _cooldown_entry(old)
            old_strikes, _old_level = _cooldown_meta(old)
            strikes = (old_strikes + 1) if (escalate and old_kind == "risk") else 1
            level = min(4, strikes) if escalate else 1
            base = max(60, int(seconds))
            duration = min(base * (2 ** (level - 1)), 7200)
            until = time.time() + duration
            data[key] = {
                "until": max(until, old_until),
                "kind": kind,
                "reason": reason[:200],
                "platform": (platform or "default").lower(),
                "egress": (egress if egress is not None else egress_id(platform)),
                "strikes": strikes,
                "level": level,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _write_cooldown(data)
    except Exception:
        pass


def clear_platform_cooldown(platform: str, egress: str | None = None) -> None:
    """清冷却：给 egress 只清那一条出口；不给则清该平台**所有出口**（熔断复位语义）。"""
    try:
        with _POOL_LOCK:
            data = _read_cooldown()
            base = f"platform:{(platform or 'default').lower()}"
            if egress is not None:
                data.pop(_platform_cooldown_key(platform, egress), None)
            else:
                for k in list(data.keys()):
                    if k == base or k.startswith(base + "@"):
                        data.pop(k, None)
            _write_cooldown(data)
    except Exception:
        pass


def platform_cooldown_enabled(platform: str) -> bool:
    """平台冷却开关：默认开启；可用 TRANSCRIPT_<PLATFORM>_COOLDOWN=0 单独关闭。"""
    key = f"TRANSCRIPT_{(platform or 'default').upper()}_COOLDOWN"
    raw = _os.getenv(key, _os.getenv("TRANSCRIPT_RISK_COOLDOWN", "1"))
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


# ---------- 请求节流器（全局 QPS 上限，三条通道共用） ----------

_TOKEN_BUCKETS: dict[str, dict] = {}
_THROTTLE_LOCK = _threading.Lock()


def throttle(domain: str, min_interval: float = 0.0, platform: str = "") -> float:
    """同一目标两次请求之间的最小间隔（带随机抖动），返回实际等待秒数。

    默认从环境变量读取：
      TRANSCRIPT_THROTTLE_MIN_MS    域名级最小间隔（毫秒），默认 2000
      TRANSCRIPT_PLATFORM_MIN_MS    平台级最小间隔（毫秒），默认 3000
      TRANSCRIPT_THROTTLE_JITTER    抖动比例，默认 0.5（在 0~50% 内随机加时）

    传入 platform 时：间隔取域名级与平台级中的较大值，且**桶按平台共享**
    （同平台的不同域名也不再并行请求），这是防"机器行为"判定的主要闸门。
    """
    base = min_interval
    if base <= 0:
        try:
            base = float(_os.environ.get("TRANSCRIPT_THROTTLE_MIN_MS", "2000")) / 1000.0
        except Exception:
            base = 2.0
    key = domain or "default"
    plat = (platform or "").strip().lower()
    if plat and plat not in ("default", "通用", "common"):
        try:
            pmin = float(_os.environ.get("TRANSCRIPT_PLATFORM_MIN_MS", "3000")) / 1000.0
        except Exception:
            pmin = 3.0
        base = max(base, pmin)
        key = f"platform:{plat}"
    if base <= 0:
        return 0.0
    try:
        jitter = max(0.0, float(_os.environ.get("TRANSCRIPT_THROTTLE_JITTER", "0.5")))
    except Exception:
        jitter = 0.5
    gap = base * (1.0 + _random.uniform(0.0, jitter))
    now = time.monotonic()
    with _THROTTLE_LOCK:
        b = _TOKEN_BUCKETS.setdefault(key, {"last": 0.0})
        wait = b["last"] + gap - now
        if wait > 0:
            b["last"] = b["last"] + gap
        else:
            b["last"] = now
            wait = 0.0
    if wait > 0:
        time.sleep(wait)
    return wait


def throttle_platform(platform: str, min_interval: float = 0.0) -> float:
    """平台级节流：同平台的所有请求共用一条节奏（各平台通道在发请求前调用）。"""
    return throttle("", min_interval, platform=platform)


def throttle_reset() -> None:
    with _THROTTLE_LOCK:
        _TOKEN_BUCKETS.clear()


# ---------- 字幕文件：URL → 纯文本 ----------
#
# yt-dlp 在 skip_download=True 时只会给出字幕 URL、不会带正文（实测：
# requested_subtitles 里只有 url/ext，没有 data）。所以"有字幕"必须由我们自己
# 把 URL 下下来；**任何时候都不能把 URL 本身当成文案**。

_SUB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def looks_like_text(text: str, min_ratio: float = 0.85) -> bool:
    """粗略判断一段内容是不是"人话"。

    用于丢弃"解压失败/编码猜错/其实不是字幕"的二进制垃圾：控制字符比例过高就
    判定为垃圾（2026-10-31 实测踩坑：B站 subtitles 里是 danmaku 弹幕 XML，
    CDN 用 raw deflate 压缩，直接当文本解会得到一堆乱码并被当成文案）。
    """
    if not text or len(text.strip()) < 2:
        return False
    sample = text[:4000]
    good = sum(1 for ch in sample if ch.isprintable() or ch in "\n\r\t")
    return (good / len(sample)) >= min_ratio


def _try_decode_subtitle(body: bytes) -> str:
    """按常见编码严格解码；解出来不像文本就返回空。

    只认 UTF-8（含 BOM）和带 BOM 的 UTF-16：
    - 不猜 GB18030 —— 它几乎能"解码"任何字节，压缩数据/二进制会被解成一堆
      看似可打印的汉字，从而绕过文本校验（2026-10-31 实测踩到）。
    - UTF-16 只认带 BOM 的，避免随机偶数字节被解成"可打印"的乱码。
    """
    encodings = ["utf-8", "utf-8-sig"]
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings.append("utf-16")
    for enc in encodings:
        try:
            text = body.decode(enc)
        except Exception:
            continue
        if looks_like_text(text):
            return text
    return ""


def _decode_subtitle_bytes(raw: bytes) -> str:
    """字幕响应体 → 字符串：先按原文解，失败再按 gzip/zlib/裸 deflate 解压。"""
    if not raw:
        return ""
    text = _try_decode_subtitle(raw)
    if text:
        return text
    import gzip as _gzip
    import zlib as _zlib

    def _raw_deflate(b: bytes) -> bytes:
        return _zlib.decompressobj(-_zlib.MAX_WBITS).decompress(b)

    for decompress in (_gzip.decompress, _zlib.decompress, _raw_deflate):
        try:
            data = decompress(raw)
        except Exception:
            continue
        text = _try_decode_subtitle(data)
        if text:
            return text
    return ""


def fetch_subtitle_text(url: str, timeout: float = 15.0, referer: str = "",
                        platform: str = "") -> str:
    """下载字幕文件（vtt/srt/json/ass）并转成纯文本；拿不到有效文本就返回空串。

    返回空串意为"没拿到内容"，调用方应当退回 ASR，而不是拿 URL 或乱码去凑文案。
    """
    if not isinstance(url, str) or not url.startswith("http"):
        return ""
    try:
        import urllib.request

        headers = {"User-Agent": _SUB_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        with http_opener(platform).open(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception:
        return ""
    text = _decode_subtitle_bytes(raw)
    return subtitle_body_to_text(text) if text else ""


def subtitle_body_to_text(raw: str) -> str:
    """把字幕文件内容（VTT/SRT/ASS/JSON）转成纯文本行。"""
    if not raw or not raw.strip():
        return ""
    head = raw.lstrip()
    if head[:1] in "{[":
        try:
            data = _json.loads(head)
        except Exception:
            data = None
        if data is not None:
            return _json_subtitle_text(data)
    body = re.sub(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->.*$", "", raw, flags=re.M)
    body = re.sub(r"^(?:WEBVTT.*|Kind:.*|Language:.*)$", "", body, flags=re.M)
    lines: list[str] = []
    for raw_line in body.splitlines():
        s = raw_line.strip()
        if not s or s.isdigit():
            continue
        s = re.sub(r"<[^>]+>", "", s)        # <c>/<i>/<00:00:01.000> 等标签
        s = re.sub(r"\{\\[^}]*\}", "", s)    # ASS/SSA 覆盖标签
        s = s.strip()
        if s and (not lines or s != lines[-1]):
            lines.append(s)
    return "\n".join(lines)


def _json_subtitle_text(data) -> str:
    """从常见 JSON 字幕结构里抽文本（含 YouTube json3 的 events/segs）。"""
    parts: list[str] = []

    def add(node, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, str):
            s = node.strip()
            if s and not re.fullmatch(r"[\d\s.,:+\-]+", s):
                parts.append(s)
        elif isinstance(node, list):
            for item in node:
                add(item, depth + 1)
        elif isinstance(node, dict):
            for key in ("text", "content", "utf8", "body", "segs", "segments",
                        "events", "lines", "subtitles"):
                if key in node:
                    add(node[key], depth + 1)

    add(data)
    out: list[str] = []
    for p in parts:
        if not out or p != out[-1]:
            out.append(p)
    return "\n".join(out)
