"""打包发布：生成可分发的绿色文件夹/zip（代码 + 一键启动 + 新手文档）。

用法（在 transcript_tool/ 下）:
    python scripts/build_release.py --check    # 只列出将被打包的内容，不写盘
    python scripts/build_release.py            # 打包
    python scripts/build_release.py --bump patch   # 先递增 VERSION 再打包

产物:
    dist/文案提取工具-v<版本>/        可直接运行的文件夹
    dist/文案提取工具-v<版本>.zip     发给别人的压缩包

★ 目录结构：外层包名 / transcript_tool/（代码都在内层，文案输出在外层）。

包里有什么:   代码、start.bat 一键启动、一份合并文档（使用说明+排错+第三方声明）
包里没有什么: whisper 模型、Playwright 内核、ffmpeg（首次运行自动下载）、
              .env、cookies/、历史库、日志（本机私有数据，绝不外带）
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # .../transcript_tool
DIST = ROOT / "dist"
VERSION_FILE = ROOT / "VERSION"
# 2026-11-05：视频号改走「元宝登录态换直链」，外部下载器（wx_dl_pkg，约 36MB）
# 连同其离线安装包一起移除；发布包从 ~38MB 降到 ~2MB。


def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def bump_version(cur: str, part: str) -> str:
    try:
        major, minor, patch = (int(x) for x in cur.split(".")[:3])
    except Exception:  # noqa: BLE001
        major, minor, patch = 0, 0, 0
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


# 白名单式收集：只打包这些，避免误带运行产物
INCLUDE_DIRS = ["app", "static", "scripts", "docs"]
INCLUDE_FILES = [
    "VERSION", "CHANGELOG.md",
    "start.bat", "run.py", "check_env.py", "login_cookie.py",
    "requirements.txt", "requirements-dev.txt", ".env.example",
    "README.md", "pytest.ini",
]

EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", "tasks", "tmp_uploads",
                "tools", "dist", "cookies", "_asr_diff"}
EXCLUDE_GLOBS = [
    "*.pyc", "*.pyo", "*.log", "*.db", "*.db-wal", "*.db-shm", "*.zip",
    ".env", ".api_token", "queue_state.json", "watch_autostart.json", "watch_dedupe.json",
    "soak_*", "final_*.png",
    # 曾用 "_*" 一把梭，结果把 app/__init__.py 也排除了 —— 36MB 版靠"命名空间包"
    # 侥幸能跑，嵌入式 Python 下直接 ModuleNotFoundError（2026-11-02 实测踩到）。
    # 只列真正要排除的临时文件类型：
    "_*.log", "_*.json",
]

# 顶层入口：纯 ASCII（bat 里带中文会踩编码坑），把控制权交给内层
TOP_START_BAT = """@echo off
title Transcript Tool
rem Entry point: hand over to transcript_tool\\start.bat (logic lives in scripts\\launch.py)
call "%~dp0transcript_tool\\start.bat"
"""

DOC_INTRO = """\
========================================
   文案提取工具 · 使用说明（简明版）
========================================

【一句话】把视频或录音丢进来，自动变成文字稿。


──────── 怎么用（就两步） ────────

第 1 步：解压
    右键压缩包 →「解压到当前文件夹」。
    ★ 不要在压缩包里直接双击运行。

第 2 步：双击 start.bat

    · 电脑没装 Python？不用管 —— 它会自动下载并安装（仅当前用户，
      不需要管理员），然后自动装好全部依赖。
    · 第一次要等 5~15 分钟（需联网下载几百 MB），以后再开就是秒级。
    · 看到  Uvicorn running on http://127.0.0.1:8000  就是成功了，
      浏览器会自动打开操作界面；没打开就手动访问  127.0.0.1:8000。
    · 黑窗口是程序本体，用的时候别关；关掉黑窗口＝停止服务。

以后每次用，都只要双击 start.bat。


──────── 界面用法 ────────

· 粘贴链接：抖音 / B站 / 快手 / 小红书 / 视频号 的链接粘进输入框，点开始
· 上传文件：本地视频、录音，选文件或直接拖进去
· 结果在「文案输出」文件夹（按日期命名的 .txt），
  也可以在界面的「历史记录」里查看、复制、导出


