"""告警（④）：失败率 / 队列积压 / 磁盘余量 / 冷却提示 / 外部推送。"""
import collections
import json

import pytest

from app import alerts, utils


@pytest.fixture(autouse=True)
def _healthy_by_default(monkeypatch):
    """默认把三个来源调成"正常"，各用例只覆盖自己要测的那一个。

    这里只打桩**数据来源**（历史库 / 队列 / 磁盘），不打桩告警函数本身，
    这样被测的判定逻辑才是真的在执行。
    """
    monkeypatch.setenv("TRANSCRIPT_ALERT_DISK_GB", "0")        # 0 → 磁盘永远不告警
    monkeypatch.setattr("app.history.history_store.list", lambda limit=20: [])
    monkeypatch.setattr("app.scheduler.queue_stats", lambda: {})
    for key in ("douyin", "kuaishou", "xiaohongshu", "bilibili"):
        utils.clear_platform_cooldown(key)
    yield
    for key in ("douyin", "kuaishou", "xiaohongshu", "bilibili"):
        utils.clear_platform_cooldown(key)


def _codes(items):
    return sorted(i["code"] for i in items)


def test_no_alerts_when_healthy():
    assert alerts.collect() == []


def test_fail_rate_alert(monkeypatch):
    rows = [{"status": "FAILURE"}] * 4 + [{"status": "SUCCESS"}] * 2
    monkeypatch.setattr("app.history.history_store.list", lambda limit=20: rows)
    items = alerts.collect()
    assert "fail_rate" in _codes(items)
    assert any("失败 4 条" in i["message"] for i in items)


def test_fail_rate_needs_minimum_samples(monkeypatch):
    monkeypatch.setattr("app.history.history_store.list",
                        lambda limit=20: [{"status": "FAILURE"}] * 2)
    assert "fail_rate" not in _codes(alerts.collect())


def test_fail_rate_below_threshold(monkeypatch):
    rows = [{"status": "FAILURE"}] + [{"status": "SUCCESS"}] * 9
    monkeypatch.setattr("app.history.history_store.list", lambda limit=20: rows)
    assert "fail_rate" not in _codes(alerts.collect())


