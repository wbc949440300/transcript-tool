# 会话交接文档 · 文案提取工具

> 面向后续接手的 agent。记录**当前实现状态、关键决策、踩过的坑、已知限制与待办**。
> 最后更新：2026-10-29

---

## 1. 项目是什么

一个仿抖虫（dousnap.com）的**视频文案提取工具**：粘贴视频链接或上传文件 → 提取元数据与全片口播文案 → 历史记录 → 多格式导出 → 可选 AI 纠错 / 结构化笔记。

### 目标路线

```
现在：Python / FastAPI（单进程，本机运行）
未来：Go 插件化载体
      · 对外暴露 = Go（宿主：API 网关 / 队列 / 存储 / 导出 / 配额 / 插件生命周期）
      · 对内实现 = 仍是 Python（现有 app/*.py 作为插件本体，不重写）
      · 插件以"进程外"形式挂载，供其它模块复用
```

写新代码时**保持插件边界**：平台逻辑放各自模块，别写进通用层。

> 架构细节与"为什么这样"见 README「架构规划：Go 插件化迁移（目标）」。
> ⚠️ 关键约束：Go 官方 `plugin` 包**不支持 Windows**（本项目开发环境是 Windows）
> → 必须走**进程外插件**（HashiCorp go-plugin / 子进程 JSON-RPC），不要用 `plugin` 包。

---

## 2. 环境与启动

| 项 | 值 |
|---|---|
| 项目路径 | `F:\文案提取工具\transcript_tool` |
| Python | 3.11.8（`F:\python`） |
| ffmpeg | `F:\tools\ffmpeg\ffmpeg-9.0.2-essentials_build\bin`（必须在 PATH） |
| GPU | NVIDIA RTX 3050 6GB（CUDA 可用，见 §5.3） |
| 启动 | `python run.py`（默认 8000 端口，`PORT` 可改） |
| 自检脚本 | `python check_env.py` |
| 登录取 cookie | `python login_cookie.py <平台>`（内置浏览器，不动系统浏览器） |
| 健康检查 | `GET http://127.0.0.1:8000/prod-api/health` |

> ⚠️ 本会话多次出现「改了代码但服务仍跑旧版本」。**每次改后端都要重启 uvicorn**。
> 启动方式（PowerShell）：
> ```powershell
> $env:PORT="8000"
> Start-Process python -ArgumentList "run.py" -WorkingDirectory "F:\文案提取工具\transcript_tool" -WindowStyle Hidden -RedirectStandardOutput "server.log" -RedirectStandardError "server_err.log"
> ```

---

## 3. 模块地图（20 个文件）

| 文件 | 职责 |
|---|---|
| `app/main.py` | FastAPI 入口、静态托管、启动时按 env 自动开监听目录 |
| `app/api.py` | **全部 22 条 REST 路由** |
| `app/models.py` | Pydantic 模型（`TaskDetail` 含 `segments`） |
| `app/links.py` | 通用链接处理：提取 URL、短链跳转、按平台归一化、明确错误提示 |
| `app/douyin.py` | 抖音专用通道（Playwright 渲染 + aweme_detail） |
| `app/kuaishou.py` | 快手专用通道（`m.gifshow.com` 分享页） |
| `app/extractor.py` | 任务编排、yt-dlp 通用抓取、ASR、GPU/模型缓存 |
| `app/robust_media.py` | yt-dlp 多源多轮重试下载 |
| `app/dual_asr.py` | 双 ASR（Vosk / SenseVoice）— **默认关闭**，见 §6.2 |
| `app/llm_correct.py` | LLM 语境纠错（替换对 JSON 方案） |
| `app/note_writer.py` | **结构化笔记（map-reduce）** |
| `app/export.py` | 导出 txt / md / docx / xlsx / srt |
| `app/history.py` | SQLite 历史（含 `extra` JSON 存 segments/纠错/笔记） |
| `app/task_store.py` | 内存任务态 + 完成自动落库 |
| `app/batch.py` | 批量队列（含限速） |
| `app/file_transcribe.py` | 上传音视频转写 |
| `app/ocr.py` | RapidOCR 图片文字识别 |
| `app/watch_folder.py` | **监听文件夹**（抓包导出自动转写） |
| `app/utils.py` | 平台识别、cookie 池/节流、进度 |
| `static/index.html` | 单文件前端（约 1000 行，无构建步骤） |

---

## 4. 已实现功能与验证数据

