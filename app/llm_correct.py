"""LLM 语境纠错（OpenAI 兼容，provider 无关）。

设计要点：
- 只做"同音字/专业术语"纠正：不改写、不增删、不改语气
- 按分段（带时间戳）分块送审，块间重叠 1 段，避免边界丢字
- 原文始终保留；返回改动清单（原文片段 → 修正片段）
- 未配置时功能自动隐藏，不会报错

配置（环境变量）：
  TRANSCRIPT_LLM_BASE_URL   默认 https://api.deepseek.com/v1
  TRANSCRIPT_LLM_API_KEY    必填（Ollama 可填任意值）
  TRANSCRIPT_LLM_MODEL      默认 deepseek-chat
  TRANSCRIPT_LLM_TIMEOUT    默认 120 秒
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from typing import Optional

from .env import load_env

_load_dotenv_done = False


def _load_dotenv() -> None:
    """兼容旧调用；.env 已由 app 包初始化统一加载。"""
    global _load_dotenv_done
    if not _load_dotenv_done:
        _load_dotenv_done = True
        load_env()


load_dotenv = _load_dotenv

logger = logging.getLogger("llm_correct")

SYSTEM_PROMPT = (
    "你是一个只输出 JSON 的校对器。除 JSON 数组外，禁止输出任何其它内容。\n"
    "任务：只找两类问题——① 同音字/错别字；② 繁体字（逐字转简体，不得改词义）。\n"
    "严禁改写句子、调整语序、增删内容、替换近义词，也不要润色或提供多个版本。\n"
    "唯一允许的输出格式：JSON 数组，例如 [{\"from\":\"力距\",\"to\":\"力矩\"}]；"
    "没有需要修改的就输出 []。\n"
    "禁止输出解释、标题、代码块、Markdown、版本对比。整个回复不超过 300 字。\n"
)


def llm_configured() -> bool:
    _load_dotenv()
    return bool(os.getenv("TRANSCRIPT_LLM_API_KEY", "").strip())


def _base_url() -> str:
    _load_dotenv()
    return os.getenv("TRANSCRIPT_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")


def _model() -> str:
    _load_dotenv()
    return os.getenv("TRANSCRIPT_LLM_MODEL", "deepseek-chat")


def _glossary() -> str:
    """领域术语表：注入提示词，显著提升专业名词纠错准确率。"""
    _load_dotenv()
    raw = os.getenv("TRANSCRIPT_LLM_GLOSSARY", "").strip()
    if not raw:
        return ""
    terms = [t.strip() for t in re.split(r"[,，、;；\s]+", raw) if t.strip()]
    return "、".join(terms)


def _thinking_mode(kind: str = "correct") -> str:
    """思考链开关（2026-10-31 实测定论）。

    优先级：`TRANSCRIPT_LLM_THINKING_<KIND>` → `TRANSCRIPT_LLM_THINKING` → 不加任何字段。
    kind：`correct`（纠错）/ `note`（生成笔记），两者可分别设置。

    - `off`（纠错推荐）：请求体带 `thinking:{"type":"disabled"}`。
      实测 spark-x2.6 默认开思维链：250 字/块要 28 秒，且 512 token 全被思考吃光、
      **正文为空**；关掉后 2.8 秒，找到的错字反而更多（34 vs 31 条）。
    - `on`：显式开思考；会同时把 max_tokens 提高到 2048（否则又会被吃光）。
    - 其它/未设置：不加这个字段，行为与切换前完全一致。
    """
    keys = ["TRANSCRIPT_LLM_THINKING"]
    if kind:
        keys.insert(0, f"TRANSCRIPT_LLM_THINKING_{kind.upper()}")
    raw = ""
    for key in keys:
        raw = (os.getenv(key, "") or "").strip().lower()
        if raw:
            break
    if raw in ("off", "false", "0", "no", "disabled", "none"):
        return "off"
    if raw in ("on", "true", "1", "yes", "enabled"):
        return "on"
    return ""


def _fallback_cfg() -> Optional[dict]:
    """备用 LLM（主服务额度用尽/订单失效时自动顶上）。

    TRANSCRIPT_LLM_FALLBACK_BASE_URL / _API_KEY / _MODEL；三样齐全才算配好。
    2026-10-31 实测：星火额度失效后接口全返回 403 11200，纠错/笔记会静默失败，
    所以加了这个自动回退。
    """
    _load_dotenv()
    base = (os.getenv("TRANSCRIPT_LLM_FALLBACK_BASE_URL", "") or "").strip()
    key = (os.getenv("TRANSCRIPT_LLM_FALLBACK_API_KEY", "") or "").strip()
    model = (os.getenv("TRANSCRIPT_LLM_FALLBACK_MODEL", "") or "").strip()
    if not (base and key and model):
        return None
    return {"base": base.rstrip("/"), "key": key, "model": model}


def _is_auth_error(exc: Exception) -> bool:
    """额度用尽 / 未授权 / 订单失效——只有这类才值得换一家重试。"""
    text = str(exc).lower()
    return any(k in text for k in ("401", "403", "11200", "quota", "authorization",
                                   "permission_error", "insufficient"))


_PRIMARY_DEAD_UNTIL = 0.0      # 主模型刚被判定"额度失效"→ 这段时间内直接用备用，不再白撞
_PRIMARY_DEAD_SECS = 600.0
_LAST_SERVED: dict = {"model": "", "fallback": False}


def last_served() -> dict:
    """最近一次调用**实际**用的是哪个模型（回退时会和配置里的主模型不同）。"""
    return dict(_LAST_SERVED)


def _primary_usable() -> bool:
    return time.time() >= _PRIMARY_DEAD_UNTIL


def primary_cooldown_remaining() -> int:
    return max(0, int(_PRIMARY_DEAD_UNTIL - time.time()))


def _mark_primary_dead() -> None:
    global _PRIMARY_DEAD_UNTIL
    _PRIMARY_DEAD_UNTIL = time.time() + _PRIMARY_DEAD_SECS


def _friendly_api_error(exc: Exception) -> str:
    """把模型不可用类错误翻成人话 —— 前端会把这句直接弹给用户看。

    用户要求（2026-10-31）：AI 纠错只用星火 spark-x2.6，不自动切别的模型；
    星火不可用就明确提示，让人一眼知道该去做什么。
    """
    text = str(exc)
    low = text.lower()
    if "11200" in text or "no valid authorization" in low:
        return ("AI 模型不可用：该 API Key 没有对应模型的授权（11200）。"
                "请到讯飞 MaaS 控制台确认这个 key 已开通/授权 spark-x2.6，或更换 key。")
    if "11221" in text:
        return "AI 模型不可用：当前套餐不包含该模型（11221）。请改用套餐内模型或升级套餐。"
    if "apikey not found" in low:
        return ("AI 模型不可用：API Key 无效（平台查不到这个 key）。"
                "请核对 .env 里的 TRANSCRIPT_LLM_API_KEY，注意控制台复制时别漏字符。")
    if any(k in text for k in ("11201", "11202", "11203", "11210", "429")):
        return "AI 调用被限流了（429/额度）。稍等一会儿再试；持续出现请到控制台看额度与并发上限。"
    if "401" in text:
        return "AI 模型不可用：鉴权失败（401）。请检查 API Key 是否正确。"
    return text


def _post_chat(messages: list[dict], timeout: Optional[float] = None,
               kind: str = "correct") -> str:
    """调用主 LLM；额度/授权类错误自动回退到备用 LLM。

    首次判定主模型不可用后，10 分钟内**直接用备用**（不然每次调用都要先撞一次 403，
    既慢又浪费一次往返）。

    **没有配置备用模型时（当前默认）**：直接抛出人能看懂的提示，
    由接口返回给前端弹出 —— 不静默换模型、也不让人猜。
    """
    fb = _fallback_cfg()
    if fb and not _primary_usable():
        return _post_chat_once(messages, timeout=timeout, kind=kind, cfg=fb)
    try:
        return _post_chat_once(messages, timeout=timeout, kind=kind)
    except Exception as exc:  # noqa: BLE001
        if fb and _is_auth_error(exc):
            _mark_primary_dead()
            logger.warning("主 LLM 不可用（%s），回退到备用模型 %s（%d 分钟内直接用备用）",
                           str(exc)[:90], fb["model"], int(_PRIMARY_DEAD_SECS // 60))
            return _post_chat_once(messages, timeout=timeout, kind=kind, cfg=fb)
        logger.warning("LLM 调用失败（无备用模型，直接报错）: %s", str(exc)[:120])
        raise RuntimeError(_friendly_api_error(exc)) from exc


def _post_chat_once(messages: list[dict], timeout: Optional[float] = None,
                    kind: str = "correct", cfg: Optional[dict] = None) -> str:
    """调用 OpenAI 兼容的 chat/completions，返回正文文本。

    kind 决定用哪套思考开关（纠错 / 笔记），并据此调整生成长度上限：
    开思考时必须给足预算（2048），否则思维链会把 token 吃光、正文为空。
    cfg 给出时用备用服务商（base/key/model）。
    """
    timeout = timeout or float(os.getenv("TRANSCRIPT_LLM_TIMEOUT", "120"))
    mode = _thinking_mode(kind)
    base = cfg["base"] if cfg else _base_url()
    budget = 2048 if mode == "on" else 512
    payload = {
        "model": cfg["model"] if cfg else _model(),
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    # 限制生成长度：这些模型容易"自由发挥"写成大段解释，导致又慢又解析不到 JSON。
    # 讯飞 MaaS 已把 max_tokens 标记为"废弃，请改用 max_completion_tokens"（实测两者都收，
    # 新字段 2.7s / 旧字段 8.3s）；其它服务商（DashScope 等）仍只认 max_tokens。
    if "xf-yun.com" in base.lower():
        payload["max_completion_tokens"] = budget
    else:
        payload["max_tokens"] = budget
    _LAST_SERVED.update(model=payload["model"], fallback=bool(cfg))
    if mode == "off":
        payload["thinking"] = {"type": "disabled"}
    elif mode == "on":
        payload["thinking"] = {"type": "enabled"}
    req = urllib.request.Request(
        f"{(cfg['base'] if cfg else _base_url())}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['key'] if cfg else os.getenv('TRANSCRIPT_LLM_API_KEY', '')}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        raise RuntimeError(f"LLM 接口返回 {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"LLM 调用失败: {exc}") from exc

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"LLM 返回格式异常: {str(data)[:200]}") from exc


_HEADER_LINE = re.compile(
    r"^\s*(视频标题|标题|来源平台|平台|来源|source|title)\s*[:：]", re.I)


def _clean_output(text: str) -> str:
    """清掉模型可能多输出的前言/标题/分隔线/代码块围栏。"""
    if not text:
        return text
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    lines = s.splitlines()
    while lines:
        cur = lines[0].strip()
        if not cur or cur in ("---", "***", "—") or _HEADER_LINE.match(cur):
            lines.pop(0)
        else:
            break
    while lines and lines[-1].strip() in ("---", "***", "—"):
        lines.pop()
    return "\n".join(lines).strip()


def _chunks(segments: list[dict], max_chars: int = 1200, overlap: int = 1) -> list[list[dict]]:
    """把分段按字数分块，块间重叠 overlap 段。"""
    out, cur, size = [], [], 0
    for seg in segments:
        t = seg.get("text") or ""
        if size + len(t) > max_chars and cur:
            out.append(cur)
            cur = cur[-overlap:] if overlap else []
            size = sum(len(s.get("text") or "") for s in cur)
        cur.append(seg)
        size += len(t)
    if cur:
        out.append(cur)
    return out


def _chunks_from_text(text: str, max_chars: int = 800) -> list[list[dict]]:
    """无分段时的兜底切块：按行累积到 max_chars，避免超长文本单次调用超时。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    chunks: list[list[dict]] = []
    cur: list[str] = []
    size = 0
    for ln in lines:
        if size + len(ln) > max_chars and cur:
            chunks.append([{"text": "\n".join(cur)}])
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append([{"text": "\n".join(cur)}])
    return chunks


