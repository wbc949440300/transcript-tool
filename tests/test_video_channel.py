"""视频号元数据（get_feed_info）+ 监听目录强化：真标题、重复去重、转写进度。

背景：视频号只能靠抓包/下载器拿视频流，网页端拿不到；但元数据有公开预览接口可用。
文件落到监听目录后，用记住的元数据把"文件名当标题"换掉；同一条视频重复下载不再重复转写。
"""
import json
from pathlib import Path

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


# ---------- 「链接模式」：贴分享链接直接出文案卡片（2026-11-04） ----------

def test_parse_feed_info_link_mode():
    """真接口响应：拿到正文/作者/头像/互动数据/话题标签；**拿不到视频流字段**。"""
    payload = {
        "data": {
            "feedInfo": {
                "description": "某条关于 AI 的短视频。#DeepSeek #AI圈瓜",
                "coverUrl": "https://finder.video.qq.com/251/20304/stodownload?encfilekey=x",
                "likeCountFmt": "152", "favCountFmt": "1080",
                "forwardCountFmt": "909", "commentCountFmt": "148",
                "createtime": 1793600410, "picInfo": [],
            },
            "authorInfo": {"nickname": "AI.Easy", "headImgUrl": "https://wx.qlogo.cn/head/0"},
            "sceneInfo": {"dynamicExportId": "export/UzFfBg", "expiredTime": 1793771671},
        }
    }
    meta = video_channel.parse_feed_info(payload)
    assert meta["title"].startswith("某条关于 AI 的短视频")
    assert meta["author"] == "AI.Easy"
    assert meta["description"] == "某条关于 AI 的短视频。#DeepSeek #AI圈瓜"
    assert meta["cover"].startswith("https://finder.video.qq.com")
    assert meta["headImg"].startswith("https://wx.qlogo.cn")
    assert (meta["likes"], meta["favCount"], meta["forwardCount"], meta["commentCount"]) == \
           ("152", "1080", "909", "148")
    assert meta["publishAt"] == "2026-11-02 14:20"
    assert meta["dynamicExportId"] == "export/UzFfBg"
    assert meta["isVideo"] is True
    assert meta["tags"] == ["DeepSeek", "AI圈瓜"]
    # 视频流字段一律取不到（这是"链接模式"只能给文案的原因）
    assert "videoUrl" not in meta and "h264VideoInfo" not in meta


def test_parse_feed_info_picture_post_is_not_video():
    payload = {"data": {"feedInfo": {"description": "图集文", "picInfo": [{"url": "x"}]},
                        "authorInfo": {"nickname": "A"}}}
    assert video_channel.parse_feed_info(payload)["isVideo"] is False


# ---------- 「统一入口」：贴分享链接 → 建任务（单条 API 与批量共用，2026-11-05） ----------

_META = {
    "title": "某条关于 AI 的短视频", "author": "AI.Easy",
    "cover": "https://finder.video.qq.com/251/20304/stodownload?encfilekey=x",
    "description": "某条关于 AI 的短视频。#DeepSeek",
    "likes": "152", "favCount": "1080", "forwardCount": "909",
    "commentCount": "148", "publishAt": "2026-11-02 14:20",
    "location": "天津市", "headImg": "https://wx.qlogo.cn/head/0",
    "tags": ["DeepSeek"], "short_uri": "ANc1wgtZqr",
    "dynamicExportId": "export/UzFfBg", "isVideo": True,
}


def test_process_channel_link_without_yuanbao_creates_card(monkeypatch, tmp_path):
    """没配元宝：贴链接 → 直接建成 SUCCESS 任务（taskType=videochannel），不启动任何下载。"""
    from app import task_store, video_channel
    from app.models import TaskStatus

    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "")
    monkeypatch.setattr(video_channel, "fetch_feed_info", lambda uri: dict(_META, short_uri=uri))
    calls: list = []
    monkeypatch.setattr(task_store.store, "create", lambda task: calls.append(("create", task)))
    monkeypatch.setattr(task_store.store, "update_status",
                        lambda task_id, status, **fields: calls.append(
                            ("update", task_id, status, fields)))

    task_id = video_channel.process_channel_link(VC_LINK, "tiny")

    assert task_id
    task = calls[0][1]
    assert task.taskType == "videochannel" and task.platform == "视频号"
    assert task.textContent == "某条关于 AI 的短视频。#DeepSeek"
    assert task.channelMeta["shortUri"] == "AbC123xyz"   # 与贴入链接的 shortUri 一致
    assert task.channelMeta["tags"] == ["DeepSeek"] and task.channelMeta["isVideo"] is True
    assert calls[-1][2] == TaskStatus.SUCCESS          # 秒成功，不排队
    assert calls[-1][3]["textContent"] == task.textContent