──────── 可选配置（不弄也能正常用） ────────

· AI 纠错 / AI 笔记：记事本打开 transcript_tool\\.env，
  在  TRANSCRIPT_LLM_API_KEY=  后面填上 key，保存。
· 视频号要拿到视频：先到「账号」页点「视频号 · 元宝登录」微信扫码
  （凭证约 60 天有效）；只要文案则不需要。
"""

DOC_TROUBLE = """\
========================================
   常见问题
========================================

1. 提示 Python 自动安装失败 / 所有下载源都失败
   → 检查网络后重新双击 start.bat；还不行就手动装 Python：
     https://www.python.org/downloads/
     ★ 务必勾选第一屏下方的  Add python.exe to PATH

2. 第一次卡在下载很慢
   → 正常，要下载几百 MB；超过 10 分钟没动静，关掉黑窗口重新双击

3. 浏览器没自动打开
   → 手动打开浏览器，地址栏输入  http://127.0.0.1:8000

4. 提示端口被占用 / 8000 打不开
   → 说明已经开着一个了；关掉旧黑窗口，再双击 start.bat

5. 杀毒软件报警或删文件
   → 首次启动会下载 ffmpeg、浏览器内核，容易被误报；
     把解压出来的文件夹加入杀毒软件白名单

6. 转写很慢
   → 首次转写要下载语音模型，之后就快了；有 NVIDIA 显卡会快很多

7. 还是不行
   → 把  transcript_tool\\app.log  和  transcript_tool\\server_err.log
     发给技术人员，并说清你双击了什么、屏幕上报了什么


──────── 数据与备份 ────────

导出的文案    文案输出\\（包根目录）
历史记录      transcript_tool\\transcript_history.db   （删掉＝清空历史）
配置          transcript_tool\\.env                    （含 API key）
"""

DOC_THIRDPARTY = """\
========================================
   第三方组件与更新记录
========================================