def _diff_changes(before: str, after: str) -> list[dict]:
    """对比原文与修正文，列出改动片段（区分实质纠错与标点规范）。"""
    sm = SequenceMatcher(None, before, after, autojunk=False)
    changes = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = before[i1:i2], after[j1:j2]
        if not a.strip() and not b.strip():
            continue
        # 只有标点/空白变化 → 归为 punctuation，不淹没真正的纠错
        is_punct = bool(re.fullmatch(r"[\s\W_]+", a or " ")) and bool(re.fullmatch(r"[\s\W_]+", b or " "))
        changes.append({"from": a, "to": b, "pos": i1,
                        "kind": "punctuation" if is_punct else "text"})
    return changes


def _parse_pairs(raw: str) -> list[dict]:
    """从模型回复中解析 [{"from":...,"to":...}]，容错处理。"""
    if not raw:
        return []
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[A-Za-z]*\n?", "", txt)
        txt = re.sub(r"\n?```\s*$", "", txt)
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        a = str(item.get("from") or "")
        b = str(item.get("to") or "")
        if a and b and a != b:
            out.append({"from": a, "to": b})
    return out


def _apply_pairs(text: str, pairs: list[dict], glossary: str = "",
                 subtitle: str = "") -> tuple[str, list[dict]]:
    """把替换对应用到文本，并做安全校验（防近义词改写）。

    subtitle 非空时开放「字幕对齐」：目标词出现在画面字幕里 → 有独立证据，
    放行长片段/低相似度的替换（用于修"整句听错"，例如 ASR 把"肥水不流外人田"
    听成"非说也没有留到外人天地里去"）。验证在本地做，不信任模型自称。
    """
    out = text
    applied = []
    terms = {t.strip() for t in re.split(r"[、,，;；\s]+", glossary or "") if t.strip()}
    sub = (subtitle or "").strip()
    for pair in pairs:
        a, b = pair["from"], pair["to"]
        if a not in out:
            continue
        # 规则 1：字幕对齐 —— 目标写法能在字幕里找到，且长度/字数差在合理范围
        if (sub and b and b in sub and len(b) <= 60
                and abs(len(a) - len(b)) <= 30):
            out = out.replace(a, b)
            applied.append({"from": a, "to": b, "kind": "subtitle"})
            continue
        # 只接受纯标点改动之外的替换；长度差过大视为改写
        if abs(len(a) - len(b)) > 2:
            continue
        # 无共享字符时：
        #   - 单字替换放行（繁→简/同音单字，如 這→这）
        #   - 多字替换必须命中术语表，否则视为近义词改写
        if not (set(a) & set(b)):
            if not (len(a) == 1 and len(b) == 1):
                if not any(t in b or b in t for t in terms):
                    logger.info("拒绝疑似改写: %s → %s", a, b)
                    continue
        # 长片段的整段替换：逐字符相似度过低视为改写（如「顯微鏡裡的事件」→「显微镜下的世界」）
        if len(a) >= 3:
            ratio = SequenceMatcher(None, a, b).ratio()
            if ratio < 0.5:
                logger.info("拒绝低相似度替换(%.2f): %s → %s", ratio, a, b)
                continue
        out = out.replace(a, b)
        applied.append({"from": a, "to": b, "kind": "text"})
    return out, applied


