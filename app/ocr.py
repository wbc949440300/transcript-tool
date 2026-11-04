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


def warmup() -> bool:
    """预热 RapidOCR 引擎：首次推理要做 ONNX 会话/图优化，实测比后续慢数倍。

    关键：用**带文字**的图，而不是纯白图——纯白图不会真正跑识别模型，
    服务重启后第一条“密集文字”视频仍会慢（实测 6 秒视频 OCR 要 2 分钟）。
    """
    try:
        import numpy as np

        engine = _get_engine()
        img = None
        try:
            from PIL import Image, ImageDraw, ImageFont

            im = Image.new("RGB", (480, 420), "white")
            d = ImageDraw.Draw(im)
            try:
                font = ImageFont.truetype("arial.ttf", 28)
            except Exception:  # noqa: BLE001
                font = ImageFont.load_default()
            # 多行文字 → 触发检测 + 多条识别（更接近真实视频的密集文字）
            for i in range(6):
                d.text((16, 20 + i * 60), f"OCR warmup {i + 1} 1234567890", fill="black", font=font)
            img = np.array(im)
        except Exception:  # noqa: BLE001
            img = np.full((64, 160, 3), 255, dtype=np.uint8)
        engine(img)
        logger.info("RapidOCR 预热完成（含文字识别）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("RapidOCR 预热失败: %s", exc)
        return False
