"""批量导入平台账号 cookie（公司给的通常是一堆 cookie 文件，或从浏览器复制的 cookie 串）。

用法：
    python scripts/import_accounts.py douyin D:\\账号\\*.txt      # 一批文件
    python scripts/import_accounts.py douyin D:\\账号\\           # 整个目录
    python scripts/import_accounts.py douyin --stdin              # 从管道/剪贴板读（每行一条：文件路径 或 cookie 串）
    python scripts/import_accounts.py douyin a.txt --name account_3 --force
    python scripts/import_accounts.py douyin --list               # 只看现有池子

导入前会校验：
  1. 能否解析出至少一条 cookie（Netscape 七字段格式，或 `name=value; name2=value2` 串）
  2. cookie 的域名是否与该平台匹配（不匹配只**警告**，仍导入——有的文件混着多个域）
目标位置：cookies/<平台>/account_N.txt（N 自动取下一个空号；--name 可指定；同名不覆盖除非 --force）

导入后建议跑一次巡检：python scripts/account_check.py <平台>
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLATFORM_DOMAIN = {
    "douyin": "douyin.com",
    "xiaohongshu": "xiaohongshu.com",
    "kuaishou": "kuaishou.com",
    "bilibili": "bilibili.com",
}
# 抖音 cookie 里真正决定登录态的几个（缺失只提示，不阻止导入）
KEY_TOKENS = {
    "douyin": ("sessionid", "sid_tt", "passport_csrf_token", "odin_tt"),
    "xiaohongshu": ("web_session", "a1", "webId"),
    "kuaishou": ("did", "kuaishou.server.web_st"),
    "bilibili": ("SESSDATA", "bili_jct"),
}


def _netscape_lines(text: str) -> list[str]:
    """挑出 Netscape 格式的有效行（七字段、制表符分隔）。"""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[0]:
            out.append("\t".join(parts[:7]))
    return out


def _from_cookie_string(text: str, domain: str) -> list[str]:
    """把 `name=value; name2=value2` 这种串转成 Netscape 行。"""
    out = []
    for pair in re.split(r"[;\n]+", text or ""):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not name or "\t" in name or "\t" in value:
            continue
        out.append(f".{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    return out


def _parse_source(path: Path, platform: str) -> tuple[list[str], list[str]]:
    """返回 (cookie 行, 警告列表)。"""
    domain = PLATFORM_DOMAIN.get(platform, "")
    warns: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        return [], [f"读不到文件: {exc}"]
    lines = _netscape_lines(text)
    if not lines:
        lines = _from_cookie_string(text, domain)
        if lines:
            warns.append("按 cookie 串解析（非 Netscape 文件）")
    if not lines:
        return [], ["解析不出任何 cookie（既不是 Netscape 文件，也不是 name=value 串）"]
    if domain and not any(domain in ln for ln in lines):
        warns.append(f"没有一条 cookie 属于 {domain}，可能拿错平台的号")
    toks = KEY_TOKENS.get(platform, ())
    if toks and not any(f"\t{t}\t" in ln for t in toks for ln in lines):
        warns.append(f"缺少关键字段（应有其一：{', '.join(toks)}）——登录态可能不完整")
    return lines, warns


def _next_index(pool_dir: Path) -> int:
    n = 1
    existing = {p.stem for p in pool_dir.glob("account_*.txt")}
    while f"account_{n}" in existing:
        n += 1
    return n


def _collect_sources(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            files.extend(sorted(q for q in p.glob("*") if q.is_file() and q.suffix.lower() in (".txt", ".cookies")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"  ⚠ 跳过不存在的路径: {t}")
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("platform", help="douyin / xiaohongshu / kuaishou / bilibili")
    ap.add_argument("targets", nargs="*", help="cookie 文件或目录")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读（每行：文件路径 或 cookie 串）")
    ap.add_argument("--name", default="", help="指定编号（如 account_3），仅单个来源时可用")
    ap.add_argument("--force", action="store_true", help="允许覆盖同名账号文件")
    ap.add_argument("--list", action="store_true", help="只列现有池子")
    args = ap.parse_args()

    platform = args.platform.strip().lower()
    if platform not in PLATFORM_DOMAIN and platform not in ("douyin", "xiaohongshu"):
        print(f"⚠ 未知平台 {platform}（仍可导入，但不会有域名校验）")
    pool_dir = ROOT / "cookies" / platform

    if args.list:
        files = sorted(pool_dir.glob("account_*.txt")) if pool_dir.is_dir() else []
        print(f"{platform} 账号池：{len(files)} 个")
        for f in files:
            print(f"  - {f.name}（{f.stat().st_size} 字节）")
        return 0

    sources = _collect_sources(args.targets)
    if args.stdin:
        text = sys.stdin.read()
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            p = Path(ln)
            if p.is_file():
                sources.append(p)
            else:                                  # 当成 cookie 串
                tmp = pool_dir / f"_stdin_{abs(hash(ln)) % 100000}.txt"
                try:
                    pool_dir.mkdir(parents=True, exist_ok=True)
                    domain = PLATFORM_DOMAIN.get(platform, "")
                    tmp.write_text("# from stdin\n" + "\n".join(_from_cookie_string(ln, domain)),
                                   encoding="utf-8")
                    sources.append(tmp)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⚠ 写入临时文件失败: {exc}")
    if not sources:
        print("没有可导入的来源。用法见文件头注释，或加 --stdin 从管道读。")
        return 2

    pool_dir.mkdir(parents=True, exist_ok=True)
    imported, failed = 0, 0
    for src in sources:
        lines, warns = _parse_source(src, platform)
        if not lines:
            print(f"✗ {src.name}: {'；'.join(warns) or '无有效 cookie'}")
            failed += 1
            continue
        if args.name:
            dst = pool_dir / f"{args.name}.txt"
        else:
            dst = pool_dir / f"account_{_next_index(pool_dir)}.txt"
        if dst.exists() and not args.force:
            print(f"✗ {src.name}: {dst.name} 已存在（用 --force 覆盖）")
            failed += 1
            continue
        body = "# 由 scripts/import_accounts.py 导入：" + src.name + "\n" + "\n".join(lines) + "\n"
        dst.write_text(body, encoding="utf-8")
        imported += 1
        note = ("；".join(warns)) if warns else "校验通过"
        print(f"✓ {src.name} → {dst.name}（{len(lines)} 条 cookie）｜{note}")
        if src.name.startswith("_stdin_"):
            try:
                src.unlink()
            except Exception:  # noqa: BLE001
                pass

    print(f"\n完成：导入 {imported} 个，失败 {failed} 个 → {pool_dir}")
    if imported:
        print(f"建议下一步：python scripts/account_check.py {platform}")
    return 0 if imported else 1


if __name__ == "__main__":
    sys.exit(main())