from __future__ import annotations

import asyncio
import logging
import json
import mimetypes
import os
import re
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .batch import start_batch
from .extractor import start_async
from .export import export_tasks, export_tasks_zip
from .file_transcribe import is_allowed_ext, start_file_task
from .history import history_store
from .links import LinkError, normalize
from .llm_correct import correct_transcript, llm_configured
from .note_writer import generate_note, note_available
from .models import (
    BatchCreated,
    BatchCreatedData,
    BatchQueryResponse,
    BatchResult,
    CreateBatchRequest,
    CreateOcrTaskRequest,
    CreateTaskRequest,
    OcrResult,
    OcrResultData,
    QueryTaskResponse,
    TaskCreated,
    TaskCreatedData,
    TaskDetail,
    TaskNode,
    TaskStatus,
    TextConvertRequest,
    ExternalDownloadRequest,
)
from .ocr import ocr_image
from .task_store import store
from .utils import (detect_platform, gen_task_id, http_opener,
                    platform_cooldown_enabled)
from . import watch_folder

logger = logging.getLogger("api")
router = APIRouter()

TMP_DIR = Path("tmp_uploads")
TMP_DIR.mkdir(exist_ok=True)


@router.post("/transcript/doTask", response_model=TaskCreated)
async def create_task(req: CreateTaskRequest):
    try:
        # 短链跳转是网络请求，放线程里，避免阻塞事件循环
        url, _platform = await asyncio.to_thread(normalize, req.workUrl)
    except LinkError as exc:
        # 视频号：微信无网页可读接口；分享链接也不是下载器 API 能吃的资源 URL。
        raw = (req.workUrl or "").strip()
        if ("weixin.qq.com/sph" in raw) or ("channels.weixin.qq.com" in raw):
            # 视频流拿不到（只有微信客户端能播），但**元数据**能取：
            # 用 finder-preview 的 get_feed_info 拿标题/作者/封面，顺带记住，
            # 等文件经下载器落到监听目录时替换"文件名当标题"（2026-10-31 加）
            from . import video_channel

            meta: dict = {}
            try:
                uri = video_channel.short_uri_from(raw)
                if uri:
                    meta = await asyncio.to_thread(video_channel.fetch_feed_info, uri)
                    if meta:
                        meta["short_uri"] = uri
                        video_channel.remember(meta)
            except Exception as exc:  # noqa: BLE001
                logger.info("视频号元数据获取失败（忽略）: %s", exc)
            head = ""
            if meta:
                bits = [b for b in (meta.get("title"), meta.get("author")) if b]
                if bits:
                    head = "已取到该视频号作品：" + " / ".join(bits) + "。"
                if meta.get("cover"):
                    head += "封面也已记下（文件落地后会自动带上）。"
            return JSONResponse(status_code=400, content={
                "code": 500,
                "msg": head + "视频号链接不能直接解析，也不能直接提交给下载器 API。"
                       "请在微信里打开视频号视频并播放，点击下载器注入的下载按钮；"
                       "文件落到下载目录后本工具会自动转写（标题/作者/封面会按上面记下的填好）。"
                       "（视频号接口要求视频号页面保持打开，否则会提示“请先初始化客户端 socket 连接”。）"
                       "也可以抓包导出到「自动监听文件夹」。",
                "data": meta or None,
            })
        return JSONResponse(status_code=400, content={"code": 500, "msg": str(exc), "data": None})

    task_id = gen_task_id()
    from . import scheduler

    if not scheduler.can_accept(url):
        return JSONResponse(status_code=429, content={
            "code": 429,
            "msg": "该平台排队已满（每个平台最多排队 TRANSCRIPT_QUEUE_MAX 条），请稍后再试。",
            "data": None,
        })
    task = TaskDetail(
        taskId=task_id,
        taskType="transcript",
        status=TaskStatus.PENDING,
        errorMessage="作品内容提取中...",
        platform=detect_platform(url),
        webpageUrl=url,
    )
    store.create(task)
    want_audio = req.type == "audio"
    ocr_frames = req.ocrFrames
    if ocr_frames is None:
        # 没显式指定 → 按平台决定：抖音/快手/小红书没有平台字幕，默认开画面文字 OCR
        from .platforms import frame_ocr_default_for

        ocr_frames = frame_ocr_default_for(url)
    start_async(task, url, want_audio, req.whisperModel, ocr_frames=bool(ocr_frames))
    return TaskCreated(data=TaskCreatedData(taskId=task_id))


@router.post("/transcript/batch/createTask", response_model=BatchCreated)
async def create_batch(req: CreateBatchRequest):
    urls = [u.strip() for u in (req.workUrls or []) if u and u.strip()]
    if not urls:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "请至少输入一条链接", "data": None})
    from . import scheduler

    if not scheduler.can_accept_many(urls):
        return JSONResponse(status_code=429, content={
            "code": 429,
            "msg": "该平台排队已满（每个平台最多排队 TRANSCRIPT_QUEUE_MAX 条），请减少条数或稍后再试。",
            "data": None,
        })
    batch_id = gen_task_id()
    start_batch(batch_id, urls, req.whisperModel)
    return BatchCreated(data=BatchCreatedData(batchId=batch_id, total=len(urls)))


