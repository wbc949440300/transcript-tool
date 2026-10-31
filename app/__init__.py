"""文案提取工具后端应用包。"""
from .env import load_env as _load_env

# 尽早加载项目根 .env；后续模块 import 时才能读到 TRANSCRIPT_* 配置
_load_env()
