# 更新记录

> 本文件是**变更的唯一真源**：每次发布前先在这里写清"改了什么、修了什么"，
> 打包脚本会自动把最新几版内容附到包内《使用说明》的最后。
> 版本号在 `VERSION` 文件里，用 `python scripts/build_release.py --bump patch` 递增。

---

## v1.0.9（2026-11-06）· 移除方式2/下载器/监听目录 + 视频号异步提交 + 进度体验

### 移除：视频号「方式 2 · 微信里点下载」与第三方下载器
- 删除 `app/external_dl.py`、全部 `/prod-api/ext/*` 接口、启动自装/代理看门狗/退出还原逻辑。
- 删除 `wx_dl_pkg/`（约 36MB 离线安装包）：发布包从约 38MB 降到约 2MB，
  不再需要管理员权限，也不再接管系统代理或安装根证书。
- 直链下载能力保留并独立为 `app/media_dl.py`（mp4 / m3u8 / mpd），提取链路的直链兜底不变。
- **监听目录整体移除**：`app/watch_folder.py`、`app/ingest.py`、`app/image_import.py`、
  全部 `/prod-api/watch/*` 接口与 4 个测试文件一并删除；本地视频/图片改用「上传文件 / 图片」。
- 打包脚本（build_release / build_offline / build_ubuntu）与文档同步去除下载器/监听目录内容。

### 视频号：必须出视频才算成功
- 绑定了凭证仍拿不到视频 → 自动重试 3 次；仍失败则任务标 FAILURE 并提示「再点一次提取」
  （附一键重试按钮），不再静默降级成只有文案的绿色「提取成功」。
- 视频下载失败自动补试 1 次；失败记录仍保留已取到的文案。
- 没绑定凭证时按钮明确变为「只要文案（无视频）」，结果卡用琥珀色说明缺少视频。

### 视频号页默认提速（2026-11-05）
- 转写模型默认改为 **tiny**（原「自动」对 ≤4 分钟视频选 medium，56 秒视频实测要 3 分 32 秒）；
  需要更准时仍可手动选 small / medium / 自动。
- 画面文字 OCR 改为**默认关闭**的复选框（与其它平台页一致）；纯音乐/无口播视频需要时手动勾。
- 同一条 56 秒视频实测：默认配置下 **49 秒**完成（原 3 分 32 秒），文字量略少（tiny 的准确率取舍）。

### 「自动」选模型升级：按机器配置 + 时长（2026-11-05）
- 新增机器档位检测（显存经 nvidia-smi，进程内缓存）：cpu / low(<4GB) / mid(4~8GB) / high(≥8GB)，
  可用 `TRANSCRIPT_ASR_TIER` 强制指定。
- 选「自动」时：cpu/low → 一律 tiny；mid → ≤4 分钟 medium、≤10 分钟 small、更长 tiny；
  high → ≤10 分钟 medium、≤30 分钟 small、更长 tiny。阈值可用
  `TRANSCRIPT_AUTO_MEDIUM_MAX_SEC` / `TRANSCRIPT_AUTO_SMALL_MAX_SEC` 覆盖。

### 画面文字 OCR 提速（2026-11-05）
- 帧 OCR 默认关方向分类器（视频帧都是正向的，cls 每帧白跑一个模型）：
  `TRANSCRIPT_FRAME_OCR_USE_CLS=1` 可开回。
- 新增「连续 N 帧无新文字即提前收工」（`TRANSCRIPT_FRAME_OCR_PATIENCE`，默认 4）：
  字幕/告示牌类视频文字很快饱和，后面帧全是重复——同一条 34 秒视频实测从 20.8s / 预热 19.4s
  降到 16.8s / 服务预热后约 10s，文字一字不少。默认 4；想要更全可用 0 关闭或加大。
- 抽帧间隔 1.5s → 2.0s（`TRANSCRIPT_FRAME_OCR_INTERVAL`）：密度略降换速度；文字真的在
  画面里且只出现一瞬的，可调小到 1.5。

### 视频号提链再省一次请求（2026-11-05）
- 绑定元宝凭证后，贴链接**不再发匿名元数据请求**（`resolve_video` 走的凭证接口本身
  就带回标题/作者/封面/正文）。`channels.weixin.qq.com` 实测有 TLS 握手间歇超时
  （6 连测 1 次超时），少发一次就少一次踩雷 + 省 3~5 秒；resolve 失败时仍回落匿名兜底。

### 修复（2026-11-05 测试发现）
- 删除历史记录时同步清掉内存任务（此前删完记录，结果页 URL 仍能打开到服务重启）。
- 导出弹窗支持 Esc 关闭；站内切页（浏览器后退/地址栏）时自动收起，遮罩不再残留。
- 视频号页的转写模型下拉真正生效（此前提交时没带 whisperModel，一直用后端默认 medium）；
  新增「自动」档：按视频时长选 medium / tiny。

