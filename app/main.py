"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="文案提取工具", version="1.0.0")

# 本机页面认证：页面加载时下发 HttpOnly + SameSite=Strict 的**页面会话** cookie
#（每次启动随机、只存内存，不是长期 API Token；2026-11-02 起）。
# 外部调用（Halo 插件/脚本）用环境变量 TRANSCRIPT_API_TOKEN 配 Bearer / X-API-Token。
_API_TOKEN_FILE = Path(__file__).resolve().parent.parent / ".api_token"


def _load_or_create_api_token() -> str:
    """会话 token：优先用环境变量；否则首次生成后落盘复用。

    以前每次重启都重新随机生成 → 已打开页面拿着旧 cookie 会全部 401，
    表现就是“刷新后列表空了/状态加载失败”。落盘后重启不再踢掉已打开的页面。
    """
    env = os.getenv("TRANSCRIPT_API_TOKEN", "").strip()
    if env:
        return env
    try:
        if _API_TOKEN_FILE.is_file():
            saved = _API_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except Exception:  # noqa: BLE001
        pass
    token = secrets.token_urlsafe(32)
    try:
        _API_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _API_TOKEN_FILE.write_text(token, encoding="utf-8")
        try:
            os.chmod(_API_TOKEN_FILE, 0o600)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    return token


_API_TOKEN = _load_or_create_api_token()
_SESSION_COOKIE = "transcript_session"
# 页面会话 token：每次进程启动随机生成，只存在内存里 —— 不落盘、也不等于 API Token。
# 前端脚本注释写的就是这个语义（"服务重启后会话 token 会变：401 就去访问首页拿新 cookie"）。
# 2026-11-02 修复：原来直接把长期 API Token 当 cookie 发出去，等于把持久凭证交给任何能
# GET / 的本机进程；现在发出的只是"本次运行有效"的会话值，重启即作废。
_SESSION_TOKEN = secrets.token_urlsafe(32)
_ALLOWED_HOSTS = [
    host.strip().lower()
    for host in os.getenv("TRANSCRIPT_ALLOWED_HOSTS", "127.0.0.1,localhost,::1").split(",")
    if host.strip()
]
_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("TRANSCRIPT_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

# 默认不开放跨域；只有显式配置允许的 Halo 等来源才加 CORS
if _ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(router, prefix="/prod-api")


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """把 FastAPI 默认的 422 统一成本项目的失败契约。

    2026-11-02：全站失败响应都是 HTTP 400/404/500 + {"code":…,"msg":"中文…","data":…}，
    只有 Pydantic 校验失败走 FastAPI 默认的 422 + {"detail":[{英文…}]}——
    前端按 code/msg 解析会拿到 undefined，形状也对不上。
    """
    missing: list[str] = []
    invalid: list[str] = []
    bad_json = False
    for err in exc.errors():
        etype = err.get("type") or ""
        if etype == "json_invalid":
            # 请求体本身不是合法 JSON：loc 里是字符偏移量（如 ("body", 11)），
            # 当字段名显示会变成"参数格式不正确: 11"，对用户毫无意义
            bad_json = True
            continue
        loc = [str(x) for x in (err.get("loc") or [])
               if x not in ("body", "query", "path", "header")]
        field = ".".join(loc) or "参数"
        if etype.endswith("missing"):
            missing.append(field)
        else:
            invalid.append(field)
    parts = []
    if bad_json:
        parts.append("请求体不是合法的 JSON")
    if missing:
        parts.append("缺少请求参数: " + ", ".join(dict.fromkeys(missing)))
    if invalid:
        parts.append("参数格式不正确: " + ", ".join(dict.fromkeys(invalid)))
    return JSONResponse(
        status_code=400,
        content={"code": 500, "msg": "；".join(parts) or "请求参数不正确", "data": None},
    )


def _same_token(value: str) -> bool:
    if not value:
        return False
    return secrets.compare_digest(value.encode("utf-8"), _API_TOKEN.encode("utf-8"))


def _same_session(value: str | None) -> bool:
    """页面会话 cookie 是否有效（会话值每次启动随机生成，只存内存，重启即失效）。"""
    if not value:
        return False
    return secrets.compare_digest(value.encode("utf-8"), _SESSION_TOKEN.encode("utf-8"))


def _request_authorized(request: Request) -> bool:
    if _same_session(request.cookies.get(_SESSION_COOKIE)):
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and _same_token(auth[7:].strip()):
        return True
    return _same_token(request.headers.get("x-api-token", "").strip())


# 接口文档也属于"接口面"：不再允许匿名读取（2026-11-02 修复）。在本机浏览器先打开首页
# 拿到会话 cookie 之后，再访问 /docs、/redoc 就正常了。
_DOC_PATHS = ("/openapi.json", "/docs", "/redoc")


@app.middleware("http")
async def _api_auth_and_session(request: Request, call_next):
    path = request.url.path
    host = (request.url.hostname or "").lower()
    # 防 DNS rebinding：只接受明确的主机名；Halo/局域网部署需同步加白名单
    if host not in _ALLOWED_HOSTS:
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "Host 不在允许列表（检查 TRANSCRIPT_ALLOWED_HOSTS）", "data": None},
        )
    if (path.startswith("/prod-api/") or path in _DOC_PATHS) \
            and request.method != "OPTIONS" and not _request_authorized(request):
        msg = "未授权：请从本机页面访问，或配置 API Token"
        if path in _DOC_PATHS:
            msg = "未授权：请先在本机浏览器打开 http://127.0.0.1:8000/ 获取页面会话，再访问接口文档"
        return JSONResponse(status_code=401, content={"code": 401, "msg": msg, "data": None})
    response = await call_next(request)
    # 页面加载后浏览器自动携带 cookie，前端无需保存/拼接 token
    if path in ("/", "/index.html") and not _same_session(request.cookies.get(_SESSION_COOKIE)):
        response.set_cookie(
            _SESSION_COOKIE, _SESSION_TOKEN, max_age=12 * 3600,
            httponly=True, samesite="strict", path="/",
        )
    return response


