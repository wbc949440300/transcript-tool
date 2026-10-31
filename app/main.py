"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .watch_folder import autostart_from_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="文案提取工具", version="1.0.0")

# 本机页面认证：页面加载时下发 HttpOnly + SameSite=Strict 会话 cookie。
# 外部调用（Halo 插件/脚本）用环境变量 TRANSCRIPT_API_TOKEN 配 Bearer / X-API-Token。
_API_TOKEN = os.getenv("TRANSCRIPT_API_TOKEN", "").strip() or secrets.token_urlsafe(32)
_SESSION_COOKIE = "transcript_session"
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


def _same_token(value: str) -> bool:
    if not value:
        return False
    return secrets.compare_digest(value.encode("utf-8"), _API_TOKEN.encode("utf-8"))


def _request_authorized(request: Request) -> bool:
    if _same_token(request.cookies.get(_SESSION_COOKIE) or ""):
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and _same_token(auth[7:].strip()):
        return True
    return _same_token(request.headers.get("x-api-token", "").strip())


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
    if path.startswith("/prod-api/") and request.method != "OPTIONS" and not _request_authorized(request):
        return JSONResponse(
            status_code=401,
            content={"code": 401, "msg": "未授权：请从本机页面访问，或配置 API Token", "data": None},
        )
    response = await call_next(request)
    # 页面加载后浏览器自动携带 cookie，前端无需保存/拼接 token
    if path in ("/", "/index.html") and not _same_token(request.cookies.get(_SESSION_COOKIE) or ""):
        response.set_cookie(
            _SESSION_COOKIE, _API_TOKEN, max_age=7 * 24 * 3600,
            httponly=True, samesite="strict", path="/",
        )
    return response


@app.on_event("startup")
async def _startup_watch():
    autostart_from_env()
    # 恢复上次页面里开启的监听目录（用户手动停过则只有 optOut 标记，不会自作主张）
    from . import watch_folder
    watch_folder.resume_last()
    # 从没设置过监听 → 自动接线下载器的下载目录（视频号零配置）
    import asyncio
    await asyncio.to_thread(watch_folder.auto_watch_downloader_dir)
    # 历史记录里的文案补齐归档到 文案输出/ 文件夹（幂等，缺哪个补哪个）
    from .archive import backfill
    await asyncio.to_thread(backfill)
    # 启动时把历史记录（含本地预览视频等任务文件）裁到保留上限，并清掉孤儿目录，防止磁盘堆积
    import asyncio

    from .history import (history_keep, history_keep_days, history_store,
                          media_keep_days, sweep_old_media,
                          sweep_orphan_task_files)
    await asyncio.to_thread(history_store.trim, history_keep(), history_keep_days())
    await asyncio.to_thread(sweep_old_media, media_keep_days())
    await asyncio.to_thread(sweep_orphan_task_files)
    # 监听目录 processed/ 的旧原片按天清理（**默认关**：TRANSCRIPT_WATCH_KEEP_DAYS=0）
    await asyncio.to_thread(watch_folder.sweep_processed)
    # 外部下载器（视频号用）：默认**不**自启 —— 它运行期间会把系统代理指向本机 MITM，
    # 而多数时候用户只是转本地文件或用别的平台；需要时在视频号页点一次「一键安装并启动」。
    # 无论是否自启，都做一次"代理体检"：上次强杀/断电可能把系统代理残留在 MITM 上（全机断网）。
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

    # 下载器"空闲自动停"（⑤，默认关）：跑完没人用就停掉，顺带还原系统代理
    try:
        from . import external_dl as _ext

        if _ext.idle_stop_min() > 0:
            _ext.idle_watch_loop()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("external_dl").debug("空闲自动停未启用: %s", exc)

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

    def _startup_ext_dl():
        log = logging.getLogger("uvicorn.error")
        from . import external_dl

        try:
            res = external_dl.sanity_check_proxy()
            # 结果同时发给 external_dl 日志（落 app.log，UTF-8；uvicorn 的会落 stderr 且是控制台编码）
            check_log = logging.getLogger("external_dl")
            if res.get("action") == "restored":
                log.warning("启动体检：系统代理残留在下载器 MITM 上（下载器不在线），已处理 → %s",
                            res.get("detail"))
                check_log.warning("启动体检：已修复残留的系统代理 → %s", res.get("detail"))
            else:
                log.info("启动体检：%s", res.get("detail"))
                check_log.info("启动体检：%s（系统代理=%s）", res.get("detail"),
                               (res.get("proxy") or {}).get("server") or "(未设置)")
        except Exception as exc:  # noqa: BLE001
            log.warning("启动体检失败（忽略）: %s", exc)

        if os.getenv("TRANSCRIPT_EXT_DL_AUTOSTART", "0").strip().lower() not in ("1", "true", "on"):
            return   # 默认不自启：页面上的状态卡会显示离线 + 一键启动按钮
        try:
            # 新机自举：未安装就先自动安装（本地包优先，否则从 GitHub 下载），再启动
            if external_dl.exe_path() is None:
                log.info("外部下载器未安装，开始自动安装（本地包优先，否则从 GitHub 下载约 37MB）...")
                res = external_dl.install()
                log.info("外部下载器自动安装完成: 来源=%s", res.get("source"))
            res = external_dl.start(allow_elevate=False)
            if res.get("started"):
                log.info("外部下载器已自动启动%s", "（原本就在运行）" if res.get("already") else "")
            else:
                log.info("外部下载器自动启动未就绪：%s（页面上点「一键安装并启动」可重试）",
                         res.get("error"))
        except Exception as exc:  # noqa: BLE001
            log.warning("外部下载器自动安装/启动失败（可在页面上点「一键安装并启动」重试）: %s", exc)

    threading.Thread(target=_startup_ext_dl, daemon=True, name="ext-dl-startup").start()

_static = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")


@app.middleware("http")
async def _no_cache_index(request, call_next):
    """index.html 禁用缓存：前端模板改动刷新即生效，避免浏览器拿旧页面。"""
    resp = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------- 退出清理：绝不把系统代理留在下载器 MITM 上 ----------
# 两条路径都登记（uvicorn 优雅退出走 shutdown 事件；解释器正常退出走 atexit），
# 硬杀/断电兜不住 —— 那种情况由下次启动的 sanity_check_proxy() 体检修复。

import atexit  # noqa: E402
import logging as _logging  # noqa: E402

from . import external_dl as _external_dl  # noqa: E402


@app.on_event("shutdown")
async def _shutdown_ext_dl():
    import asyncio

    try:
        res = await asyncio.to_thread(_external_dl.shutdown_cleanup)
        _logging.getLogger("uvicorn.error").info("退出清理：%s", res)
    except Exception as exc:  # noqa: BLE001
        _logging.getLogger("uvicorn.error").warning("退出清理失败（忽略）: %s", exc)


atexit.register(_external_dl.shutdown_cleanup)
