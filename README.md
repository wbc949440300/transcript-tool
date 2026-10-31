# 视频 / 图片 / 文件 文案提取工具

仿照抖虫(dousnap)交互与功能范围的**独立可运行**实现，含历史记录与多格式导出。

## 功能清单

**🎥 视频文案提取**
- 抖音 / 小红书 / 快手 / B站 / 视频号 五个平台入口
- **抖音专用通道**（Playwright 渲染 + aweme_detail API）：无需 cookie，兼容
  `/video/<id>`、`/jingxuan?modal_id=<id>`、`v.douyin.com` 短链
- 其余平台走 yt-dlp + **稳健多源重试**（多格式候选、UA 轮换、断点续传、指数退避）
- 文案来源自适应：有字幕取字幕；无字幕下载音频用 faster-whisper 语音转文字
- **抖音/快手固定走全片 ASR**：作品描述只作为"作品简介"，口播文案一律来自整条视频的语音转写
  （中文平台强制 `language=zh` + VAD 去静音 + beam_size=5，减少同音错字与静音幻觉）

**📚 批量提取**：一次粘贴多条链接，排队逐个处理，带进度与汇总

**♻️ 处理中任务可恢复**：切到其他页面或刷新后回到对应功能页，会自动恢复进度并继续轮询，完成直接出结果（视频 / 文件 / 批量都支持）

**🖼️ 图片文案提取**：RapidOCR 中文识别，支持图片链接与本地上传/拖拽

**📤 上传文件提取**：视频转文字 / 录音转文字（faster-whisper，CPU 可跑）

**🗂 历史记录**：SQLite 持久化（重启不丢），搜索 / 状态筛选 / 分页 / 详情 / 删除 / 清空

**📦 导出下载**（对齐抖虫）
- 5 种格式：纯文本 `.txt`、Markdown `.md`、Word `.docx`、Excel `.xlsx`、**SRT 字幕 `.srt`**（带时间戳）
- 单条与批量（最多 100 条）；批量导出打包为 ZIP，每条记录一个独立文件（文件名 `序号_标题.扩展名`）
- 导出内容：标题、平台、作者、时长、来源链接、提取时间、作品简介、口播文案/识别文字

## 目录结构
```
transcript_tool/
├── run.py
├── requirements.txt
├── transcript_history.db      # 历史记录（自动生成）
├── cookies/                   # 可选：各平台 Netscape cookies.txt
├── check_env.py               # 迁移自检脚本
├── app/
│   ├── main.py                # FastAPI 入口 + 静态托管
│   ├── api.py                 # 全部 REST 路由
│   ├── models.py              # 数据模型
│   ├── extractor.py           # 通用提取 + 抖音分支路由
│   ├── douyin.py              # 抖音 Playwright 提取通道
│   ├── robust_media.py        # yt-dlp 多源多轮重试下载
│   ├── file_transcribe.py     # 上传音视频转写
│   ├── ocr.py                 # RapidOCR
│   ├── batch.py               # 批量排队
│   ├── history.py             # SQLite 历史
│   ├── export.py              # txt/md/docx/xlsx 生成
│   ├── task_store.py          # 任务存储 + 完成自动落库
│   └── utils.py
└── static/index.html          # 前端（导航 + 各功能视图 + 历史 + 导出弹窗）
```

## 多标签页与直达链接（对齐抖虫）

点顶部「文案提取工具」下拉里的任一功能，会在**新标签页**打开（原标签保持不变），
每个功能都有独立 URL 和标签标题，可直接收藏/分享：

| 功能 | 直达地址 |
|---|---|
| 抖音视频文案提取 | `/#/video/douyin` |
| 小红书视频文案提取 | `/#/video/xhs` |
| 快手 / B站 / 视频号 | `/#/video/kuaishou`、`/#/video/bilibili`、`/#/video/qqvideo` |
| 抖音图片文案提取 / 小红书图文提取 | `/#/image/douyin_ocr`、`/#/image/xhs_ocr` |
| 视频转文字 / 录音转文字 | `/#/file/video`、`/#/file/audio` |
| 批量提取 / 历史记录 | `/#/batch`、`/#/history` |

> **所有页面跳转都在新标签打开**：顶部「首页」「历史记录」、下拉功能项、首页平台卡片、历史记录条目
> 都不会影响当前标签页（正在跑的任务保持不动）。历史记录结果页 URL 带 `?task=<id>`，刷新/收藏可还原。

## 启动
```bash
cd transcript_tool
pip install -r requirements.txt
playwright install chromium      # 抖音通道需要（本机已装）
python run.py                    # 打开 http://127.0.0.1:8000
```
环境变量：`PORT` / `HOST`；`TRANSCRIPT_USE_CUDA=1` 启用 GPU；`TRANSCRIPT_DB` 改数据库路径。
项目根 `.env` 会在 app 包导入时统一加载（不覆盖已有进程环境变量）。