### 视频号提交异步化 + 前端进度体验（2026-11-06）
- 视频号贴链接不再阻塞 POST：先返回 taskId，后台线程完成「元数据 → 换直链 → 下载」，
  实测提交从 **5~18 秒降到 0.01 秒**；页面立即进入进度页，阶段文案实时更新。
- 修复画面 OCR 勾选无效：视频号任务此前写死 `ocr_frames=True`，没勾也会全画面 OCR；
  78 秒视频实测从 **约 3 分钟降到 5.1 秒**（不勾 OCR），勾选时页面显示「正在识别画面文字」。
- 加载区新增任务进度条：转写阶段按实际 `X/Y` 填充，下载/换直链/OCR 等阶段用流动条纹，
  不再显示 `0:03 / 0:41`、`预计还需 X 分钟` 这类倒计时数字。
- 失败提示中文化：`<urlopen error …handshake timed out>` 这类技术细节只写日志，
  前端显示「腾讯接口连接超时」等可读原因；抖音失效短链（302 到首页）直接明确报错。
- 前端 API 请求加 150 秒超时，服务重启/断线时不再无限转圈。
- 控制台强制 UTF-8（修 PyCharm 中文乱码）；静默 Windows asyncio 的
  ConnectionResetError 噪音（客户端/播放器正常断开不再刷 ERROR 堆栈）。

### 页面重开后可找回进行中的任务
- 进行中入口从 `sessionStorage` 改存 `localStorage`（关掉页面不再丢）。
- 新增 `GET /prod-api/transcript/active`：页面启动时从服务端补一份未结束任务，
  换标签页/清过浏览器数据也能续上；批量按 batchId 汇总。
- 顶部新增蓝色提示条「有 N 个任务还在处理中 · 查看进度」，完成后自动消失。

---

## v1.0.8（2026-11-05）

> 修复 v1.0.7 视频号「贴链接下载」开箱即坏：直链 query 被精简成只剩 `encfilekey`+`token`，
> 而实测这些都是 CDN 签名的一部分，缺一个就 400。本版起直链原样使用，端到端已实测跑通。

### 修复：视频号直链下载 400
- `app/video_channel.py` `_clean_video_url` 改为**原样保留直链**：实测 `sign` / `basedata` /
  `m` / `uzid` / `hy` / `idx` / `X-snsvideoflag` 都参与 CDN 签名，精简任何一项都判 400
  （`X-Errno -5103397`）。
- `download_media`：`finder.video.qq.com` 走媒体代理常握手超时，改为按配置先走、失败立刻改
  直连重试一次，不把一次代理抖动算成「视频号下不了」。

### 前端与账号页（11-05 顺手）
- 账号页新增「视频号 · 元宝登录」行：微信扫码一次，凭证约 60 天，到期标红提醒重扫。
- 未登录元宝就贴视频号链接时，页面直接出通知引导登录，不等提交后才报错。

### 实测（2026-11-05）
- 探针直连下载 2.9 MB（`tmp_uploads/_vc_probe.mp4`），`ffprobe` 正常解码出 h264 流与时长。
- 经 API 全链路两条任务（10:57 / 10:59）：贴链接 → 元宝换直链 → 下载解密 → ASR 出
  159 字口播文案，均 SUCCESS（`transcript_history.db` 可查）。
- `pytest tests/ -q -p no:warnings`：330 项全绿。

---

## v1.0.7（2026-11-04）

> 视频号「贴一条链接」从只能拿元数据，变成**能直接下载视频并转写**：新增元宝登录态
> 换凭证拿 h264/h265 直链，下载时按响应头解密，复用「上传文件」同一条转写流水线。
> **新增一项登录态**：想用这个能力需先 `python login_cookie.py yuanbao` 扫码一次；
> 不配则行为与 v1.0.6 完全一致（只出元数据卡片）。

### 新增：视频号贴链接直接下载并转写
- `app/video_channel.py` 新增 `yuanbao_cookie()` / `resolve_video()` / `download_media()`：
  - 元宝 `https://yuanbao.tencent.com/api/weixin/get_parse_result`（body `{"type":"video_channel_url","url":<链接>,"scene":1}`）→ `data.playable_url`（query 里带 token / eid）；
  - 再 POST `finder-preview` 的 `get_feed_info`（body `{"baseReq":{"generalToken":<token>},"exportId":<eid>}`）→ `data.feedInfo`；
  - 直链优先级 `h264VideoInfo` → `h265VideoInfo` → 通用 `videoUrl`，并精简 query 只留 `encfilekey`+`token`；
  - `download_media` 读响应头 `X-enclen`（实测 131072），用直链 query 里的 `decodeKey` 生成 128KB 密钥流，只 XOR 前 `X-enclen` 字节后落盘；拿不到 decodeKey 时按明文保存并告警。
