"""子进程调用的跨平台默认值。

Windows 上 ffmpeg / ffprobe 这类控制台程序默认会弹出黑窗——每个转写任务都要
闪好几次（音频增强、流探测、抽帧、转封装…），打包给非技术用户时观感很差。
统一从这里取参数，避免逐个调用点漏掉。

用法::

    from .subproc import hidden
    subprocess.run(["ffprobe", ...], capture_output=True, **hidden())

注意：调用方若要显式指定 creationflags，就不要再用 hidden()（会重复传参）。
"""
from __future__ import annotations

import os
import subprocess

# Windows 的 CREATE_NO_WINDOW；用 getattr 兜底是为了在非 Windows 上也能安全求值
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def hidden() -> dict:
    """供 subprocess.run / Popen 展开的关键字：Windows 隐藏控制台，其它平台返回空。"""
    if os.name != "nt":
        return {}
    return {"creationflags": CREATE_NO_WINDOW}