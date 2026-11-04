"""打包发布：生成可分发的绿色 zip（代码 + 离线安装包 + 一键启动 + 新手文档）。

用法（在 transcript_tool/ 下）:
    python scripts/build_release.py --check    # 只列出将被打包的内容，不写盘
    python scripts/build_release.py            # 打包
    python scripts/build_release.py --bump patch   # 先递增 VERSION 再打包

产物:
    dist/文案提取工具-v<版本>/        可直接运行的文件夹
    dist/文案提取工具-v<版本>.zip     发给别人的压缩包

★ 目录结构刻意复刻原始布局（外层包名 / transcript_tool / wx_dl_pkg）：
  external_dl.py 里 `_BASE_DIR = _PROJECT_ROOT.parent`，下载器、下载目录、离线包
  都按它解析。扁平结构会让这些路径跑到包外面去；套一层就全部落在包内。

包里有什么:   代码、start.bat 一键启动、一份合并文档（使用说明+排错+第三方声明）、
              wx_dl_pkg（视频号下载器离线安装包，约 35MB；包内 .env.example 已预置
              自装开关，首次以管理员身份运行 start.bat 即自动装好）
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
# 离线安装包在项目根（transcript_tool 的上一级）——与 _BASE_DIR 的解析基准一致
PKG_SRC = ROOT.parent / "wx_dl_pkg"
# 是否把视频号下载器的离线安装包（约 35MB，占整包 98% 体积）打进去。
# True = 包约 35.9MB，视频号**免联网**自动安装（2026-11-02 用户选定）。
#        配合包内 .env.example 的 TRANSCRIPT_EXT_DL_AUTOSTART=1，
#        首次以管理员身份运行 start.bat 即可全自动装好。
# False = 包只有 ~400KB，视频号首次使用时联网从 GitHub 取（国内常失败）。
INCLUDE_WX_PKG = True


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
   文案提取工具 · 使用说明（新手版）
========================================

【一句话】把视频或录音丢进来，自动把里面说的话变成文字稿。


──────── 你要做的只有三步 ────────

第 1 步：装 Python

    打开  https://www.python.org/downloads/
    点页面中间那个黄色的 "Download Python 3.x.x" 按钮，下载安装包
    双击安装包，第一屏页面最下面有一个勾选框：

         [ ] Add python.exe to PATH

    ★ 一定要勾上它（默认是不勾的，这是新手最容易踩的坑）
    勾好后点 "Install Now"，等它装完，点 Close。

第 2 步：解压

    把 文案提取工具-vX.X.X.zip 右键 →「解压到当前文件夹」。
    ★ 不要直接在压缩包里双击运行，必须先把文件解压出来。
    ★ 建议解压到一个单独的目录（例如 D:\\文案提取工具），不要散在桌面上一堆文件里。

    解压后你会看到这样的结构（不要改动目录层次）：

        文案提取工具-vX.X.X\\
          ├─ start.bat            ← 双击这个（首次请「以管理员身份运行」）
          ├─ 使用说明.txt          ← 本文件（先读它）
          ├─ transcript_tool\\      ← 程序本体（代码都在这里）
          └─ wx_dl_pkg\\           ← 视频号下载器的离线安装包（首次自动安装用）

第 3 步：双击 start.bat

    会出现一个黑色窗口 —— 这是正常的，它就是程序本体，不要关它。
    第一次要等 3~5 分钟（它在自动下载需要的组件，需要联网）。
    等看到这一行就成功了：

         Uvicorn running on http://127.0.0.1:8000

    浏览器会自动打开操作界面。如果没打开，自己打开浏览器，
    地址栏手动输入：  127.0.0.1:8000   回车即可。

以后每次用，只要重复第 3 步：双击 start.bat。


──────── 界面怎么用 ────────

三种投喂方式，任选一种就行：

  1) 粘贴链接 —— 把抖音 / B站 / 快手 / 小红书 等平台的视频链接
     粘进输入框，点开始，等它跑完。

  2) 上传文件 —— 本地的视频、录音文件，选文件或直接拖进去。

  3) 丢进文件夹 —— 把视频文件拷贝到「transcript_tool 的上一级」下的
     wx_downloads 文件夹（首次启动后自动创建），
     它每隔几秒自动扫一次，发现新文件就自动处理。

处理完的文案在这里：
    transcript_tool\\文案输出\\ 文件夹，按日期命名的 .txt 文件
    也可以随时在界面的「历史记录」里翻看、复制、导出。


──────── 怎么关掉它 ────────

关掉那个黑色窗口就停了。
注意：关掉之后界面就打不开了；下次要用就再双击一次 start.bat。


──────── 可选：AI 纠错 / AI 笔记 ────────

不用管也没关系，不影响其它功能。
想用的话：用记事本打开 transcript_tool\\.env 文件，
找到  TRANSCRIPT_LLM_API_KEY=  这一行，在等号后面填上你的 key，
保存即可。


──────── 数据都存在哪 ────────

历史记录      transcript_tool\\transcript_history.db   （删掉＝清空历史）
导出的文案    transcript_tool\\文案输出\\               （你的成品，记得备份）
运行日志      transcript_tool\\app.log、server_err.log （出问题时发给技术人员）


──────── 出问题了？ ────────

本文档下半部分就是《遇到问题看这里》和《第三方组件与许可声明》，
直接往下翻即可。
"""

DOC_TROUBLE = """\
========================================
   遇到问题看这里
========================================

【先说最常见的一条】

  双击 start.bat 后，窗口一闪就消失了？
  → 99% 是因为没装 Python，或者装的时候没勾 "Add python.exe to PATH"。
    看下面第 1 条。


──────── 启动阶段的问题 ────────

1. 提示 "python 不是内部或外部命令 / Python not found"

   原因：装 Python 时漏勾了 "Add python.exe to PATH"。
   解决：重新运行 Python 安装包，选 "Modify" 或重新安装，
        第一屏务必勾上 "Add python.exe to PATH"。
        实在搞不定就卸载 Python 再装一次，这次记得勾。

2. 卡在 "安装 Python 依赖" 不动，或者特别慢

   原因：第一次要从网上下几百 MB 的东西，网速慢就会久。
   解决：耐心等（10 分钟以内都正常）。
        超过 10 分钟没动静，关掉黑窗口，重新双击 start.bat ——
        脚本会自动换成国内镜像重试。

3. 浏览器没有自动打开

   手动打开浏览器，地址栏输入：  http://127.0.0.1:8000

4. 提示端口被占用 / 8000 打不开

   说明已经有一个在跑了。看看是不是已经开着一个黑窗口。
   关掉旧窗口，再重新双击 start.bat 即可。

5. 杀毒软件报警或者把它删了

   本工具首次启动会下载 ffmpeg、浏览器内核，某些杀软会误报。
   把解压出来的这个文件夹加入杀毒软件白名单即可。

6. 解压后发现文件散了一地

   说明你没解压整包，而是单独拖了里面的文件出来。
   请对着 zip 右键 →「解压到当前文件夹」，
   保持  start.bat / 使用说明.txt / transcript_tool 的相对位置不变。


──────── 使用阶段的问题 ────────

7. 转写很慢

   第一次转写会下载语音识别模型（几百 MB），下完之后会快很多。
   视频越长越慢，这是正常的，不是卡死。
   有 NVIDIA 显卡会快很多；没有则自动用 CPU（约慢 10 倍）。
   装 GPU 加速：pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

8. 视频号用不了 / 找不到下载按钮

   先确认两件事：
     · **本机装了「微信 PC 端」并已登录吗？** 下载按钮是注入到微信的视频号
       页面里的，没有微信就没有使用场景（其它功能不受影响）。
     · 本包已内置下载器离线包，并预置了自装开关（不需要联网下载）。
       ★ **首次启动请右键 start.bat →「以管理员身份运行」**：
         它会自动装好下载器并启动（装根证书 + 接管系统代理需要管理员权限）。
         之后每次普通双击即可，不会再弹 UAC。

   · 如果首次你只是普通双击：它仍会尝试自动安装，但装证书那一步过不去，
     服务会在日志里写「自动启动未就绪」。这时到界面「视频号」页点一次
     「一键安装并启动」，在 UAC 框点「是」即可，之后恢复全自动。

   ★ 这个部件会安装一张根证书并临时接管系统代理，
     细节见本文档《第三方组件与许可声明》。
   不安装也完全不影响其它平台的使用。

9. 点「AI 纠错 / AI 笔记」没反应

   需要先在 .env 文件里填 TRANSCRIPT_LLM_API_KEY（见本文档《使用说明》）。

10. 界面能打开，但粘贴链接后一直不动

   该平台的抓取可能需要登录态或受频率限制。
   先用「上传文件」方式确认功能正常，再排查平台问题。
   报错提示是英文的？那是这条内容本身有问题（如已删除/链接失效），
   看提示里的中文说明即可。


──────── 还是不行 ────────

把下面两个文件发给技术人员，他们能看出绝大多数问题：

    transcript_tool\\app.log
    transcript_tool\\server_err.log

再加一句：你刚才是双击了什么、屏幕上报了什么错。
"""

DOC_THIRDPARTY = """\
========================================
   第三方组件与许可声明
========================================

本工具自己的代码不含下列组件。它们由本工具在运行/安装时获取，
或已随包附带。此处逐条列明，便于你了解这台机器上会跑什么、
哪些行为需要你知情。

────────────────────────────────────────
【1】wx_channels_download（微信视频号下载器）   ★ 安全敏感
────────────────────────────────────────
  许可     MIT 许可 + "Commons Clause" 条件 v1.0
           权利人（Licensor）：ltaoo
  来源     github.com/ltaoo/wx_channels_download
  体积     压缩包约 35MB（解压后约 75MB）
  作用     拦截微信客户端的视频号流量，把视频抓到本地
  获取     本包 wx_dl_pkg/ 目录**已附带**离线安装包，不需要联网下载；
           启动时会自动安装（需首次以管理员身份运行 start.bat，装根证书要用）。
  **前提   本机必须装有「微信 PC 端」并已登录** —— 下载按钮是注入到微信的
           视频号页面里的；没有微信，这个部件装了也没有使用场景。**
  状态     本包已预置自装开关（TRANSCRIPT_EXT_DL_AUTOSTART=1）：
           首次以管理员身份运行 start.bat 时会自动安装并启动，之后保持自动。
           若首次没以管理员运行，到界面「视频号」页点一次「一键安装并启动」即可。

  ★ 它会做三件需要你知情的事：
      · 往系统证书库安装一张根证书（用于解密 HTTPS 流量）
      · 把系统代理临时指向本机 127.0.0.1:2023（接管本机网络出口）
      · 需要管理员权限（会弹 UAC 提权确认）
    安装后本工具提供「还原系统代理」按钮；停止服务时也会自动还原。
    不使用视频号功能，可以完全不安装它，其余功能不受影响。

  注意     Commons Clause 限制"售卖"：不得提供以它功能为主的产品或服务
           来收费。若你打算把本工具作为商品分发，需另行向权利人取得授权。

────────────────────────────────────────
【2】ffmpeg / ffprobe
────────────────────────────────────────
  许可     LGPL / GPL（取决于具体构建；本工具获取的是 gyan.dev 的 essentials 构建）
  体积     约 80MB
  作用     音频重采样、视频抽帧、转封装
  获取     系统 PATH 里没有时，由 scripts/setup_new_machine.py 下载到
           项目内 transcript_tool\\tools\\ffmpeg（不污染系统 PATH）

────────────────────────────────────────
【3】Playwright Chromium
────────────────────────────────────────
  许可     Apache-2.0（Chromium 自身为 BSD 等）
  体积     约 130MB
  作用     抖音 / 小红书的浏览器渲染通道（绕过风控）
  获取     python -m playwright install chromium

────────────────────────────────────────
【4】faster-whisper 语音识别模型
────────────────────────────────────────
  许可     各模型不同（Systran/faster-whisper-* 系列多为 MIT）
  体积     数百 MB（tiny/base/small/medium 合计）
  作用     语音转文字
  获取     首次转写时自动下载；默认走国内镜像 hf-mirror.com
           可用 HF_ENDPOINT 环境变量改回官方源

────────────────────────────────────────
【5】yt-dlp
────────────────────────────────────────
  许可     Unlicense（公有领域）
  作用     通用站点的媒体解析（B站、YouTube 等走这条通道）

────────────────────────────────────────
【6】RapidOCR + onnxruntime
────────────────────────────────────────
  许可     Apache-2.0
  作用     图片文字识别、视频画面文字识别

────────────────────────────────────────
【7】其它 Python 依赖
────────────────────────────────────────
  见 transcript_tool\\requirements.txt（fastapi / uvicorn / python-docx /
  openpyxl / opencc-python-reimplemented 等）。
  各自许可随 pip 安装保留在各自的包目录中。

────────────────────────────────────────
  关于本工具自身
────────────────────────────────────────
  本工具自己的代码目前**未声明开源许可**。
  若计划对外分发或商用，建议一并补上许可证文件。
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

    # 离线安装包（可选；INCLUDE_WX_PKG=False 时不带，包体积从 35MB 降到 ~400KB）
    pkg_files: list[Path] = []
    if INCLUDE_WX_PKG and PKG_SRC.is_dir():
        pkg_files = [p for p in sorted(PKG_SRC.rglob("*")) if p.is_file()]
        pk = sum(p.stat().st_size for p in pkg_files)
        print(f"离线安装包 {len(pkg_files)} 个文件，共 {pk / 1048576:.1f} MB（{PKG_SRC}）")
    else:
        print("未附带离线安装包（视频号首次使用需联网从 GitHub 获取）")

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
    for p in pkg_files:
        dst = stage / "wx_dl_pkg" / p.relative_to(PKG_SRC)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)

    # 包内 .env.example 预置「开机自动安装视频号下载器」
    # （只改包里的副本，仓库里的源文件保持不动）
    env_ex = inner / ".env.example"
    if INCLUDE_WX_PKG and env_ex.is_file():
        t = env_ex.read_text(encoding="utf-8")
        for old, new in (("#TRANSCRIPT_EXT_DL_AUTOSTART=1", "TRANSCRIPT_EXT_DL_AUTOSTART=1"),
                         ("# TRANSCRIPT_EXT_DL_AUTOSTART=1", "TRANSCRIPT_EXT_DL_AUTOSTART=1")):
            if old in t:
                t = t.replace(old, new)
                break
        else:
            t += "\n# 本包已预置：启动时自动安装并启动视频号下载器（首次需管理员运行）\n"
            t += "TRANSCRIPT_EXT_DL_AUTOSTART=1\n"
        env_ex.write_text(t, encoding="utf-8")
        print("包内 .env.example 已预置 TRANSCRIPT_EXT_DL_AUTOSTART=1")

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
            "   形态：Windows 依赖版（目标机器需 Python 3.10+ 与网络）",
            f"   离线安装包：{'已附带' if pkg_files else '未附带（视频号需联网获取）'}",
            bar, ""]
    if commit:
        head.insert(4, f"   代码提交 {commit}")
    head += ["   升级前请先备份这三项：",
             "       transcript_tool\\transcript_history.db   历史记录",
             "       transcript_tool\\文案输出\\               导出的文案",
             "       transcript_tool\\.env                     配置（含 API key）",
             ""]
    divider = "\n\n" + "─" * 60 + "\n\n"
    doc = ("\n".join(head) + "\n"
           + DOC_INTRO.strip() + divider
           + DOC_TROUBLE.strip() + divider
           + DOC_THIRDPARTY.strip() + "\n")
    # 变更记录：附在最后（用户要求"每次更新写清改了什么、修了什么"）
    chlog = ROOT / "CHANGELOG.md"
    if chlog.is_file():
        doc += divider + "更新记录（本版改了什么 / 修了什么）\n\n" + \
               _md_to_text(chlog.read_text(encoding="utf-8")) + "\n"
    (stage / "使用说明.txt").write_text(doc, encoding="utf-8-sig")

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

    print("\n打包完成:")
    print(f"   文件夹: {stage}")
    if zip_path is not None:
        print(f"   压缩包: {zip_path}  ({size / 1048576:.1f} MB)")
    else:
        print("   压缩包: 已跳过（未加 --zip；需要发人时再加 --zip）")
    tail = " + wx_dl_pkg/" if pkg_files else "（不含 wx_dl_pkg）"
    print(f"   结构  : start.bat + 使用说明.txt + transcript_tool/{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())