@router.get("/transcript/batch/queryTask", response_model=BatchQueryResponse)
async def query_batch(batchId: Optional[str] = None):
    if not batchId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少请求参数: batchId", "data": None})
    items = store.get_batch(batchId)
    if items is None:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "任务不存在", "data": None})
    nodes: list[TaskNode] = []
    done = success = failed = 0
    all_finished = True
    for it in items:
        s = it.status
        if s == TaskStatus.SUCCESS:
            success += 1; done += 1
        elif s == TaskStatus.FAILURE:
            failed += 1; done += 1
        else:
            all_finished = False
        nodes.append(TaskNode(
            taskId=it.taskId, url=it.webpageUrl, status=s, errorMessage=it.errorMessage,
            title=it.title, textContent=it.textContent, content=it.content,
            platform=it.platform, cover=it.cover, videoUrl=it.videoUrl, duration=it.duration,
        ))
    status = TaskStatus.SUCCESS if all_finished and items else (TaskStatus.RUNNING if items else TaskStatus.FAILURE)
    return BatchQueryResponse(data=BatchResult(
        batchId=batchId, status=status, done=done, success=success,
        failed=failed, total=len(items), items=nodes,
    ))


@router.get("/transcript/queryTask", response_model=QueryTaskResponse)
async def query_task(taskId: Optional[str] = None):
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少请求参数: taskId", "data": None})
    task = store.get(taskId)
    if task is None:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "任务不存在", "data": None})
    return QueryTaskResponse(data=task)


@router.post("/transcript/image/ocr/doTask", response_model=OcrResult)
async def ocr_do_task(req: CreateOcrTaskRequest):
    """图片文案提取：支持直接传图 URL。"""
    url = (req.sourceUrl or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse(status_code=400, content={"code": 500, "msg": "请提供有效的图片链接", "data": None})
    try:
        import urllib.request
        req_ = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with http_opener(media=True).open(req_, timeout=20) as resp:
            data = resp.read()
        text = ocr_image(data)
        task_id = gen_task_id()
        _save_ocr_history(task_id, req.scene, url, text, TaskStatus.SUCCESS)
        return OcrResult(data=OcrResultData(
            taskId=task_id, status=TaskStatus.SUCCESS, text=text,
            imageUrl=url, scene=req.scene,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.error("OCR URL 失败: %s", exc)
        return OcrResult(data=OcrResultData(
            taskId=gen_task_id(), status=TaskStatus.FAILURE, text="",
            imageUrl=url, scene=req.scene,
        ), code=500, msg=str(exc))


@router.post("/transcript/image/ocr/upload")
async def ocr_upload(file: UploadFile = File(...)):
    """图片文案提取：上传图片文件。"""
    data = await file.read()
    if not data:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "文件为空", "data": None})
    try:
        text = ocr_image(data)
        task_id = gen_task_id()
        _save_ocr_history(task_id, "image-ocr", None, text, TaskStatus.SUCCESS, title=file.filename)
        return OcrResult(data=OcrResultData(
            taskId=task_id, status=TaskStatus.SUCCESS, text=text,
            scene=file.filename,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.error("OCR 上传失败: %s", exc)
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"OCR 失败: {exc}", "data": None})


@router.post("/file/transcript/doTask", response_model=TaskCreated)
async def file_transcript_do_task(file: UploadFile = File(...), whisperModel: str = "tiny",
                                  ocrFrames: str = Form("")):
    """上传本地视频/录音转文字（异步任务式）。"""
    if not (file and file.filename):
        return JSONResponse(status_code=400, content={"code": 500, "msg": "未选择文件", "data": None})
    if not is_allowed_ext(file.filename):
        return JSONResponse(status_code=400, content={"code": 500, "msg": "不支持的文件格式", "data": None})
    data = await file.read()
    if not data:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "文件为空", "data": None})
    task_id = gen_task_id()
    tmp = TMP_DIR / f"{task_id}{Path(file.filename).suffix.lower()}"
    tmp.write_bytes(data)
    task = TaskDetail(
        taskId=task_id, taskType="file_transcript", status=TaskStatus.PENDING,
        errorMessage="准备转写...", title=file.filename, platform="本地文件",
    )
    store.create(task)
    start_file_task(task, tmp, whisperModel, file.filename,
                    ocr_frames=(ocrFrames or '').strip().lower() in ('1', 'true', 'on'))
    return TaskCreated(data=TaskCreatedData(taskId=task_id))


def _save_ocr_history(task_id: str, scene: str, image_url: Optional[str],
                      text: str, status: TaskStatus, title: Optional[str] = None) -> None:
    """把 OCR 结果写入历史记录。"""
    t = TaskDetail(
        taskId=task_id, taskType="image-ocr", status=status,
        title=title or ("图片文案提取" + (" / " + scene if scene else "")),
        textContent=text, platform="图片OCR",
        webpageUrl=image_url,
        errorMessage=None if status == TaskStatus.SUCCESS else "OCR 失败",
    )
    store.create(t)
    store.update_status(task_id, status)


# ---------- 历史记录 API ----------

@router.get("/history/list")
async def history_list(keyword: Optional[str] = None, status: Optional[str] = None,
                       page: int = 1, pageSize: int = 20):
    page = max(1, page if isinstance(page, int) else 1)
    pageSize = max(1, min(100, pageSize if isinstance(pageSize, int) else 20))
    offset = (page - 1) * pageSize
    items = history_store.list(keyword=keyword, status=status, limit=pageSize, offset=offset)
    total = history_store.count(keyword=keyword, status=status)
    return {"code": 200, "msg": "操作成功",
            "data": {"list": items, "total": total, "page": page, "pageSize": pageSize}}


