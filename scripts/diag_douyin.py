"""抖音直链体检：一条链接为什么下不下来，逐档看清楚。

用法（在 transcript_tool/ 下）：
    python scripts/diag_douyin.py https://v.douyin.com/xxxx/
    python scripts/diag_douyin.py 7701063828691356937 --probe 10

做三件事（全部只读远端文件头，不整条下载）：
  1. 走和生产链路一样的匿名详情通道，拿 duration / images / bit_rate[]；
  2. 按码率从小到大远程 ffprobe 每一档，标出「有音轨 / 纯画面轨 / 探测失败」；
  3. 给出生产链路会选中的那一档，以及它为什么会被选/被跳。

下次抖音再报「下载失败」，先跑这个，一眼看出是档位问题、CDN 掐连接，还是需要 cookie。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.douyin import fetch_douyin, normalize_url          # noqa: E402
from app.extractor import _douyin_candidate_label, _url_has_audio_stream  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="抖音直链逐档体检")
    ap.add_argument("target", help="作品链接或作品 ID")
    ap.add_argument("--probe", type=int, default=8, help="最多探测几档（默认 8）")
    args = ap.parse_args()

    target = args.target.strip()
    if target.isdigit():
        target = "https://www.douyin.com/video/" + target
    url = normalize_url(target)
    print("作品:", url)

    d = fetch_douyin(url)
    dur = d.get("duration")
    print("时长: %.1f 秒 | 图集: %d 张 | 作者: %s"
          % ((float(dur) / 1000.0 if dur else 0), len(d.get("images") or []), d.get("author") or "-"))

    variants = [v for v in (d.get("video_variants") or [])
                if isinstance(v, dict) and str(v.get("url") or "").startswith("http")]
    rated = [v for v in variants if isinstance(v.get("bitrate"), (int, float)) and v["bitrate"] > 0]
    rated.sort(key=lambda v: v["bitrate"])
    print("直链: %d 档（%d 档有码率）" % (len(variants), len(rated)))

    default = d.get("video_url")
    picked = None
    for i, v in enumerate(rated[: max(1, args.probe)]):
        has = _url_has_audio_stream(v["url"], referer="https://www.douyin.com/")
        mark = {True: "有音轨", False: "纯画面轨", None: "探测失败"}[has]
        star = " ← 生产链路会选它" if has is True and picked is None else ""
        if has is True and picked is None:
            picked = v
        print("  %2d. %-22s %s" % (i + 1, _douyin_candidate_label(d, v["url"]), mark + star))

    if default:
        has = _url_has_audio_stream(default, referer="https://www.douyin.com/")
        print("  默认档: %-22s %s" % (
            _douyin_candidate_label(d, default),
            {True: "有音轨", False: "纯画面轨", None: "探测失败"}[has]))

    if picked is None:
        print("\n结论：前 %d 小档里没有一档带音轨 —— 生产链路会继续往后试，"
              "或落到默认档（大文件，易被 CDN 掐断）。" % args.probe)
        return 1
    print("\n结论：生产链路会选 %s 下载（带音轨的最小档）。"
          % _douyin_candidate_label(d, picked["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
