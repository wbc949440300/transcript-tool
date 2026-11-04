"""外部下载器（wx_channels_download）集成。

该工具（https://github.com/ltaoo/wx_channels_download）在本机 127.0.0.1:2022 提供 HTTP API：
  GET  /api/status                      服务状态（api/proxy 是否 listening）
  GET  /api/scraper/platform/status     各平台可用性
  GET  /api/config                      配置（含 download.dir）
  POST /api/v1/download_task/create_by_url  按“绝对媒体资源 URL”创建下载任务

重要（对照仓库当前实现）：
  - create_by_url 只接受带 scheme/host 的**资源 URL**（mp4/m3u8 等），不是平台分享页链接；
  - 视频号没有可直接提交的下载 API：正确流程是在微信里打开视频号并播放，
    由下载器注入的下载按钮提交当前视频流；相关 /api/channels/* 接口还要求
    微信视频号页面保持打开并初始化 client socket，否则返回
    {"code":400,"msg":"请先初始化客户端 socket 连接"}。
所以本模块对视频号分享链接直接给出明确提示，绝不把它当资源 URL 提交。

本模块只做三件事：
  1. probe()          探测是否在线，并读出它的下载目录/API/代理状态
  2. local_download() 通用直链本地下载（v260907 的 create_by_url 有上游 unique_id 缺陷）
  3. 转写由本项目负责——下载落地后由 watch_folder 自动入列（见 api 的 /ext/* 端点）

外部下载器仍负责视频号注入按钮/平台任务；直链不再依赖其 create_by_url。

设计原则：外部工具可能没装/没启动，所有调用都必须可失败、可降级，不影响本项目主流程。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("external_dl")

BASE = os.getenv("TRANSCRIPT_EXT_DL_URL", "http://127.0.0.1:2022").rstrip("/")
TIMEOUT = float(os.getenv("TRANSCRIPT_EXT_DL_TIMEOUT", "5"))

# ---------- 便携路径（换新电脑时随项目目录自动推导，env 可覆盖） ----------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent   # .../transcript_tool
_BASE_DIR = _PROJECT_ROOT.parent                          # .../文案提取工具

EXE_NAME = "wx_video_download.exe"
# 这个下载器是 Windows 专属（PowerShell / taskkill / UAC / winreg）；云上跑不了，
# 各入口都要能优雅降级，而不是抛异常把服务带崩（2026-10-31：为"云上可跑"加的守卫）
IS_WINDOWS = os.name == "nt"
_WINDOWS_ONLY_HINT = "外部下载器（视频号抓取）仅支持 Windows；云上请把视频同步到监听目录，由本服务转写"
INSTALL_DIR = Path(os.getenv("TRANSCRIPT_EXT_DL_DIR", str(_BASE_DIR / "wx_channels_download")))
DOWNLOAD_DIR = Path(os.getenv("TRANSCRIPT_EXT_DL_DOWNLOAD_DIR", str(_BASE_DIR / "wx_downloads")))
PKG_DIR = Path(os.getenv("TRANSCRIPT_EXT_DL_PKG_DIR", str(_BASE_DIR / "wx_dl_pkg")))
GITHUB_REPO = os.getenv("TRANSCRIPT_EXT_DL_REPO", "ltaoo/wx_channels_download")
LOG_HINT = r"%LocalAppData%\Temp\wx_channels_download\app.log"

# 下载器的 MITM 代理端口（它运行时会把系统代理指到本机这个端口）。
# 全机流量会走它 —— 所以"进程死了但代理还指着它"= 全机断网，必须体检 + 退出还原。
PROXY_PORT = int(os.getenv("TRANSCRIPT_EXT_DL_PROXY_PORT", "2023"))

# 本次进程是否由我们拉起下载器（退出时据此决定要不要替用户停掉它）
_STARTED_BY_US = False

# 本机 API 一律直连：urllib 在 Windows 会读注册表系统代理，而下载器运行期间
# 系统代理指向它自己的 2023（MITM），不豁免会把 127.0.0.1:2022 的请求也绕进代理
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_video_channel_share(url: str) -> bool:
    """视频号分享页/预览页链接（不是可直接下载的媒体资源 URL）。"""
    u = (url or "").lower()
    return ("weixin.qq.com/sph" in u) or ("channels.weixin.qq.com" in u)


def _get(path: str, timeout: float | None = None) -> dict:
    with _opener.open(BASE + path, timeout=timeout or TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post(path: str, payload: dict, timeout: float | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _opener.open(req, timeout=timeout or TIMEOUT * 3) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _find_key(obj, key: str, depth: int = 0):
    """在任意 JSON 结构里递归找某个键（用于从 /api/config 里挖 download.dir）。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            got = _find_key(v, key, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj[:20]:
            got = _find_key(v, key, depth + 1)
            if got is not None:
                return got
    return None


def _download_dir_from_config(cfg) -> str:
    """从配置里尽力解析下载目录。兼容 {"download":{"dir":...}}、扁平键与
    /api/config 的 application_fields 数组形态（key="download.dir"）。"""
    if isinstance(cfg, dict):
        dl = cfg.get("download")
        if isinstance(dl, dict) and isinstance(dl.get("dir"), str):
            return dl["dir"]
        for k in ("download.dir", "downloadDir"):
            if isinstance(cfg.get(k), str):
                return cfg[k]
        # /api/config 实际返回 {"application_fields":[{"key":"download.dir","effective_value":...}]}
        fields = cfg.get("application_fields")
        if isinstance(fields, list):
            for f in fields:
                if isinstance(f, dict) and f.get("key") in ("download.dir", "downloadDir"):
                    for vk in ("effective_value", "value", "default"):
                        v = f.get(vk)
                        if isinstance(v, str) and v.strip():
                            return v
        # 项目自身可能是 {"data": {...}}
        for wrapper in ("data", "config", "values"):
            if wrapper in cfg:
                got = _download_dir_from_config(cfg[wrapper])
                if got:
                    return got
    got = _find_key(cfg, "dir")
    return got if isinstance(got, str) else ""


def exe_path() -> Path | None:
    """已安装的下载器 exe 路径；未安装返回 None。"""
    p = INSTALL_DIR / EXE_NAME
    return p if p.is_file() and p.stat().st_size > 0 else None


def local_pkg() -> Path | None:
    """本地已有的安装包（优先 safe x86_64），没有返回 None。"""
    if PKG_DIR.is_dir():
        for pat in ("wx_video_download_safe_*_windows_x86_64.zip",
                    "wx_video_download_*_windows_x86_64.zip", "*.zip"):
            hits = sorted(PKG_DIR.glob(pat))
            if hits:
                return hits[0]
    return None


def probe() -> dict:
    """探测下载器状态。返回 {online, base, download_dir, platforms, error}。

    online 判定：任意一个 API 请求成功才算在线（不能只看"没抛异常"）。
    """
    out = {"online": False, "base": BASE, "download_dir": "", "platforms": [], "error": None,
           "version": "", "api_listening": None, "proxy_listening": None,
           "platform_supported": IS_WINDOWS,
           "installed": exe_path() is not None,
           "exe": str(exe_path() or ""),
           "pkg_ready": local_pkg() is not None,
           "started_by_this_service": _STARTED_BY_US,
           "system_proxy": proxy_status()}
    if not IS_WINDOWS:
        # 云端：不探测、不报错，明确告诉页面"这个功能在本机才可用"
        out["error"] = _WINDOWS_ONLY_HINT
        return out
    ok_any = False
    try:
        try:
            st = _get("/api/status")
            out["status"] = st
            sdata = st.get("data") if isinstance(st.get("data"), dict) else {}
            out["version"] = sdata.get("version") or ""
            api = sdata.get("api") if isinstance(sdata.get("api"), dict) else {}
            proxy = sdata.get("proxy") if isinstance(sdata.get("proxy"), dict) else {}
            out["api_listening"] = bool(api.get("listening")) if "listening" in api else None
            out["proxy_listening"] = bool(proxy.get("listening")) if "listening" in proxy else None
            ok_any = True
        except Exception:
            pass
        try:
            cfg = _get("/api/config")
            out["download_dir"] = _download_dir_from_config(cfg)
            ok_any = True
        except Exception:
            pass
        try:
            ps = _get("/api/proxy/status", timeout=4)
            cert = (ps.get("data") or {}).get("certificate") or {}
            out["cert_installed"] = bool(cert.get("installed"))
            out["cert_trusted"] = bool(cert.get("trusted"))
        except Exception:
            pass
        try:
            ps = _get("/api/scraper/platform/status")
            items = (ps.get("data") or {}).get("statuses") if isinstance(ps.get("data"), dict) else ps.get("statuses")
            if isinstance(items, list):
                out["platforms"] = [
                    {"name": x.get("name") or x.get("platform") or "?",
                     "available": bool(x.get("available"))}
                    for x in items
                ]
            ok_any = True
        except Exception:
            pass
        out["online"] = ok_any
        if not ok_any:
            out["error"] = "无法连接（下载器未启动或端口不是 2022）"
    except Exception as exc:  # noqa: BLE001
        logger.warning("下载器状态探测异常: %s", exc)
        out["error"] = "无法连接（下载器未启动或端口不是 2022）"
    return out


def list_video_channel_tasks(limit: int = 6) -> dict:
    """读取下载器最近的视频号任务，供页面监控（只读，不触发下载/重试）。"""
    try:
        limit = max(1, min(int(limit), 20))
    except Exception:
        limit = 6
    try:
        data = _get(f"/api/v1/download_task/list?page=1&page_size={max(20, limit * 3)}")
    except Exception as exc:  # noqa: BLE001
        # 原始异常只进日志：页面上出现的应是可读原因，而不是
        # "URLError: <urlopen error [WinError 10061] ...>" 这种半中半英的实现细节
        logger.warning("读取下载器任务失败: %s", exc)
        return {"items": [], "online": False,
                "error": "无法连接（下载器未启动或端口不是 2022）"}

    status_text = {0: "等待中", 1: "准备中", 2: "下载中", 3: "已暂停",
                   4: "合并中", 5: "已完成", 6: "失败", 7: "已取消"}
    items = []
    for task in ((data.get("data") or {}).get("list") or []):
        if not isinstance(task, dict):
            continue
        platform_id = str(task.get("platform_id") or "").lower()
        source_url = str(task.get("source_url") or "").lower()
        if not (platform_id.startswith("wxchannels")
                or "channels.weixin.qq.com" in source_url
                or "finder.video.qq.com" in source_url):
            continue
        status_code = task.get("status")
        try:
            status_code = int(status_code)
        except Exception:
            status_code = -1
        created = task.get("created_at")
        created_text = ""
        try:
            created_ts = float(created) / 1000.0
            if created_ts > 0:
                import datetime
                created_text = datetime.datetime.fromtimestamp(created_ts).strftime("%m-%d %H:%M")
        except Exception:
            pass
        try:
            progress = max(0, min(100, int(task.get("progress") or 0)))
        except Exception:
            progress = 0
        items.append({
            "id": task.get("id"),
            "name": task.get("name") or task.get("title") or f"任务 {task.get('id')}",
            "status": status_code,
            "statusText": status_text.get(status_code, "未知"),
            "progress": progress,
            "size": task.get("size") or 0,
            "downloaded": task.get("downloaded") or 0,
            "error": task.get("error") or "",
            "createdAt": created_text,
        })
        if len(items) >= limit:
            break
    return {"items": items, "online": True}


def idle_stop_min() -> float:
    """空闲多少分钟后自动停止下载器并还原系统代理（0=关闭，默认 0）。

    为什么要有这个：下载器只要在跑，系统代理就一直指向它的 MITM（绕开 clash 等），
    用户其它上网流量都被它接管。视频号下载往往是"一次下几条"然后就没了。
    """
    try:
        return max(0.0, float(os.getenv("TRANSCRIPT_EXT_DL_IDLE_STOP_MIN", "0") or 0))
    except Exception:  # noqa: BLE001
        return 0.0


def _download_dir_activity() -> float:
    """下载目录（含 processed/）里最新的文件改动时间；0 表示目录不存在或为空。"""
    newest = 0.0
    for root in (DOWNLOAD_DIR, DOWNLOAD_DIR / "processed"):
        try:
            for f in root.iterdir():
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
        except Exception:  # noqa: BLE001
            continue
    return newest


def _has_active_task() -> bool:
    """下载器自己有没有在下的任务（准备中/下载中/合并中）→ 有就不算空闲。"""
    try:
        data = list_video_channel_tasks(20)
    except Exception:  # noqa: BLE001
        return False
    for it in (data.get("items") or []):
        if int(it.get("status") or 0) in (1, 2, 4):
            return True
    return False


def should_idle_stop(now: float | None = None, idle_min: float | None = None) -> bool:
    """判定"该自动停"：下载器在线 + 没有在下的任务 + 目录 N 分钟没有新文件。

    任一条件说不清（离线 / 目录为空）都返回 False —— **宁可不停，也不要误停**。
    """
    import time

    idle_min = idle_stop_min() if idle_min is None else float(idle_min)
    if idle_min <= 0:
        return False
    now = time.time() if now is None else float(now)
    try:
        if not probe().get("online"):
            return False
    except Exception:  # noqa: BLE001
        return False
    if _has_active_task():
        return False
    newest = _download_dir_activity()
    if newest <= 0:
        return False
    return (now - newest) >= idle_min * 60


def idle_watch_loop(interval: float = 60.0) -> None:
    """后台线程：空闲超时就 `stop()`（会一并还原启动前的系统代理）。"""
    import threading
    import time

    def _loop():
        while True:
            try:
                if should_idle_stop():
                    logger.info("下载器空闲超过 %.0f 分钟 → 自动停止并还原系统代理",
                                idle_stop_min())
                    stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("空闲检查异常: %s", exc)
            time.sleep(max(30.0, interval))

    threading.Thread(target=_loop, daemon=True, name="ext-dl-idle").start()
    logger.info("下载器空闲自动停止已启用（空闲 %.0f 分钟）", idle_stop_min())


def download(url: str, download_dir: str = "", filename: str = "") -> dict:
    """提交给外部下载器的资源 URL 任务。

    ⚠️ v260907 的 create_by_url 存在上游 unique_id 缺陷：任务会创建成功，
    但异步下载失败（resource unique ID is required）。通用直链请使用
    local_download()；本函数保留给后续修复版本/平台特定任务。
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("url 不能为空")
    if is_video_channel_share(url):
        raise ValueError(
            "视频号分享链接不能直接提交给下载器 API。请在微信里打开视频号视频并播放，"
            "点击下载器注入的下载按钮；文件落到下载目录后本工具会自动转写。"
            "（create_by_url 只接受绝对媒体 URL；视频号相关接口还要求视频号页面保持打开，"
            "否则会返回“请先初始化客户端 socket 连接”。）"
        )
    obj = {"url": url, "auto_start": True}
    if download_dir:
        obj["download_dir"] = download_dir
    if filename:
        obj["filename"] = filename
    logger.info("外部下载器创建任务: %s", obj["url"][:100])
    data = _post("/api/v1/download_task/create_by_url", {"objects": [obj]})
    if not isinstance(data, dict) or data.get("code") not in (0, 200):
        raise RuntimeError(f"下载器返回错误：{(data or {}).get('msg') or (data or {}).get('code') or '未知错误'}")
    tasks = ((data.get("data") or {}).get("tasks") or [])
    if not tasks:
        raise RuntimeError(f"下载器未返回任务：{data.get('msg') or '未知错误'}")
    first = tasks[0] if isinstance(tasks[0], dict) else {}
    if not first.get("success"):
        raise RuntimeError(f"下载器创建任务失败：{first.get('error') or data.get('msg') or '未知错误'}")
    item = first.get("data") if isinstance(first.get("data"), dict) else {}
    resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
    if resource and not str(resource.get("unique_id") or "").strip():
        raise RuntimeError(
            "下载器 v260907 的 create_by_url 未生成 resource unique_id（上游缺陷）；"
            "通用直链请使用 local_download()"
        )
    return data


MEDIA_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v",
             ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac", ".wma"}


def _media_headers(url: str) -> dict:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
    }
    if "bilibili" in host or "bilivideo" in host:
        headers["Referer"] = "https://www.bilibili.com/"
    elif "kuaishou" in host or "gifshow" in host or "kwaicdn" in host:
        headers["Referer"] = "https://www.kuaishou.com/"
    return headers


def _content_ext(url: str, content_type: str) -> str:
    from urllib.parse import urlparse

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in MEDIA_EXT:
        return suffix
    ctype = (content_type or "").split(";")[0].strip().lower()
    guessed = (mimetypes.guess_extension(ctype) or "").lower()
    if guessed in MEDIA_EXT:
        return guessed
    if ctype.startswith("video/"):
        return ".mp4"
    if ctype.startswith("audio/"):
        return ".m4a"
    return ""


def _output_name(filename: str, url: str, content_type: str) -> str:
    from urllib.parse import unquote, urlparse

    raw = Path(filename or "").name
    if not raw:
        raw = unquote(Path(urlparse(url).path).name)
    raw = re.sub(r'[\\/:*?"<>|%\r\n]+', "_", raw).strip(" .")
    if not raw:
        raw = "download"
    suffix = Path(raw).suffix.lower()
    ext = _content_ext(url, content_type)
    if suffix not in MEDIA_EXT:
        if suffix:
            raw = raw[:-len(suffix)]
        raw += ext or ".mp4"
    return raw


def _download_with_ytdlp(url: str, dest: Path, filename: str, headers: dict) -> Path:
    """m3u8/mpd 或需要多流合并的直链交给 yt-dlp/ffmpeg。"""
    from yt_dlp import YoutubeDL

    stem = Path(filename).stem if filename else "%(title).120s"
    stem = re.sub(r'[\\/:*?"<>|%\r\n]+', "_", stem).strip(" .") or "download"
    before = {p.name for p in dest.iterdir() if p.is_file()}
    opts = {
        "outtmpl": str(dest / (stem + ".%(ext)s")),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "http_headers": headers,
    }
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    new_files = [
        p for p in dest.iterdir()
        if p.is_file() and p.name not in before and p.suffix.lower() in MEDIA_EXT and p.stat().st_size > 0
    ]
    if new_files:
        return max(new_files, key=lambda p: p.stat().st_mtime)
    raise RuntimeError("yt-dlp 未产生媒体文件")


def local_download(url: str, dest_dir, filename: str = "", headers: dict | None = None,
                   timeout: float = 180.0) -> Path:
    """本地直链下载（不依赖外部下载器的 create_by_url，绕开其 unique_id 缺陷）。"""
    from urllib.parse import urlparse

    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("直链必须是 http/https 资源 URL")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    merged_headers = _media_headers(url)
    merged_headers.update(headers or {})
    path_lower = parsed.path.lower()
    if path_lower.endswith((".m3u8", ".mpd")):
        logger.info("本地直链下载（HLS/DASH）: %s", url[:100])
        return _download_with_ytdlp(url, dest, filename, merged_headers)

    req = urllib.request.Request(url, headers=merged_headers)
    logger.info("本地直链下载: %s", url[:100])
    try:
        with _opener.open(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            ctype = (content_type or "").split(";")[0].strip().lower()
            if ctype in ("text/html", "application/json") or ctype.startswith("text/"):
                raise RuntimeError(f"直链返回的不是媒体文件（{ctype or 'unknown'}）")
            out = dest / _output_name(filename, url, content_type)
            if out.exists():
                out = dest / f"{out.stem}_{int(__import__('time').time())}{out.suffix}"
            tmp = out.with_suffix(out.suffix + ".part")
            try:
                with tmp.open("wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                if tmp.stat().st_size <= 0:
                    raise RuntimeError("下载结果为空")
                tmp.replace(out)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            logger.info("本地直链下载完成: %s (%d 字节)", out.name, out.stat().st_size)
            return out
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败: {exc.reason}") from exc


def download_and_wait(url: str, dest_dir, timeout_s: float = 240.0,
                      poll_s: float = 2.0, stable_rounds: int = 2,
                      headers: dict | None = None):
    """下载直链并返回落地媒体文件；失败返回 None，不抛异常。

    直链优先本地下载：v260907 的 create_by_url 存在上游 unique_id 缺陷，
    外部下载器只负责视频号注入按钮/平台任务，不再承担通用直链。
    """
    try:
        return local_download(str(url), dest_dir, headers=headers, timeout=min(float(timeout_s), 300.0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("本地直链下载失败: %s", exc)
        return None


# ---------- 自动安装 / 启动 / 停止（换新电脑可一键自举） ----------

def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_url(url: str, dest: Path, timeout: float = 300.0) -> Path:
    """下载文件（走系统代理：GitHub 在国内通常要靠用户自己的代理）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "transcript-tool/1.0 (+https://github.com/ltaoo/wx_channels_download)",
        "Accept": "*/*",
    })
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)
    return dest


