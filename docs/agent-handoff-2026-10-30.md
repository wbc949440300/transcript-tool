# Agent 交接文档 · 文案提取工具（2026-10-30）

> 补充于长期文档 `HANDOFF.md`，记录**本日会话的完整上下文**。阅读顺序：本文 → HANDOFF.md → README.md。

## 0. 一句话现状

功能已全量验证通过（链接提取 5 平台 + 上传/批量/导出/历史/OCR/AI 纠错/生成笔记）。
外部下载器（wx_channels_download）**已跑通**（API/代理/证书全就绪，见 §4 注），
且已支持**一键安装/启动/停止**（§6）；另修复小红书视频预览丢失（§5）。

## 1. 环境

| 项 | 值 |
|---|---|
| 项目路径 | `F:\文案提取工具\transcript_tool` |
| 启动 | `python run.py`（默认 8000，`PORT` 可改） |
| 健康检查 | `GET http://127.0.0.1:8000/prod-api/health` |
| Python / ffmpeg | 3.11.8（F:\python） / 9.0.2（PATH） |
| GPU | RTX 3050 6GB（CUDA 可用） |
| 注意 | **后端改完必须重启 uvicorn**；前端静态实时生效 |
| 本机限制 | 虚拟机，**无可用音频端点**（扬声器/录音设备为空）→ 录屏带声/回环录音不可行 |

## 2. 今日完成的修复（均已实测）

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | 小红书/部分 B站 报 `name '_host_of' is not defined` | `app/robust_media.py` 调用了未导入的 `_host_of` | 改用 `utils._host_of_url`；全项目扫描确认无其它同类 |
| 2 | B站成功但文案为空 | ① `vad_filter=True` 把整段语音全过滤 ② 强制 `language=zh` 使英文音频输出"中文伪翻译" | ASR 三级回退 `zh+VAD → zh+关VAD → 自动语种+关VAD`；加语种预检（`detect_language` 只收 ndarray，阈值 0.4） |
| 3 | 纠错慢且"没效果" | 模型不遵守"只输出 JSON"，写 900+ 字解释 → 慢且解析不到 | 提示词加固 + `max_tokens=512` + 分块 250 字 + 超时 300s + 分块并发 4 |
| 4 | 切到历史记录后提取结果丢失 | `showView()` 重渲染视图但不保存结果区 | 结果快照 `RESULT_CONTAINERS` + `snapshotResult()/restoreResult()`（按 视图+平台key） |

## 3. 今日新增功能

| 功能 | 实现 |
|---|---|
| 小红书**图文笔记**自动提取 | `app/xiaohongshu.py`：yt-dlp 报 `No video formats` 时自动回退「浏览器渲染取图 → RapidOCR」；**链接必须带 `xsec_token`** |
| **简/繁切换** | `POST /prod-api/text/convert`（OpenCC）；结果页「口播文案」右侧切换，**默认简体** |
| **多标签页路由** | 每功能独立 hash URL（`#/video/douyin` 等）与独立标题；点下拉功能**在新标签页打开**，原标签不变 |
| **外部下载器集成** | 见 §4 |

### 3.1 验证结论
- **全平台链接 5/5 SUCCESS**：抖音 6090 字 / 快手 509 / B站 1747 / 小红书图文 2609 / 小红书视频 2473
- **其余功能 17/17 PASS**：上传音频+视频转写、图片 OCR（上传+URL）、批量提取（done=2/success=2）、导出 txt/md/docx/xlsx/srt、历史列表/搜索/筛选/详情/删除、媒体代理 206、AI 生成笔记（1690 字/66s）
- **仍不可行**：视频号链接（微信无网页可读接口）；YouTube/TikTok 未验证

## 4. wx_channels_download 真机（✅ 已跑通，原"卡住点"已解除）

> **2026-10-30 下午更新**：重新以管理员启动后 API **1.5ms 正常响应**（0 字节超时未复现，
> 推测上次停在首次运行的确认弹窗）。MITM 证书 `installed=True/trusted=True`（SunnyNet CA），
> 下载目录 `wx_downloads` 就绪。现在整套已自动化，见 §6。