@router.get("/history/detail")
async def history_detail(taskId: Optional[str] = None):
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少 taskId", "data": None})
    row = history_store.get(taskId)
    if row is None:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "记录不存在", "data": None})
    return {"code": 200, "msg": "操作成功", "data": row}


@router.delete("/history/delete")
async def history_delete(taskId: Optional[str] = None):
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少 taskId", "data": None})
    ok = history_store.delete(taskId)
    return {"code": 200 if ok else 500, "msg": "已删除" if ok else "记录不存在",
            "data": {"deleted": ok}}


@router.delete("/history/clear")
async def history_clear():
    n = history_store.clear()
    return {"code": 200, "msg": f"已清空 {n} 条记录", "data": {"cleared": n}}


class ExportTasksRequest(BaseModel):
    taskIds: list[str]
    format: str = "txt"
    lang: str = "zh"
    corrected: int = 0
    plain: int = 0   # 1 = 每个文件只含转写正文（无标题/简介等元数据）


def _open_in_file_manager(path: str) -> None:
    """跨平台打开目录/文件：Windows 用资源管理器，macOS 用 Finder，Linux 用 xdg-open。"""
    import subprocess
    import sys

    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _file_response(content: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/archive/list")
async def archive_list(limit: int = 8):
    """文案输出文件夹里最近的归档文件（视频号页"最新文案"区块用）。"""
    import asyncio

    from .archive import archive_dir

    def _list():
        d = archive_dir()
        if not d.is_dir():
            return {"dir": "", "items": []}
        files = sorted(d.glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)
        items = []
        for p in files[: max(1, min(int(limit), 50))]:
            st = p.stat()
            items.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
        return {"dir": str(d), "items": items}

    data = await asyncio.to_thread(_list)
    return {"code": 200, "msg": "操作成功", "data": data}


def _valid_archive_name(name: str) -> bool:
    return bool(name) and name.endswith(".txt") and ".." not in name \
        and not any(c in name for c in '\\/:*?"<>|\r\n')


@router.get("/archive/content")
async def archive_content(name: str = ""):
    """读一个归档 txt 的正文（前端"复制"按钮用）。"""
    import asyncio

    from .archive import archive_dir

    if not _valid_archive_name(name):
        return JSONResponse(status_code=400, content={"code": 500, "msg": "文件名不合法", "data": None})
    p = archive_dir() / name
    if not p.is_file():
        return JSONResponse(status_code=404, content={"code": 500, "msg": "文件不存在", "data": None})

    def _read():
        return p.read_text(encoding="utf-8-sig")

    text = await asyncio.to_thread(_read)
    return {"code": 200, "msg": "操作成功", "data": {"name": name, "text": text}}


@router.get("/archive/file")
async def archive_file(name: str = ""):
    """下载一个归档 txt（中文文件名走 RFC5987）。"""
    import asyncio

    from .archive import archive_dir

    if not _valid_archive_name(name):
        return JSONResponse(status_code=400, content={"code": 500, "msg": "文件名不合法", "data": None})
    p = archive_dir() / name
    if not p.is_file():
        return JSONResponse(status_code=404, content={"code": 500, "msg": "文件不存在", "data": None})
    data = await asyncio.to_thread(p.read_bytes)
    return _file_response(data, name, "text/plain; charset=utf-8")


@router.post("/archive/open")
async def archive_open():
    """在资源管理器里打开 文案输出/ 文件夹（本机工具的便利功能）。"""
    import asyncio
    import os

    from .archive import archive_dir

    d = archive_dir()
    d.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_open_in_file_manager, str(d))
    return {"code": 200, "msg": "已打开", "data": {"dir": str(d)}}


@router.get("/ai-face/exportTask")
async def export_task(taskId: Optional[str] = None, format: str = "txt", lang: str = "zh",
                      corrected: int = 0, plain: int = 0):
    """导出单个任务结果（txt / markdown / docx / excel）；plain=1 仅导出转写正文。"""
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "暂无可导出的任务", "data": None})
    try:
        content, filename, media_type = export_tasks(
            [taskId], format, lang, source="corrected" if corrected else "original",
            plain=bool(plain))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 500, "msg": str(exc), "data": None})
    return _file_response(content, filename, media_type)


@router.post("/ai-face/exportTasks")
async def export_tasks_batch(req: ExportTasksRequest):
    """批量导出：每个任务一个文件，打包为 ZIP；plain=1 仅导出转写正文。"""
    try:
        content, filename, media_type = export_tasks_zip(
            req.taskIds, req.format, req.lang,
            source="corrected" if req.corrected else "original",
            plain=bool(req.plain))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 500, "msg": str(exc), "data": None})
    return _file_response(content, filename, media_type)


# ---------- 媒体代理（解决防盗链：浏览器无法携带 Referer） ----------

_PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_PLATFORM_REFERER = {
    "抖音": "https://www.douyin.com/",
    "快手": "https://www.kuaishou.com/",
    "小红书": "https://www.xiaohongshu.com/",
    "B站": "https://www.bilibili.com/",
    "TikTok": "https://www.tiktok.com/",
}


