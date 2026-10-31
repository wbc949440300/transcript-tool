"""迁移自检：在新电脑上运行，检查文案提取工具是否具备运行条件。

用法：  python check_env.py
只读检查，不改动任何东西；按 PASS / WARN / FAIL 给出结论与修复命令。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL = "PASS", "WARN", "FAIL"
_rows: list[tuple[str, str, str]] = []


def add(status: str, name: str, detail: str) -> None:
    _rows.append((status, name, detail))


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 9):
        add(OK, "Python 版本", f"{v.major}.{v.minor}.{v.micro}")
    else:
        add(FAIL, "Python 版本", f"{v.major}.{v.minor}.{v.micro} 过低，需 >= 3.9")


PKGS = [
    ("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("yt_dlp", "yt-dlp"),
    ("faster_whisper", "faster-whisper"), ("rapidocr_onnxruntime", "rapidocr-onnxruntime"),
    ("playwright", "playwright"), ("docx", "python-docx"), ("openpyxl", "openpyxl"),
    ("PIL", "pillow"), ("av", "av"),
]


def check_packages() -> None:
    missing = []
    for mod, pipname in PKGS:
        if importlib.util.find_spec(mod) is None:
            missing.append(pipname)
        else:
            add(OK, f"依赖 {pipname}", "已安装")
    if missing:
        add(FAIL, "缺失依赖", ", ".join(missing) + "  →  pip install " + " ".join(missing))


def check_ffmpeg() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        p = shutil.which(exe)
        if p:
            add(OK, exe, p)
        else:
            add(FAIL, exe + " 不在 PATH",
                "yt-dlp 合并音频/转码需要它。装好后把其 bin 目录加入 PATH")


def check_playwright_browser() -> None:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
        if path and Path(path).exists():
            add(OK, "Playwright Chromium", path)
        else:
            add(FAIL, "Playwright Chromium", "未安装 →  python -m playwright install chromium")
    except Exception as exc:  # noqa: BLE001
        add(FAIL, "Playwright Chromium", f"检测失败: {exc}  →  python -m playwright install chromium")


def check_whisper_models() -> None:
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    found = []
    if cache.is_dir():
        for d in cache.iterdir():
            if d.name.startswith("models--Systran--faster-whisper"):
                found.append(d.name.split("--")[-1])
    if found:
        add(OK, "Whisper 模型缓存", ", ".join(sorted(set(found))) + "（离线可用）")
    else:
        add(WARN, "Whisper 模型缓存", "本机无缓存 → 首次转写会自动联网下载（tiny≈75MB，base≈343MB）")


def check_rapidocr_models() -> None:
    try:
        import rapidocr_onnxruntime as r
        md = Path(r.__file__).parent / "models"
        n = len(list(md.glob("*.onnx"))) if md.is_dir() else 0
        if n:
            add(OK, "RapidOCR 模型", f"随 pip 包自带 {n} 个 onnx，无需额外下载")
        else:
            add(WARN, "RapidOCR 模型", "包内未见模型文件，首次使用可能需联网下载")
    except Exception as exc:  # noqa: BLE001
        add(FAIL, "RapidOCR", str(exc))


def check_writable_dirs() -> None:
    base = Path(__file__).resolve().parent
    for d in ("tasks", "tmp_uploads"):
        target = base / d
        try:
            target.mkdir(exist_ok=True)
            probe = target / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add(OK, f"目录可写 {d}", str(target))
        except Exception as exc:  # noqa: BLE001
            add(FAIL, f"目录不可写 {d}", str(exc))


def check_port() -> None:
    port = int(os.getenv("PORT", "8000"))
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        add(OK, f"端口 {port}", "空闲")
    except OSError:
        add(WARN, f"端口 {port}", "已被占用 → 用 $env:PORT=8010 换端口")
    finally:
        s.close()


def check_history_db() -> None:
    base = Path(__file__).resolve().parent
    db = base / os.getenv("TRANSCRIPT_DB", "transcript_history.db")
    if db.exists():
        add(OK, "历史记录库", f"{db.name} ({db.stat().st_size} 字节)")
    else:
        add(WARN, "历史记录库", "尚无 transcript_history.db（首次完成任务时自动创建）")


def check_network() -> None:
    # 迁移后若离线，模型下载与平台抓取都会失败，这里只做提示
    try:
        socket.create_connection(("www.douyin.com", 443), timeout=6).close()
        add(OK, "外网连通性", "可访问 douyin:443（平台抓取需要外网）")
    except Exception as exc:  # noqa: BLE001
        add(WARN, "外网连通性", f"{exc} → 无外网则无法抓取平台与下载模型")


def main() -> int:
    print("=" * 72)
    print("文案提取工具 · 环境自检")
    print("=" * 72)
    for fn in (check_python, check_packages, check_ffmpeg, check_playwright_browser,
               check_whisper_models, check_rapidocr_models, check_writable_dirs,
               check_port, check_history_db, check_network):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            add(WARN, fn.__name__, f"检查异常: {exc}")

    width = max(len(n) for _, n, _ in _rows) if _rows else 0
    for status, name, detail in _rows:
        print(f"[{status}] {name.ljust(width)}  {detail}")

    fails = [r for r in _rows if r[0] == FAIL]
    warns = [r for r in _rows if r[0] == WARN]
    print("-" * 72)
    if fails:
        print(f"结论：{len(fails)} 项必须处理，{len(warns)} 项建议处理。")
        print("一次性安装命令：")
        print("  pip install -r requirements.txt")
        print("  python -m playwright install chromium")
        print("  # 再把 ffmpeg 所在目录加入 PATH")
        return 1
    print(f"结论：可以运行（{len(warns)} 项提示，不影响启动）。")
    print("启动： python run.py   然后打开 http://127.0.0.1:8000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
