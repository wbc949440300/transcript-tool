"""新机一键部署引导：把「本机有的配置」在另一台电脑上自动配齐。

用法（在项目根目录 transcript_tool/ 下）：
    python scripts/setup_new_machine.py            # 完整安装
    python scripts/setup_new_machine.py --check    # 只检查不改动

自动处理的项：
  1. Python 依赖（pip install -r requirements.txt，失败自动换清华镜像重试）
  2. Playwright 浏览器内核（抖音/小红书渲染通道需要；可选，失败不阻塞）
  3. ffmpeg：系统 PATH 没有 → 自动下载到项目内 tools/ffmpeg（run.py 会优先用它）
  4. .env：不存在则从 .env.example 复制（LLM key 需要自己填）
  5. 其余全部自动：wx_channels_download（服务启动时自动下载+启动）、
     监听目录自动接线、whisper 模型首次使用自动下载（默认走 hf-mirror）、
     历史库/任务目录自动创建 —— 无需任何手工配置。

手动步骤只剩两件：装 Python 3.10+；在 .env 里填 LLM API key（可选）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_FFMPEG_BIN = ROOT / "tools" / "ffmpeg" / "bin"
CHECK_ONLY = "--check" in sys.argv

PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# ── 平台判断（2026-11-02 修）──────────────────────────────────────
# 此前写死 gyan.dev 的 Windows 构建、且只认 ffmpeg.exe：
# 在 Ubuntu 上跑会下载 Windows 包，然后永远找不到可执行文件 → 必然失败。
IS_WINDOWS = os.name == "nt"
FFMPEG_URL_WIN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_URL_LINUX = ("https://johnvansickle.com/ffmpeg/releases/"
                    "ffmpeg-release-amd64-static.tar.xz")
FFMPEG_URL = FFMPEG_URL_WIN if IS_WINDOWS else FFMPEG_URL_LINUX
FFMPEG_BIN_NAME = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def have_ffmpeg() -> bool:
    if (TOOLS_FFMPEG_BIN / FFMPEG_BIN_NAME).is_file():
        return True
    try:
        r = _run(["ffmpeg", "-version"])
        return r.returncode == 0
    except Exception:
        return False


def _download(url: str, dst: Path, timeout: int = 600) -> None:
    import urllib.request

    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "transcript-tool/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dst, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def _install_ffmpeg_zip() -> None:
    """Windows：从 gyan.dev 拉 essentials 构建，只取 bin/ 下的 exe。"""
    print(f"下载 ffmpeg（约 80MB）: {FFMPEG_URL}")
    zipped = ROOT / "tools" / "ffmpeg.zip"
    try:
        _download(FFMPEG_URL, zipped)
        print("解压到 tools/ffmpeg ...")
        with zipfile.ZipFile(zipped) as z:
            for m in z.infolist():
                name = m.filename.replace("\\", "/")
                if "/bin/" not in name or name.endswith("/"):
                    continue
                out = TOOLS_FFMPEG_BIN / Path(name).name
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(out, "wb") as f:
                    f.write(src.read())
        print("ffmpeg OK →", TOOLS_FFMPEG_BIN)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] ffmpeg 自动下载失败: {exc}\n"
              f"    请手动下载并解压 bin 到 {TOOLS_FFMPEG_BIN}")
    finally:
        try:
            zipped.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _install_ffmpeg_linux() -> None:
    """Linux：优先用系统包管理器（发行版推荐做法），失败再退到静态构建。

    2026-11-02 加：此前没有这条路径 —— Linux 上会去下载 Windows 的 zip，
    解压出一堆 .exe，然后服务永远找不到可用的 ffmpeg。
    """
    sudo: list[str] = []
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if shutil.which("sudo"):
            sudo = ["sudo"]
        else:
            print("[i] 非 root 且无 sudo —— 跳过包管理器，直接取静态构建")
    for exe, argv in (("apt-get", ["apt-get", "install", "-y", "ffmpeg"]),
                      ("dnf", ["dnf", "install", "-y", "ffmpeg"]),
                      ("pacman", ["pacman", "-S", "--noconfirm", "ffmpeg"])):
        if not shutil.which(exe):
            continue
        print(f"尝试：{' '.join(sudo + argv)}")
        r = _run(sudo + argv, timeout=1800)
        if r.returncode == 0 and shutil.which("ffmpeg"):
            print(f"ffmpeg 安装完成（{exe}）：{shutil.which('ffmpeg')}")
            return
        print(f"[i] {exe} 未成功，继续下一种")

    print(f"改用静态构建下载：{FFMPEG_URL}")
    tarball = ROOT / "tools" / "ffmpeg.tar.xz"
    try:
        _download(FFMPEG_URL, tarball)
        print("解压到 tools/ffmpeg ...")
        import tarfile

        with tarfile.open(tarball, "r:xz") as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                base = Path(m.name).name
                if base not in ("ffmpeg", "ffprobe", "ffplay"):
                    continue
                out = TOOLS_FFMPEG_BIN / base
                out.parent.mkdir(parents=True, exist_ok=True)
                src = t.extractfile(m)
                if src is None:
                    continue
                with open(out, "wb") as f:
                    f.write(src.read())
                out.chmod(0o755)
        print("ffmpeg OK →", TOOLS_FFMPEG_BIN)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] ffmpeg 自动安装失败: {exc}\n"
              "    请手动安装后重试，任选一种：\n"
              "      sudo apt update && sudo apt install -y ffmpeg\n"
              f"      （或把 ffmpeg / ffprobe 放进 {TOOLS_FFMPEG_BIN}）")
    finally:
        try:
            tarball.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    print("新机部署引导 · 文案提取工具")
    print(f"项目目录: {ROOT}")
    if sys.version_info < (3, 10):
        print(f"[!] Python 版本过低: {sys.version}，需要 3.10+（推荐 3.11）")
        return 1

    # 1) Python 依赖
    step("1/4 Python 依赖")
    req = ROOT / "requirements.txt"
    if CHECK_ONLY:
        r = _run([sys.executable, "-m", "pip", "check"])
        print("pip check:", "OK" if r.returncode == 0 else (r.stdout or r.stderr)[:300])
    else:
        print("pip install -r requirements.txt ...")
        r = _run([sys.executable, "-m", "pip", "install", "-r", str(req)])
        if r.returncode != 0:
            print("官方源失败，换清华镜像重试 ...")
            r = _run([sys.executable, "-m", "pip", "install", "-r", str(req),
                      "-i", PIP_MIRROR])
        print("依赖安装:", "OK" if r.returncode == 0 else "失败（见上方输出）")

    # 2) Playwright 浏览器内核
    step("2/4 Playwright 浏览器内核（抖音/小红书渲染需要）")
    try:
        r = _run([sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"])
        installed = r.returncode == 0 and "not installed" not in (r.stdout or "")
    except Exception:
        installed = False
    if installed:
        print("浏览器内核已就绪")
    elif CHECK_ONLY:
        print("[!] 未安装。完整模式运行: python -m playwright install chromium")
    else:
        print("下载 chromium（约 130MB，视网络可能较慢）...")
        r = _run([sys.executable, "-m", "playwright", "install", "chromium"], timeout=1800)
        print("浏览器内核:", "OK" if r.returncode == 0 else "失败（抖音/小红书通道暂不可用，其余功能正常）")

    # 3) ffmpeg
    step("3/4 ffmpeg")
    if have_ffmpeg():
        where = ("项目内 tools/ffmpeg"
                 if (TOOLS_FFMPEG_BIN / FFMPEG_BIN_NAME).is_file() else "系统 PATH")
        print(f"已就绪（{where}）")
    elif CHECK_ONLY:
        print("[!] 缺失。完整模式会自动装到 tools/ffmpeg"
              "（Linux 优先系统包管理器，Windows 下载 zip）")
    elif IS_WINDOWS:
        _install_ffmpeg_zip()
    else:
        _install_ffmpeg_linux()

    # 4) .env
    step("4/4 配置文件 .env")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.is_file():
        print(".env 已存在（不覆盖）")
    elif example.is_file():
        if CHECK_ONLY:
            print("[!] 缺 .env（可从 .env.example 复制）")
        else:
            shutil.copy(example, env)
            print("已从 .env.example 创建 .env —— 记得填 TRANSCRIPT_LLM_API_KEY（不填只影响 AI 纠错/笔记）")
    else:
        print("[!] 找不到 .env.example（跳过）")

    print("\n=== 完成 ===")
    print("接下来: python run.py  启动服务（监听目录 / 模型自动就位）")
    if IS_WINDOWS:
        print("视频号首次使用：若提示证书未安装，在页面点一次「一键安装并启动」（弹 UAC 点是）")
    else:
        print("视频号：Linux 上不提供下载器（那个部件是 Windows 专属）。")
        print("       替代路径 —— 在 Windows 机器上把视频号视频抓好，"
              "拷进包的 wx_downloads/ 目录，本服务会自动入库转写。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
