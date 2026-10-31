"""浸泡测试（soak）：长时间跑转写，观察内存 / SQLite / 磁盘 / 队列的稳定性。

**只走本地文件**（上传接口），不发任何平台请求 —— 所以不消耗每日额度、不会触发风控，
专门用来验证"连续跑很久会不会变慢/涨内存/堆垃圾"。

用法：
    python scripts/soak.py --minutes 60                     # 跑 60 分钟
    python scripts/soak.py --rounds 5 --files-per-round 2   # 或按轮次
    python scripts/soak.py --minutes 5 --clip-seconds 8 --model tiny   # 快速自检

产出：`soak_report.jsonl`（每轮一行）+ 结束时打印汇总（含每轮耗时、RSS、库/磁盘增长）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8000/prod-api"
REPORT = ROOT / "soak_report.jsonl"


def _curl(args: list[str], timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(["curl", "-s", *args], capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout or b"").decode("utf-8", "ignore")


def _login(port: int, jar: str) -> bool:
    """拿页面会话 cookie（和浏览器一样：访问一次首页即可）。"""
    rc, _ = _curl(["-c", jar, "-o", os.devnull, f"http://127.0.0.1:{port}/"])
    return rc == 0


def _api_get(port: int, jar: str, path: str) -> dict:
    rc, body = _curl(["-b", jar, f"{API}{path}"])
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {}


def _upload(port: int, jar: str, path: Path, model: str) -> str:
    rc, body = _curl(["-b", jar, "-F", f"file=@{path}", "-F", f"whisperModel={model}",
                      f"{API}/file/transcript/doTask"], timeout=300)
    try:
        return (json.loads(body).get("data") or {}).get("taskId") or ""
    except Exception:  # noqa: BLE001
        return ""


def _server_pid(port: int) -> str:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=20).stdout.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                return parts[-1]
    return ""


def _server_rss_mb(port: int) -> float:
    """服务进程常驻内存（MB）。中文 Windows 的 tasklist 输出是 GBK，注意解码。"""
    pid = _server_pid(port)
    if not pid:
        return 0.0
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=20)
        line = r.stdout.decode("gbk", "ignore").strip().strip('"')
        if not line or "No tasks" in line:
            return 0.0
        fields = [f.strip('"') for f in line.split('","')]
        mem = fields[-1] if fields else ""
        return round(float(mem.replace(" K", "").replace(",", "").strip()) / 1024, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _dir_mb(p: Path) -> float:
    try:
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return round(total / 1048576, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _make_clip(path: Path, seconds: int) -> None:
    """生成一段"像人声在说"的音频：正弦 + 噪声混合（ASR 照样要跑一遍推理）。"""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
           "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={seconds}",
           "-filter_complex", "amix=inputs=2:duration=shortest",
           "-ar", "16000", "-ac", "1", str(path)]
    subprocess.run(cmd, capture_output=True, timeout=120, check=True)


def _sample(port: int, jar: str) -> dict:
    hist = _api_get(port, jar, "/history/list?page=1&pageSize=1")
    risk = _api_get(port, jar, "/risk/status")
    tasks_dir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks"))
    pid = _server_pid(port)
    return {
        "server_pid": pid,
        "rss_mb": _server_rss_mb(port),
        "history_total": ((hist.get("data") or {}).get("total")),
        "tasks_mb": _dir_mb(tasks_dir),
        "cookies_mb": _dir_mb(ROOT / "cookies"),
        "disk_free_gb": round(shutil.disk_usage(str(ROOT)).free / 1073741824, 1),
        "alerts": len(risk.get("alerts") or []),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    ap.add_argument("--minutes", type=float, default=0, help="跑满多少分钟就停（0=按轮次）")
    ap.add_argument("--rounds", type=int, default=3, help="跑多少轮（--minutes>0 时忽略）")
    ap.add_argument("--files-per-round", type=int, default=2)
    ap.add_argument("--clip-seconds", type=int, default=12)
    ap.add_argument("--gap", type=float, default=5.0, help="每轮之间停顿几秒（别把库写爆）")
    ap.add_argument("--model", default="tiny")
    args = ap.parse_args()

    logger = lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)  # noqa: E731
    tmp = Path(tempfile.mkdtemp(prefix="soak_"))
    jar = str(tmp / "jar.txt")
    if not _login(args.port, jar):
        logger("登录失败：服务在跑吗？")
        return 1

    clips = []
    for i in range(args.files_per_round):
        p = tmp / f"clip_{i}.wav"
        _make_clip(p, args.clip_seconds)
        clips.append(p)
    logger(f"准备 {len(clips)} 个 {args.clip_seconds}s 音频；模型={args.model}")

    start = time.time()
    deadline = start + args.minutes * 60 if args.minutes > 0 else None
    rnd = 0
    failures = 0
    prev_pid = ""
    with REPORT.open("a", encoding="utf-8") as fh:
        while True:
            if deadline is not None:
                if time.time() >= deadline:
                    break
            elif rnd >= args.rounds:
                break
            rnd += 1
            t0 = time.time()
            ids = [_upload(args.port, jar, c, args.model) for c in clips]
            valid = [i for i in ids if i]
            if not valid:
                # 服务被重启/挂了：等待它回来再继续，而不是空转一轮
                wait_s = 0
                while wait_s < 600 and not _login(args.port, jar):
                    time.sleep(10)
                    wait_s += 10
                logger(f"第 {rnd} 轮拿不到任务号（服务不可达），已等待 {wait_s}s 后{'恢复' if wait_s < 600 else '仍不可用，跳过'}")
                if wait_s >= 600:
                    failures += 1
                    if failures >= 3:
                        logger("连续多轮服务不可达 → 停止")
                        break
                continue
            done_ids: set[str] = set()
            for _ in range(60):                      # 等这一轮跑完（最多 3 分钟）
                if time.time() - t0 > 180:
                    break
                for tid in valid:
                    if tid in done_ids:
                        continue
                    d = (_api_get(args.port, jar, f"/transcript/queryTask?taskId={tid}").get("data") or {})
                    if d.get("status") in ("SUCCESS", "FAILURE"):
                        done_ids.add(tid)
                if len(done_ids) >= len(valid):
                    break
                time.sleep(3)
            ok = len(done_ids)
            row = {"round": rnd, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "submitted": len(valid), "finished": ok,
                   "seconds": round(time.time() - t0, 1), **_sample(args.port, jar)}
            if prev_pid and row.get("server_pid") and row["server_pid"] != prev_pid:
                row["server_restarted"] = True          # 外部重启过服务，RSS 会回落到基线
                logger(f"⚠ 检测到服务被重启（PID {prev_pid} → {row['server_pid']}），RSS 基线会重置")
            prev_pid = row.get("server_pid") or prev_pid
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            logger(f"第 {rnd} 轮：{row['finished']}/{row['submitted']} 完成 "
                   f"{row['seconds']}s | RSS {row['rss_mb']}MB | 历史 {row['history_total']} 条 "
                   f"| tasks {row['tasks_mb']}MB | 磁盘余 {row['disk_free_gb']}GB")
            if row["submitted"] and row["finished"] == 0:
                failures += 1
                if failures >= 2:
                    logger("连续两轮一个都没完成 → 停止（看 app.log）")
                    break
            if args.gap > 0:
                time.sleep(args.gap)

    rows = [json.loads(x) for x in REPORT.read_text(encoding="utf-8").splitlines() if x.strip()]
    recent = rows[-max(1, rnd):]
    if recent:
        first, last = recent[0], recent[-1]
        logger("=== 汇总 ===")
        logger(f"轮次 {first['round']}→{last['round']} | RSS {first['rss_mb']}→{last['rss_mb']} MB "
               f"（增长 {round(last['rss_mb'] - first['rss_mb'], 1)} MB）")
        logger(f"历史 {first['history_total']}→{last['history_total']} 条 | "
               f"tasks {first['tasks_mb']}→{last['tasks_mb']} MB | "
               f"cookies {first['cookies_mb']}→{last['cookies_mb']} MB | "
               f"磁盘余 {last['disk_free_gb']} GB")
        logger(f"报告：{REPORT}")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())