### 4.1 平台抓取

| 平台 | 实现 | 实测 |
|---|---|---|
| 抖音 | Playwright 无头渲染 → 拦截 `aweme/detail` → 标题/作者/封面/时长/直链 | ✅ 需 61s 全流程（medium/GPU） |
| 快手 | `https://m.gifshow.com/fw/photo/<id>` 服务端渲染页解析 | ✅ 500 字转写 |
| B站 | yt-dlp + 多源重试 | ✅ 1749 字 |
| 小红书·视频 | yt-dlp `XiaoHongShuIE`（保留 `xsec_token`）+ cookies | ✅ 2026-10-29 实测通过（30.7s 视频，SUCCESS） |
| 小红书·图文 | **自动取图 → RapidOCR**（`app/xiaohongshu.py`） | ✅ 实测通过（1208 字，标题正确） |
| 视频号 | ❌ 无网页接口 → 抓包 + 监听目录 | ✅ 监听链路实测通过（链接不可行） |

> **全平台链接验证（2026-10-29 一次跑完）**：抖音 6090 字 / 快手 509 字 / B站 1747 字 /
> 小红书 2609 与 2473 字 —— **5/5 全部 SUCCESS**。

> **剩余功能验证（2026-10-29）**：上传音频/视频转写、图片 OCR（上传+URL）、批量提取
> （batch query: done=2/success=2）、导出 txt/markdown/docx/excel/srt 五种、历史列表/搜索/
> 筛选/详情/删除、媒体代理（206 透传）、AI 生成笔记（1690 字 / 66s）—— **全部通过**。
> 注：批量创建接口只返回 `{batchId,total}`，逐条结果要用 `/transcript/batch/queryTask`；
> `history/detail` 对已删除任务返回 **404**（不是 500），验证删除时要按 404 判断。
| YouTube/TikTok | yt-dlp | 未重点测 |

### 4.2 转写

- 引擎：faster-whisper（ctranslate2 4.8.4）
- 默认模型：**medium**
- 预处理：ffmpeg `16k 单声道 + highpass=f=80`（**不要加 loudnorm**，实测会压弱人声）
- 解码参数：`language=zh` + `vad_filter` + `beam_size=5` + `condition_on_previous_text=False` + `initial_prompt` 注入领域词
- **GPU 实测**：4:13 视频，small 17.3s / medium 35.9s（纯转写）；全流程 medium 61s
- **CPU 实测**：small 124s / medium ~360s

### 4.3 其他

- 批量提取（逐条 + 3s 间隔 + 抖动）
- 图片 OCR（RapidOCR，中文准确率 0.999）
- 上传视频/录音转写
- 历史记录：SQLite 持久化、搜索、状态筛选、分页、删除、清空
- 导出：txt / md / docx / xlsx / **srt（带时间戳）**
- 媒体代理：解决视频防盗链，支持预览 + 拖动 + 保存（Range 透传）
- 视频号监听目录：自动建任务 → 转写 → 移入 `processed/`
- **AI 纠错**（qwen3.8-27b）：只输出替换对 JSON，本地校验后应用
- **结构化笔记**（qwen3.8-27b）：map-reduce，YAML + 7 章节 + 完整转录

  实测（抖音 1164 字）：**114.4 秒**，2 块 + 1 次汇总 → 11 条要点、6 条金句、8 个术语、3224 字 Markdown；
  `GET /ai/note/download` 返回 200 / 8382 字节（中文文件名走 RFC5987）

---

## 5. 配置

### 5.1 `.env`（在项目根目录，已加入 `.gitignore`）

```
TRANSCRIPT_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
TRANSCRIPT_LLM_API_KEY=sk-...            # 已配置，勿外传
TRANSCRIPT_LLM_MODEL=qwen3.8-27b
TRANSCRIPT_LLM_TIMEOUT=120
TRANSCRIPT_LLM_GLOSSARY=射击,射程,弹头,尾锥,圆柱部,弧形部,力矩,弹着点
```