### 4.1 背景
视频号链接只能在微信客户端打开；其公开预览 API
（`POST https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info`，
body `{"baseReq":{"generalToken":""},"shortUri":"<id>"}`）**只返回描述/作者/封面/点赞数，无视频流**。
方案：第三方下载器取视频 → 本项目转写；并作为快手/B站失败兜底。

### 4.2 已完成
1. 下载 safe 版（未 UPX）：`wx_video_download_safe_v260907_windows_x86_64.zip`
2. **SHA256 校验通过**：`305b3b18e1df567394ae136d59274ea0908add547c589930f7f5805bbe6241a4`
3. 解压到 `F:\文案提取工具\wx_channels_download\`（exe + config.yaml + data.db）
4. 已改 `download.dir` → `F:\文案提取工具\wx_downloads`（目录已建）
5. 以管理员启动成功（PID 51596，`Start-Process -Verb RunAs`）
6. 端口 **2022(API)/2023(代理) 均 LISTEN**；系统代理已设为 `127.0.0.1:2023`
   （`HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings`: ProxyEnable=1）

### 4.3 现象
- `http://127.0.0.1:2022/api/status` 与 `/`：**连接成功但 0 字节、6-10s 超时**（`--noproxy '*'` 同样）
- 本项目 `/prod-api/ext/status` 因此 `online:false`
- 日志 `C:\Users\Develop-1\AppData\Local\Temp\wx_channels_download\app.log`：
  `09:53:18 api server listen started`，之后只有 interceptor 反复代理 `127.0.0.1:3425`（其前端），
  **无证书安装记录**；`Cert:\CurrentUser\Root` 未见明确新增 CA
- 推测：首次运行停在"安装证书/确认代理"GUI 弹窗或被杀软拦截；API 线程被阻塞。**未确认**

### 4.4 下一步建议（按顺序）
1. 看它的窗口/托盘是否有待点击的弹窗（安装证书/确认代理）
2. 查 Windows Defender / 防火墙拦截提示
3. 重启：`Stop-Process -Name wx_video_download -Force` → 再以管理员启动，同时盯 `app.log`
4. 仍无响应则放弃该工具（或换 res-downloader），保留既有能力
5. 成功路径：微信 PC 打开视频号 → 播放 → 暂停 → 点注入的「下载」→ 文件落在 `wx_downloads` → 本项目自动转写

### 4.5 清理/回滚（系统代理会影响全机网络）
```powershell
Stop-Process -Name wx_video_download -Force
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' ProxyEnable 0
# 证书：按其 docs/cli/uninstall.md，或 certutil -delstore Root "<证书名>"
```
可删目录：`F:\文案提取工具\wx_channels_download`、`F:\文案提取工具\wx_dl_pkg`

## 5. 10-30 下午追加：小红书视频"预览丢失"修复（已完成）

### 5.1 现象与根因
- 现象：小红书**视频**链接提取成功（文案正常），但结果页「视频预览/截图/保存视频」整块消失。
- 根因：小红书 CDN 是否给 http 直链 mp4 是**随机的**（同一链接 11:44 给 mp4 直链、13:24 只给
  HLS `m3u8_native`）。`extractor._pick_video_urls()` 只保留 `protocol` 含 "http" 的格式，
  HLS 全被过滤 → `videoUrlList=None` → 前端 `d.videoUrlList.length` 判空不渲染。
  转写不受影响（yt-dlp 拉 HLS 音频正常）。

### 5.2 修复方案（2026-10-30 下午，已验证）
无浏览器可播直链时，任务收尾前**兜底下载一份本地 mp4** 存进任务目录，标记
`videoUrlList=["file://video.mp4"]`；`/media/proxy` 识别 `file://` 直接服务本地文件
（FileResponse 自带 Range，预览/截图/保存全可用，前端零改动）。

| 文件 | 改动 |
|---|---|
| `app/extractor.py` | 新增 `_has_video_stream`(ffprobe)、`_ensure_mp4`(流复制转封装/改名)、`_download_local_preview`；`_run_task` 成功前挂接（优先复用外部下载器已下载的 `ready_media`）；`finally` 清理豁免 `video.mp4` |
| `app/robust_media.py` | `_pick_downloaded(work_dir, pattern)` 按请求类型优先——否则视频下载会误拿 ASR 留下的 `audio.m4a`（原隐患） |
| `app/api.py` | `_serve_local_media` + `media_proxy` 识别 `file://`；taskId 正则校验防路径穿越 |

