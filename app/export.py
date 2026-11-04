"""任务结果导出：TXT / Markdown / Word(.docx) / Excel(.xlsx)。

导出内容与抖虫保持一致：标题、作品简介、口播文案（OCR 为识别文字）、
平台、时长、作者、来源链接、提取时间。支持单条与批量（最多 100 条）。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from typing import Optional

from .history import history_store
from .task_store import store

MAX_BATCH = 100

_LABELS = {
    "zh": {
        "title": "标题", "content": "作品简介/话题", "text": "口播文案（全片语音转写）", "ocr_text": "识别文字",
        "frame_main": "作者文案", "frame_scene": "画面文字",
        "platform": "平台", "duration": "时长", "uploader": "作者", "url": "来源链接",
        "time": "提取时间", "status": "状态", "type": "类型", "index": "序号",
        "success": "成功", "failure": "失败", "empty": "(无)",
    },
    "en": {
        "title": "Title", "content": "Description", "text": "Transcript", "ocr_text": "OCR Text",
        "frame_main": "Author Copy", "frame_scene": "On-screen Text",
        "platform": "Platform", "duration": "Duration", "uploader": "Author", "url": "Source URL",
        "time": "Extracted At", "status": "Status", "type": "Type", "index": "No.",
        "success": "Success", "failure": "Failed", "empty": "(empty)",
    },
}

_TYPE_ZH = {"transcript": "视频文案", "file_transcript": "文件转写", "image-ocr": "图片OCR"}


def _labels(lang: str) -> dict:
    return _LABELS.get("en" if str(lang).lower().startswith("en") else "zh")


def _fmt_duration(ms) -> str:
    """把时长格式化为 m:ss。

    统一按"秒"存储：yt-dlp 的 duration 本来就是秒；抖音 aweme_detail 返回毫秒，
    已在提取时换算。这里对历史遗留的毫秒值做兼容（>86400 视为毫秒）。
    """
    try:
        if not ms:
            return ""
        val = float(ms)
    except Exception:
        return ""
    if val > 86400:      # 明显是毫秒（>1天的秒数）
        val = val / 1000.0
    total = int(round(val))
    if total <= 0:
        return ""
    return f"{total // 60}:{total % 60:02d}"


def _load_task(task_id: str, source: str = "original") -> Optional[dict]:
    """优先取历史记录，取不到再取内存中的任务。"""
    def _segments_from_extra(extra_raw) -> list:
        try:
            extra = json.loads(extra_raw or "{}")
            return extra.get("segments") or []
        except Exception:  # noqa: BLE001
            return []

    row = history_store.get(task_id)
    if row:
        if not row.get("segments"):
            row["segments"] = _segments_from_extra(row.get("extra"))
        try:
            extra = json.loads(row.get("extra") or "{}") or {}
        except Exception:  # noqa: BLE001
            extra = {}
        row["frame_text"] = extra.get("frameText")
        row["frame_main_text"] = extra.get("frameMainText")
        row["frame_scene_text"] = extra.get("frameSceneText")
        # source="corrected" 时用 AI 纠错版覆盖正文（导出用）
        if source == "corrected":
            try:
                extra = json.loads(row.get("extra") or "{}")
            except Exception:  # noqa: BLE001
                extra = {}
            corrected = extra.get("corrected_text")
            if corrected:
                row["text_content"] = corrected
                row["segments"] = []  # 纠错版无逐段时间戳，SRT 走整段降级
        return row
    t = store.get(task_id)
    if not t:
        return None
    return {
        "task_id": t.taskId,
        "task_type": getattr(t, "taskType", "transcript"),
        "status": t.status.value if hasattr(t.status, "value") else str(t.status),
        "title": t.title,
        "content": t.content,
        "text_content": t.textContent,
        "frame_text": getattr(t, "frameText", None),
        "frame_main_text": getattr(t, "frameMainText", None),
        "frame_scene_text": getattr(t, "frameSceneText", None),
        "platform": t.platform,
        "duration": t.duration,
        "uploader": t.uploader,
        "webpage_url": t.webpageUrl,
        "error_msg": t.errorMessage,
        "created_at": t.createTime,
        "segments": getattr(t, "segments", None) or [],
    }


def _normalize(task: dict, lang: str) -> dict:
    L = _labels(lang)
    body = (task.get("text_content") or "").strip()
    frame = (task.get("frame_text") or "").strip()
    frame_main = (task.get("frame_main_text") or "").strip()
    frame_scene = (task.get("frame_scene_text") or "").strip()
    # 没有口播时后端会把画面文字顶上当正文（字幕优先），此时正文其实是 OCR 出来的，
    # 标签不能再写"口播文案（全片语音转写）"——那是误导。
    body_from_frames = bool(body and frame and "".join(body.split()) == "".join(frame.split()))
    is_ocr = (task.get("task_type") == "image-ocr") or body_from_frames
    text_label = L["ocr_text"] if is_ocr else L["text"]
    # 正文已经就是 OCR 全文时不再重复附分组段；否则按来源分开输出
    if body_from_frames:
        frame_main = frame_scene = frame = ""
    return {
        "title": task.get("title") or L["empty"],
        "content": task.get("content") or "",
        # 成功但无口播的任务会把提示写进 error_msg —— 那是提示不是正文，不能当文案导出；
        # 只有失败任务才沿用"用错误信息填这个位置"的老行为。
        "text": task.get("text_content") or (
            (task.get("error_msg") or "") if task.get("status") != "SUCCESS" else ""
        ),
        "text_label": text_label,
        "frame_main": frame_main,
        "frame_scene": frame_scene,
        "frame_all": frame,
        "platform": task.get("platform") or "",
        "duration": _fmt_duration(task.get("duration")),
        "uploader": task.get("uploader") or "",
        "url": task.get("webpage_url") or "",
        "time": task.get("created_at") or "",
        "status": L["success"] if task.get("status") == "SUCCESS" else L["failure"],
        "type": _TYPE_ZH.get(task.get("task_type"), task.get("task_type") or ""),
    }


def _build_txt(tasks: list[dict], lang: str) -> bytes:
    L = _labels(lang)
    blocks = []
    for idx, task in enumerate(tasks, 1):
        n = _normalize(task, lang)
        lines = [
            "=" * 48,
            f"{L['index']}: {idx}" if len(tasks) > 1 else "",
            f"{L['title']}: {n['title']}",
            f"{L['platform']}: {n['platform']}",
        ]
        if n["uploader"]:
            lines.append(f"{L['uploader']}: {n['uploader']}")
        if n["duration"]:
            lines.append(f"{L['duration']}: {n['duration']}")
        if n["url"]:
            lines.append(f"{L['url']}: {n['url']}")
        lines.append(f"{L['time']}: {n['time']}")
        lines.append("-" * 48)
        if n["content"]:
            lines.append(f"{L['content']}:\n{n['content']}\n")
        lines.append(f"{n['text_label']}:\n{n['text']}")
        if n["frame_main"]:
            lines.append(f"{L['frame_main']}:\n{n['frame_main']}\n")
        if n["frame_scene"]:
            lines.append(f"{L['frame_scene']}:\n{n['frame_scene']}\n")
        elif n["frame_all"]:
            lines.append(f"{L['frame_scene']}:\n{n['frame_all']}\n")
        lines.append("=" * 48)
        blocks.append("\n".join(x for x in lines if x is not None))
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def _build_md(tasks: list[dict], lang: str) -> bytes:
    L = _labels(lang)
    parts = []
    for idx, task in enumerate(tasks, 1):
        n = _normalize(task, lang)
        lines = []
        lines.append(f"# {n['title']}" if len(tasks) == 1 else f"# {idx}. {n['title']}")
        lines.append("")
        meta = []
        if n["platform"]:
            meta.append(f"- **{L['platform']}**: {n['platform']}")
        if n["uploader"]:
            meta.append(f"- **{L['uploader']}**: {n['uploader']}")
        if n["duration"]:
            meta.append(f"- **{L['duration']}**: {n['duration']}")
        if n["url"]:
            meta.append(f"- **{L['url']}**: {n['url']}")
        meta.append(f"- **{L['time']}**: {n['time']}")
        lines.extend(meta)
        lines.append("")
        if n["content"]:
            lines.append(f"## {L['content']}")
            lines.append("")
            lines.append(n["content"])
            lines.append("")
        lines.append(f"## {n['text_label']}")
        lines.append("")
        lines.append(n["text"] or L["empty"])
        lines.append("")
        if n["frame_main"]:
            lines.append(f"## {L['frame_main']}")
            lines.append("")
            lines.append(n["frame_main"])
            lines.append("")
        if n["frame_scene"]:
            lines.append(f"## {L['frame_scene']}")
            lines.append("")
            lines.append(n["frame_scene"])
            lines.append("")
        elif n["frame_all"]:
            lines.append(f"## {L['frame_scene']}")
            lines.append("")
            lines.append(n["frame_all"])
            lines.append("")
        parts.append("\n".join(lines))
    return ("\n\n---\n\n".join(parts) + "\n").encode("utf-8")


def _build_docx(tasks: list[dict], lang: str) -> bytes:
    from docx import Document
    from docx.shared import Pt

    L = _labels(lang)
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)

    for idx, task in enumerate(tasks, 1):
        n = _normalize(task, lang)
        if len(tasks) > 1:
            doc.add_heading(f"{idx}. {n['title']}", level=1)
        else:
            doc.add_heading(n["title"], level=1)

        meta_parts = []
        if n["platform"]:
            meta_parts.append(f"{L['platform']}: {n['platform']}")
        if n["uploader"]:
            meta_parts.append(f"{L['uploader']}: {n['uploader']}")
        if n["duration"]:
            meta_parts.append(f"{L['duration']}: {n['duration']}")
        if n["url"]:
            meta_parts.append(f"{L['url']}: {n['url']}")
        meta_parts.append(f"{L['time']}: {n['time']}")
        p = doc.add_paragraph("  |  ".join(meta_parts))
        p.runs[0].font.size = Pt(9)

        if n["content"]:
            doc.add_heading(L["content"], level=2)
            doc.add_paragraph(n["content"])
        doc.add_heading(n["text_label"], level=2)
        doc.add_paragraph(n["text"] or L["empty"])
        if n["frame_main"]:
            doc.add_heading(L["frame_main"], level=2)
            doc.add_paragraph(n["frame_main"])
        if n["frame_scene"]:
            doc.add_heading(L["frame_scene"], level=2)
            doc.add_paragraph(n["frame_scene"])
        elif n["frame_all"]:
            doc.add_heading(L["frame_scene"], level=2)
            doc.add_paragraph(n["frame_all"])

        if idx < len(tasks):
            doc.add_page_break()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_xlsx(tasks: list[dict], lang: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    L = _labels(lang)
    wb = Workbook()
    ws = wb.active
    ws.title = "文案提取结果"

    headers = [
        L["index"], L["type"], L["status"], L["platform"], L["title"], L["uploader"],
        L["duration"], L["content"], L["text"], L["url"], L["time"],
    ]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="7C3AED")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for idx, task in enumerate(tasks, 1):
        n = _normalize(task, lang)
        values = [
            idx, n["type"], n["status"], n["platform"], n["title"], n["uploader"],
            n["duration"], n["content"], n["text"], n["url"], n["time"],
        ]
        row = ws.max_row + 1
        for col, value in enumerate(values, 1):
            cell = ws.cell(row=row, column=col, value=value)
            # 公式注入防护（2026-11-02）：标题/正文/作者来自第三方内容，以 = 开头的字符串会被
            # openpyxl 标成公式（data_type='f'），导出的表格一打开就执行。这里强制按纯文本写。
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                cell.data_type = "s"

    widths = [6, 12, 8, 10, 42, 16, 8, 46, 60, 40, 20]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _srt_clock(seconds: float) -> str:
    ms = int(round(float(seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt(tasks: list[dict], lang: str) -> bytes:
    """SRT 字幕：多任务时按累计时长顺延并连续编号。"""
    blocks: list[str] = []
    idx = 1
    offset = 0.0
    for task in tasks:
        segs = task.get("segments") or []
        used = False
        for seg in segs:
            text = " ".join(str(seg.get("text") or "").split())
            if not text:
                continue
            start = offset + float(seg.get("start") or 0.0)
            end = offset + float(seg.get("end") or 0.0)
            if end <= start:
                end = start + 1.0
            blocks.append(f"{idx}\n{_srt_clock(start)} --> {_srt_clock(end)}\n{text}\n")
            idx += 1
            used = True
        if used and segs:
            offset += float(segs[-1].get("end") or 0.0) + 1.0
        elif not segs:
            # 没有分段（旧记录）时，用整段文本生成一条字幕，避免空文件
            text = " ".join(str(task.get("text_content") or "").split())
            if text:
                blocks.append(f"{idx}\n{_srt_clock(offset)} --> {_srt_clock(offset + 5)}\n{text}\n")
                idx += 1
                offset += 6.0
    return ("\n".join(blocks)).encode("utf-8")


def _build_plain(tasks: list[dict], lang: str) -> bytes:
    """仅文案模式（txt/md）：每个任务只输出转写正文，无标题/简介/任何元数据。

    多任务合并在一个文件时用空行分隔；zip 批量时每个文件只有一个任务。
    """
    texts = [(_normalize(t, lang)["text"] or "").strip() for t in tasks]
    return ("\n\n".join(texts) + "\n").encode("utf-8")


def _build_plain_docx(tasks: list[dict], lang: str) -> bytes:
    """仅文案模式（docx）：只有正文的 Word 文档。"""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)
    for task in tasks:
        n = _normalize(task, lang)
        doc.add_paragraph((n["text"] or "").strip())
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


_BUILDERS = {"txt": _build_txt, "markdown": _build_md, "docx": _build_docx,
             "excel": _build_xlsx, "srt": _build_srt}
_MIME = {
    "txt": "text/plain; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "srt": "application/x-subrip; charset=utf-8",
}
_EXT = {"txt": "txt", "markdown": "md", "docx": "docx", "excel": "xlsx", "srt": "srt"}


def _select_builder(fmt: str, plain: bool):
    """plain 模式：txt/md/docx 走"仅文案"构建器；Excel/SRT 本就不含冗余，维持原样。"""
    if not plain:
        return _BUILDERS[fmt]
    if fmt == "docx":
        return _build_plain_docx
    if fmt in ("txt", "markdown"):
        return _build_plain
    return _BUILDERS[fmt]


def export_tasks(task_ids: list[str], fmt: str = "txt", lang: str = "zh",
                 source: str = "original", plain: bool = False) -> tuple[bytes, str, str]:
    """返回 (文件字节, 文件名, MIME 类型)。plain=True 时只导出转写正文。"""
    fmt = (fmt or "txt").lower()
    if fmt not in _BUILDERS:
        raise ValueError(f"不支持的导出格式: {fmt}")
    ids = [i for i in (task_ids or []) if i]
    if not ids:
        raise ValueError("暂无可导出的任务")
    if len(ids) > MAX_BATCH:
        raise ValueError(f"单次最多导出 {MAX_BATCH} 个任务")

    tasks = []
    for tid in ids:
        t = _load_task(tid, source=source)
        if t:
            tasks.append(t)
    if not tasks:
        raise ValueError("任务不存在或已删除")

    content = _select_builder(fmt, plain)(tasks, lang)
    ext = _EXT[fmt]
    if len(tasks) == 1:
        filename = f"transcript.{ext}"
    else:
        filename = f"transcript_{len(tasks)}_{datetime.now():%Y%m%d_%H%M%S}.{ext}"
    return content, filename, _MIME[fmt]


def _safe_file_stem(name: str, fallback: str) -> str:
    """把标题处理成可用作文件名的片段（去掉 Windows 非法字符）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        return fallback
    return s[:60].strip(" .") or fallback