其他可选 env：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TRANSCRIPT_COOKIE_MODE` | `fallback` | `off` / `fallback`(匿名优先) / `always` |
| `TRANSCRIPT_COOKIE_DIR` | `cookies` | cookie 目录 |
| `TRANSCRIPT_THROTTLE_MIN_MS` | `800` | 同域名最小间隔 |
| `TRANSCRIPT_BATCH_DELAY` | `3.0` | 批量逐条间隔（秒） |
| `TRANSCRIPT_RISK_COOLDOWN` | `1` | 平台风控冷却总开关（0 关闭，不建议） |
| `TRANSCRIPT_DOUYIN_COOLDOWN` | `1` | 抖音冷却开关；触发后 30 分钟 |
| `TRANSCRIPT_KUAISHOU_COOLDOWN` | `1` | 快手冷却开关；403/429/验证码/短页面触发 15 分钟 |
| `TRANSCRIPT_MODEL_CACHE` | `2` | 同时缓存几个 whisper 模型（显存小设 1） |
| `TRANSCRIPT_FORCE_CPU` | `0` | 设 1 强制 CPU |
| `TRANSCRIPT_USE_CUDA` | — | 历史遗留，现由自动探测取代 |
| `TRANSCRIPT_DUAL_ASR` | `0` | 双 ASR（默认关，见 §6.2） |
| `TRANSCRIPT_WATCH_DIR` | 空 | 启动即开启对某目录的监听 |
| `TRANSCRIPT_NOTE_WORKERS` | `3` | 笔记 map 阶段并发数 |
| `TRANSCRIPT_DB` | `transcript_history.db` | 历史库路径 |

### 5.2 Cookie（可选）

```
cookies/
├── douyin.txt / xiaohongshu.txt / bilibili.txt / kuaishou.txt / youtube.txt
└── douyin/account_1.txt, account_2.txt ...   # 账号池，自动轮换 + 冷却
```

- 三条通道都接：yt-dlp（`cookiefile`）、抖音（Playwright `add_cookies`）、快手（Cookie 头）
- **默认匿名优先**，失败才带 cookie；同一文件内**按目标域名过滤**，不外发其他站点凭证
- **获取 cookie 用 `login_cookie.py`**（内置浏览器登录 → 导出 Netscape 文件，2026-10-29 定稿）：
  `python login_cookie.py xiaohongshu`（可选 `--check` 静默刷新 / `--channel msedge` / `--headless`）
  - 只用自己的 profile `cookies/_browser_profile/`，**不碰系统 Edge/Chrome**（后者是 v20 App-Bound 加密，外部无法解密，直接绕开）
  - **窗口复用（2026-10-29 加）**：首次运行以"常驻 + `--remote-debugging-port`（默认 9333）"方式启动，
    脚本退出后窗口保留；后续运行用 `connect_over_cdp` **接管同一窗口**（只切换标签），不再每次新开窗口。
    `--no-reuse` 强制新开；`--check` 仍为无窗口静默模式；关闭窗口后下次自动重开
  - **浏览器内核**：默认**优先 Microsoft Edge**（`msedge.exe`），其次 Chrome，最后内置 chromium；
    可用 `--channel msedge|chrome` 指定。Edge/Chrome 首启自带的「新标签页」会被脚本自动关闭，
    **最终只保留一个平台标签页**（用户反馈过"多出一个空白标签/新窗口"的问题）
  - 登录态持久，会话未过期时再次运行即刷新 cookie，无需重登
  - 导出只含目标平台域；**不写 `#HttpOnly_` 前缀**（`app/utils.py` 解析器会跳过 `#` 行）
  - 策略保持 `TRANSCRIPT_COOKIE_MODE=fallback`，建议用小号；`cookies/*.txt` 与 `_browser_profile/` 均已 gitignore

### 5.3 GPU（重要）

```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

启动时 `app/extractor.py::_enable_cuda_dlls()` 会把
`site-packages/nvidia/{cublas,cudnn,cuda_nvrtc}/bin` 加入 `add_dll_directory` + PATH。
否则 ctranslate2 会报 `cublas64_12.dll is not found`。

---

## 6. 关键决策与踩坑（最高价值）

### 6.1 抖音 `caption` ≠ 字幕

`aweme_detail` 里 `caption` 是**作品描述/话题**，早期版本误当字幕用，导致"文案"是 `#子弹 #弹头…`。
现在 `douyin.extract_subtitles()` 只认真正的字幕字段（`video.subtitle.subtitle_url_list` 等），
没有就用 ASR。**抖音/快手绝大多数视频没有字幕文件。**

### 6.2 双 ASR 自动替换会劣化，不要开

实测同一条视频：

| 方案 | 结果 |
|---|---|
| whisper small | 射击×10、射程×6 |
| Vosk 自动替换 | ❌ `师傅→是否`、`子弹→紫檀` |
| SenseVoice | ⭕ 术语更准，但**丢 24% 内容**、无时间戳 |

