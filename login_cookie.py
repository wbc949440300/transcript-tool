"""内置浏览器登录 → 导出 Netscape cookies.txt（方案 B：小号登录 + fallback 策略）

用法：
    python login_cookie.py xiaohongshu          # 弹出内置浏览器，登录后自动导出
    python login_cookie.py kuaishou
    python login_cookie.py douyin --timeout 300
    python login_cookie.py xiaohongshu --check  # 不弹窗，检查已保存的登录态能否刷新 cookie
    python login_cookie.py xiaohongshu --channel msedge   # 用 Edge 内核（仍是独立 profile）

设计要点（2026-10-29 定稿）：
- **只用工具自己的浏览器 profile**（cookies/_browser_profile），不读取、不修改
  你系统里的 Edge/Chrome 数据。（Edge/Chrome 127+ 的 cookie 是 App-Bound v20 加密，
  外部程序本就无法解密——这里直接绕开，不做任何解密尝试。）
- 登录态持久化：再次运行同一命令时，若会话仍在，直接刷新 cookie 文件，无需重新登录。
- 导出**只包含目标平台域**的 cookie，与 app/utils.py 的 _primary_domain 过滤一致，
  不会把其它站点的凭证一起写进去。
- 输出为 Netscape 格式（7 字段制表符），**不写 #HttpOnly_ 前缀**——因为本项目的
  app/utils.py 解析器会跳过所有 '#' 开头的行（见其实现）。yt-dlp 读取时同样可用。
- 配合 TRANSCRIPT_COOKIE_MODE=fallback（默认）：匿名优先，失败才带 cookie，账号暴露最小。

⚠️ cookie 文件等同登录凭证：不要分享、不要提交到 Git（.gitignore 已覆盖 cookies/*.txt）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，含 ✓/✗ 等字符会崩；统一切到 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROFILE_DIR = ROOT / "cookies" / "_browser_profile"
COOKIE_DIR = ROOT / "cookies"

# 抓取端"要用哪个 profile"必须和这里一致，否则登录与抓取是两个浏览器（登录态白登）：
#   抖音抓取 → app/douyin.py 用 browser.profile_dir_for("douyin") = cookies/_browser_profile_douyin
#   小红书抓取 → 仍用共用的 cookies/_browser_profile（保持既有登录态，不搬家）
_SHARED_PROFILE_PLATFORMS = {"xiaohongshu"}


def _profile_for(platform: str, account: str = "") -> Path:
    """本次登录使用的 profile 目录（与抓取端同一命名规则）。

    - 带 --account account_1：每账号一个 profile，多账号登录态互不串；
      抓取端按同一规则取用（profile_dir_for("<平台>_account_1")）。
    - 抖音：独立目录（与 app/douyin.py 一致）。
    - 其它平台：沿用共用目录，避免把已有登录态搬走。
    """
    if not account and platform in _SHARED_PROFILE_PLATFORMS:
        return PROFILE_DIR
    name = f"{platform}_{account}" if account else platform
    try:
        from app.browser import profile_dir_for

        return profile_dir_for(name)
    except Exception:
        return ROOT / "cookies" / f"_browser_profile_{name}"

# 平台配置
#   open_url     : 登录/主页地址（游客可访问的页面即可，登录框通常在页内）
#   domains      : 参与导出的域名关键字（必须与 cookie 文件名可被解析的"主域名"一致）
#   tokens       : 出现即视为已登录的 cookie 名
#   min_len      : token 值的最小长度（过滤游客态同名 cookie）
#   page_markers : (必须出现, 必须不出现) 的页面文案校验；None 表示跳过页面校验
#   note         : 展示给用户的补充说明
PLATFORMS: dict[str, dict] = {
    "xiaohongshu": {
        "open_url": "https://www.xiaohongshu.com/explore",
        "domains": ("xiaohongshu.com",),
        "tokens": ("web_session",),
        "min_len": 20,
        # 游客也有 web_session，必须配合页面校验，否则会误判"已登录"
        "page_markers": (("发布", "通知"), ("扫码登录", "手机号登录")),
        "note": "小红书绝大多数内容需要登录态；建议用不常用的小号。",
    },
    "kuaishou": {
        "open_url": "https://www.kuaishou.com/",
        "domains": ("kuaishou.com",),
        "tokens": ("passToken", "userId"),
        "min_len": 4,
        "page_markers": None,
        "note": "快手分享页通常无需登录；受限内容才需要。",
    },
    "douyin": {
        "open_url": "https://www.douyin.com/",
        "domains": ("douyin.com",),
        "tokens": ("sessionid", "sessionid_ss"),
        "min_len": 8,
        "page_markers": None,
        "note": "抖音公开视频不需要登录；仅在受限内容失败时可配。",
    },
    "bilibili": {
        "open_url": "https://www.bilibili.com/",
        "domains": ("bilibili.com",),
        "tokens": ("SESSDATA",),
        "min_len": 8,
        "page_markers": None,
        "note": "B站公开视频不需要登录；大会员/登录可见内容才需要。",
    },
    "yuanbao": {
        # 「视频号贴链接直接下载」靠元宝的解析接口签发 token/eid，必须登录元宝
        # （微信扫码即可）。导出的 cookies/yuanbao.txt 只给 app/video_channel.py 用。
        "open_url": "https://yuanbao.tencent.com/",
        "domains": ("yuanbao.tencent.com", "tencent.com"),
        "tokens": ("hy_user", "hy_token"),
        "min_len": 6,
        "page_markers": None,
        "note": "视频号链接直接下载用的登录态；用微信扫码登录一次即可，与其它平台无关。",
    },
}


def _netscape_escape_warn(name: str, value: str) -> bool:
    """制表符/换行无法安全写入 Netscape 格式，遇到则跳过。"""
    return "\t" in name or "\t" in value or "\n" in name or "\n" in value or "\r" in value


def export_cookies(ctx, platform: str, cfg: dict, out_path: Path) -> int:
    """把当前浏览器上下文里目标平台的 cookie 写成 Netscape 文件，返回条数。"""
    cookies = ctx.cookies()
    domains = cfg["domains"]
    lines = [
        "# Netscape HTTP Cookie File",
        f"# 由 login_cookie.py 从内置浏览器导出 · {platform} · {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# 注意：本文件等同登录凭证，请勿分享或提交到版本库",
        "",
    ]
    kept = 0
    for c in cookies:
        dom = str(c.get("domain") or "")
        if not any(d in dom for d in domains):
            continue
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if not name or _netscape_escape_warn(name, value):
            continue
        expires = int(c.get("expires") or 0)
        if expires < 0:  # 会话 cookie：Netscape 约定写 0
            expires = 0
        lines.append(
            "\t".join([
                dom if dom.startswith(".") else "." + dom,  # 统一写成域级 cookie
                "TRUE",
                str(c.get("path") or "/"),
                "TRUE" if c.get("secure") else "FALSE",
                str(expires),
                name,
                value,
            ])
        )
        kept += 1

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kept


def _page_check(page, markers) -> tuple[bool, str]:
    """页面文案校验，返回 (是否通过, 摘要)。"""
    if not markers:
        return True, "（跳过页面校验）"
    must, must_not = markers
    try:
        body = page.inner_text("body")[:3000]
    except Exception as exc:
        return False, f"页面读取失败：{exc}"
    missing = [m for m in must if m not in body]
    banned = [m for m in must_not if m in body]
    if missing or banned:
        return False, f"页面未显示登录态（缺少{missing or '无'} / 出现{banned or '无'}）"
    return True, "页面已显示登录态"


def _restore_from_cookie_file(ctx, out_path: Path, cfg: dict) -> bool:
    """profile 里没有登录态时，用导出的 cookies/<平台>.txt 回灌一次。

    导出文件是登录成功那一刻的快照，而抓取端（yt-dlp / 直连请求）本来就直接读它；
    profile 则可能被清理、或根本不在这台机器上。两边不同源时 --check 会误报
    "未登录"——2026-11-02 实测：cookies/douyin.txt 里 sessionid 未过期，
    但 profile 里没有，于是状态页说"已登录"、check 说"未登录"，自相矛盾。
    """
    try:
        if not out_path.is_file():
            return False
        from app.utils import load_netscape_cookies

        cookies = load_netscape_cookies(str(out_path))
    except Exception:  # noqa: BLE001
        return False
    if not cookies:
        return False
    now = time.time()
    live = []
    for c in cookies:
        exp = c.get("expires")
        # 没有 expires = 会话 cookie，保留；有则只看未过期的
        if not exp or float(exp) > now:
            live.append(c)
    by_name = {c.get("name"): c for c in live}
    hit = [t for t in cfg["tokens"]
           if t in by_name and len(str(by_name[t].get("value") or "")) >= cfg["min_len"]]
    if not hit:
        return False
    try:
        ctx.add_cookies(live)
    except Exception:  # noqa: BLE001
        return False
    print(f"（profile 无登录态，已从 {out_path.name} 回灌 {len(live)} 条 cookie，"
          f"含 {'/'.join(hit)}）")
    return True


def _detect(ctx, page, cfg: dict) -> tuple[bool, str]:
    """检测登录态：cookie token + 页面校验。"""
    cookies = ctx.cookies()
    by_name = {c.get("name"): c for c in cookies if any(d in str(c.get("domain") or "") for d in cfg["domains"])}
    hit = [t for t in cfg["tokens"] if t in by_name and len(str(by_name[t].get("value") or "")) >= cfg["min_len"]]
    if not hit:
        return False, f"未发现登录 cookie（需要 {' / '.join(cfg['tokens'])}）"
    ok, why = _page_check(page, cfg.get("page_markers"))
    if not ok:
        return False, why
    return True, f"检测到 {'/'.join(hit)}，{why}"


def _verify_with_project_parser(path: Path, platform: str) -> None:
    """用项目自己的解析器回读一遍，确认格式能被 accept（防止写了没人认的文件）。"""
    try:
        from app.utils import load_netscape_cookies
    except Exception as exc:  # pragma: no cover
        print(f"  （跳过解析校验：{exc}）")
        return
    parsed = load_netscape_cookies(str(path))
    print(f"  项目解析器回读：{len(parsed)} 条可用 cookie")
    if parsed:
        names = sorted({c['name'] for c in parsed})
        print(f"  关键字段：{', '.join(names[:10])}{' …' if len(names) > 10 else ''}")


def _cdp_alive(port: int) -> bool:
    """检查本机调试端口上是否有浏览器在跑。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# 各内核的可执行文件候选路径（Windows）
