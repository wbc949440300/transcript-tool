"""直链媒体下载（本地完成，不依赖任何外部下载器）。

2026-11-05：原 `external_dl.local_download` 随视频号下载器一起移除，但「直链兜底」
本身仍被提取主链路使用（平台详情拿到的 mp4/m3u8 地址、用户手动提交的直链），
所以把这段能力独立出来，行为保持不变：

- mp4 等普通直链：urllib 流式下载，落到目标目录；
- m3u8/mpd：交给 yt-dlp（内部用 ffmpeg 合并）；
- 返回不是媒体（text/html、json）的响应直接判失败，避免把网页存成视频；
- 代理走 http_opener(media=True)，与其它媒体请求同一套出口配置。
"""
from __future__ import annotations

import logging
import mimetypes
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

from .utils import http_opener

logger = logging.getLogger("media_dl")

MEDIA_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v",
             ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac", ".wma"}


def _media_headers(url: str) -> dict:
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
    """下载 http/https 直链到 dest_dir，返回落地文件；失败抛异常。"""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("直链必须是 http/https 资源 URL")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    merged_headers = _media_headers(url)
    merged_headers.update(headers or {})
    if parsed.path.lower().endswith((".m3u8", ".mpd")):
        logger.info("直链下载（HLS/DASH）: %s", url[:100])
        return _download_with_ytdlp(url, dest, filename, merged_headers)

    req = urllib.request.Request(url, headers=merged_headers)
    logger.info("直链下载: %s", url[:100])
    try:
        with http_opener("", media=True).open(req, timeout=timeout) as resp:
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
            logger.info("直链下载完成: %s (%d 字节)", out.name, out.stat().st_size)
            return out
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败: {exc.reason}") from exc


def download_and_wait(url: str, dest_dir, timeout_s: float = 240.0,
                      poll_s: float = 2.0, stable_rounds: int = 2,
                      headers: dict | None = None):
    """直链下载并返回落地文件；失败返回 None（不抛异常），供提取链路兜底。"""
    try:
        return local_download(str(url), dest_dir, headers=headers, timeout=min(float(timeout_s), 300.0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("直链兜底下载失败: %s", exc)
        return None
