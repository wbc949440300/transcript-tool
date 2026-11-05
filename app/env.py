"""项目根目录 .env 的统一加载入口。

必须在 app 包初始化时最先执行，保证 history/utils 等模块
在 import 阶段读取的环境变量同样能拿到 .env 配置。
不覆盖进程中已经设置的环境变量。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_loaded = False


def load_env(path: Path | None = None) -> None:
    global _loaded
    if _loaded and path is None:
        return
    target = Path(path) if path is not None else ENV_FILE
    try:
        lines = target.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)
    _loaded = True


def prepare_runtime_env(root: Path | None = None) -> dict:
    """启动前的运行时准备（**跨平台安全**）。返回做了什么，便于日志与测试。

    - 项目内置 ffmpeg（`tools/ffmpeg/bin`，由 scripts/setup_new_machine.py 下载）
      优先于系统 PATH —— 新机部署不必手工配环境变量
    - Windows 上额外把该目录加进 DLL 搜索路径（`os.add_dll_directory` 只有 Windows 有，
      云上 Linux 跑必须能跳过）
    - HF 镜像默认值（用户显式设置过则不覆盖）
    """
    info: dict = {"ffmpeg_dir": "", "dll_dir_added": False, "hf_endpoint": ""}
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    tools_ffmpeg = base / "tools" / "ffmpeg" / "bin"
    if tools_ffmpeg.is_dir():
        p = str(tools_ffmpeg)
        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
        info["ffmpeg_dir"] = p
        add_dll = getattr(os, "add_dll_directory", None)     # 仅 Windows 存在
        if add_dll is not None:
            try:
                add_dll(p)
                info["dll_dir_added"] = True
            except Exception:  # noqa: BLE001
                pass
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    info["hf_endpoint"] = os.environ["HF_ENDPOINT"]
    return info
