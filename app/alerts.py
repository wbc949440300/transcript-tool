"""本地告警：失败率 / 队列积压 / 磁盘余量 / 平台冷却（④ 监控告警）。

设计原则：**不依赖任何外部服务**（不推送、不联网），只做两件事——
1. 打到 app.log（WARNING），而且"内容没变就不重复打"，避免每轮刷一条
2. 通过 `/risk/status` 暴露给页面横幅（用户打开页面就能看到）

阈值都可用 env 调：
  TRANSCRIPT_ALERT_FAIL_RATE   失败率阈值（默认 0.5）
  TRANSCRIPT_ALERT_TASKS       统计窗口条数（默认 20）
  TRANSCRIPT_ALERT_MIN_TASKS   窗口内至少几条才判（默认 5）
  TRANSCRIPT_ALERT_QUEUE       单平台排队积压阈值（默认 20）
  TRANSCRIPT_ALERT_DISK_GB     磁盘余量告警线（默认 2.0）
  TRANSCRIPT_ALERT_INTERVAL    巡检间隔秒数（默认 300，0=关闭）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("alerts")

_LAST_SIG = ""
_LAST_NOTIFY = 0.0
_LAST_WEBHOOK = 0.0
_NOTIFY_MIN_GAP = 600.0        # 两次桌面通知至少间隔 10 分钟，避免连弹
_WEBHOOK_MIN_GAP = 300.0       # 两次外部推送至少间隔 5 分钟
_LOCK = threading.Lock()

_COOLDOWN_PLATFORMS = (("douyin", "抖音"), ("kuaishou", "快手"),
                       ("xiaohongshu", "小红书"), ("bilibili", "B站"))


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:  # noqa: BLE001
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:  # noqa: BLE001
        return default


def _disk_alert() -> list[dict]:
    limit_gb = _env_float("TRANSCRIPT_ALERT_DISK_GB", 2.0)
    try:
        usage = shutil.disk_usage(str(Path.cwd()))
    except Exception:  # noqa: BLE001
        return []
    free_gb = usage.free / 1073741824
    if free_gb >= limit_gb:
        return []
    return [{"level": "danger", "code": "disk_low",
             "message": f"磁盘余量仅 {free_gb:.1f} GB（阈值 {limit_gb:.1f} GB），"
                        f"可调小 TRANSCRIPT_HISTORY_KEEP / TRANSCRIPT_MEDIA_KEEP_DAYS" }]


def _fail_rate_alert() -> list[dict]:
    from .history import history_store

    window = max(1, _env_int("TRANSCRIPT_ALERT_TASKS", 20))
    min_tasks = max(1, _env_int("TRANSCRIPT_ALERT_MIN_TASKS", 5))
    threshold = _env_float("TRANSCRIPT_ALERT_FAIL_RATE", 0.5)
    try:
        rows = [r for r in history_store.list(limit=window)
                if r.get("status") in ("SUCCESS", "FAILURE")]
    except Exception:  # noqa: BLE001
        return []
    if len(rows) < min_tasks:
        return []
    # 冷却期内的"秒拒"不算任务失败：那是风控闸门在保护，请求根本没发到平台。
    # 不排除的话，一次验证码会自我放大 —— 被拦 3 条 → 冷却 30 分钟 → 这期间提交的
    # 任务全被闸门挡回、各记一条 FAILURE → 显示成"60% 失败率"，看起来像整条链路崩了。
    # （2026-11-03 实测：20 条里 12 条失败，其中 9 条是冷却秒拒，真正打到平台只有 3 条）
    blocked = [r for r in rows
               if r.get("status") == "FAILURE"
               and "冷却中" in str(r.get("error_msg") or "")]
    failed = sum(1 for r in rows if r.get("status") == "FAILURE") - len(blocked)
    if failed <= 0:
        return []
    rate = failed / len(rows)
    if rate < threshold:
        return []
    extra = (f"；另有 {len(blocked)} 条因风控冷却被闸门拦下（未真正请求平台）"
             if blocked else "")
    return [{"level": "warn", "code": "fail_rate",
             "message": f"最近 {len(rows)} 条任务失败 {failed} 条（{rate:.0%}）{extra}，"
                        f"查 /risk/status 与 app.log 定位是哪一环节"}]


def _queue_alert() -> list[dict]:
    from .scheduler import queue_stats

    limit = max(1, _env_int("TRANSCRIPT_ALERT_QUEUE", 20))
    out = []
    try:
        stats = queue_stats() or {}
    except Exception:  # noqa: BLE001
        return []
    for key, row in stats.items():
        pending = int(row.get("pending") or 0)
        if pending >= limit:
            out.append({"level": "warn", "code": "queue_backlog",
                        "message": f"{key} 队列积压 {pending} 条（阈值 {limit}），"
                                   f"说明消费跟不上或者平台在冷却"})
    return out


def _cooldown_notes() -> list[dict]:
    from .utils import platform_cooldown_info

    out = []
    for key, label in _COOLDOWN_PLATFORMS:
        try:
            info = platform_cooldown_info(key)
        except Exception:  # noqa: BLE001
            continue
        remain = int(info.get("remaining") or 0)
        if remain > 0:
            out.append({"level": "info", "code": "cooldown",
                        "message": f"{label} 冷却中（约 {remain // 60 + 1} 分钟，"
                                   f"{info.get('reason') or '风控'}）"})
    return out


def _proxy_alert() -> list[dict]:
    """配了代理池但自检不通 → 报警。

    没有这条，坏代理会表现为"任务一条条失败"，很容易被误判成平台风控。
    """
    from .utils import proxy_checks, proxy_pool

    out = []
    try:
        checks = proxy_checks() or {}
    except Exception:  # noqa: BLE001
        return []
    for key, res in checks.items():
        if not res or res.get("direct"):
            continue
        if proxy_pool(key) and not res.get("ok"):
            out.append({"level": "warn", "code": "proxy_dead",
                        "message": f"{key} 的代理连不上（{res.get('detail')}），"
                                   f"检查 TRANSCRIPT_PROXY* 配置或出口是否可用"})
    return out


def _watch_alert() -> list[dict]:
    """监听目录导入失败（会自动重试，但用户得知道）——最近 30 分钟内 ≥2 次才提醒。"""
    try:
        from .watch_folder import status as watch_status

        failed = (watch_status() or {}).get("failed") or []
    except Exception:  # noqa: BLE001
        return []
    cutoff = time.time() - 1800
    recent = []
    for row in failed:
        try:
            ts = time.mktime(time.strptime(str(row.get("at") or ""), "%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            continue
        if ts >= cutoff:
            recent.append(row)
    if len(recent) < 2:
        return []
    return [{"level": "warn", "code": "watch_fail",
             "message": f"监听目录最近 30 分钟有 {len(recent)} 次导入失败"
                        f"（最新：{recent[0].get('file')}，会自动重试；详情见 app.log）"}]


def collect() -> list[dict]:
    """当前告警列表（磁盘 / 失败率 / 队列积压 / 监听导入 / 冷却 / 代理）。"""
    out: list[dict] = []
    for fn in (_disk_alert, _fail_rate_alert, _queue_alert, _watch_alert,
               _cooldown_notes, _proxy_alert):
        try:
            out.extend(fn() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("告警检查失败 %s: %s", getattr(fn, "__name__", fn), exc)
    return out


def _quiet_now(now: float | None = None) -> bool:
    """是否处于静默时段（`TRANSCRIPT_ALERT_QUIET_HOURS=22-8`，可跨午夜）。

    留空 = 不静默。格式不对也当作"不静默"—— 宁可不静默，也别因为写错把告警全吞了。
    """
    raw = (os.getenv("TRANSCRIPT_ALERT_QUIET_HOURS", "") or "").strip()
    if not raw:
        return False
    try:
        a, b = raw.replace("：", ":").replace("—", "-").split("-")
        start = int(a.strip().split(":")[0])
        end = int(b.strip().split(":")[0])
        if not (0 <= start <= 23 and 0 <= end <= 23):
            return False
    except Exception:  # noqa: BLE001
        return False
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    if start == end:
        return True                       # 22-22 视为全天静默
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end     # 跨午夜（22-8）


def _notify_gate(items: list[dict]) -> tuple[bool, str]:
    """该不该弹气泡/推微信。

    静默时段只放行 `danger`（磁盘满这类"工具要坏"的事故），`warn`/`info` 静默；
    日志与页面横幅**不受影响**，永远照记。
    """
    if not _quiet_now():
        return True, ""
    if any((i.get("level") or "") == "danger" for i in items):
        return True, "quiet-danger"
    return False, "quiet"


def push_webhook(items: list[dict]) -> bool:
    """外部推送告警：Server酱（表单）或任意 JSON webhook（企业微信/钉钉/飞书）。

    两种通道二选一，都没配就静默跳过：
      - `TRANSCRIPT_ALERT_SERVERCHAN_KEY=SCTxxxx`（SendKey）→ 推微信
        POST https://sctapi.ftqq.com/<key>.send，表单 title/desp（Server酱的协议）
      - `TRANSCRIPT_ALERT_WEBHOOK=<url>` → JSON POST，默认体是企业微信/钉钉格式
        `{"msgtype":"text","text":{"content":"…"}}`；飞书等自定义体用
        `TRANSCRIPT_ALERT_WEBHOOK_BODY={"msg_type":"text","content":{"text":"{msg}"}}`
        `{msg}` 会被替换成告警文本（已做 JSON 转义）。

    回执校验：HTTP 2xx 且（若回 JSON）`code`/`errcode`/`errno` 为 0 才算成功。
    失败只记日志，绝不抛异常影响主流程；走直连（不读系统代理）。
    """
    sc_key = (os.getenv("TRANSCRIPT_ALERT_SERVERCHAN_KEY", "") or "").strip()
    url = (os.getenv("TRANSCRIPT_ALERT_WEBHOOK", "") or "").strip()
    if not items or (not url and not sc_key):
        return False
    allow, why = _notify_gate(items)
    if not allow:
        logger.debug("静默时段（%s），跳过外部推送", why)
        return False
    global _LAST_WEBHOOK
    now = time.time()
    with _LOCK:
        if now - _LAST_WEBHOOK < _WEBHOOK_MIN_GAP:
            return False
        _LAST_WEBHOOK = now
    msg = "\n".join(f"[{i.get('level')}] {i.get('message')}" for i in items[:5])[:800]
    try:
        import urllib.parse
        import urllib.request

        from .utils import http_opener

        if not url:                      # Server酱：SendKey → 表单推送
            send_url = (os.getenv("TRANSCRIPT_ALERT_SERVERCHAN_URL", "") or "").strip() \
                or f"https://sctapi.ftqq.com/{sc_key}.send"
            body = urllib.parse.urlencode(
                {"title": f"文案提取工具告警（{len(items)} 条）", "desp": msg}).encode("utf-8")
            ctype = "application/x-www-form-urlencoded"
            url = send_url
        else:
            tpl = (os.getenv("TRANSCRIPT_ALERT_WEBHOOK_BODY", "") or "").strip()
            if tpl:
                escaped = json.dumps(msg, ensure_ascii=False)[1:-1]   # 已 JSON 转义的纯文本
                body = tpl.replace("{msg}", escaped).encode("utf-8")
            else:
                body = json.dumps({"msgtype": "text", "text": {"content": msg}},
                                  ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": ctype})
        with http_opener(media=True).open(req, timeout=10) as resp:
            code = resp.status
            raw = resp.read(500).decode("utf-8", "ignore")
        ok = 200 <= code < 300
        if ok and raw.lstrip().startswith("{"):
            try:                          # 各家回执字段不同：code / errcode / errno
                j = json.loads(raw)
                for field in ("code", "errcode", "errno"):
                    if field in j:
                        ok = int(j.get(field) or 0) == 0
                        break
                if not ok:
                    logger.warning("告警推送被拒: %s", raw[:160])
            except Exception:  # noqa: BLE001
                pass
        if ok:
            logger.info("告警已推送到外部（HTTP %s）", code)
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("告警外部推送失败: %s", exc)
        return False


def notify_desktop(items: list[dict]) -> bool:
    """Windows 托盘气泡通知（不需要任何账号或第三方依赖）。

    只用系统自带的 PowerShell + .NET NotifyIcon；非 Windows 或用户关掉
    （`TRANSCRIPT_ALERT_NOTIFY=0`）就静默跳过。两次通知至少间隔 10 分钟。
    """
    if os.name != "nt" or not items:
        return False
    # 桌面气泡默认**关**（最打扰的一种）；要就来 TRANSCRIPT_ALERT_NOTIFY=1
    if str(os.getenv("TRANSCRIPT_ALERT_NOTIFY", "0")).strip().lower() not in ("1", "true", "on", "yes"):
        return False
    allow, why = _notify_gate(items)
    if not allow:
        logger.debug("静默时段（%s），跳过桌面通知", why)
        return False
    global _LAST_NOTIFY
    now = time.time()
    with _LOCK:
        if now - _LAST_NOTIFY < _NOTIFY_MIN_GAP:
            return False
        _LAST_NOTIFY = now
    msg = "；".join(str(i.get("message") or "") for i in items[:3])[:200].replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
          f"$n.ShowBalloonTip(8000,'文案提取工具','{msg}','Warning');"
          "Start-Sleep -Seconds 9;$n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        logger.info("已弹桌面通知: %s", msg[:80])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("桌面通知失败: %s", exc)
        return False


def notify_task_done(title: str, ok: bool, chars: int = 0, note: str = "") -> bool:
    """任务完成气泡：监听目录（视频号）导入的视频转写完时弹一次。

    与"告警气泡"的区别：
    - 由 `TRANSCRIPT_WATCH_NOTIFY`（默认 **1**）控制 —— 这是"你点了下载、它跑完了"的
      直接反馈，不该还要求你额外打开告警气泡开关
    - 不与告警共用"10 分钟一次"的节流（你连下 5 条就该弹 5 次）
    - 仍然尊重静默时段（夜里不打扰）
    """
    if os.name != "nt":
        return False
    if str(os.getenv("TRANSCRIPT_WATCH_NOTIFY", "1")).strip().lower() in ("0", "false", "off", "no"):
        return False
    # 完成通知**默认不受静默时段限制**：它是"你刚下的视频跑完了"的直接反馈，
    # 不属于"告警打扰"（夜间下完视频就该知道成没成）。
    # 想让它也遵守静默时段：TRANSCRIPT_WATCH_NOTIFY_QUIET=1
    if str(os.getenv("TRANSCRIPT_WATCH_NOTIFY_QUIET", "0")).strip().lower() in ("1", "true", "on", "yes"):
        try:
            if _quiet_now():
                return False
        except Exception:  # noqa: BLE001
            pass
    head = "文案提取完成" if ok else "文案提取失败"
    tip = (title or "未命名")[:60].replace("'", "''")
    if ok:
        body = f"{tip}｜{chars} 字" if chars else tip
        icon = "Information"
        kind = "Info"
    else:
        body = f"{tip}｜{note or '请查看日志'}"[:160].replace("'", "''")
        icon = "Warning"
        kind = "Warning"
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$n=New-Object System.Windows.Forms.NotifyIcon;"
          f"$n.Icon=[System.Drawing.SystemIcons]::{icon};$n.Visible=$true;"
          f"$n.ShowBalloonTip(8000,'文案提取工具','{head}：{body}','{kind}');"
          "Start-Sleep -Seconds 9;$n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        logger.info("已弹完成通知: %s：%s", head, body[:60])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("完成通知失败: %s", exc)
        return False


def check_and_log() -> list[dict]:
    """检查一次并写日志；**只有告警内容变化时才写**，避免重复刷屏。

    新出现的 warn/danger 告警会顺带弹一次桌面通知（有 10 分钟最小间隔）。
    """
    global _LAST_SIG
    items = collect()
    sig = "|".join(sorted(f"{i['level']}:{i['code']}" for i in items))
    with _LOCK:
        changed = sig != _LAST_SIG
        _LAST_SIG = sig
    if changed:
        if not items:
            logger.info("告警已清空")
        for it in items:
            log = logger.warning if it.get("level") != "info" else logger.info
            log("告警: %s", it.get("message"))
        hot = [i for i in items if i.get("level") in ("warn", "danger")]
        if hot:
            notify_desktop(hot)
            push_webhook(hot)
    return items


def start_watch(interval: float | None = None) -> None:
    """后台线程定期巡检（默认 5 分钟；<=0 关闭）。"""
    if interval is None:
        interval = _env_float("TRANSCRIPT_ALERT_INTERVAL", 300.0)
    if interval <= 0:
        logger.info("告警巡检已关闭（TRANSCRIPT_ALERT_INTERVAL=0）")
        return

    def _loop():
        while True:
            try:
                check_and_log()
            except Exception as exc:  # noqa: BLE001
                logger.debug("告警循环异常: %s", exc)
            time.sleep(max(30.0, interval))

    threading.Thread(target=_loop, daemon=True, name="alerts-watch").start()
    logger.info("告警巡检已启动（每 %.0f 秒）", interval)