BROWSER_CANDIDATES = {
    "msedge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
}


def _browser_exe(p, channel: str | None) -> tuple[str, str]:
    """选登录用的浏览器内核：显式 --channel 优先；留空时**优先 Edge**（用户习惯），否则内置 chromium。

    返回 (可执行文件路径, 展示名)。
    """
    if channel in BROWSER_CANDIDATES:
        for path in BROWSER_CANDIDATES[channel]:
            if Path(path).exists():
                return path, channel
        return p.chromium.executable_path, "内置 chromium（未找到指定内核，已回退）"

    for path in BROWSER_CANDIDATES["msedge"]:
        if Path(path).exists():
            return path, "Microsoft Edge"
    for path in BROWSER_CANDIDATES["chrome"]:
        if Path(path).exists():
            return path, "Google Chrome"
    return p.chromium.executable_path, "内置 chromium"


def _launch_detached(p, port: int, channel: str | None = None, profile_dir: Path | None = None,
                     proxy: str | None = None):
    """常驻启动内置 chromium（脱离本进程），连上后返回 BrowserContext；失败返回 None。

    关键：窗口在本脚本结束后**继续存在**，下次运行直接 connect_over_cdp 接管，
    因此不会每次都新开一个窗口。

    proxy：登录时就走与抓取端**同一条出口**（否则"登录地在 A、抓取地在 B"
    本身就是异常信号）。
    """
    import subprocess

    exe, label = _browser_exe(p, channel)
    argv = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir or PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "about:blank",
    ]
    if proxy:
        argv.insert(3, f"--proxy-server={proxy}")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    print(f"启动 {label}：{exe}")
    try:
        subprocess.Popen(
            argv, creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"启动浏览器进程失败：{exc}")
        return None, False

    for _ in range(60):  # 最多等 30s
        if _cdp_alive(port):
            break
        time.sleep(0.5)
    else:
        return None, False

    try:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        return ctx, True
    except Exception as exc:
        print(f"连接浏览器失败：{exc}")
        return None, False