- 验证：① 本地 HLS 服务器复刻 m3u8_native-only → `_pick_video_urls==[]` → 兜底下载合成 `video.mp4` ✅
  ② TestClient：proxy 200 video/mp4 / Range 206 / download attachment / 404 / 穿越 404 ✅
  ③ TS(h264/aac)→mp4 转封装 ✅；webm(VP8/vorbis)→mp4 流复制本就不可行，跳过预览（不影响文案）
- 小红书真实链接端到端当时被限流挡住（`300011 Account exception`，账号侧临时限制），
  限流过后**重新提取同一链接即可**得到预览。
- 新 env：`TRANSCRIPT_HLS_PREVIEW`（默认 `1` 开启；设 `0` 关闭兜底下载，省流量）。
- 注意：任务目录 `tasks/<id>/video.mp4` 现在**长期保留**（供代理服务），大视频占磁盘，可按需清理。

## 6. 10-30 下午追加：下载器一键安装/启动/停止（已完成）

### 6.1 背景
视频号页面原来要求手动安装+管理员启动 wx_channels_download，页面上堆了大段指导。
现在工具自己会装、会启、会停，页面只看状态；换新电脑可一键自举。

### 6.2 实现
| 文件 | 内容 |
|---|---|
| `app/external_dl.py` | `install()`（幂等：本地包 `wx_dl_pkg/*.zip` 优先 → GitHub releases `ltaoo/wx_channels_download` 下载 safe x86_64，SHA256 按 checksums.txt 校验，安全解压，**config.yaml 的 download.dir 自动改写为本机推导路径**=便携化关键）；`start()`（PowerShell `-Verb RunAs` 提权启动 → 轮询 API ≤60s，失败带诊断：进程/端口/弹窗提示）；`stop()`（提权 taskkill + **还原启动前系统代理**，备份在 `wx_channels_download/.proxy_backup.json`）；`probe()` 增加 installed/exe/pkg_ready/version |
| `app/api.py` | `POST /ext/setup`、`/ext/start`、`/ext/stop`（全部 asyncio.to_thread） |
| `static/index.html` | 视频号页状态卡片：在线→绿标+「停止下载器」；离线→「一键安装并启动」按钮；长说明收进 `<details>` 折叠 |

### 6.3 踩坑与要点
- **中文 Windows 的 tasklist/PowerShell 输出是 GBK**：`subprocess(text=True)` 默认 UTF-8 解码会崩
  → stdout 空 → `_proc_running()` 误判"未运行"（首次 stop 就这么漏杀的）。已改 bytes + errors='ignore'。
- **系统代理**：下载器运行期间会把系统代理指向 127.0.0.1:2023（MITM 必须）；**强杀不会恢复**。
  start() 前备份注册表原值 → stop() 后还原（如 clash 的 7890）；无备份且指向 2023 时兜底关闭代理。
- **urllib 会读注册表系统代理**：下载器运行时对本机 127.0.0.1:2022 的 API 调用也会被绕进
  2023 代理 → `external_dl` 的 `_get/_post` 已改用 `ProxyHandler({})` 直连 opener。
- 安装目录/包目录/下载目录全部随项目目录推导（env 可覆盖）：`TRANSCRIPT_EXT_DL_DIR`、
  `TRANSCRIPT_EXT_DL_PKG_DIR`、`TRANSCRIPT_EXT_DL_DOWNLOAD_DIR`、`TRANSCRIPT_EXT_DL_REPO`。
- **换新电脑自举**：把整个项目目录拷走 → 页面点「一键安装并启动」即可（有 wx_dl_pkg 离线装，
  没有则 GitHub 下载）；启动/停止各弹一次 UAC 是必然的（装证书/系统代理需要管理员）。

