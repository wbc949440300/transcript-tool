"""图片文案提取（OCR）。

用 RapidOCR（onnxruntime 轻量级中文 OCR）识别图片中的文字，
用于"抖音图片文案提取 / 小红书图文提取"。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("ocr")

_engine = None
_engine_lock = threading.Lock()

BytesOrPath = Union[bytes, Path, str]


def _get_engine():
    """懒加载 + 单例 RapidOCR 引擎。"""
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            logger.info("初始化 RapidOCR 引擎...")
            _engine = RapidOCR()
            logger.info("RapidOCR 引擎就绪")
        return _engine


def ocr_image(source: BytesOrPath) -> str:
    """识别图片文字，按从上到下（再按行）合并为纯文本。

    返回空字符串表示图片无文字。
    """
    engine = _get_engine()
    # RapidOCR 接受文件路径或 bytes
    result = engine(source)
    if not result or not result[0]:
        return ""
    lines = result[0]
    # result 元素: [box(4点), text, score]
    # 收集 (vys-center, text, score)
    rows: list[tuple[float, str, float]] = []
    for box, text, score in lines:
        if not text or not text.strip():
            continue
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            ys = [pt[1] for pt in box]
            cx = sum(pt[0] for pt in box) / len(box)
            cy = sum(ys) / len(ys)
            rows.append((cy, text, float(score or 0)))
        else:
            rows.append((0.0, str(text), 1.0))
    # 按垂直位置分行（容忍少量偏移），再按水平排序
    rows.sort(key=lambda r: r[0])
    out_lines: list[str] = []
    cur_band: list[tuple[float, str]] = []
    band_cy = None
    band_thresh = 18  # 像素阈值，小于该间距视为同一行
    for cy, text, _score in rows:
        if band_cy is None:
            band_cy = cy
            cur_band = [(0.0, text)]
        elif abs(cy - band_cy) <= band_thresh:
            cur_band.append((0.0, text))  # x 排序下面统一处理
            band_cy = (band_cy + cy) / 2
        else:
            cur_band.sort(key=lambda r: r[0])
            out_lines.append(" ".join(t for _, t in cur_band))
            band_cy = cy
            cur_band = [(0.0, text)]
    if cur_band:
        cur_band.sort(key=lambda r: r[0])
        out_lines.append(" ".join(t for _, t in cur_band))
    # 去掉纯文本行之间的多余空行
    cleaned = [ln.strip() for ln in out_lines if ln and ln.strip()]
    return "\n".join(cleaned)
