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
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def have_ffmpeg() -> bool:
    if (TOOLS_FFMPEG_BIN / "ffmpeg.exe").is_file():
        return True
    try:
        r = _run(["ffmpeg", "-version"])
        return r.returncode == 0
    except Exception:
        return False


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
        where = "项目内 tools/ffmpeg" if (TOOLS_FFMPEG_BIN / "ffmpeg.exe").is_file() else "系统 PATH"
        print(f"已就绪（{where}）")
    elif CHECK_ONLY:
        print("[!] 缺失。完整模式运行会自动下载到 tools/ffmpeg")
    else:
        print(f"下载 ffmpeg（约 80MB）: {FFMPEG_URL}")
        import urllib.request
        zipped = ROOT / "tools" / "ffmpeg.zip"
        zipped.parent.mkdir(parents=True, exist_ok=True)
        try:
            req2 = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": "transcript-tool/1.0"})
            with urllib.request.urlopen(req2, timeout=600) as resp, open(zipped, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
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
            print(f"[!] ffmpeg 自动下载失败: {exc}\n    请手动下载并解压 bin 到 {TOOLS_FFMPEG_BIN}")
        finally:
            try:
                zipped.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

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
    print("接下来: python run.py  启动服务（下载器/监听/模型全部自动就位）")
    print("视频号首次使用：若提示证书未安装，在页面点一次「一键安装并启动」（弹 UAC 点是）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