def correct_transcript(text: str, segments: list[dict] | None = None,
                       title: str = "", platform: str = "",
                       subtitle: str = "") -> dict:
    """对文稿做语境纠错。

    做法：让模型只输出「替换对 JSON」，再由本地校验并应用——
    避免模型重写全文、引入近义词替换或复述提示词。

    subtitle：可选，视频**画面字幕**（OCR 得到）。给了它就能修"整句听错"：
    目标写法必须能在字幕里找到才放行（本地校验），所以不会被模型改写带偏。
    返回 {ok, corrected, changes, punctuationCount, chunks, model, error}
    """
    if not llm_configured():
        return {"ok": False, "error": "未配置 LLM（TRANSCRIPT_LLM_API_KEY）"}
    if not (text or "").strip():
        return {"ok": False, "error": "没有可纠错的文本"}

    sub_ref = (subtitle or "").strip()[:1500]     # 控制提示词长度
    sys_prompt = SYSTEM_PROMPT
    gl = _glossary()
    if gl:
        sys_prompt += (f"\n可选术语参考（仅当文本确实属于该领域时才采用，不相关请忽略）：{gl}")
    if sub_ref:
        sys_prompt += (
            "\n另外附上该视频的「画面字幕」（从画面 OCR 得到，可能有漏字或错字）。"
            "如果它与文本讲的是同一段内容，请据此判断同音字、**尤其是整句听错**："
            "这种情况允许输出较长的替换对（把听错的整段换成字幕里的写法）。"
            "与文本无关的字幕内容一律不要采用。\n画面字幕：\n" + sub_ref
        )
    if title or platform:
        bits = []
        if title:
            bits.append(f"标题：{title}")
        if platform:
            bits.append(f"平台：{platform}")
        sys_prompt += "\n（背景信息，仅供参考，不要输出：" + "；".join(bits) + "）"

    def _ask(block: str) -> list[dict]:
        # 实测：GLM-4-Flash 对 system/user 分角色不稳定（常返回 []），
        # 单条 user 消息 + 规则前置更可靠
        reply = _post_chat([
            {"role": "user", "content": sys_prompt + "\n文本：\n" + block},
        ])
        return _parse_pairs(reply)

    # 有分段 → 按分段切块；无分段（旧记录）→ 按行切块，避免超长请求超时
    # 块越小单次越快：实测 500 字常打满超时，现默认 250 字（TRANSCRIPT_CORRECT_CHUNK 可调）
    _chunk_chars = max(100, int(os.getenv("TRANSCRIPT_CORRECT_CHUNK", "250") or 250))
    chunks = _chunks(segments, max_chars=_chunk_chars) if segments else _chunks_from_text(text, _chunk_chars)
    corrected_parts: list[str] = []
    all_applied: list[dict] = []
    failed_chunks = 0
    last_error = ""

    # 分块并发（默认 4 路）：原实现串行，长文案要等 N 倍时间
    from concurrent.futures import ThreadPoolExecutor, as_completed

    blocks = ["\n".join(s.get("text") or "" for s in chunk) for chunk in chunks]

    def _work(block: str):
        if not block.strip():
            return block, [], ""
        try:
            pairs = _ask(block)
        except Exception as exc:  # noqa: BLE001
            return block, [], str(exc)
        fixed, applied = _apply_pairs(block, pairs, glossary=gl, subtitle=sub_ref)
        return fixed, applied, ""

    workers = max(1, min(8, int(os.getenv("TRANSCRIPT_CORRECT_WORKERS", "4") or 4)))
    results: list = [None] * len(blocks)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_work, b): i for i, b in enumerate(blocks)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()

    for fixed, applied, err in results:
        corrected_parts.append(fixed)
        all_applied.extend(applied)
        if err:
            failed_chunks += 1
            last_error = err
            logger.warning("分块纠错失败（保留原文）: %s", err)

    # 标点规范化统计（模型不负责标点，这里只统计与原稿的标点差异）
    corrected = "\n".join(corrected_parts)
    punct = abs(sum(1 for ch in text if ch in "，。！？；：") -
                sum(1 for ch in corrected if ch in "，。！？；："))
    if failed_chunks and failed_chunks == len([c for c in chunks if c]):
        return {"ok": False, "error": f"LLM 调用失败：{last_error}"}
    return {"ok": True, "corrected": corrected,
            "changes": all_applied,
            "punctuationCount": max(0, punct),
            "chunks": len(chunks), "failedChunks": failed_chunks,
            "model": _LAST_SERVED.get("model") or _model(),
            "fallback": bool(_LAST_SERVED.get("fallback")),
            "configuredModel": _model()}
