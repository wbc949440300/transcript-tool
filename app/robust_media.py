"""稳健的多源媒体下载（针对 yt-dlp 平台反爬与 CDN 波动）。

核心策略：
1. 多格式候选：选择器支持多个音频/视频格式优先级，平台可能返回多 CDN 源。
2. 多轮重试：外层指数退避重试，每次重新初始化 YoutubeDL 并轮换 UA。
3. 反爬参数：动态 UA、socket 超时、分块下载（http_chunk_size）、重试 DNS/连接。
4. 残留清理：每次失败清理部分下载文件。
"""

from __future__ import annotations

import logging
import random
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from .platforms import adapter_for
from .utils import (classify_failure, cookie_file_for, pick_account, platform_of,
                    proxy_for, should_use_cookie, throttle, throttle_platform, _host_of_url)

logger = logging.getLogger("robust_media")

# 常见桌面浏览器 UA 池，用于反爬轮换
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.2088.76",
]

# 音频优先格式选择器（多级 fallback）
AUDIO_FORMATS = [
    "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio[ext=opus]/bestaudio[ext=mp3]/bestaudio/best",
    "bestaudio[acodec!=none]/best" + "/best",
    "worstaudio/worst",
]
# 视频优先格式选择器（多级 fallback，含不同画质）
VIDEO_FORMATS = [
    "best[ext=mp4][height<=1080]/best[ext=mp4]/best",
    "worst[ext=mp4]/best[height<=720]/best",
    "best",
]

_ACCEPTED_AUDIO_EXT = (".m4a", ".mp3", ".webm", ".opus", ".aac", ".ogg", ".wav", ".flac")


def _referer_for(url: str) -> str:
    """按目标站点生成 Referer（写死 B站会让抖音/小红书等下载被拒）。"""
    u = (url or "").lower()
    for host in ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com",
                 "kuaishou.com", "bilibili.com", "b23.tv", "tiktok.com"):
        if host in u:
            return "https://www." + host.split("/")[0] + "/"
    return ""


def _mk_opts(outtmpl: str, fmt: str, user_agent: str, referer: str = "",
             cookiefile: Optional[str] = None, extra: Optional[dict] = None,
             proxy: Optional[str] = None) -> dict:
    """构造一次下载的参数（含反爬增强）。"""
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 10,                 # yt-dlp 内部重试碎片/连接
        "fragment_retries": 10,
        "http_chunk_size": 10_485_760, # 10MB 分块，利于断点续传与多源
        "continuedl": True,             # 断点续传
        "http_headers": headers,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if proxy:
        opts["proxy"] = proxy
    if extra:
        opts.update(extra)
    return opts


def _pick_downloaded(work_dir: Path, pattern: str = "audio") -> Optional[Path]:
    """从工作目录挑出实际下载的音/视频文件。

    pattern 是本次请求的类型（audio/video）：ASR 之后同目录会同时存在
    audio.* 和 video.*，必须优先取请求的那类，否则视频下载会误拿 audio.m4a。
    """
    preferred = sorted(work_dir.glob(f"{pattern}.*"))
    other = sorted(work_dir.glob("audio.*" if pattern == "video" else "video.*"))
    for candidates in (preferred, other):
        for c in candidates:
            if c.is_file() and c.suffix.lower() in _ACCEPTED_AUDIO_EXT and c.stat().st_size > 0:
                return c
        # 兜底：开头匹配的任意非临时媒体文件
        for c in candidates:
            if c.is_file() and c.suffix.lower() not in (".part", ".ytdl", ".tmp") and c.stat().st_size > 0:
                return c
    # 再兜底：工作目录下任何大于 0 的实际媒体文件
    for c in work_dir.iterdir():
        if c.is_file() and c.suffix.lower() not in (".part", ".ytdl", ".tmp", ".json") and c.stat().st_size > 0:
            return c
    return None


def _cleanup_partial(work_dir: Path) -> None:
    """清理失败的 .part 等临时碎片文件。"""
    try:
        for f in work_dir.iterdir():
            if f.suffix.lower() in (".part", ".ytdl", ".tmp"):
                f.unlink(missing_ok=True)
    except Exception:
        pass


def download_media(work_url: str, work_dir: Path,
                   media_type: str = "audio",
                   max_attempts: int = 3,
                   on_attempt: Optional[Callable[[int, int, str], None]] = None) -> Optional[Path]:
    """多源 + 多轮重试下载音频/视频。

    media_type: "audio" | "video"
    返回下载到的本地文件路径，全部失败返回 None。
    """
    import yt_dlp

    formats = AUDIO_FORMATS if media_type == "audio" else VIDEO_FORMATS
    pattern = "audio" if media_type == "audio" else "video"
    outtmpl = str(work_dir / f"{pattern}.%(ext)s")

    # UA 整条任务固定为一个（不再逐次重试换脸）：
    # "同一账号 + 同一 IP 短时间内表现为多个浏览器"本身就是异常信号（2026-10-31 改）
    ua = random.choice(_UA_POOL)
    plat = platform_of(work_url)
    needs_login = adapter_for(work_url).needs_login
    last_kind = ""

    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        if on_attempt:
            kind_label = "音频" if media_type == "audio" else "视频"
            on_attempt(attempt, max_attempts,
                       f"正在下载{kind_label} (尝试 {attempt}/{max_attempts})...")
        if plat not in ("", "default"):
            throttle_platform(plat)
        else:
            throttle(_host_of_url(work_url))
        # 格式选择器也逐级压榨；每次尝试用一个不同的优先级顺序
        fmt = formats[(attempt - 1) % len(formats)]
        try:
            # 清理上次的碎片
            _cleanup_partial(work_dir)
            # 只有上一轮判定"缺登录态"时才带 cookie（媒体 CDN 极少需要）
            ck = None
            if needs_login and should_use_cookie(work_url, attempt, login_required=(last_kind == "login")):
                acc = pick_account(plat)
                ck = acc or cookie_file_for(work_url)
                if ck:
                    logger.info("yt-dlp 使用账号 cookie（需要登录态）: %s", Path(ck).name)
            opts = _mk_opts(outtmpl, fmt, ua, referer=_referer_for(work_url),
                            cookiefile=ck, proxy=proxy_for(plat))
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([work_url])
            picked = _pick_downloaded(work_dir, pattern=pattern)
            if picked is not None:
                logger.info("音频/视频下载成功 (尝试 %d): %s", attempt, picked.name)
                return picked
            raise RuntimeError("未找到下载完成的媒体文件")
        except Exception as exc:  # noqa: BLE001
            last_kind = classify_failure(exc)
            err = f"尝试 {attempt}/{max_attempts} 失败（{last_kind}）: {type(exc).__name__}: {exc}"
            logger.warning(err)
            errors.append(err)
            _cleanup_partial(work_dir)
            if last_kind == "permanent":
                break            # 内容不存在/不支持：换格式再试也是浪费请求
            if attempt < max_attempts:
                backoff = 2 ** attempt + random.uniform(0, 1)
                time.sleep(backoff)

    logger.error("多源下载全部失败: %s", " | ".join(errors[-3:]))
    return None
