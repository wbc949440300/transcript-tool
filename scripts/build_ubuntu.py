"""打包 Ubuntu 版：tar.gz（代码 + start.sh + 一键部署）。

用法（在 transcript_tool/ 下）:
    python scripts/build_ubuntu.py --check    # 只盘点
    python scripts/build_ubuntu.py            # 打包

产物: dist/文案提取工具-v<版本>-ubuntu-amd64.tar.gz   （约 40MB）

形态选择：**依赖版**——目标机器需 Python 3.10+ 与网络，首次启动自动装齐。
  理由：① 服务器上已有 systemd / apt / 网络（同机其它项目已在用 ffmpeg）；
        ② 复用已在本机验证过的同一份 Python 代码，风险最低；
        ③ 完全离线版需要 Linux 的 Python/wheels/chromium/ffmpeg，
           从 Windows 交叉取材可行但**无法验证运行**，等于转嫁风险。

★ 不打包 wx_dl_pkg：视频号下载器是 Windows 专属（UAC/证书/系统代理），
  在 Linux 上无用；代码会提示改用「把文件同步到监听目录」。

★ 行尾必须 LF：CRLF 的 .sh 在 Linux 上会报 "bad interpreter"。
"""
from __future__ import annotations

import argparse
import datetime
import io
import os
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release as br  # noqa: E402  复用收集/版本逻辑

ROOT = br.ROOT
DIST = br.DIST
START_SH = ROOT / "scripts" / "linux" / "start.sh"

DOC_LINUX = """\
========================================
   文案提取工具 · Ubuntu 版使用说明
========================================

【一句话】把视频或录音丢进来，自动把里面说的话变成文字稿。

【和 Windows 版有什么不一样（先看这段）】

  ✔ 一样能用：链接抓取（抖音/快手/B站/小红书/YouTube）、上传文件转写、
    监视目录自动入库、图片 OCR、画面文字识别、导出、AI 纠错、AI 笔记、
    批量任务、历史记录、归档。

  ✘ 视频号抓取不可用。那个下载器是 Windows 专属（要装根证书、接管系统代理、
    走 UAC 提权），Linux 上跑不了。
    替代办法：在 Windows 机器上用下载器把视频号视频抓下来，
    把文件拷进本包目录下的 wx_downloads/ 文件夹，本服务会自动入库转写。
    代码也会在界面上明确提示这一点，不会报错崩掉。

  ✘ 桌面气泡通知自动跳过（Windows 专属），不影响其它功能。


【你要做的只有两步】

  第 1 步：解压

      tar -xzf 文案提取工具-vX.X.X-ubuntu-amd64.tar.gz
      cd 文案提取工具-vX.X.X-ubuntu-amd64

  第 2 步：启动

      bash start.sh

     首次会自动装 Python 依赖、ffmpeg、浏览器内核（约 3-8 分钟，需要网络）。
     看到  Uvicorn running on http://127.0.0.1:8000  就成功了。

     然后浏览器打开  http://127.0.0.1:8000


【想让它后台常驻（推荐）】

      bash start.sh --daemon

  会安装成 systemd 服务：SSH 断开不中断、机器重启自动恢复、崩溃 3 秒后自动重启。

      bash start.sh --status     查看状态
      bash start.sh --logs       实时看日志（Ctrl+C 退出查看）
      bash start.sh --stop       停止


【想让别的机器访问】

      HOST=0.0.0.0 bash start.sh

  然后用 http://<这台机器的IP>:8000 访问。
  ⚠ 目前没有内置鉴权，**只建议在内网使用**；要暴露到公网请自行加 Nginx + Basic Auth。


【环境自检】

      bash start.sh --check

  会列出 Python / ffmpeg / systemd / 代码 四项是否就绪。


【可选配置】

  .env 文件在 transcript_tool/ 下：
    TRANSCRIPT_LLM_API_KEY   填了才能用「AI 纠错 / AI 笔记」，不填不影响其它功能

  显卡加速（有 NVIDIA 显卡时）：
    .venv/bin/pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
    装了会自动用 GPU，比 CPU 快很多；没装也能跑，只是慢。


【数据存在哪】

  transcript_tool/transcript_history.db    历史记录（删掉＝清空历史）
  transcript_tool/文案输出/                 导出的文案（记得备份）
  transcript_tool/app.log                  运行日志（出问题发这个）
  wx_downloads/                            监视目录：丢文件进来就自动转写


【常见问题】

  1. 提示 Python 版本低于 3.10
     → bash start.sh 会自动 apt 安装；失败就手动：
        sudo apt update && sudo apt install -y python3 python3-venv python3-pip

  2. 提示没有 ffmpeg
     → 同上，会自动装；手动：sudo apt install -y ffmpeg

  3. 抖音/小红书抓取报错，提示浏览器相关
     → 缺 Chrome 的系统库。执行：
        .venv/bin/python -m playwright install-deps chromium
        （需要 sudo）

  4. 转写很慢
     → 首次会下载语音识别模型（几百 MB），之后快很多。
        有 NVIDIA 显卡并装了 CUDA 库会快得多。

  5. 时间显示差 8 小时
     → start.sh 已设 TZ=Asia/Shanghai。若你改用别的方式启动（如自己写 systemd），
        记得也加 Environment=TZ=Asia/Shanghai。

  6. 视频号没有下载按钮
     → 正常，见上面「和 Windows 版有什么不一样」。
"""


