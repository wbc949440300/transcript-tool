"""文案归档：每个成功的转写任务，把正文自动存成 txt 放进「文案输出」文件夹。

用户诉求（2026-10-30）：历史记录里看文案不方便，希望落地成一个文件夹直接打开看。
- 目录：项目根的 文案输出/（env TRANSCRIPT_ARCHIVE_DIR 可改）
- 文件名：日期_标题.txt（标题做安全化处理；重名自动加 (2)(3)）
- 内容：仅文案正文（与导出的"仅文案"模式一致），utf-8-sig 保证记事本正常显示
- 时机：任务成功落库时写入；服务启动时 backfill() 把历史记录里缺文件的补齐
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("archive")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = _PROJECT_ROOT.parent

MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".flv", ".m4v", ".avi",
              ".wav", ".mp3", ".m4a", ".aac", ".flac", ".opus", ".ogg",
              ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def archive_dir() -> Path:
    return Path(os.getenv("TRANSCRIPT_ARCHIVE_DIR", str(BASE_DIR / "文案输出")))


def _safe_stem(name: str) -> str:
    """标题 → 可用文件名（去非法字符、去媒体扩展名、限长）。"""
    low = (name or "").strip().lower()
    for ext in MEDIA_EXTS:
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    import re

    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:60].strip(" .")) or "未命名"


def _unique_path(directory: Path, stem: str) -> Path:
    p = directory / f"{stem}.txt"
    n = 2
    while p.exists():
        p = directory / f"{stem}({n}).txt"
        n += 1
    return p


_NO_SPEECH_NOTE = "（该视频没有口播，信息在画面文字中）"
_FRAME_MAIN_SEP = "\n\n──────────【作者文案】──────────\n"
_FRAME_SCENE_SEP = "\n\n──────────【画面文字】──────────\n"


def _norm_ws(text: str) -> str:
    return "".join((text or "").split())


def _append_frame_sections(text: str, main: str, scene: str, full: str) -> str:
    """把 OCR 结果按来源分两段追加：作者展示的主文案 / 画面背景杂字。

    两段都来自 OCR，但版式不同（实测主文案水平居中且字号约 2 倍大）。
    旧数据没有 main/scene 分组时退回整段放进【画面文字】，不丢内容。
    """
    main = (main or "").strip()
    scene = (scene or "").strip()
    full = (full or "").strip()
    if main:
        text += _FRAME_MAIN_SEP + main
    if scene:
        text += _FRAME_SCENE_SEP + scene
    if not main and not scene and full:
        text += _FRAME_SCENE_SEP + full
    return text


def save_text_file(task) -> Path | None:
    """把一个已完成任务的文案写成 txt。无正文/失败任务返回 None。

    task 是 TaskDetail/TaskNode 或含同名字段的 dict。
    """
    text = (getattr(task, "textContent", None) or "").strip()
    status = getattr(task, "status", "")
    status_v = status.value if hasattr(status, "value") else str(status)
    frame_text = (getattr(task, "frameText", None) or "").strip()
    frame_main = (getattr(task, "frameMainText", None) or "").strip()
    frame_scene = (getattr(task, "frameSceneText", None) or "").strip()
    if not text and not frame_text:
        return None
    if status_v != "SUCCESS":
        return None
    # 无口播时正文会被画面文字顶替；此时正文与 frameText 是同一份，不能重复写
    if not text or (frame_text and _norm_ws(text) == _norm_ws(frame_text)):
        text = _NO_SPEECH_NOTE
    text = _append_frame_sections(text, frame_main, frame_scene, frame_text)
    title = getattr(task, "title", None) or getattr(task, "taskId", "") or "未命名"
    created = (getattr(task, "createTime", None) or "")[:10] or ""
    stem = (created + "_" if created else "") + _safe_stem(str(title))
    d = archive_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        out = _unique_path(d, stem)
        out.write_text(text + "\n", encoding="utf-8-sig")
        logger.info("文案已归档: %s", out.name)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("文案归档失败（不影响主流程）: %s", exc)
        return None


def backfill() -> int:
    """启动时把历史记录里还没有归档文件的任务补齐（按文件名存在性判重）。"""
    from .history import history_store

    d = archive_dir()
    try:
        rows = history_store.list(limit=1000)
    except Exception:  # noqa: BLE001
        return 0
    if not rows:
        return 0
    existing = set()
    if d.is_dir():
        existing = {p.name for p in d.glob("*.txt")}
    n = 0
    for row in rows:
        text = (row.get("text_content") or "").strip()
        try:
            extra = json.loads(row.get("extra") or "{}")
        except Exception:  # noqa: BLE001
            extra = {}
        ft = (extra.get("frameText") or "").strip()
        fm = (extra.get("frameMainText") or "").strip()
        fs = (extra.get("frameSceneText") or "").strip()
        if not text and not ft:
            continue
        if row.get("status") != "SUCCESS":
            continue
        if not text or (ft and _norm_ws(text) == _norm_ws(ft)):
            text = _NO_SPEECH_NOTE
        text = _append_frame_sections(text, fm, fs, ft)
        if not text:
            continue
        title = row.get("title") or row.get("task_id") or "未命名"
        created = (row.get("created_at") or "")[:10] or ""
        stem = (created + "_" if created else "") + _safe_stem(str(title))
        if f"{stem}.txt" in existing:
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            out = _unique_path(d, stem)
            out.write_text(text + "\n", encoding="utf-8-sig")
            existing.add(out.name)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("补档失败 %s: %s", stem, exc)
    if n:
        logger.info("文案归档补齐 %d 条 → %s", n, d)
    return n