- 密钥流用纯 Python 实现的 ISAAC64（`_Isaac64`，移植 Go `rand.RandCtx64`），**不再需要 wasm / Node**，单进程即可完成解密。
- `app/api.py`：视频号解析入口检测到 `cookies/yuanbao.txt` 时，先试「直链 → 下载 → `start_file_task(platform="视频号")`」；任一步失败静默回落到原有元数据卡片，不影响旧行为。
- `app/file_transcribe.py`：`start_file_task` / `_run_file_task` 新增 `platform` 形参（默认 `"本地文件"`），让「贴链接下载」的任务平台显示为「视频号」而不是「本地文件」。

### 新增登录态：元宝（yuanbao）
- `login_cookie.py` 增加 `yuanbao` 项：打开 `https://yuanbao.tencent.com/`，微信扫码一次，导出 `cookies/yuanbao.txt`（校验 token `hy_user` / `hy_token`）。与其它平台登录态互不影响。

### 界面（static/index.html）
- 视频号页「链接模式」提示改为：已配置元宝登录态则**直接下载视频并转写**；未配置时只取正文文案 / 作者 / 封面 / 互动数据。
- 步骤说明补一条「贴链接直接下载（需元宝登录态）→ 自动转写」，并给出 `python login_cookie.py yuanbao` 的操作提示。
- 首页说明去掉「本项目为技术演示」字样。

### 配置与打包
- `.env.example`：`TRANSCRIPT_EXT_DL_AUTOSTART` 改为默认注释（不再自动拉起外部下载器）。
- `scripts/build_release.py`：打包结尾打印已保留的历史版本数量（`dist` 只增不删，任何涉及历史版本的删除必须先单独问用户）。

### 验证
- `F:\python\python.exe -m pytest tests/ -q -p no:warnings`：330 项全绿。
- 未在本次发布中实跑端到端下载（需要有效元宝登录态）；回退路径保持 v1.0.6 行为。

---

## v1.0.6（2026-11-04）

> 承接三条反馈：视频号页「排版不太协调」、进度圈显示成椭圆、视频号页把历史结果堆在页面上；
> 顺带修掉「网盘下载下来的本地视频，视频截图全失败」。**无配置变更**，覆盖升级即可。

### 界面：视频号提取页重排
- 左栏拆成 4 张独立卡（提取流程 / 下载器与监听目录 / 最近导入 / 高级选项），原先「提取流程」
  和「最近下载任务」挤在同一张卡里用细线分隔；列表行改成带图标的两行式（标题 + 状态 / 大小 / 时间），
  下载中显示百分比进度条，失败态由刺眼红字改成淡黄提示条。
- 结果卡把「视频预览 + 视频截图」提到标题下方，作品简介 / 口播文案 / 作者文案 / 画面文字往后排；
  「刷新直链」从按钮行挪到播放器下方（仅抖音 / 快手 / B 站出现）；提示条按任务类型分开说 ——
  本地文件讲「直接从磁盘播放，原文件被移动或删除需重新导入」，其余平台讲「CDN 直链几小时后过期」。
- 视频号页不再堆叠历史结果：新提取 / 新打开的结果直接覆盖旧结果，去掉了顶部「刚提取 / 历史」标签条，
  与其他平台一致；顺带修掉「清空结果后页面停在单栏空态」的连带问题（空态同步不再依赖标签条逻辑触发）。
- 进度圈显示成椭圆的根因是它被 flex 压缩（实测 230px 窄容器下由 20×20 挤成 13.19×20）：
  `.spinner{flex:0 0 auto}` + 文案容器 `flex:1 1 auto;min-width:0` 允许换行，四处加载行一并修好。

### 修复：横屏视频被压成 4:3
- `<video poster>` 配 `width:auto` 时，浏览器按**封面图尺寸**排版而不是视频真实分辨率
  （实测 1920×1080 渲染成 440×330）。改为 `aspect-ratio:16/9 + object-fit:contain`，
  并在 `loadedmetadata` 里按 `videoWidth/videoHeight` 覆盖成真实比例；竖屏走 `.media-stage.portrait`。
- 实测：抖音 712×401（比例 1.778）、视频号竖屏 269×480。

### 修复：视频号本地文件「视频截图」全失败
三层原因，逐条实测确认：
1. `file://video.mp4` 只剥掉前缀 → 得到相对路径 `video.mp4`，ffmpeg 在服务进程的工作目录下找不到
   （生产日志实证 4 次 `Error opening input: No such file or directory`）。改为解析成
   `tasks/<taskId>/video.mp4`，文件不存在就换下一条候选，不再把相对路径喂给 ffmpeg。
