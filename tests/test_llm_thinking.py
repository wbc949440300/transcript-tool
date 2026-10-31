"""思考链开关：纠错/笔记可分别设置；开思考时必须给足 token 预算。

背景（2026-10-31 实测）：spark-x2.6 默认开思维链，512 token 会被思考吃光、正文为空；
关掉后 2.8s 出结果，且找到的错字反而更多（34 vs 31 条）。
"""
import json

import pytest

import app.llm_correct as lc


class _FakeResp:
    def __init__(self, body: str):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def captured(monkeypatch):
    box = {}

    def fake_urlopen(req, timeout=None):
        box["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(json.dumps({"choices": [{"message": {"content": "[]"}}]}))

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    yield box


def test_default_no_thinking_field(monkeypatch, captured):
    monkeypatch.delenv("TRANSCRIPT_LLM_THINKING", raising=False)
    monkeypatch.delenv("TRANSCRIPT_LLM_THINKING_NOTE", raising=False)
    # 固定成"非讯飞"端点：讯飞走 max_completion_tokens，这条用例验的是 max_tokens
    monkeypatch.setenv("TRANSCRIPT_LLM_BASE_URL", "https://llm.test/v1")
    lc._post_chat([{"role": "user", "content": "x"}])
    assert "thinking" not in captured["body"]
    assert captured["body"]["max_tokens"] == 512


def test_thinking_off_for_correction(monkeypatch, captured):
    monkeypatch.setenv("TRANSCRIPT_LLM_THINKING", "off")
    lc._post_chat([{"role": "user", "content": "x"}], kind="correct")
    assert captured["body"]["thinking"] == {"type": "disabled"}


def test_note_thinking_can_be_enabled_separately(monkeypatch, captured):
    monkeypatch.setenv("TRANSCRIPT_LLM_THINKING", "off")
    monkeypatch.setenv("TRANSCRIPT_LLM_THINKING_NOTE", "on")
    monkeypatch.setenv("TRANSCRIPT_LLM_BASE_URL", "https://llm.test/v1")
    lc._post_chat([{"role": "user", "content": "x"}], kind="note")
    # 开思考必须同时抬高预算，否则正文会被思维链吃空
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert captured["body"]["max_tokens"] == 2048


def test_note_falls_back_to_base_switch(monkeypatch, captured):
    monkeypatch.setenv("TRANSCRIPT_LLM_THINKING", "off")
    monkeypatch.delenv("TRANSCRIPT_LLM_THINKING_NOTE", raising=False)
    lc._post_chat([{"role": "user", "content": "x"}], kind="note")
    assert captured["body"]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("raw,want", [
    ("off", "off"), ("false", "off"), ("disabled", "off"),
    ("on", "on"), ("true", "on"), ("", ""), ("garbage", ""),
])
def test_thinking_mode_parsing(monkeypatch, raw, want):
    monkeypatch.setenv("TRANSCRIPT_LLM_THINKING", raw)
    assert lc._thinking_mode("correct") == want