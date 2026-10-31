"""结构化笔记生成（map-reduce）。

流程：
  1. map：把转录按 800 字分块，并发让 LLM 每块提取 {key_points, quotes, terms}
  2. reduce：把所有要点交给 LLM，产出 {topics, tags, summary, narrative}
  3. 本地拼装 Markdown：YAML front matter + 固定章节 + **完整转录（不截断）**

依赖 app.llm_correct 的 OpenAI 兼容客户端与 .env 配置。
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from .llm_correct import _model, _post_chat, llm_configured, last_served as _last_served

logger = logging.getLogger("note_writer")

MAP_PROMPT = (
    "你是视频笔记助手。请从下面这段口播转录中提取信息，严格输出 JSON（不要代码块、不要解释）：\n"
    '{"key_points": ["要点1"], "quotes": ["值得记录的原文金句"], '
    '"terms": [{"term": "术语", "desc": "简要解释"}]}\n'
    "要求：key_points 3-6 条、quotes 0-3 条、terms 0-4 条；只依据原文，不要编造。\n"
    "转录：\n"
)

REDUCE_PROMPT = (
    "你是视频笔记助手。下面是一部视频的元信息和分段要点。请归纳成整体结构，严格输出 JSON（不要代码块、不要解释）：\n"
    '{"topics": ["核心主题"], "tags": ["标签"], "summary": "一句话总结（≤50字）", '
    '"narrative": "按视频顺序的连贯叙述，分 3-6 段，每段 2-4 句，只用原文信息，不要编造"}\n'
    "要求：topics 3-6 个、tags 5-10 个。\n"
)


def note_available() -> bool:
    return llm_configured()


def _parse_json_obj(raw: str) -> dict:
    if not raw:
        return {}
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[A-Za-z]*\n?", "", txt)
        txt = re.sub(r"\n?```\s*$", "", txt)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return {}
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _chunks_text(text: str, max_chars: int = 800) -> list[str]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    out, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > max_chars and cur:
            out.append("\n".join(cur)); cur, size = [], 0
        cur.append(ln); size += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def _map_chunk(block: str, hint: str = "") -> dict:
    user = MAP_PROMPT + (f"（视频主题参考：{hint}）\n" if hint else "") + block
    try:
        return _parse_json_obj(_post_chat([{"role": "user", "content": user}], kind="note"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("map 失败（跳过该块）: %s", exc)
        return {}


def _reduce(meta: dict, mapped: list[dict]) -> dict:
    parts = []
    for i, m in enumerate(mapped, 1):
        kp = m.get("key_points") or []
        if kp:
            parts.append(f"第{i}段要点：" + "；".join(str(x) for x in kp))
    body = "\n".join(parts) or "（无）"
    user = (REDUCE_PROMPT
            + f"\n标题：{meta.get('title','')}\n平台：{meta.get('platform','')}\n"
            + f"作者：{meta.get('uploader','')}\n分段要点：\n{body}")
    try:
        return _parse_json_obj(_post_chat([{"role": "user", "content": user}], kind="note"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("reduce 失败: %s", exc)
        return {}


def _fmt_duration(sec) -> str:
    try:
        s = int(float(sec or 0))
    except Exception:
        return ""
    if s <= 0:
        return ""
    return f"{s//60}:{s%60:02d}"


def _yaml_list(v: Any) -> str:
    if not v:
        return "[]"
    if isinstance(v, str):
        v = [x.strip() for x in re.split(r"[,，、]", v) if x.strip()]
    return "[" + ", ".join(str(x).replace(",", "，") for x in v) + "]"


def generate_note(task: dict, max_workers: int = 3) -> dict:
    """对任务生成结构化笔记，返回 {ok, markdown, chunks, model, error}。"""
    if not note_available():
        return {"ok": False, "error": "未配置 LLM（TRANSCRIPT_LLM_API_KEY）"}
    text = (task.get("text_content") or "").strip()
    if not text:
        return {"ok": False, "error": "没有可整理的文案（该视频可能无口播）"}

    meta = {
        "title": task.get("title") or "",
        "platform": task.get("platform") or "",
        "uploader": task.get("uploader") or "",
        "url": task.get("webpage_url") or "",
        "duration": _fmt_duration(task.get("duration")),
        "created": task.get("created_at") or "",
    }

    blocks = _chunks_text(text, 800)
    hint = meta["title"]
    try:
        max_workers = max(1, int(os.getenv("TRANSCRIPT_NOTE_WORKERS", str(max_workers))))
    except Exception:
        max_workers = 3

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        mapped = list(pool.map(lambda b: _map_chunk(b, hint), blocks))

    mapped = [m for m in mapped if m]
    reduced = _reduce(meta, mapped) if mapped else {}

    topics = reduced.get("topics") or []
    tags = reduced.get("tags") or []
    summary = reduced.get("summary") or ""
    narrative = reduced.get("narrative") or ""

    key_points: list[str] = []
    quotes: list[str] = []
    terms: list[dict] = []
    for m in mapped:
        for k in (m.get("key_points") or []):
            k = str(k).strip()
            if k and k not in key_points:
                key_points.append(k)
        for q in (m.get("quotes") or []):
            q = str(q).strip()
            if q and q not in quotes:
                quotes.append(q)
        for t in (m.get("terms") or []):
            if isinstance(t, dict) and t.get("term"):
                terms.append({"term": str(t["term"]).strip(),
                              "desc": str(t.get("desc") or "").strip()})

    md: list[str] = []
    md.append("---")
    md.append(f"title: {meta['title']}")
    if meta["uploader"]:
        md.append(f"author: {meta['uploader']}")
    if meta["url"]:
        md.append(f"source: {meta['url']}")
    if meta["platform"]:
        md.append(f"platform: {meta['platform']}")
    if meta["duration"]:
        md.append(f"duration: {meta['duration']}")
    md.append(f"date: {date.today().isoformat()}")
    md.append(f"tags: {_yaml_list(tags)}")
    md.append(f"topics: {_yaml_list(topics)}")
    if summary:
        md.append(f"summary: {summary}")
    md.append("---")
    md.append("")
    md.append(f"# {meta['title'] or '未命名视频'}")
    md.append("")

    md.append("## 核心信息")
    md.append("")
    if meta["uploader"]:
        md.append(f"- **作者/UP主**：{meta['uploader']}")
    if meta["platform"]:
        md.append(f"- **平台**：{meta['platform']}")
    if meta["duration"]:
        md.append(f"- **时长**：{meta['duration']}")
    if meta["url"]:
        md.append(f"- **来源**：{meta['url']}")
    if meta["created"]:
        md.append(f"- **提取时间**：{meta['created']}")
    md.append("")

    if key_points:
        md.append("## 关键要点")
        md.append("")
        for i, k in enumerate(key_points, 1):
            md.append(f"{i}. {k}")
        md.append("")

    if narrative:
        md.append("## 详细内容")
        md.append("")
        for para in str(narrative).split("\n"):
            para = para.strip()
            if para:
                md.append(para)
                md.append("")

    if quotes:
        md.append("## 重要引用 / 金句")
        md.append("")
        for q in quotes:
            md.append(f"> {q}")
            md.append("")

    if terms:
        md.append("## 专业术语 / 概念")
        md.append("")
        for t in terms:
            if t["desc"]:
                md.append(f"- **{t['term']}**：{t['desc']}")
            else:
                md.append(f"- **{t['term']}**")
        md.append("")

    md.append("## 完整转录")
    md.append("")
    md.append(text)

    return {"ok": True, "markdown": "\n".join(md), "chunks": len(blocks),
            "model": (_last_served().get("model") or _model()),
            "fallback": bool(_last_served().get("fallback")),
            "configuredModel": _model(),
            "keyPoints": len(key_points),
            "quotes": len(quotes), "terms": len(terms)}