def _latest_release_pkg() -> tuple[str, str | None]:
    """从 GitHub releases 找 safe x86_64 包（及 checksums 资产）。返回 (下载URL, checksumsURL)。"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": "transcript-tool/1.0",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.load(r)
    assets = rel.get("assets") or []

    def pick(pred):
        for a in assets:
            n = (a.get("name") or "").lower()
            if pred(n):
                return a
        return None

    asset = (pick(lambda n: "safe" in n and "windows_x86_64" in n and n.endswith(".zip"))
             or pick(lambda n: "windows_x86_64" in n and n.endswith(".zip")))
    if not asset:
        raise RuntimeError(f"release {rel.get('tag_name') or ''} 里没有 windows_x86_64 的 zip 资产")
    sums = pick(lambda n: n.startswith("checksum") and n.endswith(".txt"))
    return asset["browser_download_url"], (sums["browser_download_url"] if sums else None)


def _verify_pkg(zip_path: Path) -> str | None:
    """若包旁边有 checksums.txt 且包含该文件名 → 校验并返回 sha256；否则返回 None（跳过）。"""
    sums = zip_path.parent / "checksums.txt"
    if not sums.is_file():
        return None
    try:
        want = ""
        for line in sums.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].strip().lower() == zip_path.name.lower():
                want = parts[0].strip().lower()
                break
        if not want:
            return None
        got = _sha256(zip_path)
        if got != want:
            raise RuntimeError(f"安装包 SHA256 校验失败：期望 {want}，实际 {got}")
        return got
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("校验安装包失败（跳过校验）: %s", exc)
        return None


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压到安装目录。跳过路径穿越条目；保留已有 config.yaml / data.db*（用户数据）。"""
    import zipfile

    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            name = (m.filename or "").replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"安装包含不安全路径：{name}")
            target = dest / name
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            keep = target.name == "config.yaml" or target.name.startswith("data.db")
            if keep and target.exists():
                continue
            with z.open(m) as src, open(target, "wb") as f:
                f.write(src.read())