结论：交叉校验需要"裁决者"（LLM），不是"投票"。`TRANSCRIPT_DUAL_ASR` 默认 0；开 1 也只记录分歧、不改正文。

### 6.3 LLM 纠错必须用"替换对 JSON"（2026-10-29 加固）

> **新坑**：`qwen3.8-27b` 对"只输出 JSON"的长指令**不遵守**——实测会写 900+ 字的
> "两个版本"解释文，导致：①每次调用 70-300s ②`_parse_pairs` 解析不到 → **看起来"没效果"**。
> 加固：①提示词开头即声明"只输出 JSON"，结尾加长度上限 300 字、禁解释/标题/代码块
> ②payload 加 `max_tokens: 512` ③分块 500→**250 字**（`TRANSCRIPT_CORRECT_CHUNK`）
> ④超时 120→**300s**（`.env`）。实测 509 字：**75.6s、2 块、0 失败、16 条真实修正**。

- 让模型输出**全文**会被改写（`师傅→弹头`）
- 正确做法：模型只出 `[{"from":"力距","to":"力矩"}]`，本地校验后应用
- 拦截规则：长度差 >2 拒绝；无共享字且不在术语表拒绝（多字）；长度≥3 且相似度<0.5 拒绝
- **单字替换要放行**（否则 `這→这` 这种繁→简会被误杀）

### 6.4 模型选型（同一 key 实测）

| 模型 | 速度 | 质量 | 结论 |
|---|---|---|---|
| `glm-4-flash` | 2-5s | 差（输出 identity 对、`翻转型距`） | 不推荐 |
| `glm-4.5-air` | 13-98s（抖动大） | 好 | 可用 |
| `glm-5-turbo` | 37-52s | 好 | 可用 |
| `glm-5.3-flash` / `flashx` | **>120~300s**（思维链无法关闭） | — | ❌ 不可用 |
| **`qwen3.8-27b`** | **8-11s** | **好**（`设计→射击`、`力距→力矩`、整句繁→简） | ✅ **当前默认** |

> 关键经验：GLM 系列对 `system/user` 分角色**不稳定**（常返回 `[]`），
> 改成**单条 user 消息 + 规则前置**才稳定。`llm_correct.py` 已改为此写法。

### 6.5 async 端点里不能跑阻塞调用

`/ai/correct` 曾经是 `async def` 内部直接调 `urllib` → **整个服务被卡住**（期间 health 也超时）。
现已全部改 `await asyncio.to_thread(...)`。

### 6.6 长文本必须分块

- vidknot 的做法是 8000 字**直接截断**（丢内容）→ 我们不采用
- 我们：纠错按 500 字、笔记按 800 字分块；笔记用 **map-reduce**，完整转录原样附在文末
- 曾出现单块 1749 字超 120s 超时 → 分块后解决

### 6.7 视频号：微信闭环，无自动化下载

- `weixin.qq.com/sph/...` → 302 → `channels.weixin.qq.com/finder-preview/...`，页面仅 2.6KB 空壳，无任何元数据
- 微信 4.x 的 `xwechat_files\<wxid>\msg\video` **是空的**，视频号不是可用 mp4 缓存
- 参考项目 vidknot 结论一致：「无任何自动化下载路径」
- **我们的方案**：抓包工具导出 → `watch_folder` 自动监听转写（详见 `docs/视频号抓取指南.md`）

> **外部下载器调研（2026-10-30）**：`ltaoo/wx_channels_download`（Go，10k+ star，活跃）
> 用"管理员 + 装根证书 + 本地代理(127.0.0.1:2023) + 注入微信 PC 视频号页面"的方式，
> 在播放器下方加下载按钮，支持原始画质，也支持 B站/YouTube/快手。
> **与我们的最佳集成**：把它的 `download.dir` 指向本项目的监听目录 →
> 微信里点下载 → 自动转写（无需改我们的代码）。
> ⚠️ 风险：会安装根 CA 并接管系统代理（可解密本机 HTTPS）、需管理员权限、第三方二进制需校验 checksum。
> 其"分享链接解析"功能需要在其页面输入凭证（等价于第三方解析服务），与我们的链接方案无关。
> **本机限制**：虚拟机无音频端点，录屏方案只能出无声视频；硬字幕可用 OCR 兜底。