### 6.4 验证记录
- `probe`：online/installed/pkg_ready/version/download_dir 全对 ✅
- `/ext/setup` 幂等（已装只校正配置，config.yaml md5 不变）✅
- `/ext/start` 已在线 → `{"started":true,"already":true}` 不弹 UAC ✅
- 离线新机模拟（临时目录 + 本地包）：SHA256 校验通过 → 解压出 exe → config 自动指向新目录 → 幂等 ✅
- GitHub release API 解析：`wx_video_download_safe_v260907_windows_x86_64.zip`（release 无 checksums 资产时跳过校验）✅
- stop→start 真实循环：UAC×2，进程杀掉+代理还原 7890，再启动回 online ✅
- 前端截图：在线态「● 在线 · v260907 · 下载目录 …· 代理已启动」+「停止下载器」按钮，说明已折叠 ✅

## 7. 10-30 下午追加②：历史保留策略（默认近 20 条，自动删文件）

- 背景：`tasks/<id>/video.mp4`（本地预览视频）会长期保留，且旧版「删除/清空历史」不删文件，
  实测 `tasks/` 堆到 1.3GB（含 7 个孤儿目录）。
- 新策略：`TRANSCRIPT_HISTORY_KEEP`（**默认 20**，0=不限制）。触发点三处：
  ① 每次任务落库后 `task_store` → `history_store.trim(keep)`；
  ② 服务启动时 trim 一次；
  ③ 启动时 `sweep_orphan_task_files()` 清孤儿目录（记录已删但文件还在的）。
- `DELETE /history/delete|clear` 现在**连带删除 `tasks/<id>/`**（含预览视频）。
- 实测：127 条 → 20 条；tasks/ 1.3GB → 94MB（只留 2 个带 video.mp4 的目录）。
- 注意：磁盘占用上限 ≈ 最近 20 条的媒体体积（长视频多时可调小该值）。

### 7.1 关于"旧记录补预览"的结论（未做，有意不做）
自动给旧记录回填预览 = 重新拉全片（24 分钟视频约 100-400MB/条）+ 过平台风控
（小红书 cookie 当天就在限流）+ 旧 CDN 直链多已过期，且旧记录正是会被保留策略删掉的部分
——纯浪费资源。哪条旧记录想要预览，把链接重新提取一次即可（新代码会自动带上预览）。

### 6.5 追加：免 UAC + 跟随服务自动拉起（10-30 傍晚）
- 关键发现：**证书装过之后，下载器不再需要管理员**（系统代理是 HKCU 用户级、证书已在信任库）。
  `start()` 重构为：先免提权直接拉起（DETACHED_PROCESS + SW_SHOWMINNOACTIVE，无 UAC）→
  失败且 `allow_elevate=True` 时才 UAC 提权兜底；`stop()` 先普通 taskkill，管理员进程才走 UAC。
- **跟随服务自动拉起**：`main.py` startup 起后台线程调 `external_dl.start(allow_elevate=False)`
  ——服务一启动下载器就在线，全程零交互零弹窗。env `TRANSCRIPT_EXT_DL_AUTOSTART=0` 可关。
  自动模式绝不允许提权路径（避免后台突然弹 UAC）。
- 实测：停掉管理员实例（UAC 一次）→ 重启服务 → 20 秒内自动在线（无 UAC，日志"外部下载器已自动启动"）；
  之后 stop/start 循环均无 UAC（`elevated: False`）。
- 注意：下载器运行期间系统代理仍会指向 2023（MITM 必须，会绕开 clash 7890）；点「停止下载器」即还原。
  不想自动拉起就设 `TRANSCRIPT_EXT_DL_AUTOSTART=0`。

## 8. 10-30 傍晚追加②：视频号全链路打通 + GPU 转写修复

### 8.1 视频号完整流程（已实测跑通）
微信打开视频号播放 → 点下载器注入的「下载」→ 文件落 `wx_downloads/` → 监听目录自动入列
→ GPU 转写 → 历史记录可查看/导出。实测"顶级智斗"29.7 分钟视频 11923 字。

### 8.2 修复：上传/监听转写路径一直在用 CPU（重要性能坑）
`file_transcribe._transcribe_file` 还是老逻辑（`TRANSCRIPT_USE_CUDA=1` 才用 GPU），
HANDOFF §4.2 的 GPU 加速只给了链接提取路径 → 29.7 分钟视频 CPU 要转 30+ 分钟。
已改用 extractor 的 `_pick_device()/_get_model()`（GPU float16 + 模型缓存），
实测同视频 **约 6.5 分钟**（~4.5x 实时）。

