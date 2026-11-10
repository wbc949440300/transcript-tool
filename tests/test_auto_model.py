"""「自动」选模型：机器档位（显卡/显存）+ 视频时长 一起决定 tiny/small/medium。"""
from pathlib import Path

import pytest

import app.file_transcribe as ft
import app.frame_ocr as fo


@pytest.fixture
def dur(monkeypatch):
    """把时长探测换成可控值（不碰真实文件）。"""
    holder = {"d": 0}
    monkeypatch.setattr(fo, "_probe_duration", lambda p, *a, **k: holder["d"])
    return holder


def _tier(monkeypatch, tier):
    monkeypatch.setattr(ft, "_asr_tier", lambda: tier)


def test_cpu_and_low_gpus_always_tiny(monkeypatch, dur):
    dur["d"] = 30
    for tier in ("cpu", "low"):
        _tier(monkeypatch, tier)
        assert ft._resolve_auto_model("auto", Path("a.mp4")) == "tiny"


def test_mid_gpu_bands(monkeypatch, dur):
    """mid（如 RTX 3050 6GB）：≤4min medium，≤10min small，更长 tiny。"""
    _tier(monkeypatch, "mid")
    for d, want in ((60, "medium"), (240, "medium"), (300, "small"), (601, "tiny")):
        dur["d"] = d
        assert ft._resolve_auto_model("auto", Path("a.mp4")) == want, d


def test_high_gpu_bands(monkeypatch, dur):
    """high（≥8GB）：medium 放宽到 ≤10min，small 到 ≤30min。"""
    _tier(monkeypatch, "high")
    for d, want in ((600, "medium"), (1200, "small"), (1801, "tiny")):
        dur["d"] = d
        assert ft._resolve_auto_model("auto", Path("a.mp4")) == want, d


def test_unknown_duration_falls_back_to_tiny(monkeypatch, dur):
    _tier(monkeypatch, "high")
    dur["d"] = 0
    assert ft._resolve_auto_model("auto", Path("a.mp4")) == "tiny"


def test_explicit_model_wins(monkeypatch, dur):
    _tier(monkeypatch, "high")
    dur["d"] = 60
    assert ft._resolve_auto_model("medium", Path("a.mp4")) == "medium"
    assert ft._resolve_auto_model("tiny", Path("a.mp4")) == "tiny"


def test_env_overrides_medium_band(monkeypatch, dur):
    _tier(monkeypatch, "mid")
    monkeypatch.setenv("TRANSCRIPT_AUTO_MEDIUM_MAX_SEC", "60")
    dur["d"] = 100
    assert ft._resolve_auto_model("auto", Path("a.mp4")) == "small"


def test_tier_detection_real_machine(monkeypatch):
    """真机探测只验证档位合法（不绑定具体机器）。"""
    monkeypatch.delenv("TRANSCRIPT_ASR_TIER", raising=False)
    assert ft._asr_tier() in ("cpu", "low", "mid", "high")