### 6.7b 外部下载器集成（wx_channels_download，2026-10-30）

**目标**：视频号没有网页可读接口；用第三方下载器 `ltaoo/wx_channels_download` 负责取视频，
本项目负责转写（"它下载、我们转写"），并作为**快手/B站等下载失败的兜底**。

| 组成 | 说明 |
|---|---|
| `app/external_dl.py` | HTTP 客户端：`probe()` 探活+读目录/API/代理状态、`local_download()` 本地直链下载、`download_and_wait()` 供抓取兜底；`download()` 仅保留给后续修复版本/平台特定任务 |
| `GET /ext/status` | 前端展示：在线/离线、下载目录、**API/代理是否 listening**、可用平台 |
| `GET /ext/tasks` | 最近视频号下载任务、进度、失败原因（只读监控，不触发重试；页面每 12s 刷新） |
| `POST /ext/download` | 只接受**绝对媒体 URL**（mp4/m3u8），本地直链下载后自动纳入 watch_folder；不依赖外部下载器在线；视频号分享链接返回 400 + 注入按钮步骤 |
| 兜底 | `extractor._external_fallback()`：有直链时（快手/抖音等）使用本地直链下载；视频号不适用 |
| 视频号链接 | **不再放行**：`api.create_task` 固定返回注入按钮/socket 提示 |

**对照仓库当前实现的关键结论**（2026-10-30 核对 `ltaoo/wx_channels_download` 源码/OpenAPI）：
- `POST /api/v1/download_task/create_by_url` 只接受**绝对媒体资源 URL**，不是平台分享页链接；
- **v260907 的 `create_by_url` 存在上游缺陷**：任务能创建，但资源 `unique_id` 为空，
  异步下载报 `resource unique ID is required`；因此通用直链改走本地下载，不再提交给该接口；
- 视频号没有“提交分享链接直接下载”的 API：正确流程是下载器注入的下载按钮（微信里播放后点击）；
- `/api/channels/*` 视频号接口要求微信视频号页面保持打开并初始化 client socket，否则返回
  `{"code":400,"msg":"请先初始化客户端 socket 连接"}`；
- `/api/status` 返回 `data.api.listening` / `data.proxy.listening`，代理未启动时注入按钮/视频号下载不可用。

配置：`TRANSCRIPT_EXT_DL_URL`（默认 `http://127.0.0.1:2022`）、`TRANSCRIPT_EXT_DL_TIMEOUT`（默认 5s）。

**已验证**：
- `/ext/status` 在线/离线判定正确（注意：probe 不能用"没抛异常"当在线，必须任一请求成功）
- `/ext/download`（本地直链 mp4 / m3u8）→ 文件落地；watch 可继续转写
- 视频号分享链接 → `/ext/download` / `create_task` 均返回 400 + 注入按钮/socket 步骤（不会误提交给下载器）
- 本地直链返回 HTML 会明确拒绝，不会把网页存成媒体文件
- `download()` 检测到 create_by_url 生成的 resource `unique_id` 为空时直接报错，不再假成功

⚠️ 真实使用前提：下载器需以**管理员**运行并安装根证书（可解密本机 HTTPS）；`deploy_sph_worker`
需要微信凭证，不建议使用。详细风险见本项目 README「外部下载器」一节。

### 6.8 其他坑

- **媒体防盗链**：浏览器 `<video>` 无法带 Referer → 必须走 `/prod-api/media/proxy`（服务端补 Referer，透传 Range）
- **抖音直链有时效**（数小时），历史记录里旧任务的视频可能已过期；文案不受影响
- **HELD/DELETE 语义**：`/media/proxy` 的 `HEAD` 返回 404 是正常的（只注册了 GET），浏览器播放不受影响
- **whisper 常输出繁体**（`上學`、`金刀會`）→ 由 LLM 纠错做繁→简
- **模型缓存要限个数**：显存 6GB，`TRANSCRIPT_MODEL_CACHE=2`，超了会 OOM
- **视图切换会丢结果**（已修，2026-10-29）：`showView()` 对 view 做整块
  `innerHTML=views[id]()` 重渲染，原先没有保存结果区 → "提取完 → 点历史记录 → 切回来"
  结果卡（视频预览/导出/AI 按钮）全没了。
  修法：`static/index.html` 里加了**结果快照** `RESULT_CONTAINERS` + `snapshotResult()` /
  `restoreResult()`——离开前按 `视图+平台key` 缓存结果容器 HTML，切回同视图时原样还原
  （按钮走 document 级事件委托，还原后无需重绑）。
  ⚠️ 以后新增视图或改结果容器 id，记得同步更新 `RESULT_CONTAINERS`。
