"""数据模型定义。"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


# ---------- 请求模型 ----------

class CreateTaskRequest(BaseModel):
    workUrl: str
    type: str = "text"          # text / audio
    targetLanguage: str = "auto"
    whisperModel: str = "medium"
    # 画面文字（硬字幕/花字）OCR：None = 由平台决定（抖音/快手/小红书等无平台字幕的
    # 平台默认开启），True/False = 调用方显式指定（2026-10-31 改三态）
    ocrFrames: Optional[bool] = None


class CreateBatchRequest(BaseModel):
    workUrls: list[str]
    targetLanguage: str = "auto"
    whisperModel: str = "medium"


class TextConvertRequest(BaseModel):
    text: str = ""
    to: str = "s"              # s=简体（默认） / t=繁体


class CreateOcrTaskRequest(BaseModel):
    sourceUrl: Optional[str] = None   # 图片 URL（可选，也支持直接上传）
    sourceType: str = "image"          # image
    scene: str = "douyin-image-ocr"    # douyin-image-ocr / xiaohongshu-image-ocr


# ---------- 响应模型 ----------

class TaskNode(BaseModel):
    """批量任务中的单个子任务。"""
    taskId: str
    url: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    errorMessage: Optional[str] = None
    title: Optional[str] = None
    textContent: Optional[str] = None
    content: Optional[str] = None
    platform: Optional[str] = None
    cover: Optional[str] = None
    videoUrl: Optional[str] = None
    duration: Optional[float] = None


class TaskCreatedData(BaseModel):
    taskId: str
    status: TaskStatus = TaskStatus.PENDING


class TaskCreated(BaseModel):
    code: int = 200
    msg: str = "操作成功"
    data: TaskCreatedData


class BatchCreatedData(BaseModel):
    batchId: str
    total: int = 0


class BatchCreated(BaseModel):
    code: int = 200
    msg: str = "操作成功"
    data: BatchCreatedData


class TaskDetail(BaseModel):
    taskId: str
    taskType: str = "transcript"
    status: TaskStatus
    errorMessage: Optional[str] = None

    title: Optional[str] = None
    content: Optional[str] = None
    textContent: Optional[str] = None
    sourceText: Optional[str] = None
    cover: Optional[str] = None
    videoUrl: Optional[str] = None
    videoUrlList: Optional[list[str]] = None
    audioUrl: Optional[str] = None
    platform: Optional[str] = None
    duration: Optional[float] = None
    uploader: Optional[str] = None
    webpageUrl: Optional[str] = None
    createTime: Optional[str] = None
    # 带时间戳的分段（faster-whisper 原始分段），用于 SRT 导出
    segments: Optional[list[dict]] = None
    # 画面文字（OCR 识别的视频内嵌文字：硬字幕/说明文字），与口播文案分开
    frameText: Optional[str] = None
    frameBlocks: Optional[list] = None   # 画面文字的时间块 [{"t": 秒, "text": str, "kind": "main|scene"}]
    # 画面文字再分两类（2026-11-02）：作者展示的主文案 vs 画面背景/杂字
    frameMainText: Optional[str] = None
    frameSceneText: Optional[str] = None
    # 视频号「链接模式」附加元数据（2026-11-04）：贴链接只能拿到这些，
    # 视频流拿不到（微信只把播放地址发给客户端）
    channelMeta: Optional[dict] = None
    # 批量任务附加
    batchId: Optional[str] = None
    index: Optional[int] = None
    total: Optional[int] = None


class QueryTaskResponse(BaseModel):
    code: int = 200
    msg: str = "操作成功"
    data: TaskDetail


class BatchQueryResponse(BaseModel):
    code: int = 200
    msg: str = "操作成功"
    data: "BatchResult"


class BatchResult(BaseModel):
    batchId: str
    status: TaskStatus = TaskStatus.PENDING
    done: int = 0
    success: int = 0
    failed: int = 0
    total: int = 0
    items: list[TaskNode] = []


class OcrResultData(BaseModel):
    taskId: str
    status: TaskStatus = TaskStatus.SUCCESS
    text: str = ""
    imageUrl: Optional[str] = None
    scene: Optional[str] = None


class OcrResult(BaseModel):
    code: int = 200
    msg: str = "操作成功"
    data: OcrResultData


# 供反射刷新
TaskCreated.model_rebuild()
TaskCreatedData.model_rebuild()
QueryTaskResponse.model_rebuild()
BatchQueryResponse.model_rebuild()
BatchCreated.model_rebuild()
OcrResult.model_rebuild()