def _patch_config(install_dir: Path) -> None:
    """把 config.yaml 的 download.dir 改写为本机推导出的下载目录（便携化的关键）。"""
    cfg = install_dir / "config.yaml"
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        want = '"' + str(DOWNLOAD_DIR).replace("\\", "\\\\") + '"'
        lines = cfg.read_text(encoding="utf-8", errors="ignore").splitlines() if cfg.is_file() else []
        out: list[str] = []
        in_dl = False
        patched = False
        for ln in lines:
            if ln.strip() == "download:" and not ln.startswith(" "):
                in_dl = True
                out.append(ln)
                continue
            if in_dl and ln and not ln[0].isspace():
                in_dl = False
            if in_dl and ln.lstrip().startswith("dir:"):
                indent = ln[: len(ln) - len(ln.lstrip())]
                out.append(f"{indent}dir: {want}")
                patched = True
                continue
            out.append(ln)
        if not patched:
            if out and out[-1].strip():
                out.append("")
            out.append("download:")
            out.append(f'  dir: {want}')
        cfg.write_text("\n".join(out) + "\n", encoding="utf-8")
        logger.info("下载器配置已指向本机下载目录: %s", DOWNLOAD_DIR)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写下载器配置失败（不影响启动，落盘目录可能不对）: %s", exc)


