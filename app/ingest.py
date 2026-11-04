"""进水口：把任意来源的视频变成监听目录里的一个文件。

为什么需要这一层
----------------
视频号的**视频流**没有公开接口（见 `app/video_channel.py` 的 docstring、
`docs/匿名边界与资源复评-2026-11-04.md:25`）：匿名接口只给标题/正文/封面/
互动数，不给播放地址，`finder.video.qq.com` 的链要同时带 `encfilekey` 和
`token`，两者只在微信客户端播放时产生。

而"文件之后"的一切本项目已经全自动：

    文件落进监听目录 → watch_folder 轮询 → media_fingerprint 去重
    → ASR → AI 纠错 → OCR → YAML 七章节笔记

所以接入任何来源都只需要一个动作：**把文件放进监听目录**。
本模块把三种来源统一成这一个动作，换来的是来源可替换、下游一行不改：

  ingest_file()        本地已有文件（创作者后台导出 / 录屏 / 浏览器另存为）
  ingest_url()         直链（解析站返回的 CDN 地址、后台导出的下载链）
  ingest_short_uri()   视频号分享链接 → 只能拿元数据（诚实告知，不假装能下）

用法
----
    python -m app.ingest "D:\\video\\a.mp4"
    python -m app.ingest "D:\\video\\a.mp4" --move
    python -m app.ingest "https://.../a.mp4" --name "某个标题.mp4"
    python -m app.ingest "https://weixin.qq.com/sph/xxxxxxxx"
    python -m app.ingest --dir            # 只打印当前监听目录

监听目录解析顺序：`--dir` > `TRANSCRIPT_WATCH_DIR` >
服务记住的监听目录（`watch_autostart.json`）> 下载器下载目录
（`external_dl.DOWNLOAD_DIR`）。
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from .file_transcribe import is_allowed_ext

logger = logging.getLogger("ingest")


def _candidates(explicit: str = "") -> list[tuple[str, Path]]:
    """按优先级列出可能的监听目录来源。"""
    out: list[tuple[str, Path]] = []
    if explicit:
        out.append(("--dir", Path(explicit).expanduser()))
    env = os.getenv("TRANSCRIPT_WATCH_DIR", "").strip()
    if env:
        out.append(("TRANSCRIPT_WATCH_DIR", Path(env).expanduser()))
    try:
        from . import watch_folder

        cur = watch_folder.current_dir()
        if cur is not None:
            out.append(("服务记住的监听目录", cur))
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取监听目录配置失败: %s", exc)
    try:
        from . import external_dl

        out.append(("下载器下载目录", external_dl.DOWNLOAD_DIR))
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取下载器目录失败: %s", exc)
    return out


def target_dir(explicit: str = "", create: bool = True) -> Path:
    """当前监听目录。

    找不到就明确报错 —— 宁可停下，也不要把文件悄悄写到别的地方
    （否则用户以为已入队，实际什么都没发生）。
    """
    last = ""
    for source, path in _candidates(explicit):
        if path.is_dir():
            return path
        if source == "--dir":
            raise SystemExit(f"--dir 指向的目录不存在：{path}")
        if create:
            try:
                path.mkdir(parents=True, exist_ok=True)
                logger.info("监听目录不存在，已创建（来源：%s）：%s", source, path)
                return path
            except Exception as exc:  # noqa: BLE001
                last = f"{source}: {exc}"
                continue
        last = f"{source} 不存在"
    raise SystemExit(
        "找不到监听目录。请先在本工具页面上开启一次「监听文件夹」，"
        "或用 --dir 指定，或设置环境变量 TRANSCRIPT_WATCH_DIR。"
        + (f"（最后错误：{last}）" if last else "")
    )


def _unique_path(directory: Path, name: str) -> Path:
    """避免覆盖已有文件（同名自动加 _1 / _2）。"""
    stem = Path(name).stem or "video"
    suffix = (Path(name).suffix or ".mp4").lower()
    cand = directory / f"{stem}{suffix}"
    n = 1
    while cand.exists():
        cand = directory / f"{stem}_{n}{suffix}"
        n += 1
    return cand


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


def ingest_file(src: str | Path, dest_dir: Optional[Path] = None, name: str = "",
                move: bool = False) -> Path:
    """把本地文件放进监听目录（默认复制，move=True 时为移动）。"""
    src = Path(src).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"文件不存在：{src}")
    final_name = name or src.name
    if not is_allowed_ext(final_name):
        raise ValueError(
            f"不是可转写的音视频格式：{final_name}（支持 mp4/mov/mkv/avi/webm/"
            "flv/m4v/m4a/mp3/wav/aac/ogg/opus/flac/wma）")
    dest = _unique_path(dest_dir or target_dir(), final_name)

    # 先写 .part 再改名：watch_folder 只认白名单后缀，
    # 复制/下载到一半的文件不会被它当成新素材（大文件尤其重要）
    tmp = dest.with_name(dest.name + ".part")
    try:
        if move:
            shutil.move(str(src), str(tmp))
        else:
            shutil.copy2(src, tmp)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    logger.info("已放入监听目录：%s（%s%s）", dest.name, _human(dest.stat().st_size),
                "，原文件已移动" if move else "，原文件保留")
    return dest


def ingest_url(url: str, dest_dir: Optional[Path] = None, name: str = "",
               headers: Optional[dict] = None) -> Path:
    """下载 http(s) 直链并落进监听目录（HLS/DASH 自动转交 yt-dlp）。"""
    from .external_dl import local_download

    raw = (url or "").strip()
    if not raw:
        raise ValueError("url 不能为空")
    if not raw.lower().startswith(("http://", "https://")):
        raise ValueError("只接受 http/https 直链（分享页请传本地文件或先自行下载）")
    dest = dest_dir or target_dir()
    path = local_download(raw, dest, filename=name, headers=headers)
    logger.info("直链已落进监听目录：%s（%s）", path.name, _human(path.stat().st_size))
    return path


# 元数据接口对失效/不存在的 shortUri 也会正常返回 200，只是标题变成这些占位文案。
# 不识别它们的话，用户会看到"标题：此内容暂时无法播放"却以为是自己的链接有问题。
_PLACEHOLDER_TITLES = {
    "此内容暂时无法播放", "该内容暂时无法查看", "内容已删除", "此内容已被删除",
    "该内容已被删除", "视频不存在",
}


def ingest_short_uri(url: str) -> dict:
    """视频号分享链接 → 元数据（标题/正文/话题/互动数）。

    明确不做的事：不假装能取视频。这里返回 `video: None`，
    并把"为什么"写进 `reason`，避免用户以为下载失败了。
    """
    from . import video_channel

    uri = video_channel.short_uri_from(url)
    if not uri:
        raise ValueError(
            "链接里没找到视频号的 shortUri（形如 https://weixin.qq.com/sph/xxxxxxxx）")
    meta = video_channel.fetch_feed_info(uri)
    if not meta:
        return {"ok": False, "shortUri": uri,
                "reason": "元数据接口没返回内容（链接可能已失效，或被限流）"}
    title = (meta.get("title") or "").strip()
    if title in _PLACEHOLDER_TITLES and not (meta.get("description") or "").strip():
        return {"ok": False, "shortUri": uri,
                "reason": f"接口只返回了占位文案「{title}」：链接已失效、作品被删，"
                          "或这条分享链接本身无效"}
    meta["short_uri"] = uri
    video_channel.remember(meta)   # 文件日后落进监听目录时，用它把文件名换成真标题
    out = {"ok": True, "shortUri": uri, "video": None}
    for key in ("title", "author", "description", "publishAt", "tags",
                "likes", "commentCount", "favCount", "forwardCount",
                "dynamicExportId", "location"):
        out[key] = meta.get(key)
    out["reason"] = ("视频号视频流没有公开接口：这里只能拿到正文文案，"
                     "视频文件要另外取（创作者后台导出 / 录屏 / 微信内下载）")
    return out


def _print_short_uri(info: dict) -> None:
    if not info.get("ok"):
        print(f"取不到元数据：{info.get('reason')}")
        return
    print("—— 视频号作品信息（不含视频文件）——")
    print(f"标题/开头：{info.get('title') or '（无）'}")
    print(f"作者    ：{info.get('author') or '（无）'}")
    if info.get("publishAt"):
        print(f"发布    ：{info['publishAt']}")
    if info.get("tags"):
        print(f"话题    ：{' '.join('#' + t for t in info['tags'])}")
    counts = [f"{label} {info.get(key)}" for label, key in
              (("赞", "likes"), ("评", "commentCount"), ("藏", "favCount"),
               ("转", "forwardCount")) if info.get(key)]
    if counts:
        print(f"互动    ：{' / '.join(counts)}")
    desc = (info.get("description") or "").strip()
    if desc:
        print("\n—— 正文文案 ——")
        print(desc)
    else:
        print("\n（这条没有正文文案）")
    print(f"\n注：{info.get('reason')}")


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(
        prog="python -m app.ingest",
        description="把视频放进监听目录（已开启监听时转写会自动开始）")
    ap.add_argument("source", nargs="?", help="本地文件路径 / http(s) 直链 / 视频号分享链接")
    ap.add_argument("--dir", default="", help="指定监听目录（默认自动解析）")
    ap.add_argument("--name", default="", help="落盘文件名")
    ap.add_argument("--move", action="store_true", help="本地文件用移动而不是复制")
    ap.add_argument("--header", action="append", default=[], metavar="K:V",
                    help="下载直链时附加请求头，可重复（如 Referer:https://...）")
    args = ap.parse_args(argv)

    if not args.source:
        print(f"监听目录：{target_dir(args.dir)}")
        return 0

    source = args.source.strip()
    lower = source.lower()
    dest_dir = target_dir(args.dir) if args.dir else None

    try:
        if "weixin.qq.com/sph" in lower or "/finder-preview/" in lower:
            _print_short_uri(ingest_short_uri(source))
            return 0
        if lower.startswith(("http://", "https://")):
            headers = {}
            for raw in args.header:
                k, _, v = raw.partition(":")
                if k.strip() and v.strip():
                    headers[k.strip()] = v.strip()
            path = ingest_url(source, dest_dir=dest_dir, name=args.name,
                              headers=headers or None)
        else:
            path = ingest_file(source, dest_dir=dest_dir, name=args.name,
                               move=args.move)
    except (FileNotFoundError, ValueError) as exc:
        print(f"失败：{exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        # 下载/网络/解码失败统一收口成一句人话，不要把 traceback 甩给用户
        logger.debug("导入失败", exc_info=True)
        print(f"失败：{exc}")
        return 2

    print(f"已落盘：{path}")
    print("若工具页面里已开启「监听文件夹」，转写会自动开始；"
          "没开启就点一次开启（目录要与上面这个一致）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())