def _pick_page(ctx, url: str):
    """同一窗口内选标签页：优先该站点 → 其次复用空白页（避免多出"新标签页"）→ 最后才新开。"""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    pages = list(ctx.pages)

    for pg in pages:
        try:
            if host and host in (pg.url or ""):
                return pg
        except Exception:
            continue

    blank_markers = ("about:blank", "", "edge://newtab/", "chrome://newtab/", "edge://new-tab-page/")
    for pg in pages:
        try:
            if (pg.url or "") in blank_markers:
                return pg
        except Exception:
            continue

    return ctx.new_page()


def _resolve_proxy(platform: str, account: str = "") -> str:
    """登录用哪条出口：与抓取端同一规则（账号绑定出口；没配就是直连）。

    这样"登录时"和"抓取时"走同一个 IP —— 否则登录地突变本身就会被风控盯上。
    """
    try:
        from app.utils import pick_proxy_available

        px, _eg = pick_proxy_available(platform, key=(account or "anon"))
        return px or ""
    except Exception:  # noqa: BLE001
        return ""


def _mask_proxy(url: str) -> str:
    try:
        from app.utils import mask_proxy

        return mask_proxy(url)
    except Exception:  # noqa: BLE001
        return url


def main() -> int:
    ap = argparse.ArgumentParser(description="内置浏览器登录并导出 cookies/<平台>.txt")
    ap.add_argument("platform", choices=sorted(PLATFORMS), help="目标平台")
    ap.add_argument("--timeout", type=int, default=300, help="等待登录的秒数（默认 300）")
    ap.add_argument("--channel", default=None, help="浏览器内核：留空=自动（优先 Edge），可选 msedge / chrome")
    ap.add_argument("--headless", action="store_true", help="无窗口（仅用于已登录时静默刷新 cookie）")
    ap.add_argument("--check", action="store_true", help="不弹窗，只检查现有登录态并尝试刷新 cookie")
    ap.add_argument("--port", type=int, default=int(__import__("os").environ.get("TRANSCRIPT_LOGIN_CDP_PORT", "9333")),
                    help="常驻浏览器的调试端口（默认 9333，可用环境变量 TRANSCRIPT_LOGIN_CDP_PORT 改）")
    ap.add_argument("--no-reuse", action="store_true", help="强制新开窗口（默认优先复用已打开的登录窗口）")
    ap.add_argument("--account", default="",
                    help="账号池标签（如 account_1）：登录态存进该账号专属 profile，cookie 导出到 cookies/<平台>/<标签>.txt")
    ap.add_argument("--proxy", default="",
                    help="登录时使用的代理（默认自动取与抓取端同一条出口；配了代理池才需要）")
    args = ap.parse_args()

    cfg = PLATFORMS[args.platform]
    profile = _profile_for(args.platform, args.account)
    out_path = (COOKIE_DIR / args.platform / f"{args.account}.txt") if args.account \
        else (COOKIE_DIR / f"{args.platform}.txt")
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    silent = args.headless or args.check
    proxy = (args.proxy or "").strip() or _resolve_proxy(args.platform, args.account)
    print(f"平台：{args.platform}")
    if args.account:
        print(f"账号：{args.account}（抓取时会用同名 profile：{profile.name}）")
    print(f"浏览器：{args.channel or '自动（优先 Edge）'}（profile 独立于你的浏览器：{profile}）")
    if silent:
        print("模式：静默（无窗口）")
    elif _cdp_alive(args.port):
        print(f"模式：复用已打开的窗口（端口 {args.port}）"
              + ("；注意：复用的窗口沿用启动时的出口，改出口请先关闭它或加 --no-reuse" if proxy else ""))
    else:
        print(f"模式：启动常驻窗口（端口 {args.port}，下次运行直接复用）")
    print(f"出口：{_mask_proxy(proxy) if proxy else '直连（未配置代理）'}"
          + ("（与抓取端同一条）" if proxy else ""))
    print(f"输出：{out_path}")
    print(f"说明：{cfg['note']}")
    print()

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"未安装 playwright：{exc}\n请先 pip install playwright && python -m playwright install chromium")
        return 2

    with sync_playwright() as p:
        own_ctx = None          # 自己创建的静默 context（需要关闭）
        ctx = None              # 实际使用的 BrowserContext

        # 1) 优先接管已打开的登录窗口（同一窗口，不再新开）
        if not silent and not args.no_reuse and _cdp_alive(args.port):
            try:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                print(f"已接管现有浏览器窗口（调试端口 {args.port}），不会新开窗口。")
            except Exception as exc:
                print(f"接管现有窗口失败（{exc}），将新开一个。")

        # 2) --check / --headless：静默模式，不弹窗
        if ctx is None and silent:
            try:
                own_ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 900},
                    **({"channel": args.channel} if args.channel else {}),
                )
                ctx = own_ctx
            except Exception as exc:
                print(f"浏览器启动失败：{exc}")
                return 2

        # 3) 首次运行：常驻启动（默认用 Edge；窗口保持打开，下次直接接管）
        fresh = False
        if ctx is None:
            ctx, fresh = _launch_detached(p, args.port, args.channel, profile, proxy=proxy)
            if ctx is None:
                print("浏览器启动失败：无法建立调试端口")
                return 2
            print(f"已启动浏览器窗口（调试端口 {args.port}；保持打开，下次运行直接复用）。")

        def _finish(code: int) -> int:
            """结束前清理：静默/一次性窗口才关闭；常驻窗口保留给下次复用。"""
            if own_ctx is not None:
                try:
                    own_ctx.close()
                except Exception:
                    pass
            else:
                print("\n（浏览器窗口保持打开；下次运行同一命令会直接复用它，不会再新开窗口）")
            return code

        # 选中平台页：已有该站点的标签就复用，否则在同一窗口新开标签
        page = _pick_page(ctx, cfg["open_url"])
        try:
            if cfg["open_url"].split("//")[-1].split("/")[0] not in (page.url or ""):
                page.goto(cfg["open_url"], wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"打开页面失败（继续尝试检测）：{exc}")

        if fresh:
            # Edge/Chrome 首次启动会自带一个 edge://newtab/ 或 about:blank，
            # 关掉它，只保留我们正在用的这个标签
            blanks = ("about:blank", "", "edge://newtab/", "chrome://newtab/", "edge://new-tab-page/")
            for pg in list(ctx.pages):
                if pg is page:
                    continue
                try:
                    if (pg.url or "") in blanks:
                        pg.close()
                except Exception:
                    pass

        ok, why = _detect(ctx, page, cfg)
        if not ok and args.check and _restore_from_cookie_file(ctx, out_path, cfg):
            # 有页面标注的平台要重载一次，页面才能反映刚注入的 cookie
            try:
                if cfg.get("page_markers"):
                    page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            ok, why = _detect(ctx, page, cfg)
            if ok:
                why = f"{why}（登录态取自 cookie 文件 {out_path.name}）"
        if ok:
            print(f"✓ 已有登录态：{why}")
        elif args.check:
            print(f"✗ 没有可用登录态：{why}")
            print("（--check 不会弹窗；请去掉该参数重新运行并在窗口里登录）")
            return _finish(1)
        else:
            print(f"尚未登录：{why}")
            print(f"请在浏览器窗口里登录（最多等 {args.timeout} 秒）…")
            print("提示：登录后建议在本页停留几秒，等待 cookie 完全写入。\n")
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                time.sleep(3)
                ok, why = _detect(ctx, page, cfg)
                if ok:
                    print(f"✓ {why}")
                    break
            if not ok:
                print(f"✗ 等待超时：{why}")
                print("（若你确认已登录，可稍后运行 --check 重试；或加大 --timeout）")
                return _finish(1)

        time.sleep(5)  # 让剩余 cookie 落盘
        kept = export_cookies(ctx, args.platform, cfg, out_path)
        print(f"\n已写入 {kept} 条 cookie → {out_path}")
        _verify_with_project_parser(out_path, args.platform)
        _finish(0)

    print("\n下一步：")
    try:
        rel = out_path.relative_to(ROOT)
    except Exception:
        rel = out_path
    print(f"  1) 确认 {rel} 存在（已被 .gitignore 忽略，不会进版本库）")
    print("  2) 保持 TRANSCRIPT_COOKIE_MODE=fallback（默认）：匿名优先，失败才带 cookie")
    print("  3) 之后正常提取即可；cookie 失效时重新运行本命令（通常无需再次登录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())