def test_process_channel_link_with_yuanbao_downloads(monkeypatch, tmp_path):
    """配了元宝：贴链接 → 下载视频 → 走 file_transcribe 流水线（平台仍记视频号）。"""
    import app.file_transcribe as ft
    from app import task_store, video_channel

    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.chdir(tmp_path)                        # tmp_uploads 落在临时目录
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "ck")
    monkeypatch.setattr(video_channel, "fetch_feed_info", lambda uri: dict(_META, short_uri=uri))
    monkeypatch.setattr(video_channel, "resolve_video",
                        lambda raw, **kw: {"videoUrl": "https://x/v.mp4", "title": "元宝下的片子",
                                     "cover": "https://x/c.jpg"})

    def fake_download(url, dest, timeout=180.0):
        Path(dest).write_bytes(b"mp4-bytes")
        return Path(dest)

    monkeypatch.setattr(video_channel, "download_media", fake_download)
    started: dict = {}
    monkeypatch.setattr(ft, "start_file_task",
                        lambda task, tmp, model, src, **kw: started.update(
                            taskId=task.taskId, model=model, src=src,
                            platform=kw.get("platform")))

    task_id = video_channel.process_channel_link(VC_LINK, "small")

    assert task_id == started["taskId"]
    task = task_store.store.get(task_id)
    assert task.taskType == "file_transcript" and task.platform == "视频号"
    assert task.title == "元宝下的片子.mp4"
    assert started["platform"] == "视频号" and started["model"] == "small"
    assert started["src"] == "元宝下的片子.mp4"


def _quiet_history(monkeypatch):
    """SUCCESS/FAILURE 会自动落历史 + 归档：测试里拦掉，别写真实库与 文案输出/。"""
    import app.archive as archive_mod
    from app import task_store

    monkeypatch.setattr(task_store.history_store, "save", lambda t: None)
    monkeypatch.setattr(task_store.history_store, "trim", lambda *a, **k: None)
    monkeypatch.setattr(archive_mod, "save_text_file", lambda t: None)


def test_channel_link_retries_then_succeeds(monkeypatch, tmp_path):
    """元宝接口抖动（实测当天两次失败、第三次成功）：内部自动重试，用户无感。"""
    import app.file_transcribe as ft
    from app import task_store, video_channel

    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_channel.time, "sleep", lambda s: None)   # 测试不真的等
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "ck")
    monkeypatch.setattr(video_channel, "fetch_feed_info", lambda uri: dict(_META, short_uri=uri))
    calls = {"n": 0}

    def flaky(raw, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Remote end closed connection without response")
        return {"videoUrl": "https://x/v.mp4", "title": "第三次成功", "cover": "https://x/c.jpg"}

    monkeypatch.setattr(video_channel, "resolve_video", flaky)
    monkeypatch.setattr(video_channel, "download_media",
                        lambda url, dest, timeout=180.0: Path(dest).write_bytes(b"mp4"))
    started: dict = {}
    monkeypatch.setattr(ft, "start_file_task",
                        lambda task, tmp, model, src, **kw: started.update(taskId=task.taskId))

    task_id = video_channel.process_channel_link(VC_LINK, "small")

    assert calls["n"] == 3, "前两次失败应当自动重试"
    assert task_id == started["taskId"]


def test_channel_link_bound_but_resolve_keeps_failing_marks_failure(monkeypatch, tmp_path):
    """绑了凭证仍拿不到视频 → 任务 FAILURE 并提示「再点一次提取」，不再假成功（2026-11-05）。"""
    from app import task_store, video_channel
    from app.models import TaskStatus

    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.setattr(video_channel.time, "sleep", lambda s: None)
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "ck")
    monkeypatch.setattr(video_channel, "fetch_feed_info", lambda uri: dict(_META, short_uri=uri))
    monkeypatch.setattr(video_channel, "resolve_video",
                        lambda raw, **kw: (_ for _ in ()).throw(RuntimeError("SSL 握手超时")))
    _quiet_history(monkeypatch)

    task_id = video_channel.process_channel_link(VC_LINK, "tiny")
    task = task_store.store.get(task_id)

    assert task.status == TaskStatus.FAILURE
    assert task.taskType == "videochannel"
    assert "视频没拿到" in task.errorMessage and "再点一次" in task.errorMessage
    assert "SSL 握手超时" in task.errorMessage       # 真实原因要带上，便于排查
    # 失败归失败，已取到的文案不能丢（历史里仍可查到正文）
    assert task.textContent == "某条关于 AI 的短视频。#DeepSeek"
    assert task.uploader == "AI.Easy"