_BILI_AUDIO_URL = re.compile(r"-1-(?:30216|30232|30280|30250)\.m4s", re.I)
_AUDIO_URL_HINT = re.compile(r"(?:mime=audio|itag=(?:139|140|141|256|258|325|328))", re.I)


def _is_audio_only_url(u: str) -> bool:
    """旧记录可能把纯音频流排在前面（B站 302xx / mime=audio）。"""
    return bool(_BILI_AUDIO_URL.search(u or "") or _AUDIO_URL_HINT.search(u or ""))


def _resolve_media_url(task_id: str, index: int) -> tuple[Optional[str], str, str]:
    """从历史记录/内存任务里取媒体直链，返回 (url, referer, platform)。"""
    urls: list[str] = []
    platform = ""
    row = history_store.get(task_id)
    if row:
        platform = row.get("platform") or ""
        try:
            extra = json.loads(row.get("extra") or "{}")
            urls = [u for u in (extra.get("videoUrlList") or []) if u]
        except Exception:  # noqa: BLE001
            urls = []
    if not urls:
        t = store.get(task_id)
        if t is not None:
            platform = platform or (t.platform or "")
            urls = [u for u in (getattr(t, "videoUrlList", None) or []) if u]
            if not urls and getattr(t, "videoUrl", None):
                urls = [t.videoUrl]
    if not urls:
        return None, "", ""
    # 兼容旧记录：跳过纯音频，避免预览黑屏（新记录的 videoUrlList 已只收视频流）
    filtered = [u for u in urls if not _is_audio_only_url(u)]
    if filtered:
        urls = filtered
    idx = max(0, min(index, len(urls) - 1))
    return urls[idx], _PLATFORM_REFERER.get(platform, ""), platform


def _serve_local_media(task_id: str, name: str, download: int = 0):
    """服务任务目录里的本地预览视频（extractor/文件任务生成的 video.<ext>）。

    FileResponse 自带 Range 支持（starlette ≥0.36），进度条可拖动。
    """
    m = re.fullmatch(r"video\.(mp4|webm|mkv|mov|m4v)", name or "")
    if not m or not re.fullmatch(r"[0-9A-Za-z_-]+", task_id or ""):
        return JSONResponse(status_code=400, content={"code": 500, "msg": "参数不合法", "data": None})
    path = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks")) / task_id / name
    if not path.is_file():
        return JSONResponse(
            status_code=404,
            content={"code": 500, "msg": "本地预览视频不存在（可能已被清理或任务未生成）", "data": None})
    media_type = {"mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
                  "mkv": "video/x-matroska", "mov": "video/quicktime"}[m.group(1)]
    headers: dict = {"Accept-Ranges": "bytes"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="video_{task_id}.{m.group(1)}"'
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/media/proxy")
def media_proxy(request: Request, taskId: str = "", index: int = 0, download: int = 0):
    """代取视频/音频流：服务端带上 Referer/UA 拉流，浏览器即可预览与保存。

    支持 Range 透传（可拖动进度条）；download=1 时附带附件头直接下载。
    videoUrlList 里的 file:// 项表示任务目录里的本地预览视频，直接走本地文件。
    """
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少 taskId", "data": None})
    url, referer, platform = _resolve_media_url(taskId, index)
    if not url:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "该任务没有可用的媒体直链", "data": None})
    if url.startswith("file://"):
        return _serve_local_media(taskId, url[len("file://"):], download=download)

    # B站 CDN 对桌面 Chrome UA + Range 会直接断连（实测），媒体请求用简单 UA
    ua = "Mozilla/5.0" if platform == "B站" else _PROXY_UA
    headers = {"User-Agent": ua, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    try:
        upstream = http_opener(media=True).open(
            urllib.request.Request(url, headers=headers), timeout=30)
    except Exception as exc:  # noqa: BLE001
        logger.warning("媒体代理拉流失败: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"code": 500, "msg": f"媒体源暂不可用（可能已过期）：{exc}", "data": None})

    out: dict = {"Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes")}
    for h in ("Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            out[h] = upstream.headers[h]
    if download:
        out["Content-Disposition"] = f'attachment; filename="video_{taskId}.mp4"'
    media_type = upstream.headers.get("Content-Type") or ""
    # B站 CDN 对 .m4s 返回 application/octet-stream，浏览器 <video> 不会解码；补成 video/mp4
    if media_type.split(";")[0].strip().lower() in ("", "application/octet-stream", "binary/octet-stream"):
        low = (url or "").lower()
        if ".m4a" in low:
            media_type = "audio/mp4"
        elif ".webm" in low:
            media_type = "video/webm"
        else:
            media_type = "video/mp4"

    return StreamingResponse(upstream, status_code=upstream.status,
                             media_type=media_type, headers=out)


@router.get("/media/cover")
def media_cover(taskId: str = ""):
    """代取封面图（补 Referer/UA，绕过平台防盗链）。"""
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少 taskId", "data": None})
    cover, platform = "", ""
    row = history_store.get(taskId)
    if row:
        cover = row.get("cover") or ""
        platform = row.get("platform") or ""
    if not cover:
        t = store.get(taskId)
        if t is not None:
            cover = getattr(t, "cover", None) or ""
            platform = platform or (t.platform or "")
    if not cover:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "该任务没有封面", "data": None})
    headers = {"User-Agent": "Mozilla/5.0" if platform == "B站" else _PROXY_UA,
               "Accept": "image/*,*/*"}
    ref = _PLATFORM_REFERER.get(platform)
    if ref:
        headers["Referer"] = ref
    try:
        upstream = http_opener(media=True).open(
            urllib.request.Request(cover, headers=headers), timeout=20)
        data = upstream.read()
        media_type = upstream.headers.get("Content-Type") or "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        logger.warning("封面代理失败: %s", exc)
        return JSONResponse(status_code=502, content={"code": 500, "msg": f"封面获取失败：{exc}", "data": None})
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------- 刷新媒体直链（抖音/快手的直链几小时过期） ----------

