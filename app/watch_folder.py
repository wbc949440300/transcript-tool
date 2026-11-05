"""监听文件夹：抓包/下载工具导出新视频后，自动入列转写。

用法：
  1. 抓包工具（Res-downloader 等）把视频导出到某个目录
  2. 本模块轮询该目录，发现新文件（大小稳定、未处理过）就创建转写任务
  3. 转写结果进入历史记录，可正常导出

配置（环境变量）：
  TRANSCRIPT_WATCH_DIR       监听目录（为空则不启用）
  TRANSCRIPT_WATCH_INTERVAL  轮询秒数，默认 5
  TRANSCRIPT_WATCH_MODEL     转写模型，默认 medium
  TRANSCRIPT_WATCH_MOVE      1=处理后移动到 processed/ 子目录（默认 1）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .file_transcribe import is_allowed_ext, start_file_task
from .image_import import MAX_IMAGES, is_image_ext, start_image_task
from .models import TaskDetail, TaskStatus
from .subproc import hidden
from .task_store import store
from .utils import gen_task_id

logger = logging.getLogger("watch")

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_flag = threading.Event()
_state: dict = {
    "enabled": False,
    "dir": "",
    "interval": 5.0,
    "processed": 0,
    "last_event": None,
    "recent": [],          # 最近自动导入记录
}
_seen: dict[str, dict] = {}   # path -> {size, mtime}


def _state_file(watch_dir: Path) -> Path:
    return watch_dir / ".transcript_watch_state.json"


def _load_seen(watch_dir: Path) -> None:
    f = _state_file(watch_dir)
    if f.is_file():
        try:
            _seen.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass


def _save_seen(watch_dir: Path) -> None:
    try:
        _state_file(watch_dir).write_text(
            json.dumps(_seen, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _images_enabled() -> bool:
    return os.getenv("TRANSCRIPT_WATCH_IMAGES", "1").strip().lower() not in ("0", "false", "off")


def _scan_once(watch_dir: Path, model: str, move_done: bool) -> None:
    """扫描一次：媒体文件逐个转写；图片按"同一批"合成一个 OCR 任务。

    图片（如抖音图集从 App 保存下来的那一组）落盘后会在同一次扫描里被收集成一批，
    合成一条文案；不同时间丢进来的图片会分属不同批次。
    """
    try:
        entries = list(watch_dir.iterdir())
    except Exception as exc:  # noqa: BLE001
        logger.warning("监听目录读取失败: %s", exc)
        return

    media_ready: list[Path] = []
    image_ready: list[Path] = []
    for f in entries:
        if not f.is_file() or f.name.startswith("."):
            continue
        is_img = is_image_ext(f.name)
        if is_img:
            if not _images_enabled():
                continue
        elif not is_allowed_ext(f.name):
            continue
        try:
            st = f.stat()
        except Exception:
            continue
        key = str(f.resolve())
        prev = _seen.get(key)
        fingerprint = {"size": st.st_size, "mtime": int(st.st_mtime)}
        # 首次看到 → 记录并等下一轮（确认文件已写完）
        if prev is None:
            _seen[key] = fingerprint
            continue
        # 大小仍在变化 → 还在写入，跳过
        if prev.get("size") != st.st_size:
            _seen[key] = fingerprint
            continue
        if prev.get("done"):
            continue
        # 稳定且未处理 → 收进本轮批次（done 标记在真正入列时才打，失败可重试）
        (image_ready if is_img else media_ready).append(f)

    if image_ready:
        image_ready.sort(key=lambda p: (p.stat().st_mtime, p.name))
        batch = image_ready[:MAX_IMAGES]
        if len(image_ready) > MAX_IMAGES:
            logger.warning("一次收到 %d 张图片，本轮先处理前 %d 张（其余下轮继续）",
                           len(image_ready), MAX_IMAGES)
        for f in batch:
            _seen[str(f.resolve())] = {**_seen.get(str(f.resolve()), {}), "done": True}
        _enqueue_images(batch, move_done)

    for f in media_ready:
        _seen[str(f.resolve())] = {**_seen.get(str(f.resolve()), {}), "done": True}
        _enqueue(f, model, move_done)


def _stage_file(src: Path, dst: Path) -> None:
    """把文件"冻结"到 tmp_uploads（转写要读稳定副本，防下载器还在写）。

    **优先硬链接**：同一分区上是零拷贝、瞬时完成 —— 几百 MB 的视频省掉一次全量复制
    和一份临时磁盘占用；跨分区 / 文件系统不支持（Windows 上非 NTFS、或 FAT32 移到
    另一个盘）时回退 `copy2`。
    """
    try:
        os.link(str(src), str(dst))
        return
    except Exception:  # noqa: BLE001
        pass
    shutil.copy2(src, dst)


def _record_fail(name: str, error: str) -> None:
    """记一次导入失败，让页面与告警看得见（以前只在日志里，用户以为文件丢了）。"""
    try:
        with _lock:
            _state["failed"] = ([{"file": name, "error": (error or "")[:160],
                                  "at": time.strftime("%Y-%m-%d %H:%M:%S")}]
                                + (_state.get("failed") or []))[:5]
    except Exception:  # noqa: BLE001
        pass


def _enqueue_images(paths: list[Path], move_done: bool) -> None:
    """把一批图片交给 OCR：一条任务、一份文案。"""
    task_id = gen_task_id()
    tmp_dir = Path("tmp_uploads")
    tmp_dir.mkdir(exist_ok=True)
    names = [p.name for p in paths]
    copies: list[Path] = []
    try:
        for i, p in enumerate(paths):
            dst = tmp_dir / f"{task_id}_{i}{p.suffix.lower()}"
            _stage_file(p, dst)
            copies.append(dst)
        title = names[0] if len(names) == 1 else f"{Path(names[0]).stem} 等 {len(names)} 张图"
        task = TaskDetail(taskId=task_id, taskType="image-ocr", status=TaskStatus.PENDING,
                          errorMessage="监听目录自动导入图片，准备识别...",
                          title=title, platform="图片OCR")
        store.create(task)
        start_image_task(task, copies, names, notify=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("图片导入失败: %s", exc, exc_info=True)
        _record_fail(f"{len(paths)} 张图片", str(exc))
        for p in paths:                      # 撤销 done，下轮扫描自动重试
            key = str(p.resolve())
            if _seen.get(key):
                _seen[key].pop("done", None)
        for c in copies:
            try:
                c.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        return

    with _lock:
        _state["processed"] = int(_state.get("processed") or 0) + 1
        _state["last_event"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state["recent"] = ([{"file": f"{len(paths)} 张图片", "taskId": task_id,
                              "at": _state["last_event"]}] + (_state.get("recent") or []))[:10]
    logger.info("监听目录自动导入图片: %d 张 -> task %s", len(paths), task_id)

    if move_done:
        done_dir = paths[0].parent / "processed"
        try:
            done_dir.mkdir(exist_ok=True)
            for p in paths:
                shutil.move(str(p), str(done_dir / p.name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("移动已处理图片失败: %s", exc)


# ---------- 重复下载去重（视频号同一作品常被下载多次） ----------

def _dedupe_enabled() -> bool:
    return os.getenv("TRANSCRIPT_WATCH_DEDUPE", "1").strip().lower() not in ("0", "false", "off")


def _dedupe_file() -> Path:
    return Path(os.getenv("TRANSCRIPT_WATCH_DEDUPE_FILE", "watch_dedupe.json"))


def _load_dedupe() -> dict:
    f = _dedupe_file()
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_dedupe(data: dict) -> None:
    try:
        items = list(data.items())[-300:]      # 只留最近 300 条
        _dedupe_file().write_text(json.dumps(dict(items), ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("去重记录写入失败: %s", exc)


def media_fingerprint(path: Path) -> str:
    """便宜的内容指纹：文件大小 + 时长。

    不做全文件哈希（视频动辄几百 MB，哈希一次要好几分钟）；同一条视频重复下载
    几乎不会恰好大小和时长都相同，够用。
    """
    try:
        size = path.stat().st_size
    except Exception:  # noqa: BLE001
        return ""
    if not size:
        return ""
    dur = 0.0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, **hidden())
        dur = float((r.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        dur = 0.0
    return f"{size}:{dur:.1f}"


def _enqueue(path: Path, model: str, move_done: bool) -> None:
    task_id = gen_task_id()
    ext = path.suffix.lower()
    key = str(path.resolve())
    tmp_dir = Path("tmp_uploads")
    tmp_dir.mkdir(exist_ok=True)
    dst = tmp_dir / f"{task_id}{ext}"
    try:
        _stage_file(path, dst)

        # ① 重复下载去重：同一条视频（大小+时长一致）已经转写过 → 直接跳过
        fp = media_fingerprint(dst) if _dedupe_enabled() else ""
        if fp:
            prev = _load_dedupe().get(fp)
            if prev:
                logger.info("监听目录跳过重复文件: %s（内容同 task %s，不再重复转写）",
                            path.name, prev)
                with _lock:
                    _state["skipped_duplicate"] = int(_state.get("skipped_duplicate") or 0) + 1
                try:
                    dst.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
                if move_done:
                    try:
                        done_dir = path.parent / "processed"
                        done_dir.mkdir(exist_ok=True)
                        shutil.move(str(path), str(done_dir / path.name))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("移动重复文件失败: %s", exc)
                return

        # ② 视频号：按文件名找回之前记住的标题/作者/封面（下载器是按标题命名的）
        from . import video_channel

        meta = video_channel.match(path.name) or {}
        display = (meta.get("title") or path.name)

        task = TaskDetail(
            taskId=task_id, taskType="file_transcript", status=TaskStatus.PENDING,
            errorMessage="监听目录自动导入，准备转写...",
            title=display, platform="本地文件",
        )
        if meta.get("author"):
            task.uploader = meta["author"]
        if meta.get("cover"):
            task.cover = meta["cover"]
        store.create(task)
        watch_ocr = os.getenv("TRANSCRIPT_WATCH_FRAME_OCR", "1").strip().lower() not in ("0", "false")
        start_file_task(task, dst, model, display, ocr_frames=watch_ocr, notify=True)
        if fp:
            data = _load_dedupe()
            data[fp] = task_id
            _save_dedupe(data)
    except Exception as exc:  # noqa: BLE001
        # 任何一步失败都撤销 done 标记，下轮扫描自动重试（否则文件被永久跳过）
        logger.error("监听导入失败 %s: %s", path.name, exc, exc_info=True)
        _record_fail(path.name, str(exc))
        if _seen.get(key):
            _seen[key]["done"] = False
            _seen[key].pop("done", None)
        try:
            dst.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return

    with _lock:
        _state["processed"] = int(_state.get("processed") or 0) + 1
        _state["last_event"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state["recent"] = ([{"file": path.name, "taskId": task_id,
                              "at": _state["last_event"]}] + (_state.get("recent") or []))[:10]
    logger.info("监听目录自动导入: %s -> task %s", path.name, task_id)

    if move_done:
        done_dir = path.parent / "processed"
        try:
            done_dir.mkdir(exist_ok=True)
            shutil.move(str(path), str(done_dir / path.name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("移动已处理文件失败: %s", exc)


def _loop(watch_dir: Path, interval: float, model: str, move_done: bool) -> None:
    logger.info("开始监听目录: %s (每 %.1fs)", watch_dir, interval)
    while not _stop_flag.is_set():
        try:
            _scan_once(watch_dir, model, move_done)
            _save_seen(watch_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("监听扫描异常: %s", exc)
        _stop_flag.wait(interval)
    logger.info("监听已停止: %s", watch_dir)


def start(watch_dir: str, model: str = "medium", interval: float = 2.0,
          move_done: bool = True) -> dict:
    """启动监听（重复调用会先停止旧的）。配置会记住，服务重启后自动恢复。"""
    global _thread
    stop()
    d = Path(watch_dir).expanduser()
    if not d.is_dir():
        raise ValueError(f"目录不存在: {d}")
    _load_seen(d)
    _stop_flag.clear()
    _thread = threading.Thread(target=_loop, args=(d, interval, model, move_done), daemon=True)
    _thread.start()
    with _lock:
        _state.update({"enabled": True, "dir": str(d), "interval": interval})
    _remember({"dir": str(d), "model": model, "interval": interval, "moveDone": move_done})
    return status()


def stop() -> dict:
    global _thread
    _stop_flag.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3)
    _thread = None
    with _lock:
        _state["enabled"] = False
    _remember({"optOut": True})  # 手动停止 = 明确不要自动监听（不再自动接线）
    return status()


# ---------- 记住/恢复监听配置（服务重启后自动继续，不用每次手点） ----------

def _remember_file() -> Path:
    return Path(os.getenv("TRANSCRIPT_WATCH_REMEMBER_FILE", "watch_autostart.json"))


def _remember(cfg: Optional[dict]) -> None:
    try:
        if cfg is None:
            _remember_file().unlink(missing_ok=True)
        else:
            _remember_file().write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存监听配置失败: %s", exc)


def _remember_cfg() -> Optional[dict]:
    f = _remember_file()
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def resume_last() -> dict:
    """服务启动时恢复上次开启的监听（用户手动停过则只有 optOut 标记）。"""
    cfg = _remember_cfg()
    if not cfg or not cfg.get("dir"):
        return status()
    try:
        d = cfg["dir"]
        if not Path(d).is_dir():
            logger.warning("上次的监听目录不存在，跳过自动恢复: %s", d)
            return status()
        start(d, model=cfg.get("model", "medium"),
              interval=float(cfg.get("interval", 2)),
              move_done=bool(cfg.get("moveDone", True)))
        logger.info("已恢复上次的监听目录: %s", d)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动恢复监听失败: %s", exc)
    return status()


def default_dir(create: bool = False) -> Path:
    """未显式指定监听目录时的默认位置：项目旁的 wx_downloads。

    2026-11-05：外部下载器移除后，默认目录不再依赖它的安装配置；
    位置沿用原来的 wx_downloads，老用户不用改习惯。
    """
    raw = (os.getenv("TRANSCRIPT_WATCH_DIR", "") or "").strip()
    d = Path(raw) if raw else Path(__file__).resolve().parent.parent.parent / "wx_downloads"
    if create:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            logger.warning("创建默认监听目录失败: %s", d)
    return d


def status() -> dict:
    with _lock:
        st = dict(_state)
    try:
        st["usage"] = dir_usage()          # 监听目录占用（视频号原片会越攒越多）
    except Exception:  # noqa: BLE001
        st["usage"] = {}
    return st


# ---------- 本地视频库（监听目录 + processed） ----------

def current_dir(create: bool = False) -> Optional[Path]:
    """当前监听目录（供"打开监听目录"按钮与抖音图集提示用）。

    与 _media_base_dir 的区别：不要求目录已存在；create=True 时顺带建出来。
    取值顺序：正在监听的目录 → 上次记住的目录 → 无（返回 None，调用方自行兜底）。
    """
    raw = _state.get("dir") or ""
    if not raw:
        raw = (_remember_cfg() or {}).get("dir") or ""
    if not raw:
        return None
    d = Path(raw).expanduser()
    if create:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
    return d


_CLEAN_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".flv", ".m4v", ".avi",
                   ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma")


def _dir_usage(root: Path) -> tuple[int, int]:
    files = 0
    size = 0
    try:
        for f in root.iterdir():
            if f.is_file():
                files += 1
                try:
                    size += f.stat().st_size
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return files, size


def dir_usage() -> dict:
    """监听目录与 processed/ 的占用（页面显示 + 磁盘告警口径）。"""
    base = current_dir() or _media_base_dir()
    if base is None:
        return {"dir": "", "inboxFiles": 0, "inboxBytes": 0,
                "processedFiles": 0, "processedBytes": 0}
    inbox_f, inbox_b = _dir_usage(base)
    proc_f, proc_b = _dir_usage(base / "processed")
    return {"dir": str(base), "inboxFiles": inbox_f, "inboxBytes": inbox_b,
            "processedFiles": proc_f, "processedBytes": proc_b}


def sweep_processed(days: Optional[int] = None) -> int:
    """按天清理 processed/ 里的旧媒体（**默认关闭**：`TRANSCRIPT_WATCH_KEEP_DAYS=0`）。

    默认不清理是有意的：视频号原片是用户的素材，工具不替用户决定删。
    要自动清理就设天数（例如 30），启动时执行一次。
    """
    if days is None:
        try:
            days = int(os.getenv("TRANSCRIPT_WATCH_KEEP_DAYS", "0") or 0)
        except Exception:  # noqa: BLE001
            days = 0
    if days <= 0:
        return 0
    base = current_dir() or _media_base_dir()
    if base is None:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for f in (base / "processed").glob("*"):
        try:
            if (f.is_file() and f.suffix.lower() in _CLEAN_SUFFIXES
                    and f.stat().st_mtime < cutoff):
                f.unlink(missing_ok=True)
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    if removed:
        logger.info("清理 processed/ 旧媒体 %d 个（早于 %d 天）", removed, days)
    return removed


def clean_processed(days: int = 0) -> dict:
    """手动清理 processed/：days>0 只删早于 N 天的；days=0 全删。返回统计。"""
    base = current_dir() or _media_base_dir()
    if base is None:
        return {"removed": 0, "freedBytes": 0, "dir": ""}
    cutoff = (time.time() - days * 86400) if days > 0 else None
    removed = 0
    freed = 0
    for f in (base / "processed").glob("*"):
        try:
            if not f.is_file():
                continue
            st = f.stat()
            if cutoff is not None and st.st_mtime >= cutoff:
                continue
            f.unlink(missing_ok=True)
            removed += 1
            freed += st.st_size
        except Exception:  # noqa: BLE001
            continue
    if removed:
        logger.info("手动清理 processed/：删除 %d 个（释放 %.1f MB）", removed, freed / 1048576)
    return {"removed": removed, "freedBytes": freed, "dir": str(base / "processed")}


def _media_base_dir() -> Optional[Path]:
    """当前监听目录；未启用时回退到上次记住的配置。"""
    raw = _state.get("dir") or ""
    if not raw:
        cfg = _remember_cfg() or {}
        raw = cfg.get("dir") or ""
    if not raw:
        return None
    d = Path(raw).expanduser()
    return d if d.is_dir() else None


def list_media(limit: int = 100) -> dict:
    """列出监听目录及 processed 子目录里的音视频文件（不递归）。"""
    base = _media_base_dir()
    if base is None:
        return {"dir": "", "files": []}
    items = []
    for scope, root in (("processed", base / "processed"), ("inbox", base)):
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except Exception:  # noqa: BLE001
            continue
        for f in entries:
            if not f.is_file() or not is_allowed_ext(f.name):
                continue
            try:
                st = f.stat()
            except Exception:  # noqa: BLE001
                continue
            items.append({
                "name": f.name,
                "scope": scope,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"dir": str(base), "files": items[:max(1, int(limit))]}


def resolve_media(name: str, scope: str = "processed") -> Optional[Path]:
    """把本地视频库的文件名解析成真实路径（只允许监听目录/processed 的直接子文件）。"""
    base = _media_base_dir()
    if base is None:
        return None
    raw = (name or "").strip()
    safe = Path(raw).name
    if not raw or safe != raw:
        return None
    root = (base / "processed") if scope == "processed" else base
    try:
        root = root.resolve()
        p = (root / safe).resolve()
    except Exception:  # noqa: BLE001
        return None
    if root not in p.parents or not p.is_file() or not is_allowed_ext(p.name):
        return None
    return p


def autostart_from_env() -> None:
    """服务启动时按环境变量自动开启监听。"""
    d = os.getenv("TRANSCRIPT_WATCH_DIR", "").strip()
    if not d:
        return
    try:
        model = os.getenv("TRANSCRIPT_WATCH_MODEL", "medium")
        interval = float(os.getenv("TRANSCRIPT_WATCH_INTERVAL", "2"))
        move_done = os.getenv("TRANSCRIPT_WATCH_MOVE", "1") not in ("0", "false")
        start(d, model=model, interval=interval, move_done=move_done)
        logger.info("按环境变量自动监听: %s", d)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动监听启动失败: %s", exc)