2. 主抽帧路径带 `-skip_frame nokey` + `fps=1/2`，而视频号短片整段只有 **1 个关键帧**
   （`ffprobe -skip_frame nokey` 计数 = 1）→ 必定抽 0 张；抖音长视频关键帧密，所以同一套参数
   在抖音上看不出问题。现在「本地源 + 已知时长」直接走逐点并发 seek。
3. `file_transcript` 任务的 `duration` 落库是 NULL，让逐点 seek 兜底永远不触发 → 新增
   `_probe_duration()` 用 ffprobe 现场补时长。
- 另：这些片子是 tv-range 的 yuv420p，mjpeg 编码器会报 `Non full-range YUV is non-standard`，
  输出侧加 `-strict unofficial` 兜住。
- 实测：4 个视频号本地文件（7.1 / 13.4 / 7.4 / 8.0 秒，720×1280 ~ 1080×1928）由 **0 张 → 5/5 张**，
  耗时 0.40 / 0.40 / 0.69 / 0.69 秒。

### 测试
- 新增 `tests/test_local_frames.py`（4 例）：`-strict` 必须排在 `-i` 之后且本地源不加 `-headers`、
  本地文件走逐点 seek、ffprobe 补时长、`file://video.mp4` 解析成 `tasks/<taskId>/video.mp4`。
- `pytest tests/ -q -p no:warnings` → 全绿。

---
## v1.0.5（2026-11-04）

> 用户问：「视频号的分享链接能不能像抖音一样直接粘进来提取？」
> 结论：**文案能，视频流不能**。于是把能拿到的部分做成「链接模式」直接出结果。

### 新增：视频号「链接模式」
- 贴一条视频号分享链接（`https://weixin.qq.com/sph/xxxx`、`channels.weixin.qq.com/...`），
  不再回一句「暂不支持」，而是**直接生成一条成功任务**，结果页出专用卡片：
  正文文案（可复制/导出）＋ 作者与头像 ＋ 封面 ＋ 发布信息（时间/定位）＋
  赞/收藏/转发/评论 ＋ 话题标签。
- 为什么没有预览和截图：实测 `get_feed_info` 的响应里**没有** `videoUrl` /
  `h264VideoInfo` / `h265VideoInfo`（预览页 JS 就是读这几个字段才播放），
  视频号 CDN 直链也是密文（下载器侧才解密）。播放地址只发给登录态的微信客户端，
  所以网页链接拿不到视频流 —— 卡片上直接写清这一点，并指引走原路径转口播文字：
  微信里播放 → 点下载器注入的「下载」按钮 → 落盘后自动入队转写。
- 后端：`app/video_channel.py` 的 `parse_feed_info` 扩成读 `feedInfo` / `authorInfo` /
  `sceneInfo`（正文、头像、发布信息、互动数据、话题标签、动态导出 ID、是否视频）；
  `fetch_feed_info` 补 `Origin: https://channels.weixin.qq.com` 请求头（**缺它会 401**）；
  `app/api.py` 新增 `_create_channel_link_task()`，视频号分支改为「取到元数据就直出成功任务，
  取不到才回原来的失败提示」；`_PLATFORM_REFERER` 加 `视频号`（封面代理需要微信域 Referer，
  否则封面图 403）；`app/models.py` 的 `TaskDetail` 新增 `channelMeta`，历史记录 `extra` 一并落库。
- 前端：新增 `renderChannelCard()`（`renderVideoResult` 里按 `taskType==='videochannel'` 分流），
  历史记录/直达链接（`#/video/qqvideo?task=`）也能还原同一张卡片；视频号页提示改成
  「链接模式」「口播转文字」两行说明。
- 实测（真实链接 `https://weixin.qq.com/sph/ANc1wgtZqr`，作者 AI.Easy）：
  提交 **0.19 秒**即返回成功；标题/作者/正文 125 字/封面/152 赞·1086 收藏·921 转发·148 评论/
  10 个话题标签全部正确；封面代理返回 `200 image/jpg 33KB`；文案已归档到
  `文案输出/2026-11-04_<标题>.txt`；结果页卡片实拍无缺图（头像与封面均加载成功）。
- 新增回归测试 `tests/test_video_channel.py` 三例：真响应解析（并断言**不含**视频流字段）、
  图集作品 `isVideo=False`、贴链接直接建成功任务（`taskType=videochannel`、不排队）。
  `pytest tests/ -q` → **326 项全绿**。

---

## v1.0.4（2026-11-04）

> 用户反馈「有的抖音视频预览播不了、下面的视频截图出不来」，实测复现后修的三处。

