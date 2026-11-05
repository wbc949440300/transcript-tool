# -*- coding: utf-8 -*-
"""本地文件任务的「视频截图」修复：2026-11-04。

两个坑（都是同一类：ffmpeg 拿到了不该拿的参数）：
1. videoUrlList 里的 file://video.mp4 被直接剥前缀当相对路径传给 ffmpeg，
   而进程工作目录是项目根 → 永远找不到文件，5 张全 502。
2. MJPEG 素材（视频号/微信下来的片子常见）是 full-range YUV，ffmpeg 的
   **mjpeg 编码器**默认拒绝（"Non full-range YUV is non-standard"），
   必须给输出侧加 -strict unofficial；本地文件也别加 -skip_frame nokey
   （实测 MJPEG 一张都出不来），直接 5 路并发 seek 最快。
"""
from pathlib import Path

import pytest

from app import api as api_mod
from app import frame_ocr as fo


def test_frame_cmd_puts_strict_on_output_side():
    cmd = fo._frame_cmd("C:/x/video.mp4", "", 2.0, 3, "C:/x/t_%03d.jpg", scale_w=960)
    assert "-strict" in cmd and "unofficial" in cmd
    # 报错的是写 jpg 的编码器 → 必须出现在 -i 之后（输出侧）
    assert cmd.index("-strict") > cmd.index("-i")
    assert cmd[cmd.index("-strict") + 1] == "unofficial"
    # 本地源不能带 -headers（ffmpeg 直接报 Option headers not found）
    assert "-headers" not in cmd


def test_grab_thumbnails_local_file_seeks(monkeypatch, tmp_path):
    out = tmp_path / "thumbs"
    out.mkdir()
    seen = {}

    def fake_seek(source, referer, duration, n, out_dir, width=960):
        seen.update(dict(source=source, duration=duration, n=n))
        p = out_dir / "thumb_001.jpg"
        p.write_bytes(b"jpg")
        return [p]

    monkeypatch.setattr(fo, "_probe_duration", lambda *a, **k: 12.5)
    monkeypatch.setattr(fo, "_seek_thumbnails", fake_seek)
    frames = fo.grab_thumbnails("C:/x/video.mp4", 0, "", n=5, out_dir=out)
    assert [f.name for f in frames] == ["thumb_001.jpg"]
    assert seen["duration"] == 12.5 and seen["n"] == 5


def test_grab_thumbnails_probes_duration_when_missing(monkeypatch, tmp_path):
    """本地文件任务的历史记录里 duration 是空的 → 必须 ffprobe 补，否则按 2 秒瞎抽。"""
    out = tmp_path / "thumbs"
    out.mkdir()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class R:
            stdout = "42.0\n"
            stderr = ""

        return R()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(fo, "_seek_thumbnails", lambda *a, **k: [])
    fo.grab_thumbnails("C:/x/video.mp4", 0, "", n=5, out_dir=out)
    probe = [c for c in calls if c and c[0] == "ffprobe"]
    assert probe and "-show_entries" in probe[0]


def test_media_frame_resolves_file_url_to_task_dir(monkeypatch, tmp_path):
    tdir = tmp_path / "T1"
    tdir.mkdir()
    (tdir / "video.mp4").write_bytes(b"\x00")
    monkeypatch.setenv("TRANSCRIPT_TASKS_DIR", str(tmp_path))
    monkeypatch.setattr(api_mod, "_media_all_urls",
                        lambda tid: (["file://video.mp4"], "本地文件"))
    monkeypatch.setattr(api_mod, "_task_duration", lambda tid: 0)
    seen = {}

    def fake_grab(src, duration=None, referer="", n=5, out_dir=None):
        seen["src"] = src
        out_dir.mkdir(parents=True, exist_ok=True)
        p = Path(out_dir) / "thumb_001.jpg"
        p.write_bytes(b"jpg")
        return [p]

    monkeypatch.setattr(fo, "grab_thumbnails", fake_grab)
    api_mod._THUMB_FAIL.pop("T1", None)
    api_mod.media_frame(taskId="T1", i=0, n=5)
    assert seen["src"] == str(tdir / "video.mp4")
