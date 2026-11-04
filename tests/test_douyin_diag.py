"""抖音直链体检的离线单元测试（不联网、不起浏览器）。"""
from app.extractor import _douyin_candidate_label, _douyin_diag_note


D = {
    "video_url": "http://x/default",
    "audio_url": "http://x/audio.mp3",
    "video_variants": [
        {"url": "http://x/a", "bitrate": 544176, "gear": "720_4_1"},
        {"url": "http://x/b", "bitrate": 597238, "gear": "720_4_1"},
        {"url": "http://x/c", "gear": ""},
    ],
}


def test_label_uses_gear_and_kbps():
    assert _douyin_candidate_label(D, "http://x/a") == "720_4_1@544kbps"
    assert _douyin_candidate_label(D, "http://x/c") == "档"


def test_label_falls_back_to_role():
    assert _douyin_candidate_label(D, "http://x/default") == "默认档"
    assert _douyin_candidate_label(D, "http://x/audio.mp3") == "音频兜底"
    assert _douyin_candidate_label(D, "http://x/unknown") == "直链"


def test_note_keeps_every_row():
    rows = ["候选1 720_4_1@544kbps 无音轨", "候选2 默认档 下载被中断"]
    note = _douyin_diag_note(rows)
    assert "544kbps" in note and "下载被中断" in note


def test_note_empty_says_so():
    assert _douyin_diag_note([]) == "没有可用的直链候选"
