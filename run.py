"""启动脚本：文案提取工具后端。

运行时准备（内置 ffmpeg PATH / Windows DLL 目录 / HF 镜像默认值）统一放在
`app.env.prepare_runtime_env()` 里 —— 跨平台安全，且可单测（云上 Linux 跑同样走它）。
"""
import logging

if __name__ == "__main__":
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