def install() -> dict:
    """安装下载器（幂等）：已装 → 只校正配置；未装 → 本地包优先，其次 GitHub 下载。

    新电脑自举：项目目录搬到新机器后，把旧机的 wx_dl_pkg（含 zip）一起拷走即可离线装；
    没有包则现场从 GitHub releases 下载 safe x86_64 版并校验。
    """
    if not IS_WINDOWS:
        return {"installed": False, "platform_supported": False,
                "error": _WINDOWS_ONLY_HINT}
    exe = exe_path()
    src, verified = None, None
    if exe is None:
        pkg = local_pkg()
        if pkg is not None:
            src = f"本地包 {pkg.name}"
            logger.info("使用本地安装包: %s", pkg)
        else:
            PKG_DIR.mkdir(parents=True, exist_ok=True)
            dl_url, sums_url = _latest_release_pkg()
            name = dl_url.rsplit("/", 1)[-1]
            pkg = PKG_DIR / name
            logger.info("从 GitHub 下载安装包: %s", dl_url)
            _fetch_url(dl_url, pkg)
            if sums_url:
                try:
                    _fetch_url(sums_url, PKG_DIR / "checksums.txt", timeout=60)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("下载 checksums.txt 失败（跳过校验）: %s", exc)
            src = f"GitHub {GITHUB_REPO}"
        verified = _verify_pkg(pkg)
        _safe_extract(pkg, INSTALL_DIR)
        exe = exe_path()
        if exe is None:
            raise RuntimeError(f"解压后未找到 {EXE_NAME}（包内容可能不对）：{pkg}")
    _patch_config(INSTALL_DIR)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {"installed": True, "exe": str(exe), "source": src,
            "sha256_verified": verified, "download_dir": str(DOWNLOAD_DIR)}


