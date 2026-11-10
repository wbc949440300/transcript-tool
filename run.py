"""启动脚本：文案提取工具后端。

运行时准备（内置 ffmpeg PATH / Windows DLL 目录 / HF 镜像默认值）统一放在
`app.env.prepare_runtime_env()` 里 —— 跨平台安全，且可单测（云上 Linux 跑同样走它）。
"""
import logging


def _force_utf8_console() -> None:
    """PyCharm/Windows 控制台默认按系统 GBK 编码，中文报错会变乱码。

    2026-11-05：把 stdout/stderr 强制成 UTF-8，PyCharm 的 Run 窗口才能正常显示
    `[WinError 10054] 远程主机强迫关闭了一个现有的连接` 这类中文错误。
    """
    import sys

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    _force_utf8_console()
    import os

    import uvicorn

    from app.env import prepare_runtime_env

    prepare_runtime_env()
    # 应用日志（watch/extractor 等）落到文件，否则 INFO 全被丢弃、出错无从排查
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler("app.log", encoding="utf-8"),
                  logging.StreamHandler()],
    )
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    logging.info("启动文案提取服务: http://%s:%s", host, port)
    uvicorn.run("app.main:app", host=host, port=port, reload=False)