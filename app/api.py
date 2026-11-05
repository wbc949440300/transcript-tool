from __future__ import annotations

import asyncio
import logging
import json
import mimetypes
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

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
)
from .ocr import ocr_image
from .task_store import store
from .utils import (detect_platform, gen_task_id, http_opener,
                    platform_cooldown_enabled)

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
        raw = (req.workUrl or "").strip()
        # 视频号：微信无网页可读接口，normalize 不认微信域；走统一入口（与批量入口共用
        # 同一函数，见 app/video_channel.py）：元宝直链下载优先，回落「链接模式」卡片
        from . import video_channel

        if video_channel.is_channel_share(raw):
            # whisperModel 传 auto：下载后按时长自适应（短片 medium / 长视频 tiny），
            # 前端下拉可选固定模型（2026-11-05）
            task_id = await asyncio.to_thread(
                video_channel.process_channel_link, raw, req.whisperModel or "auto")
            if task_id:
                return TaskCreated(data=TaskCreatedData(taskId=task_id))
            return JSONResponse(status_code=400, content={
                "code": 500, "msg": video_channel.CHANNEL_UNRESOLVABLE, "data": None})
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


@router.get("/transcript/active")
async def active_tasks():
    """页面重开后找回「进行中」的任务（2026-11-05）。

    进行中任务的真身只在服务端内存；浏览器关掉标签页后前端存的入口会丢，
    重开页面就看不到进度、只能去历史记录翻。这里把还没结束的任务交给前端，
    由它重建轮询、把界面续上。批量的子任务不单列，按 batchId 汇总一条。
    """
    active_batches = store.list_active_batches()
    batch_set = set(active_batches)
    tasks = []
    for t in store.list_active():
        if t.batchId and t.batchId in batch_set:
            continue
        tasks.append({
            "taskId": t.taskId, "taskType": t.taskType, "platform": t.platform or "",
            "status": t.status.value if hasattr(t.status, "value") else str(t.status),
            "title": t.title or "", "errorMessage": t.errorMessage or "",
            "createTime": t.createTime or "",
        })
    batches = []
    for bid in active_batches:
        items = store.get_batch(bid) or []
        batches.append({
            "batchId": bid,
            "done": sum(1 for it in items if it.status in (TaskStatus.SUCCESS, TaskStatus.FAILURE)),
            "total": len(items),
        })
    return {"code": 200, "msg": "操作成功", "data": {"tasks": tasks, "batches": batches}}


