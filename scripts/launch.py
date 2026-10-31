"""一键启动引导（由 start.bat 调用，bat 里只留纯 ASCII 避开中文编码坑）。

职责：.env 引导 → 依赖自检（缺则安装，失败换镜像）→ 自动开浏览器 → 启动服务。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8000/#/home"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def main() -> int:
    os_x = sys.platform
    print("=" * 48)
    print("  文案提取工具 · 一键启动")
    print("=" * 48)

    if not (ROOT / ".env").exists() and (ROOT / ".env.example").exists():
        shutil.copy(ROOT / ".env.example", ROOT / ".env")
        print("[i] 已从 .env.example 创建 .env（AI 纠错/笔记需自行填 API key）")

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        import yt_dlp  # noqa: F401
        import faster_whisper  # noqa: F401
    except ImportError:
        print("[i] 首次运行：安装 Python 依赖（约 1-3 分钟）...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
        if r.returncode != 0:
            print("[i] 官方源失败，改用清华镜像重试...")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                                str(ROOT / "requirements.txt"), "-i", MIRROR])
        if r.returncode != 0:
            print("[!] 依赖安装失败，请检查网络后重试")
            return 1

    print(f"[i] 启动服务: {URL}")
    print("    （下载器/监听/模型全部自动就位；关闭本窗口即停止服务）")
    threading.Timer(3.0, lambda: webbrowser.open(URL)).start()
    try:
        r = subprocess.run([sys.executable, str(ROOT / "run.py")], cwd=str(ROOT))
        return r.returncode or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 启动异常: {exc}")
        print("    请查看 app.log / server_err.log")
    finally:
        try:
            input("按回车键关闭窗口...")
        except EOFError:
            pass