class RefreshMediaRequest(BaseModel):
    taskId: str


@router.post("/transcript/refreshMedia")
async def refresh_media(req: RefreshMediaRequest):
    """刷新已保存的媒体直链：抖音/快手直链几小时就过期，过期后预览会黑屏/保存失败。

    只重新取一次详情（抖音 = 1 次页面访问，走平台节流与冷却判定），**不重跑转写**；
    成功后写回历史记录与内存任务，前端拿到新直链即可继续预览/截图/保存。
    """
    row = history_store.get(req.taskId)
    if row is None:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "记录不存在", "data": None})
    src_url = (row.get("webpage_url") or "").strip()
    if not src_url:
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": "该记录没有原始链接，无法刷新直链", "data": None})

    def _refresh() -> dict:
        from .extractor import _douyin_stream_urls
        from .platforms import adapter_for

        adapter = adapter_for(src_url)
        if adapter.key not in ("douyin", "kuaishou") or adapter.fetch is None:
            raise LinkError("该平台不需要刷新直链（只有抖音/快手的直链会几小时过期）")
        d = adapter.fetch(src_url)
        if adapter.key == "douyin":
            _asr, urls = _douyin_stream_urls(d)
            urls = (urls or ([d["video_url"]] if d.get("video_url") else []))[:8]
            dur = d.get("duration")
            dur = (float(dur) / 1000.0) if dur else None      # 抖音是毫秒
        else:
            urls = list(d.get("video_urls") or ([d["video_url"]] if d.get("video_url") else []))[:8]
            dur = d.get("duration")
        return {"platform": adapter.label, "urls": urls, "title": d.get("title"),
                "cover": d.get("cover"), "duration": dur}

    try:
        res = await asyncio.to_thread(_refresh)
    except LinkError as exc:
        return JSONResponse(status_code=400, content={"code": 500, "msg": str(exc), "data": None})
    except Exception as exc:  # noqa: BLE001
        from .extractor import _humanize_error

        logger.warning("刷新直链失败 %s: %s", req.taskId, exc)
        return JSONResponse(status_code=502, content={
            "code": 500,
            "msg": f"刷新失败：{_humanize_error(exc, row.get('platform') or '')}",
            "data": None})

    urls = [u for u in (res.get("urls") or []) if str(u).startswith("http")]
    if not urls:
        return JSONResponse(status_code=502, content={
            "code": 500, "msg": "本次没有取到可用的直链（作品可能已失效，或平台未返回）", "data": None})

    history_store.update_extra(req.taskId, {"videoUrlList": urls, "videoUrl": urls[0]})
    history_store.update_fields(req.taskId, {"cover": res.get("cover"),
                                             "duration": res.get("duration")})
    t = store.get(req.taskId)
    if t is not None:
        t.videoUrlList = urls
        t.videoUrl = urls[0]
        if res.get("cover"):
            t.cover = res["cover"]
    return {"code": 200, "msg": f"已刷新 {len(urls)} 条直链",
            "data": {"platform": res.get("platform"), "videoUrl": urls[0],
                     "videoUrlList": urls, "cover": res.get("cover"),
                     "duration": res.get("duration")}}


# ---------- 结构化笔记（map-reduce，不截断转录） ----------

class NoteRequest(BaseModel):
    taskId: str


@router.post("/ai/note")
async def ai_note(req: NoteRequest):
    """把已提取的文案整理成结构化 Markdown 笔记。"""
    if not note_available():
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": "LLM 未配置", "data": None})
    row = history_store.get(req.taskId)
    if row is None:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "记录不存在", "data": None})
    if not (row.get("text_content") or "").strip():
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": "该记录没有口播文案，无法生成笔记", "data": None})
    import asyncio
    try:
        res = await asyncio.to_thread(generate_note, dict(row))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500,
                            content={"code": 500, "msg": f"生成失败：{exc}", "data": None})
    if not res.get("ok"):
        return JSONResponse(status_code=500,
                            content={"code": 500, "msg": res.get("error") or "生成失败", "data": res})
    history_store.update_extra(req.taskId, {
        "note_markdown": res.get("markdown") or "",
        "note_model": res.get("model"),
        "note_chunks": res.get("chunks"),
    })
    return {"code": 200, "msg": "操作成功", "data": {
        "markdown": res["markdown"], "chunks": res.get("chunks"),
        "model": res.get("model"), "keyPoints": res.get("keyPoints"),
        "quotes": res.get("quotes"), "terms": res.get("terms"),
    }}