ffmpeg / Playwright Chromium / faster-whisper 模型 / yt-dlp / RapidOCR 等
不随包附带，由程序首次运行时自动下载，许可说明见 transcript_tool\\ 内对应目录。
各版本更新记录见 transcript_tool\\CHANGELOG.md。
"""


def _md_to_text(md: str) -> str:
    """把 CHANGELOG 的 markdown 轻量转成纯文本，好在 .txt 里读。"""
    out = []
    for line in md.splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line)          # 去掉标题符号
        line = re.sub(r"^(\s*)[-*]\s+", r"\1· ", line)   # 列表项统一成 ·
        line = line.replace("**", "").replace("`", "")
        out.append(line)
    return "\n".join(out).strip()


def _excluded(p: Path) -> bool:
    if p.name in EXCLUDE_DIRS:
        return True
    # 包标记文件绝不能被排除（历史上被 "_*" 误伤过）
    if p.name == "__init__.py":
        return False
    return any(fnmatch.fnmatch(p.name, g) for g in EXCLUDE_GLOBS)


def collect() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_DIRS:
        d = ROOT / name
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts):
                continue
            if _excluded(p):
                continue
            files.append(p)
    for name in INCLUDE_FILES:
        f = ROOT / name
        if f.is_file() and not _excluded(f):
            files.append(f)
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只列出内容，不写盘")
    ap.add_argument("--bump", choices=["major", "minor", "patch"],
                    help="先把 VERSION 递增，再打包")
    ap.add_argument("--zip", action="store_true",
                    help="额外压成 zip（默认不压：只留文件夹 + 使用说明，用户要求）")
    args = ap.parse_args()

    version = read_version()
    if args.bump and not args.check:
        version = bump_version(version, args.bump)
        VERSION_FILE.write_text(version + "\n", encoding="utf-8")
        print(f"[i] 版本已递增 -> {version}")
    print(f"当前版本: v{version}")

    files = collect()
    if not files:
        print("[!] 没有可打包的内容，检查 INCLUDE_DIRS / INCLUDE_FILES")
        return 1

    total = sum(f.stat().st_size for f in files)
    print(f"代码侧 {len(files)} 个文件，共 {total / 1048576:.1f} MB")
    for f in files[:10]:
        print("   ", f.relative_to(ROOT))
    if len(files) > 10:
        print(f"    ... 其余 {len(files) - 10} 个")

    # 安全检查：绝不打包这些
    leaked = [f for f in files
              if f.name == ".env" or f.suffix in (".db", ".db-wal", ".db-shm")
              or "cookies" in f.parts]
    if leaked:
        print("[!] 检测到不应打包的文件，已中止:")
        for f in leaked[:10]:
            print("   ", f.relative_to(ROOT))
        return 1

    if args.check:
        print("\n[--check] 未写盘。去掉 --check 即开始打包。")
        return 0

    name = f"文案提取工具-v{version}"
    stage = DIST / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # 代码放进内层 transcript_tool/，复刻原始布局（见文件头注释）
    inner = stage / "transcript_tool"
    for f in files:
        dst = inner / f.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
    # 顶层入口 + 单一文档（版本信息 / 使用说明 / 排错 / 第三方声明 合成一份）
    (stage / "start.bat").write_text(TOP_START_BAT, encoding="ascii", newline="\r\n")

    commit = ""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                                capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    bar = "=" * 60
    head = [bar,
            "   文案提取工具 · 使用说明",
            f"   版本 {version} ｜ 打包于 {datetime.datetime.now():%Y-%m-%d %H:%M}",
            "   首次使用自动配置 Python 与运行环境（需联网）",
            bar, ""]
    dirty = ""
    try:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    if commit:
        head.insert(4, f"   代码提交 {commit}" + ("（含未提交改动）" if dirty else ""))
    head += ["   升级前请先备份这三项：",
             "       transcript_tool\\transcript_history.db   历史记录",
             "       文案输出\\（包根目录）               导出的文案",
             "       transcript_tool\\.env                     配置（含 API key）",
             ""]
    divider = "\n\n" + "─" * 60 + "\n\n"
    doc = ("\n".join(head) + "\n"
           + DOC_INTRO.strip() + divider
           + DOC_TROUBLE.strip() + divider
           + DOC_THIRDPARTY.strip() + "\n")
    # 更新记录不再整段附到文档末尾（2026-11-09 文档求简）：
    # CHANGELOG.md 本身随包附带，DOC_THIRDPARTY 末尾留了一行指引。
    (stage / "使用说明.txt").write_text(doc, encoding="utf-8-sig")

    # 发布红线（用户 2026-11-09 要求）：解压后双击 start.bat 必须开箱即用，
    # Python 与全部依赖由启动链自动配置。打包后校验启动链文件齐全，缺一个就中止。
    redline = [stage / "start.bat",
               inner / "start.bat",
               inner / "scripts" / "setup_python.ps1",
               inner / "scripts" / "launch.py"]
    missing = [str(p.relative_to(stage)) for p in redline if not p.is_file()]
    if missing:
        print("[!] 发布红线校验失败：启动链文件缺失（双击 start.bat 开箱即用会坏）:")
        for m in missing:
            print("   ", m)
        return 1


    # 默认不压 zip：用户要求"只保留文件 + 更新版本更新记录"，需要时显式 --zip
    if args.zip:
        zip_path = DIST / f"{name}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    z.write(p, arcname=str(Path(name) / p.relative_to(stage)))
        size = zip_path.stat().st_size
    else:
        zip_path = None

    # 历史版本一律保留（用户 2026-11-04 明确要求：后续版本更新都要保留旧版本）。
    # 这里只读目录、不做任何清理；任何涉及 dist 历史版本的删除都必须先单独问用户。
    kept = sorted((p for p in DIST.glob("文案提取工具-v*") if p.is_dir() and p != stage),
                  key=lambda p: p.stat().st_mtime)

    print("\n打包完成:")
    print(f"   文件夹: {stage}")
    print(f"   历史版本: 已保留 {len(kept)} 个（dist 只增不删）"
          + (f"，最新: {kept[-1].name}" if kept else ""))
    if zip_path is not None:
        print(f"   压缩包: {zip_path}  ({size / 1048576:.1f} MB)")
    else:
        print("   压缩包: 已跳过（未加 --zip；需要发人时再加 --zip）")
    print("   结构  : start.bat + 使用说明.txt + transcript_tool/")
    print("   发布红线: 双击 start.bat 开箱即用（Python/依赖自动配置）—— 启动链已校验齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())