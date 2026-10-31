"""pytest 全局准备：所有状态目录都指到临时目录，绝不碰真实 cookies/ 与历史库。

注意：app.utils / app.history 在 import 时就会读取环境变量，所以这里必须在
任何 app 模块被导入之前完成设置 —— conftest.py 会先于测试模块执行。
"""
import os
import pathlib
import tempfile

_TMP = tempfile.mkdtemp(prefix="transcript_tests_")

os.environ["TRANSCRIPT_COOKIE_DIR"] = os.path.join(_TMP, "cookies")
os.environ["TRANSCRIPT_DB"] = os.path.join(_TMP, "history.db")
os.environ["TRANSCRIPT_TASKS_DIR"] = os.path.join(_TMP, "tasks")
os.environ["TRANSCRIPT_ARCHIVE_DIR"] = os.path.join(_TMP, "archive")
os.environ["TRANSCRIPT_QUEUE_FILE"] = os.path.join(_TMP, "queue_state.json")
os.environ["TRANSCRIPT_ALERT_INTERVAL"] = "0"      # 测试里不要起后台巡检线程
# 测试绝不能真的往微信/机器人推消息，也不该弹桌面气泡（.env 里的真 key 会被读进来）
os.environ["TRANSCRIPT_ALERT_SERVERCHAN_KEY"] = ""
os.environ["TRANSCRIPT_ALERT_WEBHOOK"] = ""
os.environ["TRANSCRIPT_ALERT_NOTIFY"] = "0"
os.environ["TRANSCRIPT_ALERT_QUIET_HOURS"] = ""    # 测试里不要静默，否则推不出去（该测的测不到）
os.environ["TRANSCRIPT_WATCH_DEDUPE_FILE"] = os.path.join(_TMP, "watch_dedupe.json")
os.environ["TRANSCRIPT_VC_META"] = os.path.join(_TMP, "video_channel_meta.json")

# 测试里不要真的等待节流/批量间隔
os.environ["TRANSCRIPT_THROTTLE_JITTER"] = "0"
os.environ["TRANSCRIPT_THROTTLE_MIN_MS"] = "5"
os.environ["TRANSCRIPT_PLATFORM_MIN_MS"] = "5"
os.environ["TRANSCRIPT_BATCH_DELAY"] = "0"
os.environ["TRANSCRIPT_QUEUE_MAX"] = "50"
os.environ["TRANSCRIPT_COOKIE_MODE"] = "fallback"
# 固定 API token，供 TestClient 调用需要鉴权的端点（main.py 启动时读取）
os.environ["TRANSCRIPT_API_TOKEN"] = "test-token"

# 假的平台 cookie（should_use_cookie 需要"文件存在"）
_ck = pathlib.Path(os.environ["TRANSCRIPT_COOKIE_DIR"])
_ck.mkdir(parents=True, exist_ok=True)
for _name, _domain in (("douyin.txt", ".douyin.com"), ("bilibili.txt", ".bilibili.com")):
    (_ck / _name).write_text(
        f"# Netscape HTTP Cookie File\n{_domain}\tTRUE\t/\tTRUE\t0\tsessionid\tFAKE\n",
        encoding="utf-8",
    )