- **前端路由与多标签页（2026-10-29 加）**：对齐抖虫——**点下拉里的功能会在新标签页打开**，
  原标签保持不变。实现：
  - 每个功能有独立 hash 路由：`#/video/<douyin|xhs|kuaishou|bilibili|qqvideo>`、
    `#/image/<douyin_ocr|xhs_ocr>`、`#/file/<video|audio>`、`#/batch`、`#/history`、`#/home`
  - 下拉项点击执行 `window.open(url,'_blank')`；`init()` 按 hash 打开对应视图；
    顶部「首页/历史记录」、首页平台卡片、历史记录条目也统一 `window.open` 新标签打开（2026-10-30 调整），当前标签保持不变
  - 每个视图有独立 `document.title`（标签栏可区分，如"抖音视频文案提取 - 文案提取工具"）
  - ⚠️ **新增视图时必须同步更新** `urlFor()` / `routeFromHash()` / `PAGE_TITLES` 三处
- **`_host_of` NameError（2026-10-29 修）**：`app/robust_media.py` 调用了未导入的
  `_host_of`，导致"多源重试下载"路径直接崩（小红书/部分 B站 任务报
  `name '_host_of' is not defined`）。已改为 `utils._host_of_url`。
  同类隐患已用脚本全量扫描：**无其他未定义的下划线函数**。
- **VAD 可能把整段语音过滤掉（2026-10-29 修）**：实测 B站 MV 的音频在
  `vad_filter=True` 下**全部被过滤 → 0 字却判 SUCCESS**。
  现在 ASR 三级回退：`zh+VAD` → `zh+关VAD` → `自动语种+关VAD`；
  且新增**语种预检**（`detect_language` 只接受 ndarray，阈值 0.4）——
  中文平台遇到英文音频不再输出"中文伪翻译"（实测 B站英文 MV 从 928 字中文乱码 → 1747 字英文原文）。
- **小红书笔记必须带 `xsec_token`**：无 token 的 `/explore/<id>` 会 302 到 404。
  取图时只认 `sns-webpic`/`sns-img` 域，排除站点图标(`fe-static`)与头像(`sns-avatar`)。
- **AI 纠错仍慢（2026-10-29 待决）**：分块已改为并发（`TRANSCRIPT_CORRECT_WORKERS`，默认 4），
  但实测单块 500 字仍会打满 `TRANSCRIPT_LLM_TIMEOUT=120`，4 块里 3 块超时。
  可选方案：缩小分块（如 250 字）/加大超时/换更快模型（详见 §9）。
- **视频号结果堆叠（2026-10-30 完成）**：`#vresult[data-stack]` **只在视频号页**启用；
  新结果插到最上方、旧结果保留（最多 5 条），顶部「结果标签」可跳转；
  主提取按钮不再清空堆叠；快照恢复时按卡片重新初始化简/繁开关；其他平台仍是单结果替换。
  监听导入的 `file_transcript` 任务从视频号页打开时，`renderHistoryResult` 会把目标改回
  `video/qqvideo`，留在视频号页渲染视频/截图（直接走 `#/file/...` 仍是视频转文字页）。
- **本地视频库接口保留**：`GET /watch/media` / `GET /watch/media/file` 仍在（Range 播放），
  但视频号页的「本地视频库」UI 入口已按用户要求移除（2026-10-30）；「最近导入」保留。
- **视频号页两列布局（2026-10-30）**：容器加 `qq-wide`（居中最大 1120px，不铺满屏幕），`.qq-layout` 左 1fr / 右 2fr；
  左侧依次「最近导入」「提取流程 / 下载器监听」「高级选项」，右侧只放结果（视频/截图/文案）；
  结果视频 `max-height:min(62vh,560px)` 居中。DOM 由 `layoutQqPage()` 在 `bindViewEvents()` 里重排，其他平台页保持原版式。
- **前端改完不用重启**（静态文件实时读），**后端改完必须重启**
- **HTTP 头只能 latin-1**：`Content-Disposition` 里放中文文件名会 500，
  必须用 `filename*=UTF-8''<percent-encoded>` + ASCII 兜底名（已修）

