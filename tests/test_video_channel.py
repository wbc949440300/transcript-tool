"""视频号元数据（get_feed_info）+ 监听目录强化：真标题、重复去重、转写进度。

背景：视频号只能靠抓包/下载器拿视频流，网页端拿不到；但元数据有公开预览接口可用。
文件落到监听目录后，用记住的元数据把"文件名当标题"换掉；同一条视频重复下载不再重复转写。
"""
import json

import pytest

from app import video_channel, watch_folder

VC_LINK = "https://weixin.qq.com/sph/AbC123xyz"


# ---------- 元数据：链接解析 / 响应容错 / 记忆 ----------

def test_short_uri_from_links():
    assert video_channel.short_uri_from(VC_LINK) == "AbC123xyz"
    assert video_channel.short_uri_from("https://channels.weixin.qq.com/x?shortUri=Zz987654") == "Zz987654"
    assert video_channel.short_uri_from("https://www.douyin.com/video/1") == ""


def test_parse_feed_info_tolerant_to_field_names():
    """接口字段名换过几版，解析要容错（找不到就安静放弃）。"""
    payload = {"data": {"object": {"desc": "顶级智斗", "nickname": "某号",
                                   "coverUrl": "https://x/c.jpg", "likeCount": "1.2万"}}}
    meta = video_channel.parse_feed_info(payload)
    assert meta["title"] == "顶级智斗" and meta["author"] == "某号"
    assert meta["cover"].endswith("c.jpg") and meta["likes"] == "1.2万"
    assert video_channel.parse_feed_info({}) == {}
    assert video_channel.parse_feed_info({"a": 1}) == {}


def test_remember_and_match(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    video_channel.remember({"title": "顶级智斗：如何用一把枪打赢一场仗", "author": "某号",
                            "cover": "https://x/c.jpg", "short_uri": "AbC123xyz"})
    # 下载器一般按标题命名文件
    hit = video_channel.match("顶级智斗：如何用一把枪打赢一场仗.mp4")
    assert hit and hit["author"] == "某号" and hit["cover"] == "https://x/c.jpg"
    # 带前后缀（比如下载器加了 id）也能命中
    assert video_channel.match("顶级智斗：如何用一把枪打赢一场仗_20260101.mp4")
    # 不相干的文件名不该误匹配
    assert video_channel.match("完全无关的另一个视频.mp4") is None
    assert video_channel.match("短.mp4") is None


def test_remember_dedupes_by_title(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    video_channel.remember({"title": "同一个标题", "author": "A"})
    video_channel.remember({"title": "同一个标题", "author": "B"})
    items = json.loads((tmp_path / "vc.json").read_text(encoding="utf-8"))["items"]
    assert len(items) == 1 and items[0]["author"] == "B"


# ---------- 监听目录：元数据替换文件名 + 重复下载去重 ----------

def _video(tmp_path, name="片子.mp4", body=b"x" * 2048):
    p = tmp_path / name
    p.write_bytes(body)
    return p


def test_enqueue_uses_remembered_title(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.setenv("TRANSCRIPT_WATCH_DEDUPE_FILE", str(tmp_path / "dd.json"))
    monkeypatch.chdir(tmp_path)
    video_channel.remember({"title": "顶上那场仗", "author": "某号", "cover": "https://x/c.jpg"})

    enqueued = {}
    monkeypatch.setattr(watch_folder, "start_file_task",
                        lambda task, dst, model, src_name, ocr_frames=False, **kw:
                        enqueued.update(task=task, src_name=src_name))
    monkeypatch.setattr(watch_folder, "media_fingerprint", lambda p: "123:45.6")

    src = _video(tmp_path, "顶上那场仗.mp4")
    watch_folder._enqueue(src, "small", move_done=False)

    assert enqueued["src_name"] == "顶上那场仗"          # 标题不再是文件名带扩展名
    assert enqueued["task"].uploader == "某号"
    assert enqueued["task"].cover == "https://x/c.jpg"
    assert src.exists()                                  # move_done=False 不搬动


def test_enqueue_skips_duplicate_content(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    dedupe = tmp_path / "dd.json"
    monkeypatch.setenv("TRANSCRIPT_WATCH_DEDUPE_FILE", str(dedupe))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(watch_folder, "media_fingerprint", lambda p: "999:12.3")

    calls = []
    monkeypatch.setattr(watch_folder, "start_file_task",
                        lambda *a, **k: calls.append(a))

    # 第一次：正常入列并记下指纹
    first = _video(tmp_path, "第一次.mp4")
    watch_folder._enqueue(first, "small", move_done=False)
    assert len(calls) == 1
    assert json.loads(dedupe.read_text(encoding="utf-8"))["999:12.3"]

    # 第二次：同内容（同指纹）→ 跳过转写
    second = _video(tmp_path, "又下载了一次.mp4")
    watch_folder._enqueue(second, "small", move_done=False)
    assert len(calls) == 1, "重复文件不该再起转写任务"
    assert watch_folder.status()["skipped_duplicate"] >= 1


def test_dedupe_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.setenv("TRANSCRIPT_WATCH_DEDUPE", "0")
    monkeypatch.setenv("TRANSCRIPT_WATCH_DEDUPE_FILE", str(tmp_path / "dd.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(watch_folder, "media_fingerprint", lambda p: "777:7.7")
    calls = []
    monkeypatch.setattr(watch_folder, "start_file_task", lambda *a, **k: calls.append(a))
    watch_folder._enqueue(_video(tmp_path, "a.mp4"), "small", move_done=False)
    watch_folder._enqueue(_video(tmp_path, "b.mp4"), "small", move_done=False)
    assert len(calls) == 2, "关掉去重后同一内容也要照常转写"


# ---------- 转写进度提示 ----------

def test_progress_reporter_message():
    from app.extractor import _fmt_mmss, _progress_reporter
    from app.models import TaskDetail, TaskStatus
    from app.task_store import store

    task = TaskDetail(taskId="prog_1", taskType="transcript", status=TaskStatus.RUNNING)
    store.create(task)
    _progress_reporter(task)(320.0, 1780.0, 300.0)       # 已到 5:20 / 29:40，还要 5 分钟
    msg = store.get("prog_1").errorMessage
    assert "5:20" in msg and "29:40" in msg and "约 6 分钟" in msg

    _progress_reporter(task)(320.0, 1780.0, 30.0)
    assert "不到 1 分钟" in store.get("prog_1").errorMessage
    assert _fmt_mmss(65) == "1:05"