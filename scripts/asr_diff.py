"""批处理 ASR 的**文本差异**对比：batch=8 到底少了什么（视频号提速选型用）。

背景：同一段 5 分钟音频，batch=0 出 2319 字、batch=8 出 1984 字（少 14%）。
段数 174 → 31 是批处理的正常表现（合并碎片），但字数差异必须查清是
"表示差异/语气碎片"还是"真丢内容"——这决定能不能把 batch 作为默认。

用法：
    python scripts/asr_diff.py [媒体路径] [--minutes 5] [--model medium]

产出：
    <项目>/_asr_diff/batch0.txt、batch8.txt（两种模式的原文，方便你亲自核对）
    stdout 打印：重合率、丢失片段统计（按长度分级）、成句丢失的示例
"""
from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT_DIR = ROOT / "_asr_diff"

from scripts.asr_bench import cut_clip, pick_source  # noqa: E402  复用挑素材/切片段

_PUNCT = re.compile(r"[\s，。、！？；：""''（）《》…—\-.,!?;:()\[\]{}<>~|\"'`]+")


def norm(s: str) -> str:
    """比对用归一化：去空白与标点，只留内容字符。"""
    return _PUNCT.sub("", s or "")


def run_mode(clip: Path, model: str, batch: int) -> tuple[str, float]:
    import os

    os.environ["TRANSCRIPT_ASR_BATCH"] = str(batch)
    from app.extractor import _transcribe

    t0 = time.time()
    text, _segs = _transcribe(clip, model, language="zh")
    return text or "", time.time() - t0


def sentences(s: str) -> list[str]:
    parts = re.split(r"[。！？!?\n]+", s or "")
    return [p.strip() for p in parts if len(norm(p)) >= 4]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("media", nargs="?", default="")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--model", default="medium")
    ap.add_argument("--batch", type=int, default=8, help="对比用的批大小（默认 8）")
    args = ap.parse_args()
    B = max(1, int(args.batch))

    src = pick_source(args.media)
    if src is None:
        print("找不到素材：请显式传媒体路径")
        return 1
    print(f"素材：{src.name} → 取 {args.minutes} 分钟（同一段音频，两种模式各跑一次）\n")

    tmp = Path(tempfile.mkdtemp(prefix="asrdiff_"))
    clip = cut_clip(src, int(args.minutes * 60), tmp / "clip.wav")

    a, ta = run_mode(clip, args.model, 0)
    b, tb = run_mode(clip, args.model, B)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "batch0.txt").write_text(a, encoding="utf-8")
    (OUT_DIR / f"batch{B}.txt").write_text(b, encoding="utf-8")

    na, nb = norm(a), norm(b)
    print(f"batch=0 : {ta:5.1f}s  {len(a):5d} 字（归一化 {len(na)}）")
    print(f"batch={B} : {tb:5.1f}s  {len(b):5d} 字（归一化 {len(nb)}）"
          f"   相对逐段提速 {ta / max(tb, 0.1):.2f}x")
    print(f"原文已存：{OUT_DIR}/batch0.txt、batch{B}.txt\n")

    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    equal = sum(i2 - i1 for tag, i1, i2, _, _ in sm.get_opcodes() if tag == "equal")
    cov = equal / max(1, len(na))
    print(f"batch=0 的内容被 batch=8 覆盖：{cov:.1%}（重合 {equal} 字 / {len(na)}）")

    lost, extra = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace") and i2 - i1 >= 2:
            lost.append(na[i1:i2])
        if tag in ("insert", "replace") and j2 - j1 >= 2:
            extra.append(nb[j1:j2])
    lost_chars = sum(len(s) for s in lost)
    frag = [s for s in lost if len(s) < 6]
    body = [s for s in lost if len(s) >= 6]
    print(f"\nbatch={B} 缺少的片段：{len(lost)} 处 / {lost_chars} 字"
          f"（其中 <6 字的碎片 {len(frag)} 处、{sum(len(s) for s in frag)} 字；"
          f"≥6 字的成句片段 {len(body)} 处、{sum(len(s) for s in body)} 字）")
    body.sort(key=len, reverse=True)
    for s in body[:12]:
        print(f"   · 缺（{len(s)} 字）：{s}")
    if len(body) > 12:
        print(f"   … 另有 {len(body) - 12} 处成句缺失，见 _asr_diff/batch0.txt")

    print(f"\nbatch={B} 多出来的内容：{len(extra)} 处 / {sum(len(s) for s in extra)} 字")
    extra.sort(key=len, reverse=True)
    for s in extra[:5]:
        print(f"   · 多（{len(s)} 字）：{s}")

    sa, sb = sentences(a), sentences(b)
    missing_sent = 0
    for line in sa:
        n = norm(line)
        hit = any(difflib.SequenceMatcher(None, n, norm(o)).ratio() >= 0.6 for o in sb)
        if not hit:
            missing_sent += 1
    print(f"\n句子级：batch=0 的 {len(sa)} 句里，batch=8 找不到对应（相似度<0.6）的有 "
          f"{missing_sent} 句（{missing_sent / max(1, len(sa)):.0%}）")

    verdict = ("**成句内容丢失明显，不该用作默认**" if len(body) >= 5 or missing_sent >= 5
               else "以碎片级差异为主，可考虑作为可选的提速档")
    print(f"\n判定：{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())