### 6.9 长视频预览黑屏（B站 DASH，2026-10-30 修）

实测 B站 57 分钟视频：预览只有黑屏 + 时间轴，封面裂图。根因有三层：

1. `videoUrlList` 把**纯音频流**（B站 30216/30232/30280 `.m4s`）排在前面，预览取 `index=0` → 只有音频。
   现在 `extractor._pick_video_urls()` 只收 `vcodec != none` 的视频流，AVC 优先、分辨率优先 720p；
   旧记录由 `api._resolve_media_url()` 过滤 `-1-302xx.m4s` / `mime=audio`。
2. B站 CDN 对**桌面 Chrome UA + Range** 直接断连（实测 WinError 10054；`Mozilla/5.0`/移动 UA 正常）。
   媒体代理按平台换 UA：B站用 `Mozilla/5.0`，其余保持 Chrome UA。
3. CDN 对 `.m4s` 返回 `application/octet-stream`，`<video>` 不解析 → 代理补 `video/mp4`（`.m4a` → `audio/mp4`）。

另外：`/media/proxy` 与 `/media/cover` 改为**同步端点**（FastAPI 线程池），避免阻塞事件循环；
封面走 `/media/cover` 补 Referer，修掉 HDSLB 裂图。

实测：57 分钟 B站记录 `readyState=4`、640×360、可 seek 到 60s；封面 200。
**限制**：B站 DASH 音视频分离，代理预览为**纯视频（无声）**，「保存视频」同样没有音轨。
已加官方播放器选项：B站结果页可切到 `player.bilibili.com` iframe（带声音）；默认仍是代理静音预览，
iframe 按需创建、切视图/切回自动销毁。

---

## 7. API 清单（28 条，前缀 `/prod-api`）

```
POST   /transcript/doTask                 创建视频提取任务
GET    /transcript/queryTask             轮询任务
POST   /transcript/batch/createTask      批量创建
GET    /transcript/batch/queryTask       批量查询
POST   /transcript/image/ocr/doTask      图片链接 OCR
POST   /transcript/image/ocr/upload      图片上传 OCR
POST   /file/transcript/doTask           上传音视频转写
GET    /media/proxy                      媒体代理（?taskId=&index=&download=1）
GET    /media/cover                      封面代理（?taskId=，补 Referer 防裂图）
GET    /history/list|detail              历史
DELETE /history/delete|clear             删除/清空
GET    /ai-face/exportTask               单条导出（?format=txt|markdown|docx|excel|srt&corrected=1）
POST   /ai-face/exportTasks              批量导出（ZIP，每条记录一个文件）
GET    /ai/status                        LLM 状态
POST   /ai/correct                       语境纠错（{taskId}）
POST   /ai/note                          生成结构化笔记（{taskId}）
GET    /ai/note/download                 下载笔记 .md（?taskId=）
POST   /watch/start|stop                 监听目录
GET    /watch/status                     监听状态
GET    /watch/media                      本地视频库列表（监听目录 + processed）
GET    /watch/media/file                 播放/下载本地视频（Range）
POST   /text/convert                      简繁转换（{text,to:s|t}，OpenCC）
GET    /ext/status                        外部下载器状态（online/download_dir/api/proxy listening/platforms）
GET    /ext/tasks                         最近视频号下载任务（只读监控）
POST   /ext/download                      按绝对媒体 URL 下载并自动监听目录；视频号分享链接返回 400 提示
GET    /health                           健康检查
```

> 所有 `/prod-api/*` 默认需要认证：本机页面加载时自动下发 HttpOnly + SameSite=Strict Cookie；
> 外部脚本/Halo 插件需在启动服务前设置 `TRANSCRIPT_API_TOKEN`，请求带
> `Authorization: Bearer <token>` 或 `X-API-Token`。跨域默认关闭；确需浏览器跨域时设置
> `TRANSCRIPT_ALLOWED_ORIGINS`（逗号分隔来源），Halo 服务端代理调用不需要 CORS。
> Host 默认只允许 `127.0.0.1/localhost/::1`；通过局域网 IP、容器名或反代访问时把主机名加入
> `TRANSCRIPT_ALLOWED_HOSTS`。这些配置可写在项目根 `.env`，服务启动时会统一加载。

---

## 8. 已知限制

