# -*- coding: utf-8 -*-
"""抖音预览选档（h264 优先）+ 截图抽帧串行化：2026-11-04 修的两个回归点。

背景：
- 预览卡死：抖音最高码率档常是 HEVC，Chromium 系浏览器对 H.265 只能硬解，
  没硬解时表现为"进度条在走、画面不动"，所以选档就要把 h264 顶到最前。
- 截图不出：前端一次并发发 5 个 /media/frame，原先每个请求都各自清空重抽，
  互相踩踏（实测 1 个 62 秒成功、4 个 200 秒超时、磁盘最后一张不剩）。
"""
import threading
import time

import pytest

from app import api as api_mod
from app import extractor as ex
from app import frame_ocr as fo


def test_prefer_h264_moves_h264_first(monkeypatch):
    urls = ["https://v/hevc1080.mp4", "https://v/h264_720.mp4", "https://v/h264_480.mp4"]
    monkeypatch.setattr(ex, "_CODEC_MEMO", {})
    monkeypatch.setattr(ex, "_probe_codec", lambda u, *a, **k: "hevc" if "hevc" in u else "h264")
    got = ex._prefer_h264(urls)
    assert got[0] == "https://v/h264_720.mp4"
    assert set(got) == set(urls) and len(got) == 3
    assert got[1] == "https://v/h264_480.mp4"


def test_prefer_h264_keeps_order_when_unknown(monkeypatch):
    urls = ["https://v/a.mp4", "https://v/b.mp4"]
    monkeypatch.setattr(ex, "_probe_codec", lambda u, *a, **k: "")
    assert ex._prefer_h264(urls) == urls


def test_prefer_h264_single_url_untouched(monkeypatch):
    monkeypatch.setattr(ex, "_probe_codec", lambda u, *a, **k: pytest.fail("不该探测单条直链"))
    assert ex._prefer_h264(["https://v/only.mp4"]) == ["https://v/only.mp4"]


def test_probe_codec_maps_and_memoizes(monkeypatch):
    calls = []

    class _R:
        def __init__(self, out):
            self.stdout = out

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R("hevc\n")

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ex, "_CODEC_MEMO", {})
    assert ex._probe_codec("https://v/x.mp4") == "hevc"
    assert ex._probe_codec("https://v/x.mp4") == "hevc"      # 第二次走缓存
    assert len(calls) == 1
    assert "-rw_timeout" in calls[0]


def test_frame_cmd_keyframes_only_flag():
    cmd = fo._frame_cmd("https://v/a.mp4", "https://www.douyin.com/", 3.0, 5,
                        "F:/tmp/thumb_%03d.jpg", keyframes_only=True)
    assert cmd[cmd.index("-skip_frame") + 1] == "nokey"
    plain = fo._frame_cmd("F:/tmp/a.mp4", "", 3.0, 5, "F:/tmp/t_%03d.jpg")
    assert "-skip_frame" not in plain
    assert "-headers" not in plain          # 本地文件加 -headers 会直接报错


def test_media_frame_serializes_concurrent_requests(monkeypatch, tmp_path):
    """5 个并发请求只允许抽一次帧（原来 5 个各抽一遍、互相删图）。"""
    monkeypatch.setenv("TRANSCRIPT_TASKS_DIR", str(tmp_path))
    monkeypatch.setattr(api_mod, "_media_all_urls", lambda tid: (["https://v/a.mp4"], "抖音"))
    monkeypatch.setattr(api_mod, "_task_duration", lambda tid: 60.0)
    monkeypatch.setattr(api_mod, "_PLATFORM_REFERER", {})
    calls = []

    def fake_grab(source, duration=None, referer="", n=5, out_dir=None, **kw):
        calls.append(source)
        out = out_dir
        out.mkdir(parents=True, exist_ok=True)
        time.sleep(0.4)                     # 模拟慢抽帧，制造并发窗口
        paths = []
        for k in range(n):
            p = out / ("thumb_%03d.jpg" % (k + 1))
            p.write_bytes(b"\xff\xd8\xff\xd9")
            paths.append(p)
        return paths

    monkeypatch.setattr(fo, "grab_thumbnails", fake_grab)
    monkeypatch.setattr(api_mod, "_THUMB_LOCKS", {})
    monkeypatch.setattr(api_mod, "_THUMB_FAIL", {})

    results = []

    def hit(i):
        results.append(api_mod.media_frame(taskId="t1", i=i, n=5))

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(calls) == 1, "5 个并发请求应只触发一次抽帧，实际 %d 次" % len(calls)
    assert len(results) == 5
    thumbs = sorted((tmp_path / "t1" / "_thumbs").glob("thumb_*.jpg"))
    assert len(thumbs) == 5


def test_media_frame_failure_is_remembered(monkeypatch, tmp_path):
    """抽帧失败要记 60 秒，别让 5 个请求各跑一遍 180 秒超时。"""
    monkeypatch.setenv("TRANSCRIPT_TASKS_DIR", str(tmp_path))
    monkeypatch.setattr(api_mod, "_media_all_urls", lambda tid: (["https://v/a.mp4"], "抖音"))
    monkeypatch.setattr(api_mod, "_task_duration", lambda tid: 60.0)
    monkeypatch.setattr(api_mod, "_PLATFORM_REFERER", {})
    monkeypatch.setattr(fo, "grab_thumbnails", lambda *a, **k: [])
    monkeypatch.setattr(api_mod, "_THUMB_LOCKS", {})
    monkeypatch.setattr(api_mod, "_THUMB_FAIL", {})

    first = api_mod.media_frame(taskId="t2", i=0, n=5)
    second = api_mod.media_frame(taskId="t2", i=1, n=5)
    assert first.status_code == 502 and second.status_code == 502
    body = second.body.decode("utf-8")
    assert "60 秒内不再重试" in body