def export_tasks_zip(task_ids: list[str], fmt: str = "txt", lang: str = "zh",
                     source: str = "original", plain: bool = False) -> tuple[bytes, str, str]:
    """批量导出：每个任务一个独立文件，打包成 ZIP。

    与单条导出用同一套构建器，所以每条文件的排版和单条导出一致；
    文件名形如 "01_标题.txt"，重名由序号保证唯一。
    plain=True 时每个文件只有转写正文（文件名仍带标题便于识别）。
    """
    fmt = (fmt or "txt").lower()
    if fmt not in _BUILDERS:
        raise ValueError(f"不支持的导出格式: {fmt}")
    ids = [i for i in (task_ids or []) if i]
    if not ids:
        raise ValueError("暂无可导出的任务")
    if len(ids) > MAX_BATCH:
        raise ValueError(f"单次最多导出 {MAX_BATCH} 个任务")

    tasks = []
    for tid in ids:
        t = _load_task(tid, source=source)
        if t:
            tasks.append(t)
    if not tasks:
        raise ValueError("任务不存在或已删除")

    builder = _select_builder(fmt, plain)
    ext = _EXT[fmt]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, task in enumerate(tasks, 1):
            fallback = f"task_{task.get('task_id') or idx}"
            stem = _safe_file_stem(task.get("title") or "", fallback)
            zf.writestr(f"{idx:02d}_{stem}.{ext}", builder([task], lang))

    filename = f"transcripts_{len(tasks)}_{datetime.now():%Y%m%d_%H%M%S}.zip"
    return buf.getvalue(), filename, "application/zip"
