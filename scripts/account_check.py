"""逐个账号做可用性巡检（公司给的号导入后，先跑一遍知道哪些能用）。

用法：
    python scripts/account_check.py douyin                # 巡检全部（默认最多 8 个）
    python scripts/account_check.py douyin --limit 3      # 只查前 3 个
    python scripts/account_check.py douyin --list         # 只看池子，不发请求
    python scripts/account_check.py douyin --url <链接>    # 换测试链接（默认用一条公开测试视频）

处理：用该账号真实走一次抓取（抖音=浏览器通道），失败按类型给出人话原因；
     结果写进账号池健康度（`cookies/.accounts.json`），被限流/需登录的号自动进冷却。
耗时：抖音每个号约 20~30 秒（要开浏览器），25 个号约 10 分钟——建议定期或按需跑，不用每次跑。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_URL = {
    "douyin": "https://www.douyin.com/video/7696763176490192155",
    "xiaohongshu": "https://www.xiaohongshu.com/explore/6abbc3fa0000000018005941",
    "bilibili": "https://www.bilibili.com/video/BV11cx76dETE",
    "kuaishou": "https://www.kuaishou.com/short-video/3x5gu3rc5sqd9ni",
}
NO_ACCOUNT_NEEDED = {"kuaishou"}       # 分享页匿名即可


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("platform")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--url", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    platform = args.platform.strip().lower()

    from app.utils import (account_key, classify_failure, list_accounts,
                           mark_account_cooldown, record_account_result)
    from app.platforms import adapter_for

    accounts = list_accounts(platform)
    if args.list or not accounts:
        print(f"{platform} 账号池：{len(accounts)} 个")
        for f in accounts:
            print(f"  - {account_key(f)}（{Path(f).stat().st_size} 字节）")
        if not accounts:
            print("\n池子为空。先导入：python scripts/import_accounts.py " + platform + " <cookie 文件或目录>")
            return 1
        if args.list:
            return 0
    if platform in NO_ACCOUNT_NEEDED:
        print(f"提示：{platform} 分享页匿名即可读取，不需要账号池——直接用即可。")
        return 0

    adapter = adapter_for(f"https://www.{platform}.com/")
    url = args.url or DEFAULT_URL.get(platform, "")
    if not url:
        print(f"没有 {platform} 的默认测试链接，请用 --url 指定一条公开视频。")
        return 2

    todo = accounts[: max(1, args.limit)]
    print(f"将巡检 {len(todo)}/{len(accounts)} 个账号；抖音每个约 20~30 秒，请耐心等。\n")

    # 让 pick_account 只用指定账号，并让抓取端必须带上 cookie（否则默认匿名优先，测不出账号好坏）
    os.environ["TRANSCRIPT_ACCOUNT_FORCE"] = todo[0]
    os.environ["TRANSCRIPT_COOKIE_MODE"] = "always"

    ok_n = bad_n = 0
    for i, ck in enumerate(todo, 1):
        os.environ["TRANSCRIPT_ACCOUNT_FORCE"] = ck
        t0 = time.time()
        reason = ""
        try:
            data = adapter.fetch(url, use_cookie=True) if adapter.fetch else None
            if adapter.fetch is None:                     # yt-dlp 通道（B站等）
                from app.extractor import _extract_info
                data = _extract_info(url)
            ok = bool(data)
            if not ok:
                reason = "返回空数据（可能需要重新登录）"
        except Exception as exc:  # noqa: BLE001
            ok = False
            kind = classify_failure(exc)
            reason = f"[{kind}] {str(exc)[:80]}"
            if kind in ("risk", "login"):
                mark_account_cooldown(ck, 900)
        dt = time.time() - t0
        record_account_result(ck, ok, platform, reason)
        ok_n += 1 if ok else 0
        bad_n += 0 if ok else 1
        flag = "✅ 可用" if ok else "❌ 不可用"
        print(f"  [{i:2}/{len(todo)}] {account_key(ck):12} {flag}  {dt:5.1f}s  {reason}")

    os.environ.pop("TRANSCRIPT_ACCOUNT_FORCE", None)
    print(f"\n汇总：可用 {ok_n} 个，不可用 {bad_n} 个。"
          f"不可用的号已记录原因（页面 /risk/status 能看到）；被限流/需登录的已自动冷却 15 分钟。")
    return 0


if __name__ == "__main__":
    sys.exit(main())