1. **小红书**：视频与图文均已打通。视频走 yt-dlp + cookie；图文在 yt-dlp 报
   `No video formats found` 时**自动回退**为"浏览器渲染取图 → OCR"。
   ⚠️ 关键限制：**笔记链接必须带 `xsec_token`**（App 里"分享→复制链接"才有），
   否则页面 302 到 404（`当前笔记暂时无法浏览`），此时会明确提示重新复制链接
2. **视频号无法直接解析**：只能抓包导出 + 监听目录
3. **AI 纠错不稳定**：qwen3.8-27b 较好，但仍会漏改/偶发误改；术语表（`TRANSCRIPT_LLM_GLOSSARY`）对效果影响最大
4. **笔记生成较慢**：2 块 + 汇总约 114s；长视频会线性增长（并发 3）
5. **任务态在内存**：服务重启后运行中任务丢失（历史记录已落库，不受影响）
6. **单进程串行 ASR**：`_WHISPER_LOCK` 保证一次一个转写（保护 6GB 显存）
7. **平台风控仍可能发生**：抖音/快手已加平台级串行锁、持久化冷却（抖音 30min、快手 15min）
   和批量 3s 间隔，但同一 IP 高频请求仍可能触发 403/验证码；冷却期间会快速失败，
   需要等待或降低频率。视频号依赖下载器/微信链路，失败与限流无关，需看下载器状态。

---

## 9. 待办 / 下一步建议

| 优先级 | 事项 | 说明 |
|---|---|---|
| 中 | AI 纠错进一步提速 | 已分块 250 + 超时 300 + 并发 4，509 字约 75s；长文案仍线性增长，可考虑更快的模型档位 |
| 中 | 术语表界面可编辑 | 当前是军事领域词，对穿搭/游戏内容触发少；做成界面可配置 |
| 低 | 简繁转换缓存 | 目前每次渲染调一次 `/text/convert`，可加服务端 LRU |
| 中 | 术语表做成界面可编辑 | 目前只能改 `.env`，不同领域要手动换 |
| 中 | 笔记生成分块并发可调 | 长视频慢，可提高 `TRANSCRIPT_NOTE_WORKERS` |
| 中 | 笔记入库检索 | `extra.note_markdown` 已存，可加全文搜索/tags 筛选 |
| 低 | Go 插件化骨架 | 按 README「架构规划」章节顺序 |
| 低 | 批量导出纠错版 | 目前 `corrected=1` 单条可用，批量也支持但需验证 |

---

## 10. 复现验证（可直接跑）

> 接口已加认证：下面的 curl 需要带 `-H "Authorization: Bearer <TRANSCRIPT_API_TOKEN>"`；
> 浏览器页面访问 `/` 会自动获得会话 Cookie。启动服务时先设置 `TRANSCRIPT_API_TOKEN`。

```powershell
cd F:\文案提取工具\transcript_tool
python check_env.py                     # 环境自检
python run.py                           # 启动

# 健康 + LLM 状态
curl http://127.0.0.1:8000/prod-api/health
curl http://127.0.0.1:8000/prod-api/ai/status

# 取 cookie（可选，小红书等需要登录态时；会弹出独立浏览器窗口）
python login_cookie.py xiaohongshu

# 抖音提取（约 60s）
curl -X POST http://127.0.0.1:8000/prod-api/transcript/doTask `
  -H "Content-Type: application/json" `
  -d '{\"workUrl\":\"https://www.douyin.com/video/7696763176490192155\",\"whisperModel\":\"medium\"}'

# 历史 & 导出
curl "http://127.0.0.1:8000/prod-api/history/list?page=1&pageSize=5"
curl -o out.txt "http://127.0.0.1:8000/prod-api/ai-face/exportTask?taskId=<id>&format=txt"
curl -o out.srt "http://127.0.0.1:8000/prod-api/ai-face/exportTask?taskId=<id>&format=srt"
```

---

## 11. 交接注意

- `.env` 与 `cookies/` **含真实凭据**，不要提交、不要外发
- 额度：qwen3.8-27b 约 1M tokens，一条 4 分钟视频的笔记约 6-10k，约 100-150 条
- 改代码后先 `python -m py_compile app/*.py`（Windows 下用 `glob`），再重启
- 凡是"看起来像 `{{EMAIL_xxx}}` / `{{SECRET_xxx}}`"的显示内容，多半是终端显示层对 `@router` / 密钥的打码，**以文件实际内容为准**（用 `repr()` 核实）
- 历史库 `transcript_history.db` 中已有 40+ 条测试记录，交接前可按需清理

