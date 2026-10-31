"""抖音：码率分流（ASR 取最小 / 预览取最大）、账号化 profile 命名、转写失败换候选。"""
import pytest

from app import extractor as ex
from app.douyin import _profile_name_for
from app.extractor import _douyin_stream_urls
from app.models import TaskDetail, TaskStatus
from app.task_store import store


def _v(url, bitrate=None):
    return {"url": url, "bitrate": bitrate}


def test_variants_low_for_asr_high_for_preview():
    d = {"video_variants": [_v("https://v/720.mp4", 720),
                            _v("https://v/1080.mp4", 1080),
                            _v("https://v/480.mp4", 480)],
         "video_url": "https://v/default.mp4"}
    asr, preview = _douyin_stream_urls(d)
    assert asr == ["https://v/480.mp4", "https://v/720.mp4", "https://v/1080.mp4"]
    assert preview == ["https://v/1080.mp4", "https://v/720.mp4", "https://v/480.mp4"]


def test_unknown_bitrate_goes_last():
    d = {"video_variants": [_v("https://v/unknown.mp4", None),
                            _v("https://v/high.mp4", 1080)]}
    asr, preview = _douyin_stream_urls(d)
    assert asr == ["https://v/high.mp4", "https://v/unknown.mp4"]
    assert preview == ["https://v/high.mp4", "https://v/unknown.mp4"]


def test_dedup_and_filter_non_http():
    d = {"video_variants": [_v("http://a", 100), _v("http://a", 100),
                            _v("file://x", 200), _v("", 300)]}
    asr, preview = _douyin_stream_urls(d)
    assert asr == ["http://a"]
    assert preview == ["http://a"]


def test_fallback_without_variants():
    assert _douyin_stream_urls({"video_url": "http://x"}) == (["http://x"], ["http://x"])
    assert _douyin_stream_urls({}) == ([], [])


def test_profile_name_for_account_pool():
    assert _profile_name_for(None) == "douyin"
    assert _profile_name_for("") == "douyin"
    assert _profile_name_for(r"cookies/douyin.txt") == "douyin"
    assert _profile_name_for(r"cookies/douyin/account_1.txt") == "douyin_account_1"
    assert _profile_name_for(r"cookies/douyin/account_2.txt") == "douyin_account_2"


def test_login_and_fetch_use_same_profile_dir():
    """登录脚本与抓取端必须指向同一个 profile 目录，否则登录态白登。"""
    import login_cookie

    from app.browser import profile_dir_for

    assert login_cookie._profile_for("douyin") == profile_dir_for("douyin")
    assert login_cookie._profile_for("douyin", "account_1") == profile_dir_for("douyin_account_1")
    # 小红书沿用共用目录（不搬家，保留既有登录态）
    assert login_cookie._profile_for("xiaohongshu") == login_cookie.PROFILE_DIR


# ---------- 转写失败换候选档（2026-10-31：实测有"ffprobe 说有音轨、解码仍失败"的档） ----------

@pytest.fixture(autouse=True)
def _reset_low_gear(monkeypatch):
    monkeypatch.setattr(ex, "_DOUYIN_LOW_GEAR_BAD", False)


def _doc_with_variants():
    return {"title": "标题", "desc": "描述", "duration": 60000,
            "video_url": "https://v/default.mp4",
            "video_variants": [{"url": "https://v/low.mp4", "bitrate": 480},
                               {"url": "https://v/high.mp4", "bitrate": 1080}]}


def _patch_common(monkeypatch, downloaded: list) -> None:
    monkeypatch.setattr(ex, "_fetch_platform", lambda fn, url, **kw: _doc_with_variants())
    monkeypatch.setattr(ex, "_has_audio_stream", lambda path: True)

    def fake_dl(url, work_dir, referer="", platform=""):
        downloaded.append(url)
        f = work_dir / f"cand_{len(downloaded)}.mp4"
        f.write_bytes(b"x" * 32)
        return f

    monkeypatch.setattr(ex, "_download_http_media", fake_dl)


def _new_task(tid: str) -> TaskDetail:
    task = TaskDetail(taskId=tid, taskType="transcript", status=TaskStatus.PENDING)
    store.create(task)
    return task