@router.get("/ai/note/download")
async def ai_note_download(taskId: Optional[str] = None):
    """下载已生成的结构化笔记（Markdown）。"""
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少 taskId", "data": None})
    row = history_store.get(taskId)
    if row is None:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "记录不存在", "data": None})
    try:
        extra = json.loads(row.get("extra") or "{}")
    except Exception:  # noqa: BLE001
        extra = {}
    md = extra.get("note_markdown") or ""
    if not md.strip():
        return JSONResponse(status_code=404, content={"code": 500, "msg": "尚未生成笔记", "data": None})
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", (row.get("title") or "note"))[:40] or "note"
    # HTTP 头只能是 latin-1：ASCII 兜底名 + RFC5987 的 UTF-8 文件名
    ascii_name = f"note_{taskId[:12]}.md"
    utf8_name = quote(f"{safe}_note.md")
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'},
    )


_CONVERTERS: dict = {}


def _opencc(kind: str):
    """缓存 OpenCC 实例（纯 Python 实现，无原生依赖）。"""
    from opencc import OpenCC

    if kind not in _CONVERTERS:
        _CONVERTERS[kind] = OpenCC(kind)
    return _CONVERTERS[kind]


@router.post("/text/convert")
async def text_convert(req: TextConvertRequest):
    """简繁转换：to=s 转简体（默认），to=t 转繁体。用于结果页的简/繁切换。"""
    import asyncio

    text = req.text or ""
    if not text.strip():
        return {"code": 200, "msg": "操作成功", "data": {"text": "", "to": "s"}}
    to = (req.to or "s").lower()
    kind = "s2t" if to in ("t", "trad", "traditional", "繁体") else "t2s"
    try:
        out = await asyncio.to_thread(lambda: _opencc(kind).convert(text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("简繁转换失败: %s", exc)
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"转换失败：{exc}", "data": None})
    return {"code": 200, "msg": "操作成功", "data": {"text": out, "to": "t" if kind == "s2t" else "s"}}


@router.get("/ext/status")
async def ext_status():
    """外部下载器（wx_channels_download）状态：是否在线、下载目录、平台可用性。"""
    import asyncio

    from . import external_dl

    info = await asyncio.to_thread(external_dl.probe)
    return {"code": 200, "msg": "操作成功", "data": info}


@router.get("/ext/tasks")
async def ext_tasks(limit: int = 6):
    """外部下载器最近的视频号任务（只读监控，不触发重试）。"""
    import asyncio

    from . import external_dl

    data = await asyncio.to_thread(external_dl.list_video_channel_tasks, limit)
    return {"code": 200, "msg": "操作成功", "data": data}


@router.post("/ext/setup")
async def ext_setup():
    """安装外部下载器（幂等）：已装只校正便携配置；未装用本地包/GitHub 下载自举。"""
    import asyncio

    from . import external_dl

    try:
        info = await asyncio.to_thread(external_dl.install)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={
            "code": 500, "msg": f"安装失败：{exc}",
            "data": {"hint": "可把安装包 zip 放到 wx_dl_pkg/ 目录后重试（离线安装）"}})
    return {"code": 200, "msg": "操作成功", "data": info}


@router.post("/ext/start")
async def ext_start():
    """启动外部下载器（弹 UAC 提权：装证书/系统代理需要管理员）。"""
    import asyncio

    from . import external_dl

    res = await asyncio.to_thread(external_dl.start)
    ok = bool(res.get("started"))
    return {"code": 200 if ok else 500,
            "msg": "下载器已启动" if ok else (res.get("error") or "启动失败"),
            "data": res}


@router.post("/ext/stop")
async def ext_stop():
    """停止外部下载器（进程以管理员运行，停止也需要过一次 UAC）。"""
    import asyncio

    from . import external_dl

    res = await asyncio.to_thread(external_dl.stop)
    return {"code": 200 if res.get("stopped") else 500,
            "msg": "下载器已停止" if res.get("stopped") else (res.get("error") or "停止失败"),
            "data": res}


@router.post("/ext/proxy/restore")
async def ext_proxy_restore():
    """还原系统代理：下载器 MITM 残留导致"全机断网"时的救命按钮。

    正常情况不需要点：服务启动会做代理体检，服务退出时也会自动还原。
    """
    import asyncio

    from . import external_dl

    res = await asyncio.to_thread(external_dl.restore_proxy)
    return {"code": 200, "msg": res.get("detail") or "已处理", "data": res}


