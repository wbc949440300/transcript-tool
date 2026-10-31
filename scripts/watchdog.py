"""看门狗：文案提取服务挂了就自动拉起来（⑥ 服务自愈）。

用法：
    python scripts/watchdog.py --once             # 只检查一次（推荐放进计划任务）
    python scripts/watchdog.py                    # 常驻，每 60 秒检查一次
    python scripts/watchdog.py --once --dry-run   # 只报告该做什么，不真的启动

判定"活着"：TCP 能连上 127.0.0.1:<port> —— 不依赖 API token，探活不需要凭据。
拉起方式与手工重启一致：PowerShell Start-Process（隐藏窗口，日志落 server.log / server_err.log）。

注册成 Windows 计划任务（用户级，不需要管理员）：
    schtasks /create /tn "文案提取-看门狗" /sc minute /mo 5 ^
      /tr "\"F:\python\python.exe\" \"F:\文案提取工具\transcript_tool\scripts\watchdog.py\" --once"
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "watchdog.log"

logger = logging.getLogger("watchdog")


def _setup_log() -> None:
    """日志落文件；**只有真的有控制台时才加 StreamHandler**。

    计划任务用 pythonw.exe 跑（无控制台），那时 sys.stdout 是 None ——
    不加这个判断，logging 每次写日志都会自己报错刷屏。
    """
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:  # noqa: BLE001
        pass
    if getattr(sys, "stdout", None) is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)


def _alive(port: int, timeout: float = 2.0) -> bool:
    """服务是否活着：TCP 能连上就算活（比 HTTP 探活省事，也不需要 token）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _start_service(port: int, wait_s: float = 45.0) -> bool:
    """拉起服务（跨平台）。返回是否在 wait_s 秒内监听上端口。

    - Windows：CREATE_NO_WINDOW（隐藏控制台，会被 ffmpeg 等子进程继承，不会闪黑窗）
    - POSIX：start_new_session（脱离当前会话，看门狗退出也不影响服务）
    """
    py = sys.executable or "python"
    try:
        out = open(ROOT / "server.log", "ab")
        err = open(ROOT / "server_err.log", "ab")
    except Exception as exc:  # noqa: BLE001
        logger.warning("打不开日志文件: %s", exc)
        return False
    kwargs = {"cwd": str(ROOT), "stdout": out, "stderr": err}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([py, "run.py"], **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉起服务失败: %s", exc)
        return False
    finally:
        try:
            out.close()
            err.close()
        except Exception:  # noqa: BLE001
            pass
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _alive(port):
            return True
        time.sleep(1.5)
    return False


def check_once(port: int, dry_run: bool = False, starter=None) -> str:
    """检查一次。返回 alive / restarted / start_failed / dry_run。"""
    starter = starter or _start_service
    if _alive(port):
        return "alive"
    if dry_run:
        logger.warning("服务未监听 %s，但 --dry-run：不启动", port)
        return "dry_run"
    logger.warning("服务未监听 %s → 尝试拉起", port)
    ok = starter(port)
    if ok:
        logger.warning("已拉起服务（端口 %s 已就绪）", port)
        return "restarted"
    logger.error("拉起失败（看 server_err.log / app.log）")
    return "start_failed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    ap.add_argument("--interval", type=float, default=60.0, help="常驻模式下的检查间隔（秒）")
    ap.add_argument("--once", action="store_true", help="只检查一次")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不真的启动")
    args = ap.parse_args()

    _setup_log()
    if args.once:
        status = check_once(args.port, args.dry_run)
        if status != "alive":                     # 只在"有动作"时打日志，避免刷屏
            logger.info("检查结果: %s", status)
        return 0 if status in ("alive", "restarted", "dry_run") else 1

    logger.info("看门狗常驻：每 %.0f 秒检查一次 127.0.0.1:%s", args.interval, args.port)
    last = ""
    while True:
        try:
            status = check_once(args.port, args.dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查异常: %s", exc)
            status = "error"
        if status != last:
            logger.info("状态变化: %s → %s", last or "(启动)", status)
            last = status
        time.sleep(max(10.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())