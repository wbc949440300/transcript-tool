"""主 LLM 额度失效 → 自动回退备用模型（2026-10-31 加）。

起因：星火的订单/额度失效后接口全返回 403 11200，纠错与笔记会**静默失败**
（用户看到的只是"这次没改动/笔记没要点"）。现在只有"额度/授权类"错误才回退，
网络抖动之类照旧原样抛出。
"""
import json
import urllib.error

import pytest

import app.llm_correct as lc

PRIMARY = {"TRANSCRIPT_LLM_BASE_URL": "https://primary.example/v1",
           "TRANSCRIPT_LLM_API_KEY": "sk-primary",
           "TRANSCRIPT_LLM_MODEL": "primary-model"}
FALLBACK = {"TRANSCRIPT_LLM_FALLBACK_BASE_URL": "https://fallback.example/v1",
            "TRANSCRIPT_LLM_FALLBACK_API_KEY": "sk-fallback",
            "TRANSCRIPT_LLM_FALLBACK_MODEL": "fallback-model"}


class _Resp:
    def __init__(self, body: str):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _set_env(monkeypatch, *, with_fallback=True):
    for k, v in PRIMARY.items():
        monkeypatch.setenv(k, v)
    if with_fallback:
        for k, v in FALLBACK.items():
            monkeypatch.setenv(k, v)
    else:
        for k in FALLBACK:
            monkeypatch.delenv(k, raising=False)


def _ok_body():
    return json.dumps({"choices": [{"message": {"content": "[]"}}]})


def test_falls_back_on_quota_error(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr(lc, "_PRIMARY_DEAD_UNTIL", 0.0)
    seen = []

    def fake_urlopen(req, timeout=None):
        model = json.loads(req.data.decode("utf-8"))["model"]
        seen.append((req.full_url, model))
        if model == "primary-model":                 # 主模型一律 403（额度失效）
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         __import__("io").BytesIO(b'{"error":{"code":11200}}'))
        return _Resp(_ok_body())

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    assert lc._post_chat([{"role": "user", "content": "x"}]) == "[]"
    assert seen[0][1] == "primary-model"
    assert seen[1] == ("https://fallback.example/v1/chat/completions", "fallback-model")


def test_no_fallback_for_transient_error(monkeypatch):
    _set_env(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        lc._post_chat([{"role": "user", "content": "x"}])
    assert len(calls) == 1, "网络类错误不该切备用模型"


def test_auth_error_without_fallback_raises_friendly_message(monkeypatch):
    """按用户要求：不配回退时，模型不可用要抛出人能看懂的提示（前端直接弹）。"""
    _set_env(monkeypatch, with_fallback=False)
    monkeypatch.setattr(lc, "_PRIMARY_DEAD_UNTIL", 0.0)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                     __import__("io").BytesIO(b'{"error":{"code":11200}}'))

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        lc._post_chat([{"role": "user", "content": "x"}])
    msg = str(ei.value)
    assert "AI 模型不可用" in msg and "spark-x2.6" in msg
    assert len(calls) == 1, "没有备用模型时不该重试"


@pytest.mark.parametrize("raw,want", [
    ('LLM 接口返回 403: {"error":{"code":11200}}', "没有对应模型的授权"),
    ('LLM 接口返回 401: {"message":"HMAC signature cannot be verified: apikey not found"}', "平台查不到这个 key"),
    ('LLM 接口返回 403: {"error":{"code":11221}}', "套餐不包含该模型"),
    ('LLM 接口返回 429: {"error":{"code":11201}}', "被限流"),
    ("LLM 返回格式异常: {}", "LLM 返回格式异常"),        # 非模型问题：原样透传
])
def test_friendly_api_error_messages(raw, want):
    assert want in lc._friendly_api_error(RuntimeError(raw))


def test_is_auth_error_classification():
    assert lc._is_auth_error(RuntimeError("LLM 接口返回 403: quota exhausted"))
    assert lc._is_auth_error(RuntimeError("11200 no valid authorization"))
    assert not lc._is_auth_error(RuntimeError("LLM 调用失败: timed out"))
    assert not lc._is_auth_error(RuntimeError("LLM 返回格式异常: {}"))


def test_primary_is_skipped_during_cooldown(monkeypatch):
    """第一次判定主模型失效后，这段时间内后续调用直接用备用（不再白撞 403）。"""
    _set_env(monkeypatch)
    monkeypatch.setattr(lc, "_PRIMARY_DEAD_UNTIL", 0.0)
    calls = []

    def fake_urlopen(req, timeout=None):
        model = json.loads(req.data.decode("utf-8"))["model"]
        calls.append(model)
        if model == "primary-model":
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         __import__("io").BytesIO(b'{"error":{"code":11200}}'))
        return _Resp(_ok_body())

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    lc._post_chat([{"role": "user", "content": "x"}])          # 主 → 403 → 备用
    assert calls == ["primary-model", "fallback-model"]
    assert lc.primary_cooldown_remaining() > 0

    calls.clear()
    lc._post_chat([{"role": "user", "content": "x"}])          # 冷却期内：直接备用
    assert calls == ["fallback-model"]


def test_max_tokens_field_matches_provider(monkeypatch):
    """讯飞 MaaS 用官方新字段 max_completion_tokens；其它服务商仍用 max_tokens。"""
    _set_env(monkeypatch)
    monkeypatch.setattr(lc, "_PRIMARY_DEAD_UNTIL", 0.0)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(_ok_body())

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)

    lc._post_chat([{"role": "user", "content": "x"}])          # primary = 普通 OpenAI 兼容
    assert "max_tokens" in captured["body"]
    assert "max_completion_tokens" not in captured["body"]

    monkeypatch.setenv("TRANSCRIPT_LLM_BASE_URL", "https://maas-api.cn-huabei-1.xf-yun.com/v2")
    captured.clear()
    lc._post_chat([{"role": "user", "content": "x"}])          # 讯飞 MaaS
    assert "max_completion_tokens" in captured["body"]
    assert "max_tokens" not in captured["body"]


def test_fallback_cfg_requires_all_three(monkeypatch):
    _set_env(monkeypatch, with_fallback=False)
    monkeypatch.setenv("TRANSCRIPT_LLM_FALLBACK_BASE_URL", "https://x/v1")
    assert lc._fallback_cfg() is None            # 缺 key/model 不算配好
    monkeypatch.setenv("TRANSCRIPT_LLM_FALLBACK_API_KEY", "sk-x")
    monkeypatch.setenv("TRANSCRIPT_LLM_FALLBACK_MODEL", "m")
    assert lc._fallback_cfg()["model"] == "m"