### 修复
- **抖音预览选档改成 h264 优先**（`app/extractor.py` 新增 `_probe_codec` / `_prefer_h264`）：
  预览原先固定取"最高码率档"，而抖音的最高档常常是 **HEVC**（实测 3 条里 2 条首档是
  hevc 3840x2160）。Chromium 系浏览器（Chrome/Edge）的 H.265 **只能硬件解码**：显卡/驱动
  不支持、或关掉"图形加速"时，`<video>` 的表现就是「进度条在走、画面冻在第一帧、声音正常」，
  而 `canPlayType` 还返回 "probably"，前端根本判断不出来。现在取数时用 ffprobe 读文件头
  嗅探编码（`-rw_timeout` 4s + 6s 硬超时，8 条候选并发探），把 h264 那档提到最前，
  其余保持原顺序 —— 探测失败不影响原有回退链。
- **前端「播了却没画面」自动换 h264 直链**（`static/index.html`）：不再靠 `canPlayType` 猜，
  只看事实 —— 播 1.5 秒后 `getVideoPlaybackQuality().totalVideoFrames` 仍是 0，就自动
  带上 `&codec=h264` 重取，并跳回原播放位置继续播；换完 6 秒还是 0 帧，提示条直接告诉用户
  「这条作品只有 HEVC 编码，点保存视频用本机播放器看」。解码报错（`onerror`）也先试一次
  h264 再隐藏，不再一报错就判定"视频源已过期"。
- **服务端按 codec 重排候选**（`app/api.py` 的 `media_proxy` 新增 `codec` 参数）：老记录
  （选档修复前生成的）h264 可能排在很后面，所以按顺序嗅探最多 8 条、命中即停；嗅探结果
  进程内缓存，后续拖动进度条不再重复探测。
- **截图抽帧提速 + 并发踩踏**（`app/frame_ocr.py`、`app/api.py` 的 `media_frame`）：
  ① 抽帧改用 `-skip_frame nokey` 只解关键帧 —— 同一条 507s 视频由「54.8 秒出 3 张」
  变成「3.4 秒出满 5 张」（旧路径要顺序解到第 90 秒，CDN 中途截断报
  `Stream ends prematurely at 77246417, should be 166079510`，这就是"只出一两张"的真因）；
  关键帧拿不满 n 张时，改按时间点并发 seek 兜底（每个点只读几十 KB，实测 5 个点 2~3 秒）。
  ② 前端一次并发发 5 个 `/media/frame`，旧逻辑每个请求都各自清空 `_thumbs` 再重抽、
  互相删图 —— 冷缓存实测：1 个 62 秒成功、4 个 200 秒超时、磁盘最后一张不剩。现在按
  taskId 串行（同任务一把锁，不同任务互不影响），后到的等锁后直接命中缓存；
  失败还记 60 秒，避免 5 个请求各跑一遍 180 秒超时。
- 新增回归测试 `tests/test_douyin_preview.py`（7 例）：h264 优先排序、单条直链不探测、
  编码记忆、`-skip_frame nokey` 只在 http 源加、5 并发只抽一次帧、失败记忆 60 秒。
  `pytest tests/ -q` → **323 项全绿**。

### 实测（2026-11-04，匿名、无代理）
- 修复前后各跑 3 条：修复前 `first3 = [hevc 4K, hevc 4K, h264 1080p]` ×2 条；
  修复后 6 条全部 h264 打头（含一条 h264 原本排在第 5 位之后的）。
- 5 张截图：全部 200，总耗时 0.4 / 0.7 / 1.2 / 2.6 / 4.9 / 6.6 秒（原来 62 秒还可能一张都不剩）。
- 浏览器复现验证：用 `--disable-accelerated-video-decode` 关掉硬解（模拟用户的 Edge），
  首帧 0 帧 → 1.5 秒后自动切 `&codec=h264` → 画面恢复，`totalVideoFrames` 25 → 49 递增；
  结果页实拍：视频出画面 + 5 张 960x540 截图全部加载。

---

## v1.0.3（2026-11-04）

### 修复
- **抖音图集（图文）作品现在能提取**：取数入口把 `/note/<id>` 改写成 `/video/<id>`
  （`app/douyin.py` 的 `normalize_url`）。实测（2026-11-04，匿名、无代理、全新上下文）：
  直接开 `/note/` 页面从不请求详情接口；开 `/video/<id>` 时抖音自己跳回 `/note/<id>`
  并正常请求 `aweme/v1/web/aweme/detail`，返回 `desc` + `images[]`。3 条图集实测
  6.7s / 3.8s / 10.5s，图片 5 / 1 / 20 张。
- **图集判据修正**（`app/extractor.py` 的 `_run_douyin_task`）：旧逻辑拿"没有 video_url"
  当图集判据，但图集作品的 `aweme_detail` 同样带 `video/play_addr`（轮播合成流），
  会让图集误走视频 ASR（转出来是背景音乐）。改为"有 `images[]` 就走 OCR"，OCR 无文字时
  用作品描述兜底。
