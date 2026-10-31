"""调度器：每平台串行、跨平台并行、队列容量、预算与冷却闸门。"""
import threading
import time

import pytest

from app import risk, scheduler, utils
from app.models import TaskDetail, TaskStatus
from app.task_store import store


@pytest.fixture
def fake_run(monkeypatch):
    """替换真正的抓取流程，只记录时间窗，不联网。"""
    spans: dict[str, list] = {}
    lock = threading.Lock()

    def fake(task, url, want_audio, model, ocr_frames=False):
        with lock:
            spans.setdefault(url, [time.time(), None])
        time.sleep(0.15)
        store.update_status(task.taskId, TaskStatus.SUCCESS, textContent="ok")
        with lock:
            spans[url][1] = time.time()

    monkeypatch.setattr("app.extractor._run_task", fake)
    return spans


def _mk(url: str) -> TaskDetail:
    task = TaskDetail(taskId=utils.gen_task_id(), taskType="transcript",
                      status=TaskStatus.PENDING,
                      platform=utils.detect_platform(url), webpageUrl=url)
    store.create(task)
    return task


def _wait(tasks, timeout=8.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if all(t.status in (TaskStatus.SUCCESS, TaskStatus.FAILURE) for t in tasks):
            return
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束: {[t.status for t in tasks]}")


def _overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def test_serial_within_platform(fake_run):
    urls = [f"https://www.douyin.com/video/{i}" for i in range(3)]
    tasks = [_mk(u) for u in urls]
    for t, u in zip(tasks, urls):
        assert scheduler.submit(t, u, False, "tiny")
    _wait(tasks)

    spans = [fake_run[u] for u in urls]
    assert all(s[1] for s in spans)
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            assert not _overlap(spans[i], spans[j]), "同一平台不应并发"
    assert all(t.status == TaskStatus.SUCCESS for t in tasks)


def test_parallel_across_platforms(fake_run):
    d = "https://www.douyin.com/video/77"
    k = "https://www.kuaishou.com/short-video/77"
    td, tk = _mk(d), _mk(k)
    assert scheduler.submit(td, d, False, "tiny")
    assert scheduler.submit(tk, k, False, "tiny")
    _wait([td, tk])
    assert _overlap(fake_run[d], fake_run[k]), "不同平台应能并行"


def test_queue_capacity(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_QUEUE_MAX", "2")
    urls = [f"https://www.tiktok.com/@a/video/{i}" for i in range(3)]
    assert scheduler.can_accept_many(urls) is False
    assert scheduler.can_accept_many(urls[:2]) is True


def test_budget_gate_blocks_without_running(monkeypatch, fake_run):
    monkeypatch.setenv("TRANSCRIPT_TIKTOK_DAILY_BUDGET", "1")
    risk.reset("tiktok")
    u1 = "https://www.tiktok.com/@a/video/1"
    u2 = "https://www.tiktok.com/@a/video/2"
    t1, t2 = _mk(u1), _mk(u2)
    assert scheduler.submit(t1, u1, False, "tiny")
    assert scheduler.submit(t2, u2, False, "tiny")
    _wait([t1, t2])

    assert t1.status == TaskStatus.SUCCESS
    assert t2.status == TaskStatus.FAILURE
    assert "额度" in (t2.errorMessage or "")
    assert u2 not in fake_run, "预算不足时不应真的执行抓取"
    risk.reset("tiktok")


def test_cooldown_gate_blocks_without_running(fake_run):
    utils.mark_platform_cooldown("xiaohongshu", 600, kind="risk", reason="HTTP 403")
    u = "https://www.xiaohongshu.com/explore/abc"
    t = _mk(u)
    assert scheduler.submit(t, u, False, "tiny")
    _wait([t])
    assert t.status == TaskStatus.FAILURE
    assert "冷却" in (t.errorMessage or "")
    assert u not in fake_run
    utils.clear_platform_cooldown("xiaohongshu")


def test_success_clears_breaker(fake_run):
    utils.mark_platform_cooldown("bilibili", 600, kind="risk", reason="HTTP 403")
    utils.clear_platform_cooldown("bilibili")   # 模拟冷却到期
    u = "https://www.bilibili.com/video/BV1xx411c7mD"
    t = _mk(u)
    assert scheduler.submit(t, u, False, "tiny")
    _wait([t])
    assert t.status == TaskStatus.SUCCESS
    assert utils.platform_cooldown_info("bilibili")["remaining"] == 0


def test_queue_stats_shape():
    stats = scheduler.queue_stats()
    assert isinstance(stats, dict)
    for key, row in stats.items():
        assert set(row) == {"pending", "running"}
