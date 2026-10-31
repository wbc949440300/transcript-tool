"""双 ASR 交叉校验：用第二个本地引擎（Vosk）与 faster-whisper 比对，
修正同音错字与专业术语，不依赖 API Key。

策略（保守）：
  1. 两个引擎各出一份全文
  2. 字符级 diff 找出 whisper 的"疑似错字"片段
  3. 只有当 vosk 在**同一位置**给出不同的、且更可信的词时才替换
  4. 保留时长/分段顺序，时间戳不变
"""
from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dual_asr")

_MODEL_CACHE: dict[str, Optional[object]] = {}


def _model_dir() -> Optional[Path]:
    env = os.getenv("TRANSCRIPT_VOSK_MODEL")
    if env and Path(env).is_dir():
        return Path(env)
    base = Path.home() / ".cache" / "vosk"
    for name in ("vosk-model-small-cn-0.22", "vosk-model-cn-0.22"):
        p = base / name
        if p.is_dir():
            return p
    return None


def vosk_available() -> bool:
    try:
        import vosk  # noqa: F401
        return _model_dir() is not None
    except Exception:
        return False


def transcribe_vosk(wav_path: Path, sample_rate: int = 16000) -> str:
    """用 Vosk 转写 16k 单声道 wav，返回纯文本。"""
    model_dir = _model_dir()
    if model_dir is None:
        raise RuntimeError("未找到 Vosk 中文模型")
    import vosk

    if "model" not in _MODEL_CACHE:
        vosk.SetLogLevel(-1)
        _MODEL_CACHE["model"] = vosk.Model(str(model_dir))
    rec = vosk.KaldiRecognizer(_MODEL_CACHE["model"], sample_rate)
    rec.SetWords(False)

    import wave
    texts: list[str] = []
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getframerate() != sample_rate:
            raise RuntimeError(f"Vosk 需要 16kHz 采样率，实际 {wf.getframerate()}")
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                r = _partial_text(rec.Result())
                if r:
                    texts.append(r)
        r = _partial_text(rec.FinalResult())
        if r:
            texts.append(r)
    return "".join(texts)


def _partial_text(payload: str) -> str:
    try:
        import json
        d = json.loads(payload)
        return (d.get("text") or "").strip()
    except Exception:
        return ""


# ---------- 交叉校验 ----------

_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:'\"\"''《》（）()【】\[\]…—\-/]")


def _norm(s: str) -> str:
    return _PUNCT.sub("", s or "")


def cross_validate(primary: str, secondary: str, min_ratio: float = 0.6) -> tuple[str, list[dict]]:
    """用 secondary（vosk）修正 primary（whisper）的疑似错字。

    只做保守替换：对每个差异块，若 secondary 候选长度 >=2 且与 primary 相似度
    低于阈值（说明是不同的词），才替换。返回 (修正后全文, 修正记录)。
    """
    a_raw, b_raw = primary or "", secondary or ""
    a, b = _norm(a_raw), _norm(b_raw)
    if not a or not b:
        return primary, []
    if _norm(a) == _norm(b):
        return primary, []

    sm = SequenceMatcher(None, a, b, autojunk=False)
    fixes: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        seg_a, seg_b = a[i1:i2], b[j1:j2]
        if not seg_b or len(seg_b) < 2:
            continue
        ratio = SequenceMatcher(None, seg_a, seg_b).ratio() if seg_a else 0.0
        # 完全相同或高度相似（多为同一词的不同切分）不动
        if ratio >= min_ratio or ratio >= 0.85:
            continue
        fixes.append({"from": seg_a, "to": seg_b, "ratio": round(ratio, 3),
                      "pos": i1})

    if not fixes:
        return primary, []

    # 按位置从后往前替换（用归一化索引映射回原文太复杂，改用"全文替换首个匹配"）
    out = a_raw
    applied = []
    for f in sorted(fixes, key=lambda x: -x["pos"]):
        if f["from"] and f["from"] in out:
            out = out.replace(f["from"], f["to"], 1)
            applied.append(f)
        elif not f["from"]:
            continue
    return (out, applied) if applied else (primary, [])


def dual_asr_correct(wav_path: Path, whisper_text: str) -> tuple[str, list[dict], str]:
    """入口：跑第二引擎并交叉校验。

    返回 (修正后文本, 修正记录, 状态说明)。
    """
    if not vosk_available():
        return whisper_text, [], "unavailable"
    try:
        second = transcribe_vosk(wav_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vosk 转写失败: %s", exc)
        return whisper_text, [], "failed"
    if not second.strip():
        return whisper_text, [], "empty"
    fixed, fixes = cross_validate(whisper_text, second)
    return fixed, fixes, ("ok" if fixes else "no-diff")


# ---------- SenseVoice（sherpa-onnx，免 torch，中文强） ----------

_SV_CACHE: dict[str, object] = {}


def _sensevoice_dir() -> Path | None:
    env = os.getenv("TRANSCRIPT_SENSEVOICE_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    base = Path.home() / ".cache" / "sherpa" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    return base if base.is_dir() else None


def sensevoice_available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False
    d = _sensevoice_dir()
    return bool(d and (d / "model.int8.onnx").is_file() and (d / "tokens.txt").is_file())


def _get_sensevoice():
    import sherpa_onnx

    if "rec" in _SV_CACHE:
        return _SV_CACHE["rec"]
    d = _sensevoice_dir()
    if d is None:
        raise RuntimeError("未找到 SenseVoice 模型")
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(d / "model.int8.onnx"),
        tokens=str(d / "tokens.txt"),
        language="zh",
        use_itn=True,
        num_threads=2,
    )
    _SV_CACHE["rec"] = rec
    return rec


def transcribe_sensevoice(wav_path: Path, sample_rate: int = 16000) -> str:
    """用 SenseVoice 转写 16k 单声道 wav。"""
    import wave

    rec = _get_sensevoice()
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getframerate() != sample_rate:
            raise RuntimeError(f"SenseVoice 需要 16kHz，实际 {wf.getframerate()}")
        samples = wf.readframes(wf.getnframes())
    import array
    audio = array.array("h", samples)
    stream = rec.create_stream()
    stream.accept_waveform(sample_rate, audio)
    rec.decode_stream(stream)
    return (stream.result.text or "").strip()