@router.post("/ext/download")
async def ext_download(req: ExternalDownloadRequest):
    """按“绝对媒体 URL”下载并自动把下载目录纳入监听。

    外部下载器 v260907 的 create_by_url 有上游 unique_id 缺陷，通用直链
    统一走本地下载；视频号仍使用下载器注入按钮/平台任务。
    """
    import asyncio
    import threading
    from urllib.parse import urlparse

    from . import external_dl

    url = (req.url or "").strip()
    if not url:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "url 不能为空", "data": None})
    if external_dl.is_video_channel_share(url):
        return JSONResponse(status_code=400, content={
            "code": 400,
            "msg": "视频号分享链接不能直接提交给下载器 API。"
                   "请在微信里打开视频号视频并播放，点击下载器注入的下载按钮；"
                   "文件落到下载目录后本工具会自动转写。"
                   "（视频号接口还要求视频号页面保持打开，否则会返回“请先初始化客户端 socket 连接”。）",
            "data": None,
        })
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return JSONResponse(status_code=400, content={
            "code": 400, "msg": "只接受 http/https 绝对媒体 URL", "data": None})

    info = await asyncio.to_thread(external_dl.probe)
    target = (req.dir or info.get("download_dir") or str(external_dl.DOWNLOAD_DIR)).strip()
    if not target:
        return JSONResponse(status_code=400, content={
            "code": 400, "msg": "未指定下载目录，且无法从下载器配置读取", "data": info})

    started = None
    try:
        st = watch_folder.status()
        if (not st.get("enabled")) or (st.get("dir") != target):
            started = watch_folder.start(target, model=req.whisperModel or "medium",
                                         interval=2.0, move_done=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动监听下载目录失败: %s", exc)

    def _download():
        try:
            external_dl.local_download(url, target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地直链下载失败: %s", exc)

    threading.Thread(target=_download, daemon=True, name="ext-direct-download").start()
    return {"code": 200, "msg": "已开始直链下载；文件落地后自动转写",
            "data": {"mode": "local", "watch": started, "downloadDir": target}}


@router.get("/risk/status")
async def risk_status():
    """各平台状态：冷却/熔断 + 排队 + 今日额度（供页面顶部提示 + 排查）。"""
    import time

    from . import alerts, scheduler
    from .utils import (account_pool_snapshot, mask_proxy, platform_cooldown_info,
                        proxy_checks, proxy_for, proxy_pool)

    data = {}
    for key, row in scheduler.stats().items():
        info = platform_cooldown_info(key)
        remain = int(max(0, info.get("remaining") or 0))
        data[key] = {
            "label": row.get("label") or key,
            "enabled": platform_cooldown_enabled(key),
            "cooldownRemaining": remain,
            "cooldownUntil": int(time.time() + remain) if remain > 0 else 0,
            "kind": info.get("kind") or "",
            "reason": info.get("reason") or "",
            "strikes": int(info.get("strikes") or 0),
            "level": int(info.get("level") or 0),
            "queue": {"pending": int(row.get("pending") or 0),
                      "running": bool(row.get("running"))},
            "budget": row.get("budget") or {},
            # 账号池（谁在干活、谁被冷却）与出口（池子几条、当前走哪条，已脱敏）
            "accounts": account_pool_snapshot(key),
            "proxy": {"poolSize": len(proxy_pool(key)),
                      "current": mask_proxy(proxy_for(key) or ""),
                      "check": proxy_checks().get(key)},
        }
    return {"code": 200, "msg": "操作成功", "data": data,
            "alerts": alerts.collect()}


@router.post("/proxy/check")
async def proxy_check_now():
    """手动跑一次代理自检（配完代理点一下就知道通不通，不用拿任务试错）。"""
    import asyncio

    from .utils import proxy_check, proxy_pool

    def _run():
        out = {}
        for key in ("douyin", "kuaishou", "xiaohongshu", "bilibili"):
            if proxy_pool(key):
                out[key] = proxy_check(key)
        return out or {"_none": {"ok": True, "detail": "没有任何平台配置了代理（全部直连）"}}

    data = await asyncio.to_thread(_run)
    bad = [k for k, v in data.items() if isinstance(v, dict) and not v.get("ok", True)]
    return {"code": 200 if not bad else 500,
            "msg": "全部出口可用" if not bad else f"有出口不通：{', '.join(bad)}",
            "data": data}


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ai/status")
async def ai_status():
    """LLM 纠错能力状态（前端据此显示/隐藏按钮）。"""
    from . import llm_correct as _lc

    fb = _lc._fallback_cfg()
    return {
        "code": 200,
        "msg": "操作成功",
        "data": {
            "enabled": llm_configured(),
            "model": _lc._model(),
            "baseUrl": _lc._base_url(),
            # 主模型额度失效时会自动切到备用模型（2026-10-31 加）
            "fallbackModel": (fb or {}).get("model") or "",
        },
    }


# ---------- 监听文件夹（抓包导出 → 自动转写） ----------

class WatchStartRequest(BaseModel):
    dir: str = ""   # 留空 = 自动使用下载器的下载目录
    model: str = "medium"
    interval: float = 5.0
    moveDone: bool = True


@router.post("/watch/open")
async def watch_open():
    """在资源管理器里打开监听目录（抖音图集/图片导入用：图片丢进去即自动成一条文案）。"""
    import asyncio
    import os

    d = watch_folder.current_dir(create=True)
    if d is None:
        from . import external_dl

        d = external_dl.DOWNLOAD_DIR
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.to_thread(_open_in_file_manager, str(d))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={
            "code": 500, "msg": f"打开失败：{exc}", "data": {"dir": str(d)}})
    return {"code": 200, "msg": "已打开监听目录", "data": {"dir": str(d)}}


@router.get("/watch/status")
async def watch_status():
    return {"code": 200, "msg": "操作成功", "data": watch_folder.status()}


class WatchCleanRequest(BaseModel):
    days: int = 0      # >0 只删早于 N 天的；0 = 全删（手动清理语义）


@router.post("/watch/clean")
async def watch_clean(req: WatchCleanRequest | None = None):
    """清理监听目录 processed/ 里的原片（视频号素材会越攒越多）。

    默认**不自动清理**（`TRANSCRIPT_WATCH_KEEP_DAYS=0`）；这里是手动触发。
    """
    import asyncio

    days = max(0, int((req.days if req else 0) or 0))
    res = await asyncio.to_thread(watch_folder.clean_processed, days)
    mb = round((res.get("freedBytes") or 0) / 1048576, 1)
    return {"code": 200,
            "msg": f"已清理 {res.get('removed', 0)} 个文件（释放 {mb} MB）" if res.get("removed")
                   else "没有可清理的文件",
            "data": {**res, "freedMB": mb, "usage": watch_folder.dir_usage()}}


@router.get("/watch/media")
async def watch_media_list():
    """监听目录（含 processed）里的本地音视频列表，供视频号页「本地视频库」展示。"""
    return {"code": 200, "msg": "操作成功",
            "data": await asyncio.to_thread(watch_folder.list_media)}


@router.get("/watch/media/file")
def watch_media_file(name: str = "", scope: str = "processed"):
    """播放/下载监听目录里的本地视频（FileResponse 自带 Range）。"""
    path = watch_folder.resolve_media(name, scope)
    if not path:
        return JSONResponse(status_code=404,
                            content={"code": 500, "msg": "文件不存在或路径不合法", "data": None})
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(path, media_type=media_type,
                        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"})


@router.post("/watch/start")
async def watch_start(req: WatchStartRequest):
    watch_dir = (req.dir or "").strip()
    if not watch_dir:  # 留空 = 自动接线下载器的下载目录
        from . import external_dl
        try:
            external_dl.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        watch_dir = str(external_dl.DOWNLOAD_DIR)
    try:
        data = watch_folder.start(watch_dir, model=req.model,
                                  interval=max(1.0, float(req.interval)),
                                  move_done=bool(req.moveDone))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": f"启动失败：{exc}", "data": None})
    return {"code": 200, "msg": "已开始监听", "data": data}


@router.post("/watch/stop")
async def watch_stop():
    return {"code": 200, "msg": "已停止监听", "data": watch_folder.stop()}


class AiCorrectRequest(BaseModel):
    taskId: str


@router.post("/ai/correct")
async def ai_correct(req: AiCorrectRequest):
    """对指定任务的文案做 LLM 语境纠错（保留原文，返回改动清单）。"""
    if not llm_configured():
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": "LLM 未配置（TRANSCRIPT_LLM_API_KEY）", "data": None})
    row = history_store.get(req.taskId)
    if row is None:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "记录不存在", "data": None})
    # 只拿正文：成功但无口播的任务会在 error_msg 里放"没有识别到口播"的提示，
    # 那是提示不是文本，不能拿去纠错
    text = row.get("text_content") or ""
    if not text.strip():
        return JSONResponse(status_code=400, content={"code": 500, "msg": "没有可纠错的文本", "data": None})
    segments = []
    extra = {}
    try:
        extra = json.loads(row.get("extra") or "{}")
        segments = extra.get("segments") or []
    except Exception:
        extra = {}
        segments = []
    # 画面字幕（OCR）作为纠错参考：能修 ASR 的整句听错（用户实测场景）
    frame_text = (extra.get("frameText") or "").strip()
    try:
        # 纠错是阻塞的网络调用，放到线程池执行，避免卡住整个事件循环
        import asyncio
        res = await asyncio.to_thread(
            correct_transcript, text, segments,
            row.get("title") or "", row.get("platform") or "", frame_text)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500,
                            content={"code": 500, "msg": f"纠错失败：{exc}", "data": None})
    if not res.get("ok"):
        return JSONResponse(status_code=500,
                            content={"code": 500, "msg": res.get("error") or "纠错失败",
                                     "data": res})
    # 持久化，便于后续导出纠错版
    history_store.update_extra(req.taskId, {
        "corrected_text": res.get("corrected") or "",
        "corrected_model": res.get("model"),
        "corrected_changes": res.get("changes") or [],
    })
    return {"code": 200, "msg": "操作成功", "data": res}