def test_channel_link_no_stream_keeps_card_with_bound_flag(monkeypatch, tmp_path):
    """作品本身没有视频流（图集/已删）：重试无意义，保留元数据卡片并标记已绑定。"""
    from app import task_store, video_channel
    from app.models import TaskStatus

    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "ck")
    monkeypatch.setattr(video_channel, "fetch_feed_info", lambda uri: dict(_META, short_uri=uri))
    n = {"calls": 0}

    def no_stream(raw, **kw):
        n["calls"] += 1
        raise RuntimeError("视频号没给视频直链（内容可能已删除或只有图片）")

    monkeypatch.setattr(video_channel, "resolve_video", no_stream)
    _quiet_history(monkeypatch)

    task_id = video_channel.process_channel_link(VC_LINK, "tiny")
    task = task_store.store.get(task_id)

    assert n["calls"] == 1, "决定性失败不该重试"
    assert task.status == TaskStatus.SUCCESS and task.taskType == "videochannel"
    assert task.channelMeta["bound"] is True


def test_channel_link_download_failure_marks_failure(monkeypatch, tmp_path):
    """直链拿到了但视频下载失败（CDN 偶发 400）：补试一次仍失败 → FAILURE，不降级。"""
    from app import task_store, video_channel
    from app.models import TaskStatus

    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_channel.time, "sleep", lambda s: None)
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "ck")
    monkeypatch.setattr(video_channel, "fetch_feed_info", lambda uri: dict(_META, short_uri=uri))
    monkeypatch.setattr(video_channel, "resolve_video",
                        lambda raw, **kw: {"videoUrl": "https://x/v.mp4", "title": "下载会失败"})
    calls = {"n": 0}

    def bad_download(url, dest, timeout=180.0):
        calls["n"] += 1
        raise RuntimeError("HTTP Error 400: Bad Request")

    monkeypatch.setattr(video_channel, "download_media", bad_download)
    _quiet_history(monkeypatch)

    task_id = video_channel.process_channel_link(VC_LINK, "tiny")
    task = task_store.store.get(task_id)

    assert calls["n"] == 2, "同一直链应当补试一次"
    assert task.status == TaskStatus.FAILURE
    assert "视频下载失败" in task.errorMessage and "再点一次" in task.errorMessage


def test_process_channel_link_unresolvable(monkeypatch):
    """元数据拿不到、也没配元宝 → 返回 None（调用方给出 CHANNEL_UNRESOLVABLE 提示）。"""
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "")
    monkeypatch.setattr(video_channel, "fetch_feed_info", lambda uri: {})
    assert video_channel.process_channel_link(VC_LINK, "tiny") is None
    assert "元数据" in video_channel.CHANNEL_UNRESOLVABLE


def test_batch_accepts_channel_link(monkeypatch, tmp_path):
    """批量入口：视频号链接不再 FAILURE，复用占位任务建成链接模式卡片。"""
    import app.archive as archive_mod
    from app import batch as batch_mod, task_store
    from app.models import TaskStatus

    monkeypatch.setenv("TRANSCRIPT_VC_META", str(tmp_path / "vc.json"))
    monkeypatch.setattr(video_channel, "yuanbao_cookie", lambda: "")
    monkeypatch.setattr(video_channel, "fetch_feed_info",
                        lambda uri: {"title": "批量里的视频号", "author": "某号",
                                     "description": "批量正文内容"})
    # SUCCESS 会自动落历史 + 归档文案：测试里拦掉，别写真实库与 文案输出/
    monkeypatch.setattr(task_store.history_store, "save", lambda t: None)
    monkeypatch.setattr(task_store.history_store, "trim", lambda *a, **k: None)
    monkeypatch.setattr(archive_mod, "save_text_file", lambda t: None)

    batch_mod.start_batch("batch_vc", [VC_LINK], "tiny")
    for th in list(batch_mod._BATCH_THREADS):
        th.join(timeout=10)

    items = task_store.store.get_batch("batch_vc")
    assert items and items[0].status == TaskStatus.SUCCESS
    assert items[0].taskType == "videochannel" and items[0].platform == "视频号"
    assert items[0].batchId == "batch_vc" and items[0].index == 0
    assert items[0].textContent == "批量正文内容"


def test_fail_no_video_marks_platform(monkeypatch):
    """绑了凭证、自动重试后仍无视频 → FAILURE，且平台必须标「视频号」。

    回归：批量占位任务初始 platform 是 detect_platform 给的「微信」，失败时
    不带 platform 字段的话，失败列表的平台列会显示成「微信」（2026-11-05 修）。
    """
    from app import task_store, video_channel
    from app.models import TaskStatus

    calls = []
    monkeypatch.setattr(task_store.store, "create", lambda t: calls.append(("create", t)))
    monkeypatch.setattr(task_store.store, "update_status",
                        lambda tid, st, **f: calls.append(("update", tid, st, f)))

    tid = video_channel._fail_no_video(
        "https://weixin.qq.com/sph/ZZZ999", "没给视频直链（图集或已删除）", {}, task=None)

    assert tid
    kind, _tid, status, fields = calls[-1]
    assert kind == "update" and status == TaskStatus.FAILURE
    assert fields["platform"] == "视频号"
    assert "视频" in fields["errorMessage"]