def test_queue_backlog_alert(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_ALERT_QUEUE", "20")
    monkeypatch.setattr("app.scheduler.queue_stats",
                        lambda: {"douyin": {"pending": 25, "running": True}})
    items = alerts.collect()
    assert "queue_backlog" in _codes(items)
    assert any("积压 25 条" in i["message"] for i in items)


def test_queue_below_threshold(monkeypatch):
    monkeypatch.setattr("app.scheduler.queue_stats",
                        lambda: {"douyin": {"pending": 3, "running": False}})
    assert "queue_backlog" not in _codes(alerts.collect())


def test_disk_alert(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_ALERT_DISK_GB", "5")
    monkeypatch.setattr(alerts.shutil, "disk_usage",
                        lambda p: collections.namedtuple("u", "total used free")(0, 0, 1024 ** 3))
    hit = [i for i in alerts.collect() if i["code"] == "disk_low"]
    assert hit and hit[0]["level"] == "danger"


def test_cooldown_note():
    utils.mark_platform_cooldown("douyin", 600, kind="risk", reason="HTTP 403")
    items = alerts.collect()
    assert "cooldown" in _codes(items)
    assert any(i["level"] == "info" for i in items)


def test_check_and_log_is_idempotent():
    utils.mark_platform_cooldown("kuaishou", 600, kind="risk", reason="验证码")
    first = alerts.check_and_log()
    second = alerts.check_and_log()
    assert _codes(first) == _codes(second)


class _Hook:
    """本地假 webhook：只收 POST，记下 body 与 content-type，可指定回执。"""

    def __init__(self, reply: str = "ok"):
        import http.server
        import threading

        got = self.got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                got["body"] = self.rfile.read(n).decode("utf-8")
                got["ctype"] = self.headers.get("Content-Type") or ""
                self.send_response(200)
                self.end_headers()
                self.wfile.write(reply.encode("utf-8"))

            def log_message(self, *args):  # 静音
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/hook"

    def stop(self):
        self.srv.shutdown()


def test_webhook_posts_alert(monkeypatch):
    """外部推送：默认体是企业微信/钉钉格式，消息在里面。"""
    hook = _Hook()
    monkeypatch.setenv("TRANSCRIPT_ALERT_WEBHOOK", hook.url)
    monkeypatch.delenv("TRANSCRIPT_ALERT_WEBHOOK_BODY", raising=False)
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    try:
        ok = alerts.push_webhook([{"level": "warn", "message": "磁盘快满了"}])
    finally:
        hook.stop()
    assert ok is True
    body = json.loads(hook.got["body"])
    assert body["msgtype"] == "text" and "磁盘快满了" in body["text"]["content"]


def test_webhook_custom_template(monkeypatch):
    hook = _Hook()
    monkeypatch.setenv("TRANSCRIPT_ALERT_WEBHOOK", hook.url)
    monkeypatch.setenv("TRANSCRIPT_ALERT_WEBHOOK_BODY",
                       '{"msg_type":"text","content":{"text":"{msg}"}}')
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    try:
        alerts.push_webhook([{"level": "danger", "message": "带\"引号\"的告警"}])
    finally:
        hook.stop()
    body = json.loads(hook.got["body"])          # 引号必须已被正确转义
    assert body["msg_type"] == "text"
    assert '带"引号"的告警' in body["content"]["text"]


def test_webhook_skips_when_unset(monkeypatch):
    monkeypatch.delenv("TRANSCRIPT_ALERT_WEBHOOK", raising=False)
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    assert alerts.push_webhook([{"level": "warn", "message": "x"}]) is False


def test_webhook_failure_is_silent(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_ALERT_WEBHOOK", "http://127.0.0.1:9/none")
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    assert alerts.push_webhook([{"level": "warn", "message": "x"}]) is False   # 不抛异常


def test_webhook_rate_limited(monkeypatch):
    hook = _Hook()
    monkeypatch.setenv("TRANSCRIPT_ALERT_WEBHOOK", hook.url)
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    try:
        assert alerts.push_webhook([{"level": "warn", "message": "第一次"}]) is True
        assert alerts.push_webhook([{"level": "warn", "message": "第二次"}]) is False   # 5 分钟内只推一次
    finally:
        hook.stop()


def test_serverchan_form_push(monkeypatch):
    """Server酱：SendKey → **表单** POST（title/desp），回执 code=0 才算成功。"""
    hook = _Hook(reply='{"code":0,"message":"SUCCESS"}')
    monkeypatch.delenv("TRANSCRIPT_ALERT_WEBHOOK", raising=False)
    monkeypatch.setenv("TRANSCRIPT_ALERT_SERVERCHAN_KEY", "SCTtestkey")
    monkeypatch.setenv("TRANSCRIPT_ALERT_SERVERCHAN_URL", hook.url)
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    try:
        ok = alerts.push_webhook([{"level": "warn", "message": "队列积压 30 条"}])
    finally:
        hook.stop()
    assert ok is True
    assert "x-www-form-urlencoded" in hook.got["ctype"]
    from urllib.parse import parse_qs
    form = parse_qs(hook.got["body"])
    assert "告警" in form["title"][0]
    assert "队列积压 30 条" in form["desp"][0]


def test_serverchan_rejected_is_false(monkeypatch):
    """回执 code 非 0（SendKey 无效等）→ 返回 False，不抛异常。"""
    hook = _Hook(reply='{"code":40001,"message":"bad key"}')
    monkeypatch.delenv("TRANSCRIPT_ALERT_WEBHOOK", raising=False)
    monkeypatch.setenv("TRANSCRIPT_ALERT_SERVERCHAN_KEY", "SCTbad")
    monkeypatch.setenv("TRANSCRIPT_ALERT_SERVERCHAN_URL", hook.url)
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    try:
        assert alerts.push_webhook([{"level": "warn", "message": "x"}]) is False
    finally:
        hook.stop()


def test_webhook_errno_nonzero_is_false(monkeypatch):
    """通用 JSON webhook：errcode 非 0 也算失败（企业微信/钉钉就是这样回执的）。"""
    hook = _Hook(reply='{"errcode":1,"errmsg":"invalid webhook url"}')
    monkeypatch.setenv("TRANSCRIPT_ALERT_WEBHOOK", hook.url)
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    try:
        assert alerts.push_webhook([{"level": "warn", "message": "x"}]) is False
    finally:
        hook.stop()


# ---------- 静默通知：不影响体验 ----------

def _ts(hour: int) -> float:
    import time
    return time.mktime(time.struct_time((2026, 10, 31, hour, 30, 0, 0, 0, -1)))


@pytest.mark.parametrize("expr,hour,want", [
    ("22-8", 23, True), ("22-8", 3, True), ("22-8", 12, False), ("22-8", 8, False),
    ("9-18", 15, True), ("9-18", 20, False),
    ("0-0", 13, True),          # 全天静默
    ("", 3, False),             # 没配 = 不静默
    ("garbage", 3, False),      # 写错 = 不静默（别把告警全吞了）
    ("25-30", 3, False),
])
def test_quiet_hours(monkeypatch, expr, hour, want):
    monkeypatch.setenv("TRANSCRIPT_ALERT_QUIET_HOURS", expr)
    assert alerts._quiet_now(_ts(hour)) is want


def test_quiet_keeps_danger_only(monkeypatch):
    """静默时段：warn/info 不弹，danger（工具要坏）照样放行。"""
    monkeypatch.setattr(alerts, "_quiet_now", lambda now=None: True)
    assert alerts._notify_gate([{"level": "warn", "message": "x"}])[0] is False
    assert alerts._notify_gate([{"level": "info", "message": "x"}])[0] is False
    assert alerts._notify_gate([{"level": "danger", "message": "x"}])[0] is True


def test_quiet_suppresses_wechat_push(monkeypatch):
    """静默时段连请求都不发（微信响就是打扰）。"""
    hook = _Hook(reply='{"code":0}')
    monkeypatch.setenv("TRANSCRIPT_ALERT_SERVERCHAN_KEY", "SCTx")
    monkeypatch.setenv("TRANSCRIPT_ALERT_SERVERCHAN_URL", hook.url)
    monkeypatch.setattr(alerts, "_LAST_WEBHOOK", 0.0)
    monkeypatch.setattr(alerts, "_quiet_now", lambda now=None: True)
    try:
        assert alerts.push_webhook([{"level": "warn", "message": "夜里不该推"}]) is False
        assert "body" not in hook.got
    finally:
        hook.stop()


def test_desktop_notify_is_off_by_default(monkeypatch):
    """桌面气泡默认关：不加任何配置就不会弹。"""
    monkeypatch.delenv("TRANSCRIPT_ALERT_NOTIFY", raising=False)
    assert alerts.notify_desktop([{"level": "warn", "message": "x"}]) is False