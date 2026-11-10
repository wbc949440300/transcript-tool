"""ASR 提速对比：**批处理推理 vs 逐段推理**（视频号长视频为主）。

背景：代码注释写"6GB 显存 + medium + batch8 实测不 OOM"，但默认值是 0（关）。
要改默认值就得先拿到同音频、同模型下的实测数据 —— 这个脚本就是干这个的。

用法：
    python scripts/asr_bench.py <媒体路径> --minutes 5
    python scripts/asr_bench.py <媒体路径> --minutes 5 --model medium

输出：每种模式的耗时、实时倍率(x Realtime)、峰值显存、识别字数（看质量有没有掉）。
顺序刻意做成 0 → 8 → 0：第二次的 batch=0 是"热模型"基准，用来排除"第二次本来就快"的假象。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MEDIA_EXT = (".mp4", ".mov", ".mkv", ".webm", ".flv", ".m4v", ".avi",
             ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus")


def pick_source(explicit: str = "") -> Path | None:
    """只接受显式路径：监听目录已移除，不再猜素材位置。"""
    p = Path(explicit) if explicit else None
    return p if p and p.is_file() else None


def cut_clip(src: Path, seconds: int, out: Path) -> Path:
    """取一段片段并转成与生产链路一致的 16k 单声道 wav（两种模式输入完全一样）。"""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", str(seconds),
                    "-vn", "-ac", "1", "-ar", "16000", str(out)],
                   check=True, capture_output=True)
    return out


class VramSampler:
    """后台采样显存占用（nvidia-smi 不可用就静默跳过）。"""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._th = None

    def __enter__(self):
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False

    def _loop(self):
        while not self._stop.is_set():
            try:
                r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                                    "--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=10)
                val = int((r.stdout or "0").strip().splitlines()[0])
                self.peak = max(self.peak, val)
            except Exception:  # noqa: BLE001
                return
            self._stop.wait(self.interval)


def run_once(audio: Path, model_name: str, batch: int) -> dict:
    os.environ["TRANSCRIPT_ASR_BATCH"] = str(batch)
    from app.extractor import _transcribe

    with VramSampler() as vr:
        t0 = time.time()
        text, segs = _transcribe(audio, model_name, language="zh")
        dt = time.time() - t0
    dur = float(getattr(segs[-1], "get", lambda k, d=None: None)("end", 0) or 0) if segs else 0.0
    return {"batch": batch, "sec": round(dt, 1), "chars": len(text or ""), "segments": len(segs or []),
            "peak_vram_mb": vr.peak, "audio_sec": round(dur, 1) or None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("media", nargs="?", default="", help="媒体文件（省略则自动挑最大的）")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--model", default="medium")
    args = ap.parse_args()

    src = pick_source(args.media)
    if src is None:
        print("找不到可用的媒体文件：请显式传媒体路径")
        return 1
    print(f"素材：{src.name}（{src.stat().st_size/1048576:.1f} MB）→ 取 {args.minutes} 分钟片段")

    tmp = Path(tempfile.mkdtemp(prefix="asrbench_"))
    clip = cut_clip(src, int(args.minutes * 60), tmp / "clip.wav")
    print(f"片段就绪：{clip.stat().st_size/1048576:.1f} MB\n")

    results = []
    for batch in (0, 8, 0):            # 头尾都跑 0：第二次排除"热模型"假象
        r = run_once(clip, args.model, batch)
        results.append(r)
        rt = (r["audio_sec"] / r["sec"]) if r.get("audio_sec") else 0
        print(f"batch={r['batch']:<2} 耗时 {r['sec']:>6.1f}s | {rt:.2f}x 实时 | "
              f"峰值显存 {r['peak_vram_mb'] or '?'} MB | {r['chars']} 字 / {r['segments']} 段")

    cold0, b8, warm0 = results
    if warm0["sec"] > 0:
        print(f"\n结论：batch=8 相对**热模型下的**逐段推理提速 "
              f"{warm0['sec']/max(b8['sec'],0.1):.2f}x（{warm0['sec']}s → {b8['sec']}s）")
    print("提示：显存接近上限就别把 batch 调更大；字数明显变少说明该 batch 不可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())