- 端到端验证：图集 5 张 / 20 张 → OCR 出 343 字 / 903 字（RapidOCR，9.0s / 27.8s），
  全程未使用账号、未使用代理。
- **长视频/直播回放「抖音视频下载失败（多源重试耗尽）」修复**（2026-11-04 用户反馈）：
  `aweme_detail` 的 `bit_rate[]` 里混着**纯画面轨**（DASH video-only，ffprobe 只见
  hevc/h264、没有 aac）。旧逻辑按码率从小到大硬下，连撞两个空轨（16.8MB / 18.3MB）
  才落到默认档；而默认档常是 1080p（4 分钟约 90MB），容易被 CDN 掐连接
  （`WinError 10054`），最后退到 yt-dlp 又被要求
  `Fresh cookies (not necessarily logged in) are needed` 连拒 3 次 → 整条任务失败。
  现在下载前先用 ffprobe 远程读文件头探音轨（`app/extractor.py` 的
  `_url_has_audio_stream`），**纯画面轨直接跳过、不下载**；候选档由 2 档扩到 6 档。
  实测同一条 26 档回放（`7701063828691356937`，4:19）：跳过 2 个纯画面轨后
  第 3 档（597kbps，aac+hevc）一次成功，提交到出文稿 11 秒（含模型加载）。
- **抖音选档失败现在自带「逐档体检」**（app/extractor.py）：候选每档都会记下
  档名@码率与实际结果（无音轨 / 下载被中断 / 下载后无音轨(18.3MB) / 转写失败(类型) / 成功），
  失败时整串写进任务报错与日志，例如「抖音视频下载失败：候选1 720_4_1@544kbps 无音轨；
  候选2 默认档 下载被中断；yt-dlp 兜底也没取到（抖音解析器现在对匿名请求要求 cookie）」。
  以前只报「多源重试耗尽」，看不出卡在哪一档。
- **远程探音轨加上硬上限**：实测 CDN 抖动时 ffprobe 远程读头会从 0.2s 挂到 19s
  （把整条链路拖慢到近 3 分钟）。现在加 -rw_timeout 5s + 子进程 8s 硬超时，
  探测拿不到结论就记一行日志、直接改为「下载后本地验证」，不让优化反噬主链路。
  同一 4:19 回放端到端由 176 秒降到 26 秒（09:53:06 提交 → 09:53:32 归档）。
- 新增离线体检脚本 scripts/diag_douyin.py：给一条抖音链接/ID，逐档列出
  「有音轨 / 纯画面轨 / 探测失败」并指出生产链路会选哪一档，下次报错先跑它。
  配套 4 条离线单测 tests/test_douyin_diag.py（不联网、不起浏览器）。

### 优化与变更
- **「同时识别画面文字」改为默认不勾选**（2026-11-03 按需求）：抽帧 + OCR 会让每条任务
  多花几十秒到几分钟，改为需要时手动勾选。平台维度的默认值仍保留在
  `app/platforms.py` 的 `adapter.frame_ocr_default`（现在全是 False），要按平台恢复默认开只改这一处。
  **抖音图集的 OCR 不受这个开关影响** —— 那是图集唯一的内容来源，走
  `app/extractor.py` 的 `_ocr_images`，始终执行。
- **切换页面时停播并释放视频流**（`static/index.html` 的 `showView()`）：以前离开提取页
  只是把视图隐藏，`<video>` 仍在后台继续拉流、进度条照走（挂多条任务时尤其明显），
  现在切走即 `pause()` + 移除 `src` + `load()`。
- **刷新直链的行为口径**：B站自 2026-11-03 起也可以「刷新直链」（走 yt-dlp 重取），
  现在只有小红书等平台会返回「该平台不需要刷新直链」。
- 打包前回归：`pytest tests/` **316 项全绿**；顺手修正 12 条**过期测试**（不是产品缺陷）：
  图集 OCR 的假响应缺 `headers`、OCR 失败用例 patch 错了入口（会真的去请求外网）、
  画面文字 OCR 的默认值、把 B站当"不支持刷新"、预算门用例还在测已移除的 TikTok 通道
  （改用快手）。`app/platforms.py` 的字段注释与 `frame_ocr_default_for` 文档字符串
  也一并改成与代码一致的口径。