### 8.3 修复：监听目录重启后丢失
新增 `watch_folder.resume_last()`：页面点过「开始监听」即写入 `watch_autostart.json`，
服务重启自动恢复；手动「停止」则删除记忆。`TRANSCRIPT_WATCH_REMEMBER_FILE` 可改路径。

### 8.4 修复：应用日志全被丢弃（排查盲区）
`run.py` 原来没有 `logging.basicConfig`，watch/extractor 等 INFO 日志全部丢失。
现落 `app.log`（UTF-8）+ stderr，排查问题先看它。

### 8.5 加固：监听导入失败自动重试
`_enqueue` 原来只对 copy2 做容错且失败后 done 标记不撤销 → 文件被永久跳过。
现整个入列过程 try/except，失败撤销 done + 删临时副本 + 下轮自动重试（日志带堆栈）。

### 8.6 踩坑记录
- 中断转写进程会丢任务（内存任务态不落库）：重启服务前先看有没有 RUNNING 的转写。
- `_enqueue` 的 `processed` 计数在 282MB copy2 完成才 +1，轮询状态别急着下结论。
- processed/ 里的文件不会被重新扫描；要重转就把文件移回监听根目录（状态文件按"路径+大小+mtime"记忆）。

## 9. 10-30 晚追加：监听目录自动接线（零配置）

- 页面不再需要手动填监听路径：
  ① 服务启动时若从未设置过监听 → `watch_folder.auto_watch_downloader_dir()` 自动监听
     下载器的下载目录（`external_dl.DOWNLOAD_DIR`，与写入下载器 config 的是同一路径）；
  ② 「一键安装并启动」成功后前端顺手接线（未启用时）；
  ③ `/watch/start` 的 dir 允许留空 = 自动用下载器目录；
  ④ 输入框自动回填当前监听/下载器目录。
- 尊重用户选择：手动「停止」写入 `{"optOut":true}` 记忆 → 不再自动接线；
  显式配置过目录则始终以用户的为准。
- 验证：删除 watch_autostart.json 模拟全新环境 → 重启 → 自动监听 wx_downloads 并自举记忆 ✅；
  浏览器确认输入框回填与状态行 ✅。

## 10. 10-30 晚追加②：视频号页面重构（去重）

- 原页面 4 块内容互相重复（红色提示/下载器卡/监听卡/死输入卡），现合并为**一张卡**：
  三步流程（微信播放 → 点注入下载 → 历史记录看文案）+ 下载器/监听两条状态行 +
  「停止下载器」+ 折叠的「高级选项 / 故障排查」（手动监听目录、直链下载、说明）。
- 删除视频号页无用的链接输入卡（textarea/模型选择/提取按钮 —— 视频号本就不能用链接解析）。
- `startWatch` 允许留空（后端自动用下载器目录）；修掉直链按钮文案不一致的小 bug。
- 其它平台的视频页模板不变。绑定代码都有存在性保护，元素缺失安全。

## 11. 10-30 晚追加③：新机全自举部署

- **下载器自动安装**：服务启动线程现在"未安装 → install()（本地包优先/否则 GitHub）→ start()"，
  新机拷项目 + 启动即全自动（双实例模拟实测通过：安装→提取→探活→不重复拉起）。
- **probe 增强**：新增 `cert_installed/cert_trusted`（读 /api/proxy/status）；
  前端在线但证书未装 → 橙色警告 +「修复证书（管理员）」按钮（stop→elevated start）。
- **便携 ffmpeg**：`run.py` 把 `tools/ffmpeg/bin` 前置到 PATH（setup 脚本自动下载到这里）；
  HF 默认走 hf-mirror（`HF_ENDPOINT` setdefault，用户显式设置不覆盖）。
- **引导脚本** `scripts/setup_new_machine.py`（--check 只读检查）：依赖（镜像重试）、
  playwright chromium、ffmpeg 自动下载、.env 创建。
- **新文件**：`.env.example`（无密钥模板）、`docs/新机部署指南.md`。
- 手工步骤只剩：装 Python、拷项目、跑引导脚本、（可选）填 LLM key。

