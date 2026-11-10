"""画面文字 OCR 的接线：三条收尾路径都要跑；默认一律关（2026-11-03 按需求改）。

背景：旧实现把 OCR 那段代码只放在"通用通道"后面，而抖音/快手是提前 return 的
专用通道 → 用户在抖音/快手勾了"识别画面文字"也**不会执行**（2026-10-31 修）。
用户实测：画面截图文字"肥水不流外人田"，ASR 听成"非说也没有留到外人天地里去"。
"""
import pytest
from fastapi.testclient import TestClient

import app.extractor as ex
from app.models import TaskDetail, TaskStatus
from app.platforms import frame_ocr_default_for
from app.task_store import store

TOKEN = "test-token"
DY = "https://www.douyin.com/video/7612933474718141747"
KS = "https://www.kuaishou.com/short-video/3x5gu3rc5sqd9ni"
BL = "https://www.bilibili.com/video/BV1xx411c7mD"


def _mk(tid: str) -> TaskDetail:
    task = TaskDetail(taskId=tid, taskType="transcript", status=TaskStatus.PENDING)
    store.create(task)
    return task


@pytest.fixture
def spy_frame_ocr(monkeypatch):
    calls = []
    monkeypatch.setattr(ex, "_run_frame_ocr",
                        lambda task, url, work_dir: calls.append(url))
    return calls


@pytest.mark.parametrize("url,kind", [(DY, "douyin"), (KS, "kuaishou")])
def test_frame_ocr_runs_on_platform_channels(monkeypatch, spy_frame_ocr, url, kind, tmp_path):
    monkeypatch.setenv("TRANSCRIPT_TASKS_DIR", str(tmp_path))
    if kind == "douyin":
        monkeypatch.setattr(ex, "_run_douyin_task",
                            lambda t, u, a, m, w: setattr(t, "textContent", "口播"))
    else:
        monkeypatch.setattr(ex, "_run_kuaishou_task",
                            lambda t, u, a, m, w: setattr(t, "textContent", "口播"))
    task = _mk(f"frame_{kind}_1")
    ex._run_task(task, url, False, "tiny", ocr_frames=True)
    assert spy_frame_ocr == [url], "专用通道也必须执行画面文字 OCR"
    assert task.status == TaskStatus.SUCCESS


def test_frame_ocr_skipped_when_disabled(monkeypatch, spy_frame_ocr, tmp_path):
    monkeypatch.setenv("TRANSCRIPT_TASKS_DIR", str(tmp_path))
    monkeypatch.setattr(ex, "_run_douyin_task",
                        lambda t, u, a, m, w: setattr(t, "textContent", "口播"))
    task = _mk("frame_dy_off")
    ex._run_task(task, DY, False, "tiny", ocr_frames=False)
    assert spy_frame_ocr == []


def test_run_frame_ocr_sets_frame_text(monkeypatch, tmp_path):
    import app.frame_ocr as fo

    monkeypatch.setattr(fo, "extract_frame_text",
                        lambda src, duration=None, referer="": {
                            "blocks": [{"t": 0.0, "text": "肥水不流外人田"}],
                            "text": "肥水不流外人田", "dropped": 0})
    work = tmp_path / "t1"
    work.mkdir()
    (work / "video.mp4").write_bytes(b"x")
    task = _mk("frame_text_1")
    ex._run_frame_ocr(task, DY, work)
    assert task.frameText == "肥水不流外人田"


def test_run_frame_ocr_without_source_is_silent(tmp_path):
    work = tmp_path / "t2"
    work.mkdir()
    task = _mk("frame_no_src")
    ex._run_frame_ocr(task, DY, work)      # 不应抛异常
    assert not getattr(task, "frameText", None)


@pytest.mark.parametrize("url,want", [
    (DY, False), (KS, False),
    ("https://www.xiaohongshu.com/explore/abc", False),
    (BL, False),
    ("https://example.com/v/1", False),
])
def test_default_frame_ocr_by_platform(url, want):
    """★ 2026-11-03 按需求改为**所有平台默认不勾选**。

    抽帧 + OCR 会让每条任务多花几十秒到几分钟，改为"需要时手动勾选"
    （前端「同时识别画面文字」默认不勾）。平台维度仍保留在
    adapter.frame_ocr_default 上，将来要按平台恢复默认开，只改 app/platforms.py 那一处。
    """
    assert frame_ocr_default_for(url) is want


def test_frame_cmd_omits_headers_for_local_files():
    """本地文件带 -headers 会让 ffmpeg 直接报错，必须是 http 源才加。"""
    from app.frame_ocr import _frame_cmd

    local = _frame_cmd(r"tasks\x\video.mp4", "https://www.douyin.com/", 5.0, 48, "f_%03d.jpg")
    assert "-headers" not in local
    remote = _frame_cmd("https://v.douyinvod.com/a.mp4", "https://www.douyin.com/", 5.0, 48,
                        "f_%03d.jpg")
    assert "-headers" in remote and any("Referer:" in t for t in remote)
    plain = _frame_cmd("https://v.douyinvod.com/a.mp4", "", 5.0, 48, "f_%03d.jpg")
    assert "-headers" not in plain