@app.on_event("startup")
async def _startup_watch():
    import asyncio

    # 历史记录里的文案补齐归档到 文案输出/ 文件夹（幂等，缺哪个补哪个）
    from .archive import backfill
    await asyncio.to_thread(backfill)
    # 启动时把历史记录（含本地预览视频等任务文件）裁到保留上限，并清掉孤儿目录，防止磁盘堆积
    from .history import (history_keep, history_keep_days, history_store,
                          media_keep_days, sweep_old_media,
                          sweep_orphan_task_files, sweep_tmp_uploads)
    await asyncio.to_thread(history_store.trim, history_keep(), history_keep_days())
    await asyncio.to_thread(sweep_old_media, media_keep_days())
    await asyncio.to_thread(sweep_orphan_task_files)
    # 上传副本：正常跑完会自己删，中断/强杀留下的在这里清（只删够旧的，免得误伤排队中的任务）
    await asyncio.to_thread(sweep_tmp_uploads)
    import logging
    import os
    import threading

    # 队列恢复（③ 持久队列）：上次重启前没跑完的（排队中 / 运行中）任务重新入队
    from . import alerts, scheduler

    try:
        await asyncio.to_thread(scheduler.restore)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("scheduler").warning("队列恢复失败: %s", exc)
    # 告警巡检（④）：失败率 / 队列积压 / 磁盘余量 → 落 app.log + 页面横幅
    alerts.start_watch()

    # 代理自检：只对**配了代理池**的平台各探一次（连不上会进告警，免得误判成平台风控）
    def _proxy_selfcheck():
        log = logging.getLogger("proxy")
        from .utils import proxy_check, proxy_pool

        for key in ("douyin", "kuaishou", "xiaohongshu", "bilibili"):
            if not proxy_pool(key):
                continue
            res = proxy_check(key)
            if res.get("ok"):
                log.info("代理自检 %s：通（%s，%s 条出口）", key, res.get("detail"), res.get("pool"))
            else:
                log.warning("代理自检 %s：**不通** → %s", key, res.get("detail"))

    threading.Thread(target=_proxy_selfcheck, daemon=True, name="proxy-selfcheck").start()

    def _warm_models():
        # 先把默认 whisper 模型载入显存（约 9 秒），再预热 OCR 的文字识别
        try:
            from .extractor import warmup_default_model
            warmup_default_model()
        except Exception:  # noqa: BLE001
            pass
        try:
            from .ocr import warmup
            warmup()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_warm_models, daemon=True, name="model-warmup").start()

_static = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")


@app.middleware("http")
async def _no_cache_index(request, call_next):
    """index.html 禁用缓存：前端模板改动刷新即生效，避免浏览器拿旧页面。"""
    resp = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