## 主要接口
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/prod-api/transcript/doTask` | 创建视频提取任务 |
| GET | `/prod-api/transcript/queryTask?taskId=` | 轮询任务 |
| POST | `/prod-api/transcript/batch/createTask` | 批量创建 |
| GET | `/prod-api/transcript/batch/queryTask?batchId=` | 批量查询 |
| POST | `/prod-api/transcript/image/ocr/doTask` | 图片链接 OCR |
| POST | `/prod-api/transcript/image/ocr/upload` | 图片上传 OCR |
| POST | `/prod-api/file/transcript/doTask` | 上传音视频转写 |
| GET | `/prod-api/history/list` | 历史列表（keyword/status/page/pageSize） |
| GET | `/prod-api/history/detail?taskId=` | 历史详情 |
| DELETE | `/prod-api/history/delete?taskId=` | 删除一条 |
| DELETE | `/prod-api/history/clear` | 清空 |
| GET | `/prod-api/ai-face/exportTask?taskId=&format=&lang=` | 单条导出 |
| POST | `/prod-api/ai-face/exportTasks` | 批量导出 |
| GET | `/prod-api/risk/status` | 抖音/快手风控冷却状态 |
| GET | `/prod-api/ext/tasks?limit=` | 最近视频号下载任务（只读监控） |

`format` 取值：`txt` / `markdown` / `docx` / `excel` / `srt`。

### 接口认证与跨域

`/prod-api/*` 默认要求认证：

- **本机页面**：访问 `/` 时服务端自动下发 `HttpOnly + SameSite=Strict` 会话 Cookie，前端无需保存或拼接 Token。
- **外部脚本 / Halo 插件**：启动前在进程环境变量设置 `TRANSCRIPT_API_TOKEN=...`，请求带 `Authorization: Bearer <token>` 或 `X-API-Token: <token>`。
- **Host 校验**：默认只接受 `127.0.0.1` / `localhost` / `::1`；通过局域网 IP、容器名或反向代理访问时，把主机名加入 `TRANSCRIPT_ALLOWED_HOSTS`（逗号分隔）。
- **跨域默认关闭**：只有显式设置 `TRANSCRIPT_ALLOWED_ORIGINS=https://halo.example`（逗号分隔多个来源）才开放 CORS；Halo 服务端代理调用不需要 CORS。
- Token 和允许来源可写在项目根 `.env`（服务启动时统一加载），也可由启动脚本/服务管理器设置；修改后需重启服务。

## 实测记录
| 项目 | 结果 |
|---|---|
| 抖音 jingxuan?modal_id 链接 | ✅ 无需 cookie 提取成功（MJ《Beat It》、3D弹头等） |
| B站有字幕视频 | ✅ 字幕快路径 |
| B站无字幕口播视频 | ✅ whisper CPU 转写 1749 字 |
| 批量提取 | ✅ 2/2 成功 |
| 图片 OCR 上传 | ✅ 识别准确、行序正确 |
| 文件上传转写 | ✅ SUCCESS |
| 历史持久化（重启后） | ✅ SQLite 保留 |
| 导出 txt/md/docx/xlsx | ✅ 真实下载，Office 文件可被 python-docx/openpyxl 解析 |
| 批量导出 | ✅ ZIP 打包，每条记录一个文件（实测 2 条 → 2 个 .txt） |
| 前端交互 | ✅ 无 JS 错误 |

## 视频预览与保存（媒体代理）

问题：抖音/B站等平台的视频直链带**防盗链**，浏览器 `<video>` 无法携带 Referer，
表现就是播放器一直转圈、也无法另存。

解决：新增服务端代理 `GET /prod-api/media/proxy`
- 服务端按平台补上 `Referer`/`UA` 拉流，再转发给浏览器
- 透传 `Range`，支持拖动进度条（实测 1920x1080、时长 253.33s、可 seek）
- `&download=1` 附 `Content-Disposition: attachment`，可直接**保存视频**
- 直链过期（抖音 CDN 有时效）时返回明确错误，前端**自动隐藏预览**并提示重新提取

实测：
```
Range: bytes=0-1048575  → 206 Partial Content, content-range: bytes 0-1048575/36190171
download=1              → 206 + attachment; filename="video_<taskId>.mp4"
浏览器加载              → readyState=4, duration=253.33, videoWidth=1920（无错误）
失败路径（无媒体）      → 自动替换为"视频源已过期或不可访问，已隐藏预览"
```

> **长视频 / B站 DASH**：预览只选视频流（排除纯音频），代理按平台换 UA 并补 `video/mp4`；
> B站音视频分离，代理预览**无声**（需要声音请打开原平台页面）。
> 封面走 `GET /prod-api/media/cover?taskId=` 代理，避免防盗链裂图。
>
> **B站播放器选项**：B站结果页提供「站内预览（静音）」/「B站播放器（带声音）」切换；
> 默认不加载 iframe，点击才创建官方播放器，切走/切回自动销毁。

## 时间戳与 SRT 字幕

转写时保留 faster-whisper 的原始分段（start/end/text），存进历史记录的 `extra.segments`，
导出时可直接生成 SRT 字幕：

- 单任务 → `transcript.srt`，编号连续、时间戳合法（实测 36 段，覆盖全片 95s）
- 批量 → 多任务按累计时长顺延、编号不重置（实测 36+1 块）
- **旧记录兼容**：早期任务没有分段数据时，自动降级为"整篇文本一条字幕"，不会导出空文件

```
1
00:00:00,000 --> 00:00:04,000
在港中生上学究竟有多爽,今天就带你们来体验一下。
```

## 字段语义（重要，避免误解）

工具里有两个不同的文本字段，含义完全不同：

| 界面字段 | 数据来源 | 说明 |
|---|---|---|
| **作品简介 / 话题标签** | 抖音 `desc`、快手 `caption`、B站 description | 作者写的标题与 #话题，**不是口播内容** |
| **口播文案（全片语音转写）** | faster-whisper 转写整条视频音轨 | 真正覆盖全片的口播文字 |

### 关于抖音"字幕"

**抖音绝大多数视频不提供可抓取的字幕文件。** `aweme_detail` 里的 `caption` 字段是
作品描述/话题（如 `#子弹 #弹头 #3d动画科普`），早期版本误把它当字幕用，
导致你看到的"字幕"其实是话题标签。现已修正：

- `app/douyin.py::extract_subtitles()` 只认真正的字幕字段（`video.subtitle.subtitle_url_list` 等）
- 有真字幕 → 直接用（比 ASR 准）
- 无真字幕 → **一律下载整条视频做全片 ASR**

### 没有口播的视频

纯音乐 / MV / 风景 / 商品展示类视频没有语音，ASR 结果为空是**正确行为**
（不会拿话题标签凑数）。界面会提示：
"未识别到语音：该视频可能没有口播，例如纯音乐/MV/风景片"

## 转写质量（抖音 49kbps 音轨实测）

抖音音轨普遍只有 ~49kbps，直接喂给模型错字很多。以下是同一条视频的实测对比：

| 配置 | 结果 |
|---|---|
| `base` 模型、不预处理 | `你使用狙击枪设计远处的师傅…我把子弹做长一点事实`（设计/事实/设成/原住部 全错） |
| `small` 模型 | 部分修正，仍留 `胡醒步/原住部` 等错词 |
| **`small` + 16k 重采样 + highpass + initial_prompt** | ✅ `你使用狙击枪射击远处的师傅…我把子弹做长一点试试…射程反而降低`（射击×10、射程×6、尾锥×3、圆柱部×2，错词归零） |

当前默认管线（`app/extractor.py` + `app/models.py`）：
1. ffmpeg 转 16k 单声道 + `highpass=f=80` 去低频隆隆声
   （**不加 loudnorm**：实测含枪声的视频里响度归一会把人声压弱、反而伤识别）
2. faster-whisper **`medium` 模型（默认）**，可用 tiny/base/small/medium/large-v3 覆盖
3. `language=zh` + `vad_filter` + `beam_size=5` + `condition_on_previous_text=False`
4. `initial_prompt` 注入领域词（射击/射程/弹头/尾锥/圆柱部/力矩）

`medium` 已设为默认：GPU 上纯转写仅比 `small` 慢约 2 倍（35.9s vs 17.3s），
但术语准确率明显更好（见下节档位实测）。显存或速度吃紧时可退回 `small`
（CPU 上 medium 约为 small 的 3 倍耗时）。

## GPU 加速（默认启用，实测 10 倍）

本机有 **RTX 3050 6GB**，之前因缺 cuBLAS 只能跑 CPU。已通过 pip 安装 CUDA 运行库并自动注入：

```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

`app/extractor.py` 启动时会：
1. 把 `site-packages/nvidia/{cublas,cudnn,cuda_nvrtc}/bin` 加入 DLL 搜索路径
2. `ctranslate2.get_cuda_device_count() > 0` 时用 `cuda/float16`
3. 失败自动回退 `cpu/int8`
4. **模型实例缓存**（默认保留最近 2 个，可用 `TRANSCRIPT_MODEL_CACHE` 调整），避免每次任务重载 5s

### 实测（同一条 4:13 抖音视频）

| 配置 | 纯转写耗时 | 含下载+增强总耗时 |
|---|---|---|
| small / CPU | 124s | ~2.5 分钟 |
| medium / CPU | ~360s | 6-8 分钟 |
| **small / GPU** | **17.3s** | ~45 秒 |
| **medium / GPU** | **35.9s** | **61 秒** |

medium + GPU 的质量仍是最佳：射击×11、射程×6、圆柱部×5、尾锥×5、力矩✓，零 `原住部/胡醒/力距` 错词。

### 相关开关
- `TRANSCRIPT_FORCE_CPU=1` 强制 CPU
- `TRANSCRIPT_MODEL_CACHE=2` 同时缓存几个模型（显存小可设 1）
- `TRANSCRIPT_CPU_THREADS=0` CPU 线程数（0=自动）

## 模型档位实测（同一条 4:13 抖音视频）

| 模型 | 字数 | 关键术语表现 | 加载耗时 |
|---|---|---|---|
| tiny | — | 错字多（聚集枪/设程） | ~1s |
| small | 1156 | 射击×10/射程×6；错：翻转力距、胡醒步、原住部 | ~5s |
| **medium（当前默认，推荐）** | 1183 | **射击×12/射程×6/尾锥×5/圆柱部×5/力矩✓**；无 力距/胡醒/原住部 错词 | ~5s |

medium 实测关键句：
```
你直接做得更尖,会让弧形部和圆柱部相连的部位夹角太大   ← 之前 small 是"胡醒步/原住部"
飞了一会儿就失去平衡翻滚了                          ← 之前是"翻棍"
由于太长导致空气阻力的翻转力矩太大                 ← 之前是"翻转力距"
```

- medium 模型约 1.5GB，本机已缓存，后续转写不再下载
- CPU 上 medium 转写约是 small 的 3 倍耗时（4 分钟视频约 6-8 分钟）
- UI 已提供 tiny / small / medium(默认) / large-v3 四档，可随时切换

## 视频号：抓包导出 + 自动监听（已实现）

> **页面布局**：视频号页是两列——左侧 1/3 是「最近导入」「提取流程 / 下载器监听」「高级选项」，
> 右侧 2/3 只放结果区（视频 + 截图 + 文案）。页面内容居中、最大 1120px（不铺满屏幕），
> 结果视频限高 560px；其他页面仍是原来的窄版式。「文案输出」文件夹入口在高级选项里。

> **结果堆叠**：视频号页连续提取时，新结果插到最上方、旧结果保留在下方（最多 5 条），
> 上方有结果标签可快速切回上一次结果；主提取按钮不会再清空已显示的结果。
> 监听导入的文件任务从视频号页打开时，会**留在视频号页**渲染（视频/截图/文案），不再跳到「视频转文字」。
> 「最近导入的本地视频 / 文件」列出已转写记录（点击打开完整结果）；
> 「文案输出」文件夹入口在高级选项里。本地视频库/最新文案列表已按需求移除。

视频号没有网页可读接口，无法像抖音那样贴链接解析。本工具提供**本机闭环**：

```
抓包工具（Res-downloader 等）导出视频
        ↓ 落到指定目录
本工具「自动监听文件夹」检测到新文件
        ↓ 自动建任务（medium + GPU）
转写完成 → 历史记录 → 导出 TXT/MD/Word/Excel/SRT
        ↓ 需要更准
AI 纠错（qwen3.8-27b）
```

- 前端入口：视频号页面 → **自动监听文件夹** 卡片（填目录 → 开始监听）
- API：`GET /watch/status`、`POST /watch/start`、`POST /watch/stop`
- 环境变量：`TRANSCRIPT_WATCH_DIR` / `TRANSCRIPT_WATCH_MODEL` / `TRANSCRIPT_WATCH_INTERVAL` / `TRANSCRIPT_WATCH_MOVE`
- 实测：放入 wav → 自动建任务 → 转写 SUCCESS → 文件移入 `processed/`

详细操作步骤见 [docs/视频号抓取指南.md](docs/视频号抓取指南.md)。

## AI 语境纠错（DashScope / qwen3.8-27b）

配置在 `.env`（不进 git）：

```
TRANSCRIPT_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
TRANSCRIPT_LLM_API_KEY=<你的 key>
TRANSCRIPT_LLM_MODEL=qwen3.8-27b
TRANSCRIPT_LLM_TIMEOUT=120
TRANSCRIPT_LLM_GLOSSARY=射击,射程,弹头,尾锥,圆柱部,弧形部,力矩,弹着点
```

### 工作方式（不是"让模型重写全文"）

> 2026-10-29 加固：提示词强制"只输出 JSON"（模型原本会输出大段解释，导致慢且无效），
> 分块 250 字、超时 300s、`max_tokens=512`。实测 509 字 75.6s 完成、16 条真实修正。

1. 让模型只输出**替换对 JSON**：`[{"from":"力距","to":"力矩"}]`
2. 本地校验后应用：
   - 长度差 >2 → 拒绝
   - 无共享字符且未命中术语表 → 拒绝（防「师傅→狙击手」这类改写）
3. 原文完整保留，另存 `corrected_text`，可**导出纠错版**（txt/md/docx/xlsx/srt）

### 实测（历史数据：GLM-4-Flash 时期）

> 下表为早期 `glm-4-flash` 配置下的记录。当前默认模型 `qwen3.8-27b`
> 单次耗时约 **8-11 秒 / 1180 字**（见 §模型选择说明），纠错效果更好。

| 项 | 结果 |
|---|---|
| 单次耗时 | 2.5-3 秒（1180 字） |
| 实测改动 | 蛋头→弹头、原柱布→圆柱部、尾追步→尾锥（3 处，全部正确） |
| 误改拦截 | 「师傅→狙击手」被规则拒绝 ✅ |
| 导出纠错版 | ✅ 含「弹头」、不含「蛋头」 |


### 平台无关（三平台实测）

> 同样为 `glm-4-flash` 时期记录，仅证明“纠错与抓取渠道无关”这一点。

AI 纠错作用于**历史记录里的文本**，与抓取渠道无关；抖音/B站/快手实测：

| 平台 | 耗时 | 改动 | 示例 |
|---|---|---|---|
| 抖音 | 1.9s | 2 | 蛋头→弹头 |
| B站 | 14.3s | 13 | 潔哥→洁哥、金刀會→金刀会（繁→简）、基層兵→基层兵 |
| 快手 | 3.5s | 1 | 照授同情→遭遇同情 |

浏览器端同样验证：任意平台的历史记录详情都会出现 **AI 纠错** 按钮，
纠错后显示改动清单，可切换原文/纠错版，可导出纠错版。

### 允许的两类修改

1. **同音字/错别字**（力距→力矩、蛋头→弹头）
2. **繁体转简体**（whisper 对中文常输出繁体；这是逐字转换，不算改写）

### 拦截规则（防改写）

- 长度差 > 2 → 拒绝
- 无共享字符且未命中术语表 → 拒绝（拦掉「师傅→狙击手」）
- 长度 ≥3 且逐字符相似度 < 0.5 → 拒绝（拦掉「顯微鏡裡的事件→显微镜下的世界」）

### 模型选择说明

同一把 key 实测：

| 模型 | 速度 | 质量 | 结论 |
|---|---|---|---|
| `glm-4-flash` | 2-5s | 差（输出 identity 对、`翻转型距`） | 不推荐 |
| `glm-4.5-air` | 13-98s（抖动大） | 好 | 可用 |
| `glm-5-turbo` | 37-52s | 好 | 可用 |
| `glm-5.3-flash` / `flashx` | **>120~300s**（思维链无法关闭） | — | ❌ 不可用 |
| **`qwen3.8-27b`** | **8-11s** | **好**（`设计→射击`、`力距→力矩`、整句繁→简） | ✅ **当前默认** |

- 术语表（`TRANSCRIPT_LLM_GLOSSARY`）对准确率影响最大，按你的领域改这一项即可
- GLM 系列对 `system`/`user` 分角色**不稳定**（常返回 `[]`），`llm_correct.py` 已改为
  **单条 user 消息 + 规则前置**，切换模型时不要改回双角色写法

## 双 ASR 交叉校验（实测记录）

已接入两个"第二引擎"，均为**本地、免 API Key**：

| 引擎 | 依赖 | 模型体积 | 中文表现 |
|---|---|---|---|
| faster-whisper int8（主，当前 medium） | ctranslate2 | medium≈1.5GB / small≈343MB | 基线 |
| Vosk small-cn | vosk | 42MB | ❌ 明显弱于 whisper |
| SenseVoice（sherpa-onnx） | sherpa-onnx | 228MB(int8) | ⭕ 部分术语更准，但会丢内容 |

### 实测（同一条抖音视频，4:13）

> 该组对比在 `small` 为默认时进行，结论（自动替换会劣化）与主模型档位无关。

| 输出 | 字数 | 关键表现 |
|---|---|---|
| whisper small | 1151 | 射击×10、射程×6；错：设计/原住部 |
| **Vosk 自动替换** | 1151 | ❌ **变差**：`师傅`→`是否`、`子弹`→`紫檀` |
| SenseVoice 独立转写 | 877 | ⭕ 射击/试试/射程 更准，但**丢 24% 内容**、无时间戳 |

### 结论与默认行为

**自动替换不可靠**：两个引擎的切分与长度不同，逐字 diff 无法可靠对齐；
Vosk 会污染正文，SenseVoice 虽术语更准但会丢内容。

因此当前默认：
- `TRANSCRIPT_DUAL_ASR=0`（**关闭**，避免劣化与额外耗时）
- 若开启（=1），**只记录分歧、不修改正文**（安全模式）
- `TRANSCRIPT_VOSK_MODEL` / `TRANSCRIPT_SENSEVOICE_DIR` 可指定模型路径

要让纠错真正可靠，需要"裁决者"而非"投票"——即方案 B（LLM 读两份文本+上下文判定），
这也是参考项目 vidknot 的做法（其 DualASRCorrector 用搜索取证 + LLM 保守改写）。

## Cookie 支持（可选）

需要登录态或被风控的内容，可放一份 Netscape 格式 cookies.txt，程序**按平台自动加载**：

```
cookies/
├── douyin.txt        # 抖音：提升受限内容成功率（公开视频通常不需要）
├── xiaohongshu.txt   # 小红书：多数内容需要登录态（web_session/a1）
├── bilibili.txt      # B站：大会员/登录可见内容
├── kuaishou.txt      # 快手：受限内容
└── default.txt       # 其它平台兜底
```

使用策略：默认 **fallback（匿名优先，失败才用）**，见下节「Cookie 与账号安全」。

接入点（三条通道都会用）：
- yt-dlp：`cookiefile` 参数（元数据抓取 + 多源下载）
- 抖音通道：注入 Playwright context
- 快手通道：拼接 `Cookie` 请求头

实测：无 cookie 时流程正常；注入 cookie（含无效值）也不会报错或降级。

**获取方式（推荐）：用项目自带的内置浏览器登录并导出，不需要碰你自己的浏览器**

```bash
python login_cookie.py xiaohongshu          # 打开独立浏览器窗口，登录后自动写出 cookies/xiaohongshu.txt
python login_cookie.py kuaishou             # 同理：kuaishou / douyin / bilibili
python login_cookie.py xiaohongshu --check  # 不弹窗，静默检查已保存的登录态并刷新 cookie
python login_cookie.py xiaohongshu --no-reuse  # 强制新开窗口（默认是复用）
```

**浏览器内核与会话复用**

- **默认用 Microsoft Edge** 打开（自动探测 `msedge.exe`；没有则回退 Chrome/内置 chromium）。
  想强制指定：`--channel msedge` 或 `--channel chrome`。
- **只保留一个标签页**：首次启动时 Edge 会自带一个「新标签页」，脚本会自动把它关掉，
  只留下目标平台页面（不会再出现"多一个空白标签"）。
- **窗口常驻复用**：首次运行启动的窗口（调试端口默认 `9333`）在脚本退出后**保持打开**；
  之后运行同一命令会**直接接管这个窗口**（只切换标签），不再新开窗口。
  关掉窗口后，下次运行会自动重开一个。端口可用 `TRANSCRIPT_LOGIN_CDP_PORT` 或 `--port` 改。

> 说明：登录窗口必须是"我们可控"的浏览器实例（需要调试端口才能导出 cookie）。
> 你自己日常开的 Edge 没有开调试端口，无法直接接管——所以工具用的是**独立的 Edge 配置目录**
> （`cookies/_browser_profile/`），和你日常的 Edge 互不影响。

- **浏览器隔离**：使用独立 profile `cookies/_browser_profile/`（已被 `.gitignore` 忽略），
  与你系统里的 Edge/Chrome 完全隔离——**不读取、不修改**它们的任何数据
- **登录态持久**：下次运行同一命令，若会话未过期则直接刷新 cookie 文件，**无需重新登录**
- **最小导出**：只写目标平台域的 cookie（与 `app/utils.py::_primary_domain` 的过滤规则一致），
  不会把其它站点凭证一起写进文件
- 可选 `--channel msedge` 用 Edge 内核（仍是独立 profile）；`--headless` 用于已登录时静默刷新

其它方式：也可用 "Get cookies.txt" 类扩展导出 → 另存为对应文件名。

⚠️ cookie 等同登录凭证，不要分享、不要提交到 Git。也可用环境变量 `TRANSCRIPT_COOKIE_DIR` 换目录。

### Cookie 与账号安全（重要）

**cookie = 你的登录凭证**。带上它请求，平台看到的就是"你在访问"；自动化行为可能触发风控。
因此本项目默认采用**保护账号**的策略：

| 模式 | 行为 | 适用 |
|---|---|---|
| `fallback`（**默认**） | **匿名优先**；只有匿名失败/无数据时，才带 cookie 重试一次 | 日常使用 |
| `off` | 完全不用 cookie | 只用公开内容，最安全 |
| `always` | 每次都带 cookie | 不推荐（全程暴露账号） |

用环境变量切换：`TRANSCRIPT_COOKIE_MODE=off|fallback|always`

实测验证（探针）：
```
有 cookie 文件时走抖音提取 → 调用顺序 [use_cookie=False]  ← 首次匿名，成功即不用
模拟匿名失败             → 调用顺序 [False, True]        ← 失败才降级用 cookie
```

其它风控缓解措施：
- **平台串行**：抖音、快手抓取加平台级锁，同一时刻只跑一个请求；多标签/批量任务会排队
- **风控冷却**：抖音失败冷却 30 分钟，快手 HTTP 403/429、验证码/短页面冷却 15 分钟；
  冷却写入 `cookies/.cooldown.json`，**重启服务后仍然生效**，期间直接快速失败；
  页面顶部会显示剩余冷却时间
- **批量限速**：批量任务逐条启动，默认间隔 3s（+ 随机抖动），
  可用 `TRANSCRIPT_BATCH_DELAY` 调整（设 0 关闭，不建议）
- **域名节流**：默认同域名 800ms 一次，可用 `TRANSCRIPT_THROTTLE_MIN_MS` 调大
- 三条通道已贯通该策略：yt-dlp（`cookiefile`）、抖音（Playwright 注入）、快手（Cookie 头）

**建议**：
1. 公开视频**不要**放 cookie——现有通道已实测可免登录提取（抖音 Playwright、快手分享页、B站 yt-dlp）
2. 确实需要登录态时，建议用**小号**而不是主号
3. 低频使用；不要 7×24 批量刷
4. cookie 文件等同密码，别分享、别提交到 Git

## 参考实现评估

### suonian/vidknot（影音笔记舱）

体量大得多的项目（11+ 平台 / LLM 结构化笔记 / Obsidian·飞书·Notion 同步）。评估结论：

- ✅ **视频号**：他们同样结论 —— 「微信封闭生态，**无任何自动化下载路径**」，建议桌面抓包工具（res-downloader / putyy）导出后走本地文件；已把这条写进我们前端指引
- ✅ **小红书**：确认需要登录 Cookie（web_session/a1），**图片笔记必须保留 xsec_token**（与我们已做的归一化一致）
- 🔶 **双 ASR 交叉校验**：本地 faster-whisper + 云端 ASR（SiliconFlow）比对 → SequenceMatcher 找差异 → 搜索+LLM 保守纠错。**需付费 API Key**，暂未接入
- 🔶 **知乎/抖音 z 层兜底**：其抖音路径依赖 f2 工具 + Cookie（我们 Playwright 免登录已可用）
- ✅ **Cookie 支持**：已吸收（见上节，按平台自动加载，三条通道贯通）

### quanji081/video-transcriber

（详见下节「时间戳与 SRT 字幕」与本节 Cookie 部分）

| 能力 | 我们 | quanji081 | vidknot |
|---|---|---|---|
| Web 界面 / 历史 / 导出 | ✅ | ❌ | ❌（但会同步 Obsidian/飞书） |
| 抖音 | ✅ Playwright（免登录，实测可用） | ⚠️ HTTP 直连（实测已失效） | ⚠️ f2 + Cookie |
| 快手 | ✅ 专用通道（实测可用） | ⚠️ 无 extractor | ⚠️ 未验证 |
| 小红书 | ⏳ 归一化就绪(+Cookie 后可试) | — | ✅ 需 Cookie |
| 视频号 | ❌ 平台限制 | ❌ | ❌（同结论） |
| 时间戳/SRT | ✅ | ✅ | ✅ |
| Cookie | ✅ 已吸收 | ✅ | ✅ |
| 双 ASR 纠错 | ❌ 待评估 | ❌ | ✅（需 API Key） |

## 架构规划：Go 插件化迁移（目标）

> 当前阶段：仍以 **Python / FastAPI** 在本环境迭代。
> 下文是**后续迁移到 Go 插件化架构**的规划与回向约束，写代码时保持插件边界。

### 总体设计

```
                 ┌────────────────────────────┐
                 │  核心进程 (Go)  核心/宿主     │
                 │  API 网关 · 任务队列 · 存储  │
                 │  历史 · 导出 · 权限/配额     │
                 └──────────┬─────────────────┘
                            │  go-plugin（Hashicorp go-plugin / 或 WASM）
        ┌───────────┬───────┴────────┬───────────────┐
   ┌────┴─────┐ ┌───┴─────┐   ┌──────┴────┐   ┌───────┴────┐
   │ 平台插件   │ │ 媒体/下载 │   │ ASR 引擎  │   │  OCR 引擎   │
   │ douyin    │ │ yt-dlp  │   │ whisper   │   │ rapidocr   │
   │ kuaishou  │ │ (net/http│   │ (可替换)  │   │ (可替换)    │
   │ bilibili  │ │  直连)   │   │           │   │            │
   │ xiaohongshu│ │          │   │           │   │            │
   └───────────┘ └──────────┘   └───────────┘   └────────────┘
```

### 核心（Go 宿主，非插件）

| 领域 | 当前 Python 模块 | 说明 |
|---|---|---|
| HTTP API | `app/api.py` | 用 Go 写路由重写 |
| 任务状态机 | `app/task_store.py` | 内存 + SQLite 持久化 |
| 历史记录 | `app/history.py` | SQLite，保持 schema |
| 导出 | `app/export.py` | txt/md/docx/xlsx/srt |
| 链接归一化 | `app/links.py` | 平台无关的 URL 提取/短链 |

### 插件实现语言：Python（不重写）

> **核心决策（2026-10-29 明确）**：Go 只做**载体/宿主**；各插件（平台抓取 / 下载 / ASR / OCR）
> **继续用 Python 实现**，以**进程外插件**形式挂到 Go 宿主上，供宿主与其它模块复用。
> —— 即：迁移 ≠ 重写，是把现有 `app/*.py` 的能力包装成可被别的模块调用的插件。

**三句话概括（对外 / 对内 / 边界）**

| 维度 | 结论 |
|---|---|
| **对外暴露** | **Go**（宿主暴露 API 与插件协议；外部模块只对接 Go，不直接调 Python） |
| **对内实现** | **仍是 Python**（现有 `app/*.py` 原样作为插件本体，不重写、不重踩坑） |
| **复用方式** | 插件可被**任意模块**调用（如 ASR 插件同时服务"提取"与"笔记/纠错"流程） |

| 层 | 语言 | 职责 |
|---|---|---|
| 宿主（载体） | **Go** | API 网关、任务队列、存储/历史、导出、配额、插件生命周期管理 |
| 插件 | **Python（沿用现有代码）** | 平台抓取（douyin/kuaishou/…）、媒体下载、ASR、OCR、LLM 调用 |

**插件边界 = 进程边界**（子进程 + 协议通信），不是同进程函数调用。

**通信方式（待定，两个候选）**

| 方案 | 说明 | 备注 |
|---|---|---|
| HashiCorp go-plugin（gRPC over stdio） | 跨平台、生态成熟 | 推荐 |
| 子进程 + JSON-RPC/stdio（自研最简协议） | 零依赖，可参照现有 `app/tools/*.py` 风格演进 | 备选 |

**接口形状（语言无关示意）**

```
插件进程启动 → 握手（声明插件名 / 版本 / 能力）
宿主调用:  FetchMeta(url) │ GetMediaURL(meta) │ Transcribe(file) │ Ocr(images)
插件返回:  JSON 结果 + 错误码；超时 / 重试 / 配额由宿主统一控制
```

> ⚠️ **技术约束（重要）**：Go 官方 `plugin` 包（`.so` 动态加载）**不支持 Windows**
> （仅 Linux / FreeBSD / macOS），而本项目开发环境是 Windows
> → **必须走"进程外插件"方案，不要用 `plugin` 包**。

**为什么这样更合适**

1. 现有 Python 代码含大量实战结论（抖音 `caption`≠字幕、快手 `m.gifshow` 通道、
   ASR 参数调优、模型选型），直接复用，不重写、不重新踩坑
2. 插件可被**别的模块**复用：ASR 插件既能服务提取流程，也能服务笔记/纠错流程
3. 跨语言友好：宿主将来若再换语言，只需实现同一套进程协议

### 插件清单（实现语言：Python，沿用现有模块）

每个平台插件实现一个统一接口，例如：

```go
type PlatformPlugin interface {
    Detect(url string) bool               // 是否负责该链接
    Resolve(ctx, raw string) (Link, error) // 归一化链接
    FetchMeta(ctx, url) (Meta, error)      // 标题/作者/封面/时长/字幕
    GetMediaURL(ctx, meta) (string, error) // 直链
}
```

| 插件名 | 现有 Python 实现（= 将来的插件本体） |
|---|---|
| `douyin` | `app/douyin.py`（核心） |
| `kuaishou` | `app/kuaishou.py`（核心） |
| `ytdlp`（通用：B站/小红书/TikTok） | `app/extractor.py` + `app/robust_media.py` |
| `asr`（whisper） | `app/extractor.py::_transcribe` |
| `asr2`（可选 SenseVoice,双ASR） | 预留 |
| `ocr` | `app/ocr.py` |

### 回向约束（当前写代码时注意）

1. **平台能力解耦**：每个平台的抓取逻辑只在对应模块内，不散落在 api/extractor 里。
   目前 douyin = `douyin.py`、kuaishou = `kuaishou.py` 已符合；B站/小红书走 yt-dlp 通用层，可接受。
2. **别把平台专属逻辑写进 `utils.py` / `links.py` 的通用分支**——那是宿主代码。
3. **接口契约稳定**：`TaskDetail` 字段、`history` schema、`export` 格式尽量别大改，
   迁移时按 schema 复刻。
4. **config 走环境变量**，已用 `PORT/HOST/TRANSCRIPT_DB/TRANSCRIPT_COOKIE_DIR/TRANSCRIPT_USE_CUDA`，
   后续 Go 也读同样的 env。

### 迁移顺序建议

1. 核心 API + 任务状态机 + 历史（宿主骨架）
2. ytdlp 通用下载插件（覆盖最多平台）
3. douyin / kuaishou 两个已实战验证的插件
4. ASR / OCR 引擎插件化（可替换性最强）
5. 导出、媒体代理、批量

## 迁移到新电脑

**不需要 cookie。** 抖音走 Playwright 真实浏览器渲染，无需登录态即可提取。

新电脑准备清单：
1. Python ≥ 3.9
2. `pip install -r requirements.txt`
3. `python -m playwright install chromium`  ← 抖音通道必需
4. 安装 ffmpeg 并把其 `bin` 目录加入 **PATH**（yt-dlp 合并/转码音频必需）
5. 复制整个 `transcript_tool` 目录；想保留历史就一并拷 `transcript_history.db`
6. 自检：`python check_env.py`（逐项 PASS/WARN/FAIL 并给出修复命令）

会自动处理、无需手动搬运的：
- **Whisper 模型**：首次转写自动联网下载到 `~/.cache/huggingface`（tiny≈75MB、base≈343MB）；
  想离线可提前拷该缓存目录
- **RapidOCR 模型**：随 pip 包自带 `.onnx`，装完即用
- 代码内**无绝对路径、无写死主机**；前端用同源 `/prod-api`，换机器不用改

可选环境变量：`PORT` / `HOST` / `TRANSCRIPT_DB` / `TRANSCRIPT_TASKS_DIR` / `TRANSCRIPT_USE_CUDA=1`

## 链接支持情况（实测）

输入支持"整段分享文案"：自动从中文/emoji 包裹的文本里提取链接，并跟随短链跳转。

| 平台 | 链接形态 | 实现 | 实测 |
|---|---|---|---|
| 抖音 | `/video/<id>`、`/jingxuan?modal_id=<id>`、`v.douyin.com`、分享文案 | Playwright 渲染 + aweme_detail + **全片 ASR** | ✅ 通过 |
| B站 | `bilibili.com/video/BVxxx`、`b23.tv` | yt-dlp | ✅ 通过 |
| 快手 | `kuaishou.com/short-video/<id>`、`fw/photo/<id>`、`v.kuaishou.com` | **专用通道**：`m.gifshow.com/fw/photo/<id>` 服务端渲染页 | ✅ 通过（标题/作者/95.6s/487字 ASR） |
| 小红书·视频 | `explore/<id>`、`discovery/item/<id>`、`xhslink.com` | yt-dlp XiaoHongShuIE；保留 `xsec_token`；配合 `cookies/xiaohongshu.txt` | ✅ 实测通过 |
| 小红书·图文 | 同上 | **自动取图 → OCR**（yt-dlp 报无视频流时自动回退） | ✅ 实测通过（1208 字） |
| 视频号 | `weixin.qq.com/sph/...` | — | ❌ 平台无网页可读接口 |
| YouTube / TikTok | 标准链接 | yt-dlp | TikTok 未验证；**YouTube 已按需求关闭**（粘贴链接会明确提示） |

补充说明：
- **快手**：www 站无 cookie 返回 `{"result":2}` 风控、移动端跳验证码；但分享页 `m.gifshow.com` 是 SSR，可直读 caption/userName/duration/mp4，无需登录。
- **小红书**：`xsec_token` 已保留；**需先 `login_cookie.py xiaohongshu` 取一次登录 cookie**。
  链接**必须带 `xsec_token`**（App「分享→复制链接」自带），否则会 302 到 404。
  图文笔记无视频流时会**自动取图并 OCR**，无需手动上传。
- **简/繁显示**：结果页「口播文案」右侧有 **简体 / 繁体** 切换，**默认简体**
  （服务端 OpenCC 转换，接口 `POST /prod-api/text/convert`）。
- **视频号**：已在前端给出明确提示，建议"视频转文字"上传。

## 外部下载器集成（视频号 → 转写）

视频号没有网页可读接口。本工具可对接第三方下载器
[`ltaoo/wx_channels_download`](https://github.com/ltaoo/wx_channels_download)：
**它负责取视频，本项目负责转写**。快手/B站等抓取失败时，直链兜底改由本项目本地下载完成。

**视频号的真实流程（对照该仓库实现）**：
下载器以管理员启动并安装证书 → 微信打开视频号并播放视频 → 点下载器注入的下载按钮 →
文件落到 `download.dir` → 本工具监听该目录自动转写。
视频号分享链接**不能直接提交**给下载器 API：`create_by_url` 只接受 mp4/m3u8 等绝对媒体 URL；
视频号相关接口还要求微信视频号页面保持打开（client socket），否则返回
`{"code":400,"msg":"请先初始化客户端 socket 连接"}`。

> v260907 的 `create_by_url` 还有上游缺陷：任务能创建，但 resource `unique_id` 为空，
> 下载会报 `resource unique ID is required`。因此本项目不再把通用直链交给该接口，
> 直链统一走本地下载（支持 mp4/m3u8/mpd）；视频号注入按钮/平台任务仍由下载器负责。

**使用前提**：视频号注入按钮需要下载器以管理员身份运行（会安装根证书并接管系统代理，
默认 API `127.0.0.1:2022`）。直链下载不依赖下载器在线；未安装时界面显示「离线」不影响本地直链。

| 能力 | 说明 |
|---|---|
| 界面入口 | 「视频号视频文案提取」页 → **外部下载器** 卡片（API/代理状态 / 下载目录 / 直链提交） |
| `GET /prod-api/ext/status` | 探活 + 读取 `download.dir` + API/代理是否 listening + 平台可用性 |
| `GET /prod-api/ext/tasks` | 最近视频号下载任务、进度、失败原因（只读，不自动重试） |
| `POST /prod-api/ext/download` | 只接受**绝对媒体 URL**（mp4/m3u8），本地下载并**自动把下载目录加入监听**；视频号分享链接返回 400 + 正确步骤 |

视频号页会展示最近下载任务及失败原因（证书/socket/unique_id 等会给出对应处理提示），
并提示不要同时处理多个视频号下载；该列表只读，不会自动重试或操作下载器。
| 自动兜底 | 常规抓取失败后，用本地**直链**兜底（快手/抖音等）；视频号仍走注入按钮/监听目录 |

配置：`TRANSCRIPT_EXT_DL_URL`（默认 `http://127.0.0.1:2022`）、`TRANSCRIPT_EXT_DL_TIMEOUT`（默认 5 秒）。

> 安全提示：该下载器会安装根证书，可解密本机全部 HTTPS 流量；请只在你信任并需要时运行，
> 不用时按其文档卸载证书与代理。其 `deploy_sph_worker`（云端 Worker + 微信凭证）不建议使用。

## 说明与边界
- 技术演示用途：抓取的是公开视频的元数据/字幕，ASR/OCR 针对你有权检查的内容；
  请遵守各平台服务条款与版权。
- 抖音仍有频率风控：偶发下载/访问失败请重试；高频批量场景需自备登录 cookie。
- 结果页视频/封面预览依赖对方 CDN，可能出现 403 防盗链，不影响文案提取与导出。
- 内存任务表用于运行中状态，历史记录落 SQLite；生产可整体换成数据库。
