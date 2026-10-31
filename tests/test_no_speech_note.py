"""成功但无文字（纯音乐 / 无旁白）的任务要给一句明确提示，而不是"成功 + 空白"。"""
from fastapi.testclient import TestClient

from app import extractor as ex
from app.history import history_store
from app.models import TaskDetail, TaskStatus
from app.task_store import store


def _task(tid: str, **kw) -> TaskDetail:
    return TaskDetail(taskId=tid, taskType="transcript", status=TaskStatus.PENDING, **kw)


def test_note_when_nothing_at_all():
    assert ex._empty_text_note(_task("note_1")) == ex.NO_SPEECH_NOTE
    assert "没有识别到口播内容" in ex.NO_SPEECH_NOTE


def test_no_note_when_text_exists():
    assert ex._empty_text_note(_task("note_2", textContent="这是一段正常的口播文案")) is None


def test_short_hallucination_gets_a_note():
    """静音/纯音乐上 whisper 会幻听出极短文字（实测 "You"），也要给提示。"""
    t = _task("note_2b", textContent="You")
    t.segments = [{"start": 0.0, "end": 1.0, "text": "You"}]
    note = ex._empty_text_note(t)
    assert note and "幻听" in note and "You" in note


def test_short_text_from_subtitle_is_not_flagged():
    """字幕/图片路径没有 segments：短文本不该被当成幻听（只有 ASR 才判断）。"""
    t = _task("note_2c", textContent="早安")
    assert ex._empty_text_note(t) is None
    assert ex._empty_text_note(t, asr=False) is None


def test_no_note_when_only_frame_text():
    """画面文字识别到了内容，就不该说"没有识别到口播"。"""
    t = _task("note_3")
    t.frameText = "画面里的字"
    assert ex._empty_text_note(t) is None


def test_mark_success_writes_note_for_empty_task():
    t = _task("note_4", title="纯音乐视频")
    store.create(t)
    ex._mark_success(t)
    assert t.status == TaskStatus.SUCCESS
    assert t.errorMessage == ex.NO_SPEECH_NOTE


def test_mark_success_keeps_message_empty_when_text_exists():
    t = _task("note_5", title="有口播", textContent="这是一段正常的口播文案内容")
    store.create(t)
    ex._mark_success(t)
    assert t.errorMessage is None


def test_ai_correct_refuses_hint_text(monkeypatch):
    """提示语不是文本：没有正文时纠错要直接拒绝，而不是去"纠错"那句提示。"""
    monkeypatch.setenv("TRANSCRIPT_LLM_API_KEY", "test-key")
    history_store.clear()
    t = TaskDetail(taskId="note_6", taskType="transcript", status=TaskStatus.SUCCESS,
                   title="纯音乐", textContent="")
    t.createTime = "2026-10-31 11:00:00"
    t.errorMessage = ex.NO_SPEECH_NOTE
    history_store.save(t)

    from app.main import app

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    r = client.post("/prod-api/ai/correct", json={"taskId": "note_6"},
                    headers={"X-API-Token": "test-token"})
    assert r.status_code == 400
    assert "没有可纠错的文本" in r.json()["msg"]