## 12. 10-30 晚追加④：批量导出「仅文案」模式

- 导出弹窗新增「仅文案」开关：勾选后每个文件**只含转写正文**（无标题/简介/链接等元数据），
  文件名仍带标题便于识别（如 `02_视频标题.txt`）；对单条导出同样生效。
- 实现：`export.py` 新增 `_build_plain`（txt/md）与 `_build_plain_docx`（纯正文 Word），
  `_select_builder(fmt, plain)` 分发；API 加 `plain` 参数（exportTask query / exportTasks body）。
- Excel/SRT 不受影响（本身无冗余），勾选框在选中这两种格式时自动禁用。
- 实测：plain=1 的 zip 每个文件纯正文（断言无"标题:/平台:/来源链接"）；plain=0 原样式不变。

## 13. 10-30 晚追加⑤：全量回归测试报告（40 项全过）+ start.bat

| 阶段 | 项 | 结果 |
|---|---|---|
| A API 冒烟 | health / ai/status / 下载器(online+cert+pkg) / 监听 / 简繁转换 / 历史列表 | 7/7 PASS |
| B 真实提取 | 抖音专用通道 9586 字；快手通道 421 字（small 模型） | 2/2 PASS |
| C 文件类 | 上传 wav 转写（GPU）、图片 OCR（封面 URL）、批量队列 1/1 | 3/3 PASS |
| D 导出/AI | TXT/MD/DOCX/XLSX/SRT 五格式、批量 ZIP 仅文案、AI 纠错、笔记 1484 字+下载、媒体代理 200/206 | 11/11 PASS |
| E 清理/前端 | 删除 4 条测试任务（联动清文件）+ 前端 6 视图渲染 | 6/6 PASS |

未测项（有意）：小红书（账号限流中，避免加重）；YouTube/TikTok（从未验证）；视频号端到端需微信手动点下载（当日已实测 29.7min→11923 字）。

**start.bat**（纯 ASCII 三行壳 + `scripts/launch.py` 全逻辑）：
依赖自检→缺则装（镜像重试）→ .env 引导 → 自动开浏览器 → 启动服务。
已实测通过。注意：bat 里的中文在中文 Windows 下有编码坑（UTF-8/GBK × 控制台代码页），
所以 bat 文件本身保持纯 ASCII，所有中文输出由 Python 完成。

### 本轮排查小记
- 系统代理指向下载器 MITM(2023) 时，本机 python urllib 测 8000 端口偶发 422（代理转发丢 query）：
  测试脚本一律用 `ProxyHandler({})` 直连 opener（与 external_dl 同款做法）。
- `/transcript/batch/*` 字段名是 `workUrls`（不是 urls），批量响应条目在 `data.items`。

## 14. 10-30 晚追加⑥：闪窗修复 + 文案自动归档

### 14.1 空闲闪黑窗（用户反馈"监听一直在弹窗又很快关了"）
- 根因：下载器以 DETACHED_PROCESS（无控制台）启动，它空闲时周期性调 reg.exe 等辅助命令，
  每个命令都要新建控制台 → 黑窗闪现。WMI 轮询抓到 reg+conhost 闪现佐证。
- 修复：`_launch_plain` 改用 CREATE_NO_WINDOW（隐藏控制台可被子进程继承）。
  教训：改完代码必须重启服务才生效（当晚就踩了"改了但跑旧逻辑"的坑）。
- 验证：空闲 90 秒进程创建数 = 0（修复前 15 秒内就有 reg/conhost 闪现）。

### 14.2 文案自动归档（用户诉求：文案应该放文件夹里方便查看）
- 新模块 `app/archive.py`：每个成功任务自动把正文存为 `文案输出/日期_标题.txt`
  （utf-8-sig，记事本直开；标题安全化+去媒体扩展名；重名加 (2)(3)）。
- 触发：任务成功落库时（task_store 钩子）+ 服务启动 `backfill()` 补齐历史（按文件名判重）。
- env：`TRANSCRIPT_ARCHIVE_DIR`。前端视频号页说明已加一句。
- 实测：启动补齐 12 个文件（含顶级智斗 11923 字），空闲验证通过。

## 15. 10-30 晚追加⑦：视频号页「最新文案」直取区块