def test_frame_cmd_downscales_when_asked():
    """抽帧缩到 720 宽：OCR 快一倍以上，字幕照样清楚。"""
    from app.frame_ocr import _frame_cmd

    cmd = _frame_cmd("a.mp4", "", 5.0, 48, "f_%03d.jpg", scale_w=720)
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=720:-2" in vf and "fps=1/5.00" in vf


def test_frame_cmd_crop_applied_after_scale():
    """字幕带裁剪：scale 之后追加 crop=iw:ih*比例:0:ih*top。"""
    from app.frame_ocr import _frame_cmd

    cmd = _frame_cmd("a.mp4", "", 5.0, 48, "f_%03d.jpg", scale_w=720, crop="0.55:0.45")
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=720:-2" in vf
    assert "crop=iw:ih*0.45:0:ih*0.55" in vf


def test_frame_cmd_crop_ignored_without_scale_or_bad():
    """没有 scale 时 crop 不追加；非法 crop 直接忽略（保持全画面）。"""
    from app.frame_ocr import _frame_cmd, _crop_vf

    assert _crop_vf("") == ""
    assert _crop_vf("bad") == ""
    assert _crop_vf("1.5:2") == ""          # 越界
    assert _crop_vf("0.5:0.4") == ",crop=iw:ih*0.40:0:ih*0.50"
    cmd = _frame_cmd("a.mp4", "", 5.0, 48, "f_%03d.jpg", scale_w=0, crop="0.55:0.45")
    assert "crop=" not in cmd


def test_probe_then_full_scan(monkeypatch):
    """两段式：先探针（8 帧、覆盖整条视频），命中才全扫（48 帧）。"""
    import app.frame_ocr as fo

    calls = []

    def fake_scan(source, referer, interval, frames_n, W, H, tmp, tag, patience=0, crop=''):
        calls.append((tag, round(interval, 2), frames_n))
        if tag == "p":
            return [], 0, frames_n, [("字幕文字", 0.5, 0.8, 0.12)]
        return [{"t": 0.0, "text": "字幕文字"}], 0, frames_n, []

    monkeypatch.setattr(fo, "_scan", fake_scan)
    monkeypatch.setattr(fo, "_video_duration", lambda p: 600.0)
    monkeypatch.setattr(fo, "_video_size", lambda p: (1080, 1920))

    res = fo.extract_frame_text("dummy.mp4", max_frames=48)
    assert [c[0] for c in calls] == ["p", "f"]
    assert calls[0][1] == 75.0     # 探针：600s / 8 帧（覆盖整条视频）
    assert calls[1][1] == 12.5     # 全扫：600s / 48 帧
    assert res["text"] == "字幕文字" and res["skipped"] is False and res["probed"] == 8


def test_probe_skips_full_scan_when_no_text(monkeypatch):
    """画面里没有正文类文字 → 只扫探针帧就结束（大量视频走这条路，省下几分钟）。"""
    import app.frame_ocr as fo

    calls = []

    def fake_scan(source, referer, interval, frames_n, W, H, tmp, tag, patience=0, crop=''):
        calls.append(tag)
        return [], 0, frames_n, []

    monkeypatch.setattr(fo, "_scan", fake_scan)
    monkeypatch.setattr(fo, "_video_duration", lambda p: 120.0)
    monkeypatch.setattr(fo, "_video_size", lambda p: (720, 1280))

    res = fo.extract_frame_text("dummy.mp4", max_frames=48)
    assert calls == ["p"]
    assert res["skipped"] is True and res["text"] == ""


def test_probe_has_text_ignores_corner_watermark():
    from app.frame_ocr import _probe_has_text

    assert _probe_has_text([("水印", 0.05, 0.05, 0.03)]) is False    # 角落小字：水印特征
    assert _probe_has_text([("正文字幕", 0.5, 0.8, 0.08)]) is True   # 居中/大字：正文
    assert _probe_has_text([]) is False


def test_api_uses_platform_default_when_unspecified(monkeypatch):
    """请求里不带 ocrFrames → 按平台决定；显式 false/true 则以调用方为准。"""
    seen = []
    monkeypatch.setattr("app.api.start_async",
                        lambda task, url, want_audio, model, ocr_frames=False:
                        seen.append(ocr_frames))
    client = TestClient(__import__("app.main", fromlist=["app"]).app,
                        base_url="http://127.0.0.1:8000")
    hdr = {"X-API-Token": TOKEN}

    client.post("/prod-api/transcript/doTask", json={"workUrl": DY}, headers=hdr)
    client.post("/prod-api/transcript/doTask", json={"workUrl": BL}, headers=hdr)
    client.post("/prod-api/transcript/doTask",
                json={"workUrl": DY, "ocrFrames": False}, headers=hdr)
    # 请求不带 ocrFrames → 平台默认（抖音现在也是 False，见 2026-11-03 的默认值调整）
    assert seen == [False, False, False]