# ---------- 平台账号登录（本机桌面用；① 账号页后端） ----------

class LoginRequest(BaseModel):
    platform: str
    account: str = ""


@router.get("/login/status")
async def login_status():
    """各平台账号登录状态（纯读本地文件：cookie 是否存在、多久前登录、profile、账号冷却）。"""
    import asyncio

    from . import account_login

    return {"code": 200, "msg": "操作成功",
            "data": await asyncio.to_thread(account_login.accounts_status)}


@router.post("/login/start")
async def login_start(req: LoginRequest):
    """拉起登录脚本（会弹出本机浏览器窗口，需要人工扫码/输密码）。"""
    import asyncio

    from . import account_login

    res = await asyncio.to_thread(account_login.start_login, req.platform, req.account)
    if res.get("started"):
        return {"code": 200, "msg": res.get("hint") or "登录窗口已打开", "data": res}
    return {"code": 200 if res.get("already") else 500,
            "msg": res.get("error") or "启动失败", "data": res}


@router.post("/login/check")
async def login_check(req: LoginRequest):
    """验证登录态是否仍有效（无窗口地跑一次 login_cookie --check，会顺带刷新 cookie）。"""
    import asyncio

    from . import account_login

    res = await asyncio.to_thread(account_login.check_login, req.platform, req.account)
    return {"code": 200 if res.get("ok") else 500,
            "msg": "登录态有效" if res.get("ok") else (res.get("error") or "登录态无效"),
            "data": res}


@router.post("/login/open-dir")
async def login_open_dir():
    """在资源管理器里打开 cookies 目录。"""
    import asyncio

    from . import account_login

    d = await asyncio.to_thread(account_login.open_cookies_dir)
    await asyncio.to_thread(_open_in_file_manager, d)
    return {"code": 200, "msg": "已打开 cookies 文件夹", "data": {"dir": d}}