### 新增：发现页（B站搜索 / 抖音一级评论，零账号零代理）
- 顶部导航新增「发现」（`#/discover`），两条通道各自落在独立模块里：
  - **B站关键词搜索**（`app/bilibili.py`）：纯 HTTP，无需浏览器 —— 访客指纹
    `buvid3/buvid4` 来自 `/x/frontend/finger/spi`，wbi 签名取自未登录也返回的
    `nav.wbi_img`（`w_rid = md5(sorted_query + mixin)`）。20 条/页、`numResults=1000`
    可翻页，另有热搜词。实测「编程副业」第 1 页返回 18 条有效视频。
  - **抖音一级评论**（`app/douyin_comments.py`）：必须借作品页上下文发请求 ——
    纯 Python 直连（哪怕带上浏览器全套 cookie）只会拿到 **0 字节 body**，是 TLS/JS
    指纹层面的拦截；页面内 `fetch` 实测 **不需要 a_bogus**，`count` 可自定（5/20/50）、
    `cursor` 任意翻。298 条评论的作品取 50 条 = 3 页 / 5.1 秒（含浏览器启动）。
  - 接口：`GET /prod-api/discover/bilibili/search`、`GET /prod-api/discover/bilibili/hot`、
    `POST /prod-api/discover/douyin/comments`（`app/api.py` 末尾）。
  - 前端：搜索结果带勾选框 —— 单条「提取文案」跳到 B站提取页并填好链接；勾选后
    「用选中链接批量提取」跳到批量页填好每行一条。评论可「复制全部」/「下载 txt」
    （昵称、点赞、IP 属地一并落盘）。
- 测试：`tests/test_discover.py`（6 条，纯离线：wbi 签名、时长解析、字段裁剪、参数校验）。

### 已知仍受限（不改代码就能确认的边界）
- 抖音**关键词搜索**：访客 `status_code=2483`「请先登录，再继续搜索吧」→ 必须账号。
- B站**访客字幕**：16 条抽样 0 条返回字幕（`player/wbi/v2` 的 `subtitle.subtitles` 为空），
  B站文案仍走 ASR。
- B站**访客评论**：只有 3 条最热、翻不到第二页（抖音一级评论已支持，见上）。

---

## v1.0.2（2026-11-04）

> 本版为**现有功能基线快照**（打包于 2026-11-04），用于在后续「抖音/B站免代理·免号池采集」改造前留档回退。

### 新增
- **账号页加「退出」按钮**（每个账号一个，含默认账号）。
  此前只有「登录/验证」，**没有退出通道** —— 被平台标记的号、废弃的号
  无法从界面清掉，会一直留在池子里被反复选中。
  点「退出」会删除该号的：登录 cookie 文件 + 它专用的浏览器 profile
  （被平台打上的标记就在 profile 里，一并清掉）。**删的是登录态，不是历史记录**，
  重新登录即可恢复。删不掉时（浏览器窗口还开着会占用 profile）会如实回报，不假装成功。

### 变更
- **Cookie 策略仍为 `fallback`（曾一度改为 `always`，已回滚）**。
  11-02 曾改成 `always`（每次请求都带登录 cookie），理由是"抖音返回验证码页、
  归类为风控而非「需要登录」，导致 `fallback` 下登录 cookie 一次都用不上"。
  **11-03 实测证明这个方向是反的**：
  - 同一条出口、同一个全新 profile，**匿名成功、带 cookie 被拦**
  - 说明病根不是"匿名裸奔"，而是**这个账号/旧 profile 已被平台标记**；
    `always` 反而让每个请求都带上被标记的账号，平台每次直接甩验证码
  - 那次"匿名只跑 2 条就被拦"，真实原因是**旧 profile 脏**，与匿名无关

  现回到 `fallback`（匿名优先，只在平台明确要求登录时才动用账号）。
  `off` / `fallback` / `always` 三个值都保留，按需自选。

### 修复
- **无头浏览器的 UA 会泄漏 `HeadlessChrome`** —— `app/browser.py` 在调用方没传 `user_agent`
  时用浏览器自带的默认 UA，而无头模式下那是 `...HeadlessChrome/156...`，等于向目标站广播
  "我是自动化"。实测：小红书通道（`app/xiaohongshu.py:94`）没传 UA，发出去的就是它。
  现改为**统一兜底**一个正常桌面 Chrome UA。
- **"换用干净浏览器上下文重试"用的反而是更差的浏览器** —— 持久路径指定了
  `channel="msedge"`（真实 Edge），而非持久路径没指定，Playwright ≥1.49 会启动
  **`chromium_headless_shell`**（精简无头内核，最易被识别）。于是"重试"必然也被拦，
  日志里表现为"换用干净上下文重试仍被拦"。现两条路径统一用真实 Edge 内核。
- **pip 依赖安装的回退逻辑失效** —— 原逻辑是"官方源失败才换清华镜像"，
  但国内访问 PyPI 常常是「慢而不成功」，回退永远不触发，装依赖能挂 20 分钟以上
  （在 Ubuntu 容器里实测踩到）。改为**镜像优先** + `--timeout 30 --retries 2`，
  失败再回官方源。
  - 影响范围：`scripts/linux/start.sh`（Ubuntu）、`scripts/launch.py`（Windows 首次启动）
  - 可用环境变量 `TRANSCRIPT_PIP_INDEX` 指定其它镜像

