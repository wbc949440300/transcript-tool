"""字幕优先（第 3 步）：画面字幕当正文 + 参与纠错。

用户实测场景：画面截图文字是"肥水不流外人田"，ASR 听成"非说也没有留到外人天地里去"。
- 字级替换对救不了整句听错 → 允许「字幕对齐」替换（目标写法必须能在字幕里找到）
- 完全没有口播时（纯音乐/告示牌视频）→ 直接把画面字幕当正文
"""
import json

import pytest

import app.extractor as ex
import app.llm_correct as lc
from app.models import TaskDetail, TaskStatus
from app.task_store import store

ASR = "非说也没有留到外人天地里去"
SUB = "肥水不流外人田"


def test_subtitle_backed_pair_is_applied():
    fixed, applied = lc._apply_pairs(ASR, [{"from": ASR, "to": SUB}], subtitle=SUB)
    assert fixed == SUB
    assert applied and applied[0]["kind"] == "subtitle"


def test_long_pair_rejected_without_subtitle():
    fixed, applied = lc._apply_pairs(ASR, [{"from": ASR, "to": SUB}])
    assert fixed == ASR and applied == []


def test_pair_target_must_exist_in_subtitle():
    """字幕里找不到的目标写法一律不放行（防模型借字幕名义改写）。"""
    fixed, applied = lc._apply_pairs(ASR, [{"from": ASR, "to": "另一句完全不同的话"}], subtitle=SUB)
    assert fixed == ASR and applied == []


def test_pair_target_too_long_is_rejected():
    long_to = "很" * 80
    fixed, applied = lc._apply_pairs(ASR, [{"from": ASR, "to": long_to}], subtitle=ASR + long_to)
    assert fixed == ASR and applied == []


def test_correct_transcript_passes_subtitle_into_prompt(monkeypatch):
    seen = {}

    def fake_post(messages, timeout=None, kind="correct"):
        seen["prompt"] = messages[0]["content"]
        return json.dumps([{"from": ASR, "to": SUB}], ensure_ascii=False)

    monkeypatch.setattr(lc, "_post_chat", fake_post)
    monkeypatch.setattr(lc, "llm_configured", lambda: True)
    res = lc.correct_transcript(ASR, None, "标题", "抖音", SUB)

    assert res["ok"] and SUB in res["corrected"]
    assert "画面字幕" in seen["prompt"] and SUB in seen["prompt"]
    assert res["changes"][0]["kind"] == "subtitle"


def test_correct_transcript_without_subtitle_unchanged(monkeypatch):
    def fake_post(messages, timeout=None, kind="correct"):
        assert "画面字幕" not in messages[0]["content"]
        return "[]"

    monkeypatch.setattr(lc, "_post_chat", fake_post)
    monkeypatch.setattr(lc, "llm_configured", lambda: True)
    res = lc.correct_transcript(ASR, None, "", "")
    assert res["ok"] and res["corrected"] == ASR


def _frame_ocr_stub(monkeypatch, text=SUB):
    import app.frame_ocr as fo

    monkeypatch.setattr(fo, "extract_frame_text",
                        lambda src, duration=None, referer="": {
                            "blocks": [{"t": 0.0, "text": text}], "text": text, "dropped": 0})


def _task_with(text: str, tid: str) -> TaskDetail:
    task = TaskDetail(taskId=tid, taskType="transcript", status=TaskStatus.PENDING,
                      textContent=text)
    store.create(task)
    return task


def test_image_only_video_uses_subtitle_as_main_text(monkeypatch, tmp_path):
    _frame_ocr_stub(monkeypatch)
    work = tmp_path / "v1"
    work.mkdir()
    (work / "video.mp4").write_bytes(b"x")
    task = _task_with("", "sub_main_1")          # 无口播
    ex._run_frame_ocr(task, "https://www.douyin.com/video/1", work)
    assert task.textContent == SUB               # 字幕成了正文
    assert task.frameText == SUB
    assert task.frameBlocks and task.frameBlocks[0]["t"] == 0.0


def test_normal_speech_is_not_overwritten(monkeypatch, tmp_path):
    _frame_ocr_stub(monkeypatch)
    work = tmp_path / "v2"
    work.mkdir()
    (work / "video.mp4").write_bytes(b"x")
    spoken = "这是一段正常的口播内容，长度足够，不应该被画面文字覆盖掉。"
    task = _task_with(spoken, "sub_main_2")
    ex._run_frame_ocr(task, "https://www.douyin.com/video/1", work)
    assert task.textContent == spoken            # 口播保留
    assert task.frameText == SUB                 # 画面文字单独存