"""画面文字识别：视频抽帧 → OCR → 按「频率 + 位置 + 字号」分类去重。

用户需求（2026-10-30）：把"画面里的文字"与"口播文案"分开提取，且区分
创作者内容（硬字幕/说明文字）与噪音（水印/台标/界面元素）。

分类规则（对所有平台统一，无需按平台特判）：
- 临时出现（<60% 帧出现）                          → 硬字幕/花字   → 画面文字 ✓
- 持续出现（≥60% 帧）+ 角落 + 小字                 → 水印/台标/UI  → 丢弃 ✗
- 持续出现但居中大字（如视频号"告示牌"型视频）      → 正文          → 画面文字 ✓
（"角"= 水平与垂直都靠边；"小"= 字高 < 6% 画面高）

输出：blocks（带首次出现时间）+ 合并文本。OCR 引擎复用 ocr.py 的单例。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("frame_ocr")

_MIN_LINE_SCORE = 0.45      # OCR 置信度下限
_PERSISTENT_RATIO = 0.6     # 出现帧占比 ≥ 该值视为"常驻"
_CORNER_X = 0.25            # 水平靠边阈值（距左右边缘 25% 内）
_CORNER_Y = 0.16            # 垂直靠边阈值（距上下边缘 16% 内）
_SMALL_H = 0.06             # 小字阈值（字高/画面高）
_MIN_TEXT_LEN = 1


def _video_duration(path: str) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((r.stdout or "").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def _video_size(path: str) -> tuple[int, int]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        parts = (r.stdout or "").strip().split(",")
        return int(parts[0]), int(parts[1])
    except Exception:  # noqa: BLE001
        return 0, 0


def _norm_key(text: str) -> str:
    """分组键：去掉空白/标点/大小写差异，剩下"内容骨架"。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text).lower()


def _ocr_lines(img: Path, W: int, H: int) -> list[tuple[str, float, float, float]]:
    """单帧 OCR，返回 [(text, cx, cy, h)]，坐标均为 0~1 归一化。"""
    from . import ocr as ocr_mod

    engine = ocr_mod._get_engine()
    result = engine(str(img))
    out: list[tuple[str, float, float, float]] = []
    for item in (result[0] if result and result[0] else []):
        try:
            box, text, score = item[0], item[1], item[2]
        except Exception:  # noqa: BLE001
            continue
        if not text or not text.strip() or float(score or 0) < _MIN_LINE_SCORE:
            continue
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        cx = ((min(xs) + max(xs)) / 2) / max(W, 1)
        cy = ((min(ys) + max(ys)) / 2) / max(H, 1)
        h = (max(ys) - min(ys)) / max(H, 1)
        out.append((text.strip(), cx, cy, h))
    return out


def _is_corner(cx: float, cy: float) -> bool:
    return (cx < _CORNER_X or cx > 1 - _CORNER_X) and (cy < _CORNER_Y or cy > 1 - _CORNER_Y)


def _frame_cmd(source: str, referer: str, interval: float, max_frames: int,
               out_pattern: str, scale_w: int = 0) -> list[str]:
    """构造抽帧命令。

    - 只有 http(s) 源才加 `-headers`：对**本地文件**加这个选项 ffmpeg 会直接报
      "Option headers not found" 而整条抽帧失败（2026-10-31 实测踩到）。
    - scale_w > 0 时把帧缩到该宽度：1080x1920 每帧 OCR 要 ~6 秒，缩到 720 宽
      字幕照样清楚、整体快一倍以上（2026-10-31 实测）。
    """
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if referer and str(source).lower().startswith(("http://", "https://")):
        cmd += ["-headers", f"Referer: {referer}\r\nUser-Agent: Mozilla/5.0\r\n"]
    vf = f"fps=1/{interval:.2f}"
    if scale_w > 0:
        vf += f",scale={int(scale_w)}:-2"
    cmd += ["-i", source, "-vf", vf, "-frames:v",
            str(max_frames), "-q:v", "4", out_pattern]
    return cmd


_PROBE_FRAMES = 8          # 探针帧数：先扫这几帧，没有"正文类文字"就跳过全扫
_PROBE_MIN_LINES = 1       # 探针至少命中几条"非水印"文字才算有画面文字


