"""任务调度：每个平台一条串行队列（P1）。

为什么这么做：
- 以前"一个任务一个线程"，批量 10 条就是 10 路并发打平台 —— 平台侧看到的是
  "一个人同时开十个窗口"，这是最典型的机器行为。
- 现在同一平台同时只跑 1 个任务（跨平台仍然并行），平台侧看到的是"排队办事"。
- 出队前先过两道闸：平台冷却（熔断）→ 每日预算。任何一道不过，直接失败并给出
  人能看懂的原因，**不再碰平台**。
- 队列有上限（每平台），满了接口直接拒绝，避免把内存/磁盘排爆。

平台并发=1 也顺便让"半开探测"变成自然行为：冷却到期后的第一条任务就是试探，
成功 → 熔断清零；再失败 → 冷却翻倍（见 utils.mark_platform_cooldown）。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import risk
from .models import TaskDetail, TaskStatus
from .platforms import adapter_for
from .task_store import store
from .utils import (clear_platform_cooldown, platform_cooldown_enabled,
                    platform_cooldown_remaining)

logger = logging.getLogger("scheduler")

_QUEUES: dict[str, queue.Queue] = {}
_WORKERS: dict[str, threading.Thread] = {}
_GUARD = threading.Lock()
_SAVE_LOCK = threading.Lock()       # 落盘专用（避免并发写同一个 tmp）
_STATE: dict[str, dict] = {}
_JOBS: dict[str, "_Job"] = {}      # taskId -> job（排队中或正在跑），用于落盘恢复


def _queue_file() -> Path:
    return Path(os.getenv("TRANSCRIPT_QUEUE_FILE", "queue_state.json"))


@dataclass
class _Job:
    task: object
    url: str
    want_audio: bool
    model: str
    ocr_frames: bool
    platform: str
    state: str = "queued"          # queued / running


def _job_to_dict(job: _Job) -> dict:
    try:
        task_data = job.task.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        logger.debug("任务序列化失败: %s", exc)
        task_data = None
    return {"task": task_data, "url": job.url, "want_audio": job.want_audio,
            "model": job.model, "ocr_frames": job.ocr_frames,
            "platform": job.platform, "state": job.state,
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}


def _read_state() -> dict:
    f = _queue_file()
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state() -> None:
    """把"排队中 + 正在跑"的任务落盘（③ 持久队列）。原子写，失败只记日志。

    Windows 上 rename 可能因为杀软/并发读而短暂失败（WinError 32），
    这里用**每次唯一的 tmp 名** + 专用锁 + 短暂重试来兜住。
    """
    try:
        with _GUARD:
            jobs = [_job_to_dict(j) for j in _JOBS.values()]
        payload = json.dumps({"jobs": jobs, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                             ensure_ascii=False)
        f = _queue_file()
        with _SAVE_LOCK:
            tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(payload, encoding="utf-8")
            for attempt in range(3):
                try:
                    tmp.replace(f)
                    return
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
    except Exception as exc:  # noqa: BLE001
        logger.warning("队列状态落盘失败（不影响运行）: %s", exc)


def restore() -> int:
    """服务启动时恢复队列：把上次没跑完的（排队 + 运行中）任务重新入队。

    运行中被重启打断的任务会从头再跑一遍（ASR 结果本来就没落库），
    这比"悄悄丢掉"更符合预期。
    """
    jobs = (_read_state().get("jobs") or [])
    if not jobs:
        return 0
    n = 0
    for item in jobs:
        task_data = item.get("task")
        if not isinstance(task_data, dict):
            continue
        try:
            task = TaskDetail.model_validate(task_data)
        except Exception:  # noqa: BLE001
            continue
        created = task.createTime
        task.status = TaskStatus.PENDING
        store.create(task)
        if created:
            task.createTime = created
        url = (item.get("url") or task.webpageUrl or "").strip()
        if not url:
            continue
        if submit(task, url, bool(item.get("want_audio")),
                  item.get("model") or "medium", ocr_frames=bool(item.get("ocr_frames"))):
            n += 1
    if n:
        logger.info("队列恢复：重新入队 %d 条（上次重启前未完成）", n)
        _save_state()
    return n


def _queue_max() -> int:
    try:
        return max(1, int(os.getenv("TRANSCRIPT_QUEUE_MAX", "100")))
    except Exception:
        return 100


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url or "", re.I)
    return (m.group(1).lower() if m else "") or "default"


def queue_key(url: str) -> str:
    """排队键：已知平台按平台排；通用站点按域名排（否则所有站点会串成一条）。"""
    a = adapter_for(url)
    if a.key != "default":
        return a.key
    return "host:" + _host_of(url)


def can_accept(url: str) -> bool:
    key = queue_key(url)
    with _GUARD:
        q = _QUEUES.get(key)
        return q is None or q.qsize() < _queue_max()


def can_accept_many(urls: list[str]) -> bool:
    """批量提交前的一次性容量检查（按平台累计，而不是逐条看当前深度）。"""
    counts: dict[str, int] = {}
    for u in urls:
        k = queue_key(u)
        counts[k] = counts.get(k, 0) + 1
    limit = _queue_max()
    with _GUARD:
        for k, n in counts.items():
            q = _QUEUES.get(k)
            depth = q.qsize() if q is not None else 0
            if depth + n > limit:
                return False
    return True


def submit(task, url: str, want_audio: bool, whisper_model: str,
           ocr_frames: bool = False) -> bool:
    """把任务放进对应平台的队列。返回 False 表示队列已满。"""
    key = queue_key(url)
    adapter = adapter_for(url)
    with _GUARD:
        q = _QUEUES.get(key)
        if q is None:
            q = queue.Queue()
            _QUEUES[key] = q
        if q.qsize() >= _queue_max():
            return False
        _STATE.setdefault(key, {"pending": 0, "running": False})
        _STATE[key]["pending"] += 1
        job = _Job(task=task, url=url, want_audio=want_audio, model=whisper_model,
                   ocr_frames=ocr_frames, platform=adapter.key)
        _JOBS[task.taskId] = job
        q.put(job)
        th = _WORKERS.get(key)
        if th is None or not th.is_alive():
            th = threading.Thread(target=_worker, args=(key,), daemon=True, name=f"queue-{key}")
            _WORKERS[key] = th
            th.start()
    _save_state()          # 落盘放在锁外（_save_state 自己会加锁）
    try:
        pending = queue_stats().get(key, {}).get("pending", 1)
        store.update_status(task.taskId, TaskStatus.PENDING,
                            errorMessage=f"排队中（{adapter.label}队列第 {pending} 位）")
    except Exception:  # noqa: BLE001
        pass
    return True


def queue_stats() -> dict:
    with _GUARD:
        return {k: {"pending": int(v.get("pending") or 0), "running": bool(v.get("running"))}
                for k, v in _STATE.items()}


def stats() -> dict:
    """按平台汇总队列 + 预算 + 熔断（供 /risk/status）。"""
    queues = queue_stats()
    out = {}
    for a in adapter_for_all():
        q = queues.get(a.key, {})
        out[a.key] = {
            "label": a.label,
            "pending": int(q.get("pending") or 0),
            "running": bool(q.get("running")),
            "budget": risk.budget_state(a.key),
            "breaker": risk.breaker_state(a.key),
        }
    return out


def adapter_for_all():
    from .platforms import all_adapters

    return all_adapters()


def _pause_between_tasks() -> None:
    """同一平台两条任务之间的间隔（平台级节流之外的额外缓冲）。"""
    try:
        delay = float(os.getenv("TRANSCRIPT_BATCH_DELAY", "3.0"))
    except Exception:
        delay = 3.0
    if delay > 0:
        time.sleep(delay + random.uniform(0, delay * 0.5))


def _fail(task, msg: str) -> None:
    try:
        store.update_status(task.taskId, TaskStatus.FAILURE, errorMessage=msg)
    except Exception:  # noqa: BLE001
        pass


def _run_job(job: _Job) -> None:
    adapter = adapter_for(job.url)
    plat = job.platform
    # 闸门 1：熔断/冷却
    if platform_cooldown_enabled(plat):
        remain = platform_cooldown_remaining(plat)
        if remain > 0:
            _fail(job.task,
                  f"{adapter.label}风控冷却中（约 {int(remain // 60) + 1} 分钟后自动恢复）。"
                  "刚被限流时继续请求会加深标记，建议先提取其他平台。")
            return
    # 闸门 2：每日预算
    allowed, bstate = risk.allow(plat)
    if not allowed:
        _fail(job.task,
              f"{adapter.label}今日额度已用完（{bstate['used']}/{bstate['limit']} 条）。"
              "明天自动恢复；上限 = 单账号额度 × 账号池账号数，"
              f"可用 TRANSCRIPT_{plat.upper()}_DAILY_BUDGET 调整单账号额度。")
        return
    risk.consume(plat)

    from .extractor import _run_task

    # 队列提示会一直挂在 errorMessage 上，这里先换成"开始提取"，避免界面显示
    # "RUNNING + 排队中第 1 位"这种自相矛盾的状态。
    try:
        store.update_status(job.task.taskId, TaskStatus.RUNNING, errorMessage="正在开始提取...")
    except Exception:  # noqa: BLE001
        pass

    _run_task(job.task, job.url, job.want_audio, job.model, ocr_frames=job.ocr_frames)

    # 成功一次 = 平台是健康的 → 熔断清零（半开探测成功）
    try:
        if getattr(job.task, "status", None) == TaskStatus.SUCCESS:
            clear_platform_cooldown(plat)
    except Exception:  # noqa: BLE001
        pass


def _worker(key: str) -> None:
    logger.info("队列启动: %s", key)
    while True:
        with _GUARD:
            q = _QUEUES.get(key)
        if q is None:
            return
        try:
            job = q.get(timeout=1.0)
        except queue.Empty:
            with _GUARD:
                # 空闲且没新任务 → 退出线程，避免长期占着（下次 submit 会重建）
                if q.empty():
                    _QUEUES.pop(key, None)
                    _WORKERS.pop(key, None)
                    return
            continue
        with _GUARD:
            st = _STATE.setdefault(key, {"pending": 0, "running": False})
            st["pending"] = max(0, int(st.get("pending") or 0) - 1)
            st["running"] = True
            job.state = "running"
        _save_state()          # 标记为"正在跑"，服务崩了下次启动会重跑
        try:
            _run_job(job)
        except Exception:  # noqa: BLE001
            logger.exception("任务执行异常: %s", getattr(job.task, "taskId", "?"))
            _fail(job.task, "任务执行异常（详见 app.log）")
        finally:
            with _GUARD:
                st = _STATE.setdefault(key, {"pending": 0, "running": False})
                st["running"] = False
                _JOBS.pop(getattr(job.task, "taskId", ""), None)
            _save_state()
            _pause_between_tasks()