- 用户反馈：重构后的视频号页没有直接"拿到文案"的出口。现已补上「最新文案」区块：
  列出 文案输出/ 最近 6 个归档文件，每个带「复制全文」（clipboard API）和「下载」，
  顶部「打开文件夹」直接在资源管理器打开归档目录。
- 新端点：`GET /archive/list`、`GET /archive/content`（复制用）、`GET /archive/file`
  （下载，复用 _file_response 的 RFC5987 中文名处理）、`POST /archive/open`（os.startfile）。
  文件名白名单校验（禁路径分隔符/..，仅 .txt）。
- 实测：列表渲染 6 条 + 复制/下载/打开文件夹按钮齐全（浏览器截图确认）。

## 16. 10-30 晚追加⑧：用户十条反馈逐项处理

1. **小红书慢**：预览下载原本重新跑一遍完整 yt-dlp 提取（平台请求翻倍）。已改 `_ffmpeg_pull_hls`
   直接用首次提取缓存的 m3u8 地址拉流（不再二次请求平台）。
2/3. **抖音黑视频/黑截图**：服务端验证代理流正常（h264 1080x1920，首帧亮度 143）。黑屏最可能是
   直链过期（抖音直链仅数小时有效）或 ASR 占用 GPU 时解码竞态。加固：抽帧元数据超时 12→20s、
   seek 后等一拍二次绘制防黑帧。若仍黑请告知具体任务。
4. **小红书只见封面**：13:15 修复前的任务没有本地预览，重新提取即有；新任务已验证带 file:// 预览。
5. **快手 new-reco**：官方网页端行为（信息流不改 URL）。用 App 里「分享→复制链接」得 v.kuaishou.com 短链。
6. **B站**：该任务有 8 条直链且代理实测 200/正常画面；截图没出是加载超时竞态，已加固（见 2/3）。
7. **视频号无预览**：文件任务现在保留视频到 tasks/<id>/，videoUrlList=file:// → 历史详情有预览+截图。
   无音轨视频给出友好报错（原报 tuple index out of range）。
8. **风控**：抖音 cookies 目录里没有抖音 cookie → 一直匿名跑，几次就 403。跑 login_cookie.py 存一份
   抖音登录 cookie 即可大幅缓解；另预览下载不再二次请求平台（减半请求）。
9. **图文提取合并**：菜单合并为单一「图片文案提取（OCR）」，去掉图片链接输入（只留上传）；
   旧 key（douyin_ocr/xhs_ocr）全部迁移到 ocr。
10. **视频转文字报错**：已复现根因=无音轨视频。修复见 7。
回归：无音轨→友好报错；带音轨视频→成功+file://预览+代理 200。

## 17. 10-30 晚追加⑨：画面文字识别（OCR 视频内嵌文字）

- 新模块 `app/frame_ocr.py`：ffmpeg 抽帧（按时长自适应 1.5~5s/帧，上限 48 帧）→ RapidOCR
  （复用 ocr.py 单例）→ 按「频率+位置+字号」三重分类去重：
  - 临时出现（<60% 帧）→ 硬字幕/花字 → 画面文字 ✓
  - 常驻+角落+小字 → 水印/台标/UI → 丢弃 ✗
  - 常驻但居中大字（视频号告示牌型）→ 正文 ✓
- 入口：① 链接任务 `ocrFrames` 参数（视频页勾选框）② 文件上传 `ocrFrames` 表单字段
  （视频转文字页勾选框）③ 监听导入默认开启（`TRANSCRIPT_WATCH_FRAME_OCR`，默认开）。
- **无音轨视频不再报错**：纯画面视频（视频号告示牌常见）跳过 ASR、直接走画面文字，
  之前报 tuple index out of range 的场景现在能拿到文字。
- 存储：`TaskDetail.frameText`（models）+ history extra + 结果卡独立区块
  「画面文字（OCR）」+ 归档 txt 追加【画面文字】段。
- 坑：multipart 里 bool 表单字段尾部 CRLF 会让 pydantic 解析失败（"1\r\n"）→
  用 `str = Form("")` + 手动解析。
- 验证：无音轨+居中文字测试视频 → SUCCESS + frameText 精确命中 ✅（口播/画面分离正确）。