def _proc_running() -> bool:
    """进程是否在跑。注意：tasklist 在中文 Windows 输出 GBK，必须容错解码。

    非 Windows（云端）没有 tasklist，也没有这个下载器 —— 直接返回 False，
    让上层走"离线/不支持"分支，而不是抛异常。
    """
    import subprocess

    if not IS_WINDOWS:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV"],
                           capture_output=True, timeout=15)
        out = (r.stdout or b"").decode("utf-8", errors="ignore").lower()
        if EXE_NAME.lower() not in out:
            # 双保险：GBK 字节直接匹配（exe 名是 ASCII，两种编码一致）
            out2 = (r.stdout or b"").decode("gbk", errors="ignore").lower()
            return EXE_NAME.lower() in out2
        return True
    except Exception:
        return False


def _port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except Exception:
        return False


def _ps_elevate(script: str) -> tuple[bool, str]:
    """以管理员运行一条 PowerShell 语句（触发 UAC，用户确认后执行）。"""
    import subprocess

    r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=120)
    ok = r.returncode == 0
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    return ok, err


_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _proxy_get() -> tuple[int, str]:
    """读当前系统代理 (ProxyEnable, ProxyServer)。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as k:
            en = int(winreg.QueryValueEx(k, "ProxyEnable")[0] or 0)
            try:
                sv = str(winreg.QueryValueEx(k, "ProxyServer")[0] or "")
            except OSError:
                sv = ""
            return en, sv
    except Exception:  # noqa: BLE001
        return 0, ""


def _proxy_set(enable: int, server: str) -> None:
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as k:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, enable)
            if server:
                winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写系统代理失败: %s", exc)
    # 只改注册表，部分应用（含微信）不会立刻重读；发一次 WinINet 变更通知
    try:
        import ctypes

        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        wininet.InternetSetOptionW(None, 37, None, 0)  # INTERNET_OPTION_REFRESH
    except Exception:  # noqa: BLE001
        pass


def _proxy_backup_path() -> Path:
    return INSTALL_DIR / ".proxy_backup.json"


def _write_proxy_backup_if_needed() -> None:
    """把代理改成本机 MITM 前，备份用户原始代理（只备份一次，保留最早值）。

    - 已有有效备份 → 不动（可能上次还没还原）；
    - 备份损坏/为空 → 重新写；
    - 当前已指向 MITM → 不备份（否则会把 MITM 自己当"原始值"）。
    """
    backup = _proxy_backup_path()
    try:
        if backup.is_file():
            try:
                data = json.loads(backup.read_text(encoding="utf-8") or "{}")
                if "enable" in data or "server" in data:
                    return
            except Exception:  # noqa: BLE001
                pass
        en, sv = _proxy_get()
        if _proxy_points_to_mitm(sv):
            return
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps({"enable": en, "server": sv}), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("备份系统代理失败: %s", exc)


def _launch_plain(exe: Path) -> bool:
    """免提权直接拉起进程（证书已装过时足够；不弹 UAC）。

    用 CREATE_NO_WINDOW（而非 DETACHED_PROCESS）：给下载器一个隐藏控制台，
    它之后拉起的辅助命令（reg/taskkill 等）会继承这个隐藏控制台，
    否则每个辅助命令都会闪一个新的黑窗（2026-10-30 用户实测反馈）。
    """
    import subprocess

    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags = subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE：最小化且不激活
        subprocess.Popen([str(exe)], cwd=str(INSTALL_DIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000,  # CREATE_NO_WINDOW
                         startupinfo=si)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("免提权启动失败: %s", exc)
        return False


def _api_ready(timeout: float = 1.0) -> bool:
    """轻量就绪检查：只打 /api/status。

    启动过程中端口可能已经打开、但 API 还没 ready；用完整 probe() 会连打 4 个
    接口（每个 5 秒超时），这正是"点启动后很久才弹 UAC"的主因。
    """
    try:
        _get("/api/status", timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def _wait_online(wait_s: float) -> bool:
    import time

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _api_ready(1.0):
            return True
        time.sleep(0.4)
    return False


def _mark_started_by_us() -> None:
    """标记"下载器是本进程拉起的"——退出清理据此决定要不要替用户停掉它。"""
    global _STARTED_BY_US
    _STARTED_BY_US = True


def start(wait_s: float = 60.0, allow_elevate: bool = True) -> dict:
    """启动下载器。已在运行 → 直接返回。

    流程：先免提权直接拉起（证书已装过时不需要管理员，不弹 UAC）；
    失败且 allow_elevate=True 时才走 UAC 提权兜底（首次装证书/被策略拦时需要）。
    自动拉起场景（服务启动时）必须 allow_elevate=False，绝不在后台弹 UAC。
    """
    if not IS_WINDOWS:
        return {"started": False, "platform_supported": False,
                "error": _WINDOWS_ONLY_HINT}
    if _api_ready(1.0):
        return {"started": True, "already": True}
    exe = exe_path()
    if exe is None:
        return {"started": False, "error": "未安装",
                "hint": "请先安装（页面上点「一键安装并启动」，或 POST /ext/setup）"}
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _write_proxy_backup_if_needed()

    # 1) 免提权启动（常规路径，无 UAC）；只等一小段就转 UAC，
    #    避免"免提权进程起不来但要干等 60 秒才弹管理员确认框"
    try:
        plain_wait_s = min(wait_s, float(os.getenv("TRANSCRIPT_EXT_DL_PLAIN_WAIT", "8") or 8))
    except Exception:  # noqa: BLE001
        plain_wait_s = min(wait_s, 8.0)
    if _launch_plain(exe) and _wait_online(plain_wait_s):
        _mark_started_by_us()
        return {"started": True, "elevated": False, "proxy": proxy_status()}

    if not allow_elevate:
        return {"started": False, "error": "自动启动未就绪",
                "process_running": _proc_running(),
                "ports": {"2022": _port_open(2022), "2023": _port_open(2023)},
                "hint": ("免提权启动后 API 未就绪：可能证书未安装或被杀软拦截。"
                         "请在页面上点一次「一键安装并启动」（会弹管理员确认），之后恢复自动。")}

    # 2) UAC 提权兜底（手动点击场景才允许）
    ps = (f"Start-Process -FilePath '{INSTALL_DIR / EXE_NAME}' "
          f"-WorkingDirectory '{INSTALL_DIR}' -Verb RunAs")
    ok, err = _ps_elevate(ps)
    if not ok:
        return {"started": False, "error": "启动失败",
                "hint": "UAC 被取消或无法提权（管理员确认框需要点「是」）",
                "detail": err[:300]}
    if _wait_online(wait_s):
        _mark_started_by_us()
        return {"started": True, "elevated": True, "proxy": proxy_status()}
    return {"started": False, "error": "已尝试启动但 API 未就绪",
            "process_running": _proc_running(),
            "ports": {"2022": _port_open(2022), "2023": _port_open(2023)},
            "hint": ("请到它的窗口/托盘看是否有待确认弹窗（证书/代理），"
                     f"或查看日志 {LOG_HINT}；也可被杀软拦截")}


def stop(wait_s: float = 20.0, allow_elevate: bool = True) -> dict:
    """停止下载器并还原用户原系统代理。

    先试普通 taskkill（免提权启动的进程够用）；进程是管理员身份时才走 UAC 兜底。
    allow_elevate=False 时绝不弹 UAC（退出清理用：关机时弹确认框体验很糟）。
    """
    if not IS_WINDOWS:
        # 云端本来就没有这个进程："已停止"更符合调用方语义（退出钩子不会误报）
        return {"stopped": True, "already": True, "platform_supported": False,
                "note": _WINDOWS_ONLY_HINT}
    if not _proc_running():
        return {"stopped": True, "already": True, "proxy": _restore_proxy()}
    import subprocess
    import time

    killed = False
    try:
        r = subprocess.run(["taskkill", "/IM", EXE_NAME, "/F"],
                           capture_output=True, timeout=20)
        killed = (r.returncode == 0)
    except Exception:  # noqa: BLE001
        killed = False
    if killed:
        # 正常杀掉后进程通常立刻消失；只等很短，避免失败时白等
        deadline = time.time() + 2
        while time.time() < deadline and _proc_running():
            time.sleep(0.3)
    if _proc_running():  # 没权限/杀不掉 → 立刻走 UAC 兜底
        if not allow_elevate:
            # 进程杀不掉也要把代理还原掉：否则机器留在"代理指向已停止的 MITM"
            return {"stopped": False, "error": "进程以管理员运行，未提权时无法停止",
                    "proxy": _restore_proxy()}
        ok, err = _ps_elevate(
            "Start-Process taskkill -Verb RunAs -WindowStyle Hidden "
            f"-ArgumentList '/IM {EXE_NAME} /F'")
        if not ok:
            return {"stopped": False, "error": "停止失败（需要管理员确认）", "detail": err[:300],
                    "proxy": _restore_proxy()}
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(1.5)
            if not _proc_running():
                break
        else:
            return {"stopped": False, "error": "进程仍在运行，请从它的界面手动退出",
                    "proxy": _restore_proxy()}
    return {"stopped": True, "proxy": _restore_proxy()}


def _proxy_points_to_mitm(server: str) -> bool:
    """ProxyServer 是否指向本机 MITM 端口。

    ProxyServer 可能是 "127.0.0.1:2023" 或 "http=127.0.0.1:2023;https=127.0.0.1:2023"。
    """
    s = (server or "").strip()
    if not s:
        return False
    for token in s.replace(" ", "").split(";"):
        host = token.split("=", 1)[-1].rstrip("/")
        if host.endswith(f":{PROXY_PORT}") and ("127.0.0.1" in host or "localhost" in host):
            return True
    return False


def proxy_status() -> dict:
    """当前系统代理状态 + 是否指向本机 MITM（供页面展示与体检）。

    区分两个概念：
      is_mitm      ProxyServer 字符串指向本机 MITM 端口
      active_mitm  代理**开着**且指向 MITM —— 这时"下载器一死就全机断网"
    """
    en, sv = _proxy_get()
    mitm = _proxy_points_to_mitm(sv)
    return {"enable": int(en), "server": sv, "is_mitm": mitm,
            "active_mitm": bool(en) and mitm, "mitm_port": PROXY_PORT}


def use_mitm_proxy() -> dict:
    """一键把系统代理指向本机 MITM（注入下载按钮的必要条件）。

    会先备份用户原始代理；停止下载器/退出服务时由 _restore_proxy() 还原。
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": _WINDOWS_ONLY_HINT}
    _write_proxy_backup_if_needed()
    _proxy_set(1, f"127.0.0.1:{PROXY_PORT}")
    st = proxy_status()
    ok = bool(st.get("is_mitm"))
    return {"ok": ok, "proxy": st,
            "detail": (f"系统代理已指向本机 127.0.0.1:{PROXY_PORT}" if ok
                       else "写入后校验未通过，请检查系统代理设置")}