def test_transcribe_failure_switches_to_next_candidate(monkeypatch, tmp_path):
    downloaded, calls = [], []
    _patch_common(monkeypatch, downloaded)

    def fake_transcribe(path, model, language=None, vad=True, **kwargs):  # on_progress 由调用方传入
        calls.append(str(path))
        if len(calls) == 1:
            raise RuntimeError("tuple index out of range")
        return "识别到的文字", [{"start": 0.0, "end": 1.0, "text": "识别到的文字"}]

    monkeypatch.setattr(ex, "_transcribe", fake_transcribe)
    task = _new_task("dy_fallback_1")
    work_dir = tmp_path / task.taskId
    work_dir.mkdir()

    ex._run_douyin_task(task, "https://www.douyin.com/video/1", False, "small", work_dir)

    assert len(calls) == 2, "第一档转写失败后应换下一档"
    assert downloaded[0] == "https://v/low.mp4"
    assert downloaded[1] == "https://v/high.mp4"
    assert task.textContent == "识别到的文字"
    assert task.segments


def test_all_candidates_undecodable_falls_back_to_ytdlp(monkeypatch, tmp_path):
    downloaded = []
    _patch_common(monkeypatch, downloaded)

    def boom(*args, **kwargs):
        raise RuntimeError("解码失败")

    monkeypatch.setattr(ex, "_transcribe", boom)
    monkeypatch.setattr(ex, "download_media", lambda *a, **k: None)
    task = _new_task("dy_fallback_2")
    work_dir = tmp_path / task.taskId
    work_dir.mkdir()

    with pytest.raises(RuntimeError) as ei:
        ex._run_douyin_task(task, "https://www.douyin.com/video/1", False, "small", work_dir)
    assert "转写失败" in str(ei.value)
    # 三档都被试过（低/高/默认）
    assert len(downloaded) == 3


def test_ytdlp_fallback_rescues_undecodable_candidates(monkeypatch, tmp_path):
    downloaded = []
    _patch_common(monkeypatch, downloaded)
    fallback_file = tmp_path / "fallback_video.mp4"
    fallback_file.write_bytes(b"y" * 32)

    def fake_transcribe(path, model, language=None, vad=True, **kwargs):  # on_progress 由调用方传入
        if "fallback" not in str(path):
            raise RuntimeError("解码失败")
        return "兜底识别文字", []

    monkeypatch.setattr(ex, "_transcribe", fake_transcribe)
    monkeypatch.setattr(ex, "download_media", lambda *a, **k: fallback_file)
    task = _new_task("dy_fallback_3")
    work_dir = tmp_path / task.taskId
    work_dir.mkdir()

    ex._run_douyin_task(task, "https://www.douyin.com/video/1", False, "small", work_dir)
    assert task.textContent == "兜底识别文字"


# ---------- 图集（图文）作品：网页端拿不到，要给可执行提示且不重试 ----------

def test_is_note_url():
    from app.douyin import is_note_url

    assert is_note_url("https://www.douyin.com/note/7605223942487974501")
    assert is_note_url("https://www.douyin.com/note/7605223942487974501?previous_page=x")
    assert not is_note_url("https://www.douyin.com/video/7696763176490192155")
    assert not is_note_url("https://v.douyin.com/abc/")


def test_note_message_is_permanent_and_not_retried():
    """图集提示必须是 permanent（不给平台再发一次请求）。"""
    from app.douyin import is_note_url  # noqa: F401  触发模块导入
    from app.utils import classify_failure

    msg = ("这是抖音的图集（图文）作品：网页端不支持图集数据（实测匿名 / 带账号 cookie / "
           "分享页都拿不到），所以本工具无法自动提取。"
           "可行做法：在抖音 App 里保存该图集的图片，再用本工具的「图片文案提取（OCR）」上传识别。")
    assert classify_failure(RuntimeError(msg)) == "permanent"

    calls = []

    def fetch(url, use_cookie=False, **kw):
        calls.append(use_cookie)
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        ex._fetch_platform(fetch, "https://www.douyin.com/note/7605223942487974501")
    assert calls == [False], "图集不该触发第二次抓取"