---

## v1.0.1（2026-11-02）

### 新增
- **媒体代理节流**：`/media/proxy` 与 `/media/cover` 此前是唯一没有限速闸门的对外路径。
  现按 CDN 域名独立节流（默认 500ms，`TRANSCRIPT_MEDIA_THROTTLE_MS` 可调，`=0` 关闭）。
  间隔刻意比抓取通道松 —— 媒体打的是 CDN，且要支撑播放器的 Range 流式拖动。
- **日预算随账号池扩容**：上限 = 单账号额度 × 账号池账号数（池为空时 ×1，与旧行为一致）。
- 新增文档 `docs/风控功能清单.md`：盘点哪些功能涉及平台风控、现有三级闸门、仍开着的缺口。

### 修复
- **`scripts/setup_new_machine.py` 在 Linux 上会下载 Windows 版 ffmpeg** —— 改为按平台选择：
  Windows 走 gyan.dev 的 zip，Linux 优先 `apt/dnf/pacman`、失败退到静态构建。
  结尾提示也改成平台感知的（Linux 不再提 UAC）。

### 文档
- 包内《使用说明》补上视频号功能的**必要前提：本机必须装有「微信 PC 端」并已登录**
  （下载按钮是注入到微信的视频号页面里的）。
- 修正第三方声明里的一处自相矛盾：同一段既写"启动时会自动安装"、
  又写"默认不安装、不自启"（改配置时漏改了后半句）。

---

## v1.0.0（2026-11-02）

首个打包版本。相对 10-31 的代码状态，修复了以下问题：

### 修复
- **文件转写丢失 ASR 参数**：上传文件与监视目录（视频号主链路）走的 `model.transcribe(path)`
  是 whisper 全默认参数 —— 丢了 `language="zh"`、简体中文 `initial_prompt`、`vad_filter`、
  `condition_on_previous_text=False`。实测后果是**输出繁体、静音段易出幻觉文本**。
  现改为委托 `extractor._transcribe`，与链接抓取走同一套参数。
- **10 处 ffmpeg/ffprobe 未隐藏控制台**：Windows 上每个转写任务都会闪黑窗
  （音频增强、流探测、转封装、HLS 拉流、抽帧、时长指纹…）。
  新增 `app/subproc.py` 统一提供 `hidden()`，10 处调用点全部补齐。
- **`/ext/tasks` 把 Python 异常原文吐给前端**：此前界面会出现
  `URLError: <urlopen error [WinError 10061] ...>` 这类半中半英的实现细节。
  改为统一的中文提示，原始异常只进日志。
- **`_humanize_error` 缺规则**：只有 403、没有 404，也没有"网页无法下载""网络超时"
  这些常见报错，导致它们原样回显英文。补齐三条规则。
- **Pydantic 校验失败返回 FastAPI 默认 422**：形状是 `{"detail":[...]}`、内容是英文，
  与全站 `{code, msg, data}` 契约不一致。统一为 HTTP 400 + 中文提示，
  畸形 JSON 单独给"请求体不是合法的 JSON"（不再把字符偏移量当字段名展示）。
- **导出接口状态码不一致**：`/ai-face/exportTask` 对"任务不存在"返回 400，
  而同类接口返回 404。现统一为 404（格式错误等仍为 400）。
- **登录状态与校验不同源**：`/login/status` 的 `logged_in` 只看 cookie 文件存在且非空，
  而 `/login/check` 读的是浏览器 profile —— 两者会给出矛盾的结论
  （实测：状态页说抖音"已登录"、check 说"未登录"）。
  现状态判定要求**至少一条未过期 cookie**，且 check 在 profile 无登录态时**回落到 cookie 文件**。
- **打包脚本误排除 `app/__init__.py`**：排除规则里的 `_*` 把包标记文件也一起排除了。
  小包靠 Python 的"命名空间包"侥幸能跑，换到嵌入式 Python 才暴露成
  `ModuleNotFoundError: No module named 'app'`。

### 新增
- `start.bat` 的首启环境自检（缺 ffmpeg / 浏览器内核时自动跑新机部署）。
- 三个打包脚本：`build_release.py`（Windows 依赖版）、`build_offline.py`（Windows 完全离线版）、
  `build_ubuntu.py`（Ubuntu 依赖版）。
- Ubuntu 的一键启动脚本 `scripts/linux/start.sh`。
- 视频号下载器的离线安装包随包附带，并在包内 `.env.example` 预置自装开关
  （`TRANSCRIPT_EXT_DL_AUTOSTART=1`）：首次以管理员身份运行 `start.bat` 即自动装好。
- 包内《使用说明》：合并为单一文档，含使用说明 + 遇到问题 + 第三方组件与许可声明。