@router.post("/transcript/image/ocr/doTask", response_model=OcrResult)
async def ocr_do_task(req: CreateOcrTaskRequest):
    """图片文案提取：支持直接传图 URL。"""
    url = (req.sourceUrl or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse(status_code=400, content={"code": 500, "msg": "请提供有效的图片链接", "data": None})
    try:
        import urllib.request
        req_ = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        from .utils import http_get_bytes

        data, _ = http_get_bytes(req_, timeout=20)
        text = ocr_image(data)
        task_id = gen_task_id()
        _save_ocr_history(task_id, req.scene, url, text, TaskStatus.SUCCESS)
        return OcrResult(data=OcrResultData(
            taskId=task_id, status=TaskStatus.SUCCESS, text=text,
            imageUrl=url, scene=req.scene,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.error("OCR URL 失败: %s", exc)
        # 与其它接口一致：失败走 HTTP 5xx（body 形状不变，页面照旧能解析出 msg）
        return JSONResponse(status_code=500, content={
            "code": 500, "msg": str(exc),
            "data": {"taskId": gen_task_id(), "status": "FAILURE", "text": "",
                     "imageUrl": url, "scene": req.scene},
        })


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
    if not ok:
        # 与 /history/detail 一致：记录不存在就 404（删除本身仍是幂等的，前端只看 body）
        return JSONResponse(status_code=404, content={"code": 500, "msg": "记录不存在", "data": None})
    # 2026-11-05 修：历史删了，内存里那份也要删，否则结果页 URL 还能打开到重启为止
    store.discard(taskId)
    return {"code": 200, "msg": "已删除", "data": {"deleted": True}}


@router.delete("/history/clear")
async def history_clear():
    n = history_store.clear()
    # 已结束的任务从内存里一并移除；运行中的保留，别打断正在跑的任务
    store.discard_finished()
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
    """下载响应：HTTP 头只能是 latin-1，中文名必须走 RFC5987 的 filename*。

    2026-11-02 修复：原实现直接 f'filename="{中文名}"'，Starlette 以 latin-1 编码 header
    抛 UnicodeEncodeError → 归档里凡是中文名的文件下载必 500（归档 84 个文件 71 个含中文，
    server_err.log 记了 12 处 latin-1 栈）。改成 ASCII 兜底名 + filename*=UTF-8''。
    """
    stem, ext = os.path.splitext(filename)
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "download"
    ascii_name = ascii_stem + ext
    utf8_name = quote(filename, safe="")
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition":
                 f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'},
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
        # "任务不存在或已删除" 与其它接口的 404 语义对齐；
        # 其余（格式不支持 / 无任务 / 超批量）仍是 400。此前一律 400，兄弟接口不一致。
        code = 404 if "任务不存在" in str(exc) else 400
        return JSONResponse(status_code=code,
                            content={"code": 500, "msg": str(exc), "data": None})
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
        # 同上：全部任务都不存在时按 404，其余按 400
        code = 404 if "任务不存在" in str(exc) else 400
        return JSONResponse(status_code=code,
                            content={"code": 500, "msg": str(exc), "data": None})
    return _file_response(content, filename, media_type)


# ---------- 媒体代理（解决防盗链：浏览器无法携带 Referer） ----------

_PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _throttle_media(url: str) -> float:
    """媒体请求（平台 CDN 直链）的节流。

    2026-11-02 补：此前 /media/proxy 与 /media/cover **完全没有限速**，
    是唯一绕过 throttle 闸门的对外路径——预览/封面刷新一频繁就直接打到 CDN。

    间隔比抓取通道松得多，原因是：
      · 媒体打的是 CDN，风控强度远低于平台接口
      · /media/proxy 供播放器流式使用，会连续发 Range 请求；
        若套用抓取通道的平台级节流（默认 3s），拖动进度条会被卡死

    桶按 **CDN 域名**独立，因此不与同平台的抓取节奏互相阻塞，
    也不同 CDN 之间互相排队。TRANSCRIPT_MEDIA_THROTTLE_MS=0 可关闭。
    """
    try:
        ms = float(os.getenv("TRANSCRIPT_MEDIA_THROTTLE_MS", "500") or 500)
    except Exception:  # noqa: BLE001
        ms = 500.0
    if ms <= 0:
        return 0.0
    from .utils import throttle

    host = (urlparse(url).netloc or "").lower()
    return throttle(host or "media", min_interval=ms / 1000.0)
_PLATFORM_REFERER = {
    "抖音": "https://www.douyin.com/",
    "快手": "https://www.kuaishou.com/",
    "小红书": "https://www.xiaohongshu.com/",
    "B站": "https://www.bilibili.com/",
    # 视频号：封面走 finder.video.qq.com，要带微信域 Referer 才不 403
    "视频号": "https://channels.weixin.qq.com/",
}


_BILI_AUDIO_URL = re.compile(r"-1-(?:30216|30232|30280|30250)\.m4s", re.I)
_AUDIO_URL_HINT = re.compile(r"(?:mime=audio|itag=(?:139|140|141|256|258|325|328))", re.I)


def _is_audio_only_url(u: str) -> bool:
    """旧记录可能把纯音频流排在前面（B站 302xx / mime=audio）。"""
    return bool(_BILI_AUDIO_URL.search(u or "") or _AUDIO_URL_HINT.search(u or ""))


_CODEC_CACHE: dict = {}


def _sniff_codec(url: str, platform: str) -> str:
    """嗅探直链的视频编码，返回 'h264' / 'hevc' / ''（未知）。

    2026-11-04 补：Chrome 的 H.265 只能**硬件解码** —— 显卡/驱动不支持时，
    <video> 会表现为「进度条照走、画面冻在第一帧」（时间、音频都正常），
    而 canPlayType 还会返回 "probably" 骗人。抖音同一作品常常同时给
    hevc 与 h264 两套直链，所以预览可以退回 h264 那档。

    只取前 256KB 找 mp4 的 sample entry 四字码（avc1 / hvc1），不整段下载；
    结果按直链缓存在进程内（直链本身几小时就过期，够用）。
    """
    if url in _CODEC_CACHE:
        return _CODEC_CACHE[url]
    codec = ""
    try:
        ua = "Mozilla/5.0" if platform == "B站" else _PROXY_UA
        headers = {"User-Agent": ua, "Accept": "*/*", "Range": "bytes=0-262143"}
        referer = _PLATFORM_REFERER.get(platform, "")
        if referer:
            headers["Referer"] = referer
        _throttle_media(url)
        with http_opener(media=True).open(
                urllib.request.Request(url, headers=headers), timeout=15) as resp:
            head = resp.read(262144)
        if b"avc1" in head or b"avc3" in head:
            codec = "h264"
        elif b"hvc1" in head or b"hev1" in head:
            codec = "hevc"
    except Exception as exc:  # noqa: BLE001
        logger.info("编码嗅探失败（按未知处理）: %s", exc)
    if len(_CODEC_CACHE) > 500:
        _CODEC_CACHE.clear()
    _CODEC_CACHE[url] = codec
    return codec


def _media_all_urls(task_id: str) -> tuple[list[str], str]:
    """该任务的**全部候选直链**（首选在前）+ 平台名。

    ★ 2026-11-03 新增：抖音会给多条 CDN 直链，同一时刻可用性并不一致 ——
    实测同一任务，取第一次 502（21 秒后断链），取第二次 1.6MB/s 正常。
    只认一条会让播放器反复卡死，所以要能把整份候选列表交给调用方轮着试。
    """
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
    # 兼容旧记录：跳过纯音频，避免预览黑屏（新记录的 videoUrlList 已只收视频流）
    filtered = [u for u in urls if not _is_audio_only_url(u)]
    return (filtered or urls), platform


def _resolve_media_url(task_id: str, index: int) -> tuple[Optional[str], str, str]:
    """从历史记录/内存任务里取媒体直链，返回 (url, referer, platform)。"""
    urls, platform = _media_all_urls(task_id)
    if not urls:
        return None, "", ""
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
def media_proxy(request: Request, taskId: str = "", index: int = 0, download: int = 0,
                codec: str = ""):
    """代取视频/音频流：服务端带上 Referer/UA 拉流，浏览器即可预览与保存。

    支持 Range 透传（可拖动进度条）；download=1 时附带附件头直接下载。
    videoUrlList 里的 file:// 项表示任务目录里的本地预览视频，直接走本地文件。
    codec=h264 时优先挑 h264 那条直链（前端播放无画面时自动带上来，见 index.html）。
    """
    if not taskId:
        return JSONResponse(status_code=400, content={"code": 500, "msg": "缺少 taskId", "data": None})
    urls, platform = _media_all_urls(taskId)
    if not urls:
        return JSONResponse(status_code=404, content={"code": 500, "msg": "该任务没有可用的媒体直链", "data": None})
    if urls[0].startswith("file://"):
        return _serve_local_media(taskId, urls[0][len("file://"):], download=download)

    # 前端发现「播放但画面不动」时会带 codec=h264 重来：把 h264 那条提到最前。
    # 老记录（选档逻辑修复前生成的）h264 可能排在很后面，所以这里按顺序嗅探整份
    # 候选（最多 8 条，找到第一条命中的就停）；每条只读 256KB，仅在回退路径上跑。
    if codec and codec.strip().lower() in ("h264", "hevc"):
        want = codec.strip().lower()
        hit = ""
        for cand in urls[:8]:
            if _sniff_codec(cand, platform) == want:
                hit = cand
                break
        if hit and hit != urls[0]:
            logger.info("媒体代理: 按 codec=%s 把第 %d 条提到最前", want, urls.index(hit) + 1)
            urls = [hit] + [u for u in urls if u != hit]

    # B站 CDN 对桌面 Chrome UA + Range 会直接断连（实测），媒体请求用简单 UA
    ua = "Mozilla/5.0" if platform == "B站" else _PROXY_UA
    headers = {"User-Agent": ua, "Accept": "*/*"}
    referer = _PLATFORM_REFERER.get(platform, "")
    if referer:
        headers["Referer"] = referer
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng

    # 从 index 开始逐条试：抖音给的多条直链同一时刻可用性并不一致（实测同一任务
    # 一条 502 断链、另一条 1.6MB/s 正常），只认一条会让播放器反复卡死。
    n = len(urls)
    order = [urls[(max(0, index) + i) % n] for i in range(n)]
    upstream = None
    last_exc = None
    url = order[0] if order else ""
    for i, cand in enumerate(order, 1):
        _throttle_media(cand)
        try:
            upstream = http_opener(media=True).open(
                urllib.request.Request(cand, headers=headers), timeout=30)
            url = cand                      # 后面补 Content-Type 时要用命中的那条
            if i > 1:
                logger.info("媒体代理：前 %d 条直链不通，换到第 %d 条成功", i - 1, i)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("媒体直链 %d/%d 拉流失败，换下一条: %s", i, n, exc)
    if upstream is None:
        return JSONResponse(
            status_code=502,
            content={"code": 500,
                     "msg": f"媒体源暂不可用（{n} 条直链都试过，可能已过期；"
                            f"点上方「刷新直链」可重新取）：{last_exc}",
                     "data": None})

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
    _throttle_media(cover)
    try:
        from .utils import http_get_bytes

        data, hdrs = http_get_bytes(urllib.request.Request(cover, headers=headers), timeout=20)
        media_type = hdrs.get("Content-Type") or "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        logger.warning("封面代理失败: %s", exc)
        return JSONResponse(status_code=502, content={"code": 500, "msg": f"封面获取失败：{exc}", "data": None})
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=86400"})


def _task_duration(task_id: str) -> float | None:
    """从历史/内存任务里取时长（抽帧要按它均匀取样）。"""
    dur = None
    row = history_store.get(task_id)
    if row:
        dur = row.get("duration")
    if dur is None:
        t = store.get(task_id)
        if t is not None:
            dur = getattr(t, "duration", None)
    try:
        d = float(dur)
        return d if d > 0 else None
    except (TypeError, ValueError):
        return None


_THUMB_LOCKS: dict = {}
_THUMB_LOCKS_GUARD = threading.Lock()
_THUMB_FAIL: dict = {}
_THUMB_FAIL_COOLDOWN = 60.0


def _thumb_lock(task_id: str) -> threading.Lock:
    """每个任务一把抽帧锁（同一任务并发请求必须串行，不同任务互不影响）。"""
    with _THUMB_LOCKS_GUARD:
        lk = _THUMB_LOCKS.get(task_id)
        if lk is None:
            if len(_THUMB_LOCKS) > 300:
                _THUMB_LOCKS.clear()
            lk = _THUMB_LOCKS[task_id] = threading.Lock()
        return lk


@router.get("/media/frame")
def media_frame(taskId: str = "", i: int = 0, n: int = 5):
    """结果页「视频截图」用的**服务端抽帧**。

    ★ 2026-11-03 改：截图以前是浏览器另开一个 video 元素、seek 到 5 个时间点再
    drawImage —— 那要求"第二路流能及时缓冲到目标位置"，而抖音/B站 CDN 对同一条
    直链的并发流本来就慢，实测就是 seek 超时、截图全黑（失败率高到没法用）。
    现在改由服务端 ffmpeg 直接从 URL 抽帧，浏览器只负责 <img> 显示，
    彻底绕开"浏览器要实时缓冲"这条路。

    抽到的帧缓存在任务目录的 `_thumbs/` 下：第 1 次请求抽全部 n 张，
    后续按序号直接命中缓存，不会重复抽（也不会重复打 CDN）。
    """
    if not taskId:
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": "缺少 taskId", "data": None})
    i = max(0, min(int(i or 0), 11))
    n = max(1, min(int(n or 5), 12))
    tdir = Path(os.getenv("TRANSCRIPT_TASKS_DIR", "tasks")) / taskId
    cache = tdir / "_thumbs"
    target = cache / f"thumb_{i + 1:03d}.jpg"

    if not target.is_file():
        # ★ 2026-11-04 修「截图不出来 / 只出一两张」：
        #   前端一次并发发 5 个 /media/frame，原先每个请求都各自清空 _thumbs 再重抽，
        #   互相踩踏 —— 实测冷缓存下 5 个并发：1 个 62 秒成功、4 个 200 秒超时，
        #   最后磁盘上一张不剩。现在同一 taskId 串行，后到的等锁后直接命中缓存。
        with _thumb_lock(taskId):
            if target.is_file():
                pass                        # 等锁期间别人已经抽好，直接走缓存
            elif time.time() - _THUMB_FAIL.get(taskId, 0.0) < _THUMB_FAIL_COOLDOWN:
                return JSONResponse(status_code=502, content={
                    "code": 500,
                    "msg": "抽帧刚失败过，60 秒内不再重试（直链多半已过期，点「刷新直链」）",
                    "data": None})
            else:
                urls, platform = _media_all_urls(taskId)
                if not urls:
                    return JSONResponse(status_code=404, content={
                        "code": 500, "msg": "该任务没有可用的媒体直链", "data": None})
                from .frame_ocr import grab_thumbnails

                dur = _task_duration(taskId)
                referer = _PLATFORM_REFERER.get(platform, "")
                frames: list = []
                for u in urls[:3]:        # 同 /media/proxy 的理由：一条不通就换下一条
                    if u.startswith("file://"):
                        # file://video.mp4 指的是 tasks/<taskId>/video.mp4；直接剥前缀会得到相对路径，
                        # ffmpeg 在服务进程的 CWD 下找不到 → 报 Error opening input（本地文件抽帧全失败的根因）
                        local = tdir / u[len("file://"):]
                        if not local.is_file():
                            continue
                        u = str(local)
                    frames = grab_thumbnails(u, dur, referer, n=n, out_dir=cache)
                    if frames:
                        break
                if not frames:
                    _THUMB_FAIL[taskId] = time.time()
                    if len(_THUMB_FAIL) > 500:
                        _THUMB_FAIL.clear()
                    return JSONResponse(status_code=502, content={
                        "code": 500,
                        "msg": "抽帧失败：这条直链当前拉不动（可能已过期或被限速），"
                               "点「刷新直链」后再试", "data": None})
                _THUMB_FAIL.pop(taskId, None)

    if not target.is_file():
        # 抽到了但张数不足（视频比预期短）→ 退回最后一张，别让前端空着
        have = sorted(cache.glob("thumb_*.jpg"))
        if not have:
            return JSONResponse(status_code=404,
                                content={"code": 500, "msg": "没有抽到帧", "data": None})
        target = have[-1]
    return FileResponse(target, media_type="image/jpeg",
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
        from .extractor import _douyin_stream_urls, _extract_info, _pick_video_urls
        from .platforms import adapter_for

        adapter = adapter_for(src_url)

        # ★ 2026-11-03 加：B站没有平台专用 fetch，走 yt-dlp 重取一遍信息 ——
        # 故意的：与首次提取用**同一条路径**，直链形状才一致（同样只收视频流，
        # 混进纯音频会让预览黑屏）。B站 CDN 的直链同样几小时过期，预览靠它续命。
        if adapter.key == "bilibili":
            info = _extract_info(src_url)
            urls = _pick_video_urls(info.get("formats") or [])
            if not urls:
                u = info.get("url") or ""
                if u and str(info.get("vcodec") or "").lower() not in ("", "none"):
                    urls = [u]
            dur = info.get("duration")
            return {"platform": adapter.label, "urls": (urls or [])[:8],
                    "title": info.get("title"), "cover": info.get("thumbnail"),
                    "duration": (float(dur) if isinstance(dur, (int, float)) else None)}

        if adapter.key not in ("douyin", "kuaishou") or adapter.fetch is None:
            raise LinkError("该平台不需要刷新直链（抖音/快手/B站的直链会几小时过期）")
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
    # 汇总所有平台的"能用但有问题"（轮换型/境外/机房），供前端弹窗逐条展示
    problems: list[str] = []
    for k, v in data.items():
        if isinstance(v, dict):
            for p in (v.get("problems") or []):
                problems.append(f"[{k}] {p}")
    msg = ("全部出口可用" if not problems
           else f"发现 {len(problems)} 个问题，详见弹窗")
    return {"code": 200 if not problems else 500,
            "msg": msg, "data": data, "problems": problems}


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


@router.post("/login/logout")
async def login_logout(req: LoginRequest):
    """退出登录：删除该账号的 cookie 文件与浏览器 profile。

    删的是登录态，不是历史记录；重新登录即可恢复。
    （2026-11-03 新增：此前只有「登录/验证」，被标记或废弃的号没法从界面清掉。）
    """
    import asyncio

    from . import account_login

    res = await asyncio.to_thread(account_login.logout_account, req.platform, req.account)
    return {"code": 200 if res.get("ok") else 500,
            "msg": res.get("msg") or ("已退出登录" if res.get("ok") else "退出失败"),
            "data": res}


@router.post("/login/open-dir")
async def login_open_dir():
    """在资源管理器里打开 cookies 目录。"""
    import asyncio

    from . import account_login

    d = await asyncio.to_thread(account_login.open_cookies_dir)
    await asyncio.to_thread(_open_in_file_manager, d)
    return {"code": 200, "msg": "已打开 cookies 文件夹", "data": {"dir": d}}


# ---------- 设置：可视化编辑 .env（改完立即生效，不必重启）----------

@router.get("/settings")
async def get_settings():
    """白名单内的可配置项，按分组返回当前**实际生效**的值。"""
    from . import settings_api

    return {"code": 200, "msg": "操作成功",
            "data": {"groups": settings_api.read_settings()}}


@router.post("/settings")
async def post_settings(payload: dict):
    """保存设置。

    只接受 `settings_api.SCHEMA` 白名单里的键；其余键直接忽略（不做任何写入）。
    校验失败的项会逐条返回原因，校验通过的项**照常生效**，不会因为一项错就全丢。
    """
    import asyncio

    from . import settings_api

    values = (payload or {}).get("values") or {}
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="values 必须是对象")
    out = await asyncio.to_thread(settings_api.save_settings, values)
    if out.get("errors"):
        return {"code": 400, "msg": "部分配置未生效（见 errors）", "data": out}
    return {"code": 200, "msg": "已保存并立即生效", "data": out}


# ---------- 发现：B站匿名搜索 / 抖音一级评论（免账号、免代理）----------
#
# 2026-11-04 实测：B站搜索与抖音一级评论都能在"零 cookie、零代理"下拿到，
# 差别只在姿势 —— B站纯 HTTP（buvid3 + wbi 签名），抖音必须借页面上下文发请求
# （纯 Python 会被 TLS/JS 指纹挡成空 body）。这两条通道用来补"关键词发现"和
# "评论区素材"，正文提取仍走原来的 yt-dlp / 浏览器 + ASR 链路。

@router.get("/discover/bilibili/search")
async def discover_bilibili_search(keyword: str = "", page: int = 1, pageSize: int = 20):
    """B站关键词搜索（访客态，20 条/页）。返回的 url 可直接喂给批量提取。"""
    from . import bilibili

    try:
        data = await asyncio.to_thread(bilibili.search_videos, keyword, page, pageSize)
    except bilibili.BiliError as exc:
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": str(exc), "data": None})
    return {"code": 200, "msg": "操作成功", "data": data}


@router.get("/discover/bilibili/hot")
async def discover_bilibili_hot(limit: int = 10):
    """B站热搜词（访客态）。"""
    from . import bilibili

    try:
        data = await asyncio.to_thread(bilibili.hot_search, limit)
    except bilibili.BiliError as exc:
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": str(exc), "data": None})
    return {"code": 200, "msg": "操作成功", "data": data}


class DouyinCommentsRequest(BaseModel):
    url: str
    maxCount: int = 100
    pageSize: int = 20


@router.post("/discover/douyin/comments")
async def discover_douyin_comments(req: DouyinCommentsRequest):
    """抖音一级评论（访客态）：起一次浏览器、页面内翻页取评论。"""
    from .douyin_comments import CommentError, fetch_comments

    try:
        data = await asyncio.to_thread(fetch_comments, req.url, req.maxCount, req.pageSize)
    except CommentError as exc:
        return JSONResponse(status_code=400,
                            content={"code": 500, "msg": str(exc), "data": None})
    except Exception as exc:  # noqa: BLE001  浏览器起不来等
        logger.warning("抖音评论采集失败: %s", exc)
        return JSONResponse(status_code=500,
                            content={"code": 500, "msg": "采集失败：%s" % exc, "data": None})
    return {"code": 200, "msg": "操作成功", "data": data}