def _scan(source: str, referer: str, interval: float, frames_n: int, W: int, H: int,
          tmp: Path, tag: str):
    """抽 frames_n 帧 → OCR → 按频率/位置/字号聚合。

    返回 (blocks, dropped, 实际帧数, 原始行)；原始行供探针判定用。
    """
    cmd = _frame_cmd(source, referer, interval, frames_n,
                     str(tmp / f"{tag}_%03d.jpg"), scale_w=W)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    frames = sorted(tmp.glob(f"{tag}_*.jpg"))
    if not frames:
        logger.warning("抽帧失败(%s): %s", tag, (r.stderr or "")[:150])
        return [], 0, 0, []

    total = len(frames)
    agg: dict[str, dict] = {}
    order: list[str] = []
    raw: list[tuple[str, float, float, float]] = []
    for idx, f in enumerate(frames):
        t = round(idx * interval, 1)
        for text, cx, cy, h in _ocr_lines(f, W, H):
            if len(text.strip()) < _MIN_TEXT_LEN:
                continue
            k = _norm_key(text)
            if not k:
                continue
            raw.append((text, cx, cy, h))
            rec = agg.get(k)
            if rec is None:
                rec = {"count": 0, "first_t": t, "samples": {text: 0},
                       "corner": 0, "small": 0}
                agg[k] = rec
                order.append(k)
            rec["count"] += 1
            rec["samples"][text] = rec["samples"].get(text, 0) + 1
            if _is_corner(cx, cy):
                rec["corner"] += 1
            if h < _SMALL_H:
                rec["small"] += 1

    blocks: list[dict] = []
    dropped = 0
    for k in order:
        rec = agg[k]
        persistent = rec["count"] >= max(3, _PERSISTENT_RATIO * total)
        corner = rec["corner"] >= rec["count"] * 0.6
        small = rec["small"] >= rec["count"] * 0.6
        if persistent and corner and small:
            dropped += 1  # 水印/台标/界面元素
            continue
        text = max(rec["samples"].items(), key=lambda x: x[1])[0]
        blocks.append({"t": rec["first_t"], "text": text})
    blocks.sort(key=lambda b: b["t"])
    return blocks, dropped, total, raw


def _probe_has_text(raw: list[tuple[str, float, float, float]]) -> bool:
    """探针判定：画面里有没有"像正文/字幕"的文字。

    单帧看不出"是否常驻"，这里只排除"既靠边又很小"的行（水印/台标/UI 的典型特征），
    其余的（居中、或字够大）都算有内容 → 值得跑全扫。
    """
    hits = 0
    for text, cx, cy, h in raw:
        if len(_norm_key(text)) < 2:
            continue
        if _is_corner(cx, cy) and h < _SMALL_H:
            continue
        hits += 1
        if hits >= _PROBE_MIN_LINES:
            return True
    return False


def extract_frame_text(source: str, duration: float | None = None,
                       max_frames: int = 48, referer: str = "") -> dict:
    """主入口。source 可为本地文件或 http 直链（ffmpeg 直接拉流）。

    两段式（2026-10-31 提速）：先扫少量**探针帧**；没有画面文字就直接返回，
    不再跑完整的 48 帧（大量视频没有硬字幕，这一刀省掉大部分时间）。

    返回 {"blocks": [{"t": 秒, "text": str}], "text": "按时间合并的文本",
          "dropped": 丢弃的水印条数, "probed": 探针帧数, "skipped": 是否跳过全扫}
    """
    from . import ocr as ocr_mod  # 确保引擎预热计入调用方计时

    max_frames = max(1, int(max_frames))
    dur = duration or _video_duration(source)
    W, H = _video_size(source)
    if not W or not H:
        W, H = 720, 1280
    scale_w = min(720, W)
    if scale_w < W:
        H = max(1, int(round(H * scale_w / W)))
    W = scale_w
    try:
        probe_n = int(os.getenv("TRANSCRIPT_FRAME_OCR_PROBE", str(_PROBE_FRAMES)) or _PROBE_FRAMES)
    except Exception:  # noqa: BLE001
        probe_n = _PROBE_FRAMES
    probe_n = max(1, min(probe_n, max_frames))

    tmp = Path(tempfile.mkdtemp(prefix="frames_"))
    try:
        # 1) 探针：帧数少，但间隔按整条视频均分（覆盖率不缩水）
        p_interval = 1.5 if not dur else max(1.5, dur / probe_n)
        p_blocks, p_dropped, p_total, raw = _scan(source, referer, p_interval, probe_n,
                                                  W, H, tmp, "p")
        if not _probe_has_text(raw):
            logger.info("画面文字探针: %d 帧未见字幕/正文文字 → 跳过全扫", p_total)
            return {"blocks": [], "text": "", "dropped": 0,
                    "probed": p_total, "skipped": True}

        if probe_n >= max_frames:
            blocks, dropped, total = p_blocks, p_dropped, p_total
        else:
            # 2) 全扫：覆盖整条视频（间隔按总时长均分，不封顶）
            interval = 1.5 if not dur else max(1.5, dur / max_frames)
            blocks, dropped, total, _raw = _scan(source, referer, interval, max_frames,
                                                 W, H, tmp, "f")
        merged = "\n".join(b["text"] for b in blocks)
        logger.info("画面文字: 探针 %d 帧命中 → 全扫 %d 帧 → %d 条（丢弃水印 %d）",
                    p_total, total, len(blocks), dropped)
        return {"blocks": blocks, "text": merged, "dropped": dropped,
                "probed": p_total, "skipped": False}
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