DOC_TROUBLE_LINUX = """\
========================================
   遇到问题看这里（Ubuntu 版）
========================================

【先说最常见的两条】

  · 报 "Permission denied"
    → 用 bash 调用即可（本说明全用  bash start.sh ）；
      或先给执行权限：chmod +x start.sh

  · 报  $'\\r': command not found  之类
    → start.sh 的行尾被改成了 CRLF（Windows 编辑器常见）。
      执行一次：  sed -i 's/\\r$//' start.sh   然后重跑。
      本包出厂就是 LF，正常不会遇到。


──────── 安装阶段 ────────

1. 提示 Python 版本低于 3.10
   → start.sh 会自动 apt 安装；失败就手动：
       sudo apt update && sudo apt install -y python3 python3-venv python3-pip

2. 创建 venv 失败 / 提示 ensurepip is not available
   → 缺 python3-venv：
       sudo apt install -y python3-venv

3. 装依赖很慢或卡住
   → 首次要下几百 MB。脚本失败会自动换清华镜像重试。
     超过 10 分钟没动静，Ctrl+C 中断后重新执行 bash start.sh。

4. 没检测到 ffmpeg
   → start.sh 会自动 apt 装；手动：
       sudo apt update && sudo apt install -y ffmpeg

5. 提示没有 systemd，--daemon 用不了
   → 容器或精简系统常见。不影响使用，直接前台跑：
       bash start.sh


──────── 使用阶段 ────────

6. 抖音 / 小红书抓取报错，提到浏览器或库缺失
   → 缺 Chromium 的系统库（libnss3 等）：
       .venv/bin/python -m playwright install-deps chromium
     需要 sudo。

7. 转写很慢
   → 首次会下载语音识别模型（medium 约 1.5GB），之后快很多。
     这台机器若有 NVIDIA 显卡，装上 CUDA 库能快约 10 倍：
       .venv/bin/pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

8. 视频号找不到下载按钮
   → 正常。视频号下载器是 Windows 专属（要装根证书、接管系统代理、
     走 UAC 提权），Linux 上不提供。
     替代：在 Windows 机器上用下载器抓好视频，把文件拷进本包的
     wx_downloads/ 目录，本服务会自动入库转写。

9. 时间差 8 小时
   → start.sh 已设 TZ=Asia/Shanghai。若你改用自己写的 systemd 单元，
     记得也加 Environment=TZ=Asia/Shanghai。

10. 界面能打开，但粘贴链接后一直不动
   → 该平台的抓取可能需要登录态或受频率限制。先试「上传文件」确认
     功能正常，再排查平台问题。


──────── 还是不行 ────────

  先跑一次环境自检（会列出 Python / ffmpeg / systemd / 代码 四项）：
      bash start.sh --check

  日志在哪：
      前台运行     → 屏幕输出直接看
      --daemon     → bash start.sh --logs

  然后把「你执行了什么 + 屏幕上的报错」发出来。
"""