def sanity_check_proxy() -> dict:
    """启动体检：代理指向本机 MITM 但下载器不在线 → 还原/关闭。

    强杀 / 断电时系统代理会残留在 MITM 端口上，那样**全机断网**且用户找不到原因。
    这是唯一能兜住"上次没退干净"的时机，所以每次服务启动都跑一次（很轻）。
    """
    st = proxy_status()
    if not (st["enable"] and st["is_mitm"]):
        return {"action": "none", "proxy": st, "detail": "系统代理未指向本机 MITM"}
    if probe().get("online"):
        return {"action": "kept", "proxy": st, "detail": "下载器在线，代理保持不动"}
    detail = _restore_proxy() or "系统代理已关闭"
    logger.warning("启动体检：系统代理指向本机 MITM(%s) 但下载器不在线 → %s",
                   st["server"] or f"127.0.0.1:{PROXY_PORT}", detail)
    return {"action": "restored", "proxy": proxy_status(), "detail": detail}


def proxy_watchdog_loop(interval: float = 60.0, consecutive_misses: int = 3) -> None:
    """常驻看门狗：下载器崩溃/被强杀后，系统代理仍指向本机 MITM → 自动还原。

    2026-11-02 修复：原来只在服务启动那一刻做一次 sanity_check_proxy（main.py 里那个
    一次性线程），服务常驻期间下载器死掉就没人管 → 全机流量一直指向无人监听的
    127.0.0.1:2023，用户只会看到"突然全都上不了网"。这里每 interval 秒复查一次：

    - 代理没指向 MITM  → 什么都不做；
    - 进程都不在了      → 直接判定离线（不用等）；
    - 进程还在但 API 连续 consecutive_misses 次探不到 → 才算掉线，避免下载繁忙时误还原。
    """
    import time

    if not IS_WINDOWS:
        return
    misses = 0
    logger.info("代理看门狗已启动（每 %ss 复查一次，连续 %s 次探不到才还原）",
                int(interval), consecutive_misses)
    while True:
        time.sleep(max(5.0, float(interval)))
        try:
            st = proxy_status()
            if not (st["enable"] and st["is_mitm"]):
                misses = 0
                continue
            alive = _proc_running()
            if alive and probe().get("online"):
                misses = 0
                continue
            if alive:
                misses += 1
                if misses < max(1, int(consecutive_misses)):
                    continue
            detail = _restore_proxy() or "系统代理已关闭"
            logger.warning("代理看门狗：系统代理指向本机 MITM(%s) 但下载器已不在线（%s）→ %s",
                           st["server"] or f"127.0.0.1:{PROXY_PORT}",
                           f"进程在，连续 {misses} 次探不到" if alive else "进程已退出", detail)
            misses = 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("代理看门狗本轮异常（忽略）: %s", exc)


def restore_proxy() -> dict:
    """手动还原系统代理（页面上的"还原代理"按钮）。"""
    before = proxy_status()
    detail = _restore_proxy() or "系统代理无需还原"
    return {"restored": True, "before": before, "after": proxy_status(), "detail": detail}


def shutdown_cleanup() -> dict:
    """进程退出前的清理：绝不让机器留在"系统代理指向本机 MITM"的状态。

    - 下载器是本进程拉起的 → 停掉它（stop() 内部还原代理），但不弹 UAC；
    - 代理仍指向本机 MITM → 还原（工具关了就不该继续接管全机流量）；
    - 都没命中 → 不动。
    幂等：FastAPI shutdown 与 atexit 都会调它。
    """
    global _CLEANED, _STARTED_BY_US
    if _CLEANED:
        return {"action": "done", "detail": "已清理过"}
    _CLEANED = True
    out: dict = {"action": "none", "detail": "无需处理"}
    try:
        stopped = False
        if _STARTED_BY_US and _proc_running():
            res = stop(allow_elevate=False)
            stopped = bool(res.get("stopped"))
            logger.info("退出清理：停止下载器%s（%s）",
                        "成功" if stopped else "未成功（管理员进程，未提权）",
                        res.get("proxy") or res.get("error") or "")
            _STARTED_BY_US = False
            out = {"action": "stopped" if stopped else "stop_failed", "stop": res}
        st = proxy_status()
        if st["enable"] and st["is_mitm"]:
            if not stopped:
                detail = _restore_proxy()
                logger.info("退出清理：还原系统代理（%s）→ %s", st["server"], detail or "无需还原")
                if out.get("action") in (None, "none"):
                    out["action"] = "restored"
                    out["detail"] = detail
            st = proxy_status()
        out["proxy_status"] = st      # 结果里总带上代理现状，方便日志/接口核对
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("退出清理失败: %s", exc)
        return {"action": "error", "detail": str(exc)}


_CLEANED = False


def _restore_proxy() -> str:
    """强杀不会恢复系统代理：有备份就还原，没备份且指向 MITM 就先关掉。"""
    backup = _proxy_backup_path()
    try:
        if backup.is_file():
            b = json.loads(backup.read_text(encoding="utf-8"))
            _proxy_set(int(b.get("enable") or 0), str(b.get("server") or ""))
            backup.unlink(missing_ok=True)
            return f"已还原系统代理（ProxyEnable={b.get('enable')} {b.get('server') or ''}）"
        en, sv = _proxy_get()
        if _proxy_points_to_mitm(sv):
            _proxy_set(0, sv)
            return "系统代理已关闭（未找到启动前备份；如需 clash 请重新开启）"
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("还原系统代理失败: %s", exc)
        return "系统代理还原失败，请手动检查（设置 → 代理）"