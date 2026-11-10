# 本目录约定

本仓库是 `文案提取工具` 的源码目录，下面是最关键的四条协作约定：

- **发新版必须保留 dist 里全部历史版本目录**（`dist\文案提取工具-v<版本>\`），dist 只增不删；删任何历史版本前必须先问用户。
- 默认只出文件夹 + 更新 `dist\版本更新记录.md`，不加 `--zip`。
- 发版前 `pytest tests/ -q -p no:warnings` 必须全绿。
- **发版红线（2026-11-09 起）：用户解压后只需双击 start.bat**，Python 与全部依赖由启动链
  （`scripts\setup_python.ps1` 自动装 Python → `scripts\launch.py` 自动装依赖/ffmpeg/浏览器内核）
  自动配置，不许要求用户手动装环境；`build_release.py` 打包时自动校验启动链文件齐全。