DOC_THIRDPARTY_LINUX = """\
========================================
   第三方组件与许可声明（Ubuntu 版）
========================================

本工具自己的代码不含下列组件。它们由本工具在安装/运行时获取，
或来自系统的包管理器。此处逐条列明，便于你了解这台机器上会装什么、跑什么。

────────────────────────────────────────
【1】ffmpeg / ffprobe
────────────────────────────────────────
  许可     LGPL / GPL（取决于发行版打包方式；Ubuntu 仓库版为 LGPL 兼容构建）
  来源     系统包管理器：sudo apt install -y ffmpeg
  作用     音频重采样、视频抽帧、转封装
  获取     start.sh 检测不到时自动 apt 安装（需要网络与 sudo）

────────────────────────────────────────
【2】Playwright Chromium
────────────────────────────────────────
  许可     Apache-2.0（Chromium 自身为 BSD 等）
  体积     约 130-170 MB
  作用     抖音 / 小红书的浏览器渲染通道（绕过风控）
  获取     .venv/bin/python -m playwright install chromium
  附加     Chromium 还需要一批系统库（libnss3、libatk 等），
           由 .venv/bin/python -m playwright install-deps chromium 安装（需 sudo）

────────────────────────────────────────
【3】faster-whisper 语音识别模型
────────────────────────────────────────
  许可     各模型不同（Systran/faster-whisper-* 系列多为 MIT）
  体积     medium 约 1.5 GB
  作用     语音转文字
  获取     首次转写时自动下载；默认走国内镜像 hf-mirror.com
           可用 HF_ENDPOINT 环境变量改回官方源

────────────────────────────────────────
【4】yt-dlp
────────────────────────────────────────
  许可     Unlicense（公有领域）
  作用     通用站点的媒体解析（B站、YouTube 等走这条通道）

────────────────────────────────────────
【5】RapidOCR + onnxruntime
────────────────────────────────────────
  许可     Apache-2.0
  作用     图片文字识别、视频画面文字识别

────────────────────────────────────────
【6】其它 Python 依赖
────────────────────────────────────────
  见 transcript_tool/requirements.txt（fastapi / uvicorn / faster-whisper /
  python-docx / openpyxl / opencc-python-reimplemented 等）。
  各自许可随 pip 安装保留在 .venv/ 里各自的位置。

────────────────────────────────────────
【7】关于视频号下载器
────────────────────────────────────────
  本包**不含、也不会安装**视频号下载器（wx_channels_download）。
  它是 Windows 专属（UAC 提权 / 装根证书 / 接管系统代理），Linux 上不提供。
  处理视频号的替代路径见本文档开头的《使用说明》。

────────────────────────────────────────
  关于本工具自身
────────────────────────────────────────
  本工具自己的代码目前**未声明开源许可**。
  若计划对外分发或商用，建议一并补上许可证文件。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    version = br.read_version()
    if not START_SH.is_file():
        print(f"[!] 缺少 {START_SH}")
        return 1
    # Linux 上 CRLF 会报 bad interpreter —— 打包前必须确认
    if b"\r" in START_SH.read_bytes():
        print("[!] start.sh 含 CRLF 行尾，Linux 上会失败。请转成 LF 再打包。")
        return 1

    files = br.collect()
    print(f"Ubuntu 版 v{version}：{len(files)} 个代码文件 + start.sh")
    if args.check:
        print("[--check] 未写盘。")
        return 0

    name = f"文案提取工具-v{version}-ubuntu-amd64"
    stage = DIST / name
    if stage.exists():
        import shutil as _sh
        _sh.rmtree(stage)
    stage.mkdir(parents=True)

    inner = stage / "transcript_tool"
    for f in files:
        dst = inner / f.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(f.read_bytes())      # 原样字节，保证 LF 不被改写

    (stage / "start.sh").write_bytes(START_SH.read_bytes())

    # 单一文档：版本信息 / 使用说明 / 排错 / 第三方声明 合成一份
    bar = "=" * 60
    head = [bar,
            "   文案提取工具 · 使用说明（Ubuntu 版）",
            f"   版本 {version} ｜ 打包于 {datetime.datetime.now():%Y-%m-%d %H:%M}",
            "   形态：依赖版（目标机器需 Python 3.10+ 与网络）",
            f"   平台：Linux amd64（{'不含视频号下载器 —— Windows 专属' if not (ROOT.parent / 'wx_dl_pkg').is_dir() else '不含视频号下载器 —— Windows 专属'}）",
            bar, ""]
    head += ["   升级前请先备份这三项：",
             "       transcript_tool/transcript_history.db   历史记录",
             "       transcript_tool/文案输出/                导出的文案",
             "       transcript_tool/.env                     配置（含 API key）",
             ""]
    divider = "\n\n" + "─" * 60 + "\n\n"
    doc = ("\n".join(head) + "\n"
           + DOC_LINUX.strip() + divider
           + DOC_TROUBLE_LINUX.strip() + divider
           + DOC_THIRDPARTY_LINUX.strip() + "\n")
    # 变更记录：附在最后（与 Windows 包一致）
    chlog = ROOT / "CHANGELOG.md"
    if chlog.is_file():
        doc += divider + "更新记录（本版改了什么 / 修了什么）\n\n" + \
               br._md_to_text(chlog.read_text(encoding="utf-8")) + "\n"
    (stage / "使用说明.txt").write_text(doc, encoding="utf-8-sig")
    (stage / "wx_downloads").mkdir(exist_ok=True)
    (stage / "wx_downloads" / "把视频文件放这里.txt").write_text(
        "把要转写的视频/音频文件复制到本文件夹，服务会自动发现并转写。\n"
        "视频号视频也走这条路（本服务在 Linux 上不能直接抓视频号）。\n",
        encoding="utf-8-sig")

    tar_path = DIST / f"{name}.tar.gz"
    if tar_path.exists():
        tar_path.unlink()
    # 逐个写入并显式设定权限：start.sh 与目录需要可执行/可进入
    with tarfile.open(tar_path, "w:gz", compresslevel=6) as t:
        def _info(p: Path, mode: int) -> tarfile.TarInfo:
            info = t.gettarinfo(str(p), arcname=str(Path(name) / p.relative_to(stage)))
            info.mode = mode
            info.uid = info.gid = 0          # 归零，避免带上构建者的本机 uid
            info.uname = info.gname = "root"
            info.mtime = int(time.time())
            return info

        def _add_dir(p: Path) -> None:
            t.addfile(_info(p, 0o755))       # 目录：无 fileobj

        def _add_file(p: Path, mode: int) -> None:
            with open(p, "rb") as fh:
                t.addfile(_info(p, mode), fh)

        _add_dir(stage)
        for p in sorted(stage.rglob("*")):
            if p.is_dir():
                _add_dir(p)
            elif p.is_file():
                # start.sh 必须可执行；其余保持 644
                _add_file(p, 0o755 if p.name == "start.sh" else 0o644)

    size = tar_path.stat().st_size
    print(f"\n打包完成: {tar_path}  ({size / 1048576:.1f} MB)")
    print("   结构: start.sh + 使用说明.txt + transcript_tool/ + wx_downloads/")
    print("   已排除: wx_dl_pkg（视频号下载器，Windows 专属）")
    return 0


if __name__ == "__main__":
    sys.exit(main())