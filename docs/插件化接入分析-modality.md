# 插件化接入分析 · 目标宿主 = modality

> 分析对象：`F:\project\modality`（Go 1.27 + Gin + PostgreSQL + gopn）
> 被接入方：`F:\文案提取工具\transcript_tool`（Python 3.11 + FastAPI + faster-whisper）
> 分析目标：**只做方案判断，不改代码**
> 分析日期：2026-10-30
> 前置结论：上一篇《插件化分析-参考Halo.md》的路线 C（整站迁移）在这里变为**必须面对的现实**

---

## 0. 结论先行

**modality 的插件机制是"Go 二进制 + gRPC 子进程"，你的 Python 项目装不进去。但这反而是好消息——它和你在 `README` 里已经定下的「Go 宿主 + Python 插件」几乎是同一张图纸。**

| 问题 | 结论 |
|---|---|
| 能不能把 `transcript_tool` 打成 `.mgpn` 直接装进 modality | ❌ 不能。`.mgpn` 里的 `binary` 必须是能被 `exec.Command` 直接执行、并用 hashicorp/go-plugin 握手的 **Go 程序** |
| 要不要因此把 Python 全重写成 Go | ❌ 不要。ASR/OCR/yt-dlp/Playwright 在 Go 侧没有等价物（这正是上一篇路线 C 否掉的理由） |
| 正确的接法是什么 | ✅ **三段式**：Go 薄插件（`.mgpn`，进 gopn）+ Python 算法服务（本机/容器，gRPC 或 HTTP）+ 宿主侧任务模型落 PostgreSQL |
| 现在能不能直接开工 | ⚠️ **不能**。modality 的插件链路有 4 处断点（详见 §3），其中 2 处是硬阻塞，必须先修 |
| 最大的架构冲突 | **任务模型**。modality 假设"插件调用是毫秒级"，你的任务是分钟级 + GPU 独占（详见 §4） |

一句话：**架构方向是对的，但 modality 现在还不是一个能跑真实插件的宿主。先补宿主，再接插件。**

---

## 1. modality 的插件机制（实测拆解）

### 1.1 形态

| 项 | 事实 | 出处 |
|---|---|---|
| 插件包 | `.mgpn`（tar.gz），内含 `plugin.json` + 可执行二进制 + 可选 `ui/` | `manifest/manifest.go:13`、`mgpn.go` |
| 清单字段 | `name`(kebab-case，须等于目录名) / `version` / `binary` / `services` / `grants` / `extensions` / `ui` / `min_host_version` | `manifest.go:22-44` |
| 进程模型 | `hashicorp/go-plugin` + gRPC + autoMTLS，子进程 | `runner/runner.go:120-136` |
| 协议版本 | 固定 `1`，宿主与插件必须一致 | `runner/protocol.go:4` |
| 能力授权 | `grants` 声明 → `access.Registry`，**默认拒绝**，区分 `read` / `controlled-write` | `gopn.go:173`、`access/capability.go` |
| 扩展点 | `singleton` / `multi-instance` + `priority`（大者先执行） | `extensionpoint/definition.go` |
| UI | 静态资源挂到 `/plugins/<name>/`，SPA fallback + ETag；路由由 `routes.json` 纯数据声明 | `ui/handler.go`、`app/plugin/mount.go` |
| 生命周期 | `installed → starting → running → stopping → stopped / failed`，崩溃自动重启（指数退避，上限 5 次） | `runner.go:22-28` |
| 健康检查 | 每 10s `Ping()`，连续 3 次失败判崩溃 | `runner.go:282-330` |

### 1.2 和 Halo 的对照

有趣的是，这套设计明显是照着 Halo 抄的：`plugin.json` ↔ `plugin.yaml`、`grants` ↔ 权限、`extensions` ↔ 扩展点、`ui/` ↔ `resources/ui`。**你上一篇分析里"该抄的 7 个设计"，modality 已经抄了 6 个。**

差异在于隔离强度：

| 维度 | Halo | modality/gopn | 你的需求 |
|---|---|---|---|
| 隔离 | 类加载器（同 JVM） | **独立进程**（gRPC + mTLS） | 独立进程 ✅ |
| 语言 | 仅 Java | 仅 Go（`exec` 直接跑二进制） | 需要 Python |
| 崩溃影响 | 拖垮主站 | 只杀子进程，自动重启 | ✅ |
| 资源控制 | 无 | 无（无 cgroup/ulimit，PRD 明确"不做沙箱"） | 需要 GPU 独占 |

**modality 的隔离比 Halo 强，语言自由度比 Halo 窄。** 这个组合决定了：隔离不是问题，语言才是问题。

---

## 2. 为什么 Python 装不进去（技术细节）

`runner.go:120` 的启动方式是：

```go
Cmd: exec.Command(filepath.Join(dir, binary))
```

go-plugin 的握手协议是：子进程向 **stdout** 输出一行 `CORE-PROTOCOL-VERSION|APP-PROTOCOL-VERSION|NETWORK-TYPE|NETWORK-ADDR|PROTOCOL`，然后在该地址提供 gRPC 服务。这套协议在 Go、Python、Java、Rust 都有官方实现（`hashicorp/go-plugin` 有多语言客户端）。

**理论上有 `py-plugin` 这类 Python 实现。** 但在这个宿主里不行，因为：

1. **`manifest.go:133-142` 强制校验二进制可执行位**：`info.Mode().Perm()&0o111 == 0` 就拒绝。Python 脚本要过这关，得依赖 shebang + 可执行位，在 Windows 上直接不可行（下面 §3.2 会实测说明）。
2. **`exec.Command` 不设 `Cmd.Dir` 也不设 `Cmd.Env`**（`runner.go:120-136`，我 grep 过整个包，两处都没有）。这意味着子进程的环境变量、工作目录完全继承宿主，**你没有任何渠道把 `TRANSCRIPT_LLM_API_KEY`、`CUDA_VISIBLE_DEVICES`、模型缓存路径传给插件**。
3. 就算塞进去了，`.mgpn` 里要打包 CUDA 运行时、ffmpeg、whisper 模型（几 GB），而安装上限是 100MB（`install.go:23`）。

**因此：Python 侧只能作为「外部服务」存在，不能作为 gopn 插件的 `binary`。**

---

## 3. 当前链路的 6 处断点（全部实测，非推测）

我用最小程序实际跑了每个环节。这一章是本文最有价值的部分——**如果不先修这些，你按任何方案做完都跑不起来。**

### 3.1 【硬阻塞】安装链路必然失败：临时目录名 ≠ 插件名

`install.go:76`：

```go
mf, loadErr := manifest.LoadManifest(filepath.Join(extractDir, "plugin.json"))
```

而 `LoadManifest` 会执行 `validate(dir)`，其中包含：

```go
case checkDir && filepath.Base(dir) != m.Name:
    errs = append(errs, ValidationError{Field: fieldName, Message: "须与目录名一致"})
```

`extractDir` 是 `os.MkdirTemp("", "plugin-install-*")` 生成的，形如 `plugin-install-1442959654`，**永远不等于插件名**。

**实测输出**：

```
临时目录名: plugin-install-1442959654
install.go 安装校验结果: err=manifest: name: 须与目录名一致; ...
```

即：**任何插件包，只要走上传安装，100% 失败。**

而代码库自己的测试已经承认了这一点（`install_test.go:203-209`）：

```go
require.Error(t, err)
assert.True(t, buserr.Is(err, plugin.ErrPackageInvalid),
    "清单目录名校验与临时解压目录冲突(生产缺陷),此处必然失败")
```

**修复方式**：`install.go:76` 改用 `manifest.LoadManifestFromArchive(...)`（该函数本就为"跳过目录名校验"而存在，`discover` 用的就是它）。

### 3.2 【硬阻塞】Windows 上二进制可执行位校验恒失败

`manifest.go:140`：

```go
case info.Mode().Perm()&0o111 == 0:
    errs = append(errs, ValidationError{Field: fieldBinary, Message: "文件不可执行: " + m.Binary})
```

Go 在 Windows 上对**任何**文件返回 `Perm() == 0666`（我写了最小 Go 程序验证）：

```
Perm()=0666  exec-bit-set=false
```

我最初用 Python 的 `os.stat` 模拟，得到 `0777`，**这个结论是错的**——Python 和 Go 在 Windows 上的 `mode` 语义不同。实测 Go 才是准的。

**后果**：在 Windows 上开发 modality，`Discover` 会跳过**所有**插件。实测：

```
[WARN] manifest: 跳过无效插件 plugin-a.mgpn: binary: 文件不可执行: plugin-a
got 0 manifests, want 2: []
```

注意这只是**开发期**问题——生产是 Linux 容器（`deployments/Dockerfile` 用 alpine，`CGO_ENABLED=0`），tar 里的 `0o755` 能正确保留。但这意味着**你无法在 Windows 上本地联调插件**，必须在 Docker 或 WSL 里做。

**修复方式**：`if runtime.GOOS == "windows"` 时跳过该校验，或改为白名单后缀（`.exe`）。

### 3.3 【硬阻塞】安装后的落盘形态，扫描器不认

`install.go:207` 把插件装到 `plugins/<name>/`（**目录**），然后调 `Sync(ctx, pluginDir())`。

但 `discover_impl.go:27-32` 只认 `.mgpn` **文件**：

```go
if entry.IsDir() {
    continue          // ← 目录直接跳过
}
if filepath.Ext(entry.Name()) != MgpSuffix {
    continue
}
```

**实测**：

```
== 场景1:目录形态(安装后的实际落盘形态) ==
发现 0 个, err=<nil>
```

即：**装完 = 看不见。** 重启后 `bootstrapPlugins` 调 `Sync` 同样发现 0 个，插件从注册表消失（数据库里有记录，但目录扫不到，状态对不上）。

这不是"设计如此"，而是 `Sync` 的语义与安装落盘形态不匹配：`Sync` 面向"手动把 `.mgpn` 丢进目录"，`install` 面向"解压成目录"。两条路必须统一。

**修复方向**（二选一）：
- **A（改动小）**：安装时保留 `.mgpn` 原包到 `plugins/`，解压目录放 `plugins/.unpacked/<name>/`，`Discover` 只读 `.mgpn`；
- **B（语义更顺）**：`Discover` 同时支持"目录含 `plugin.json`"和"`.mgpn` 文件"两种形态，目录形态走 `LoadManifestFromArchive`。

建议 B——它同时解决 3.1（安装时目录名已经是插件名，可以用 `LoadManifest`）。

### 3.4 【阻塞】宿主没有任何地方能调到插件

`Nexus.Dispense()` 存在（`gopn.go:217`），`service.Registry` 也完整。但**全仓 `grep SetService` 的结果只有测试**：

```
core/library/gopn/facade_e2e_test.go:125
core/library/gopn/gopn_test.go:114,116
```

生产代码里**一次都没有注册过服务工厂**。也就是说：插件能启动、能握手、能被 ping 通，但**宿主没有任何类型安全的客户端去调它**。

这正是 `.agents/notes/2026-10-23-plugin-system.md` 里自己列的 Follow-up #2：

> Replace the JSON-compatible broker channel with typed proto contracts and per-call SAM authorization on the host side.

**对你的含义**：gopn 目前是"能管进程、能挂 UI"的骨架，**还不是"能干活"的插件总线**。你要接业务，必须先补：定义 proto → `SetService("transcript.v1.Transcriptor", factory)` → 在某个 service 里 `Dispense` 后调用。

### 3.5 【阻塞】扩展点只登记，不分发

`registerExtensions` 把声明写进 `extensionpoint.Registry`，但**没有任何代码去按扩展点查找实现并调用**（`grep "Extensions\."` 只有注册、删除、查询三处，无 dispatch）。

`access.Authorize` 同样**零生产调用**——`grants` 只是登记，没有任何请求被真正鉴权。

**对你的含义**：`grants` 和 `extensions` 现在是"元数据"，不是"机制"。别指望靠它们做权限隔离或插件间协作。

### 3.6 【中等】`min_host_version` 不校验

`manifest.go:29` 声明了 `MinHostVersion`，但 `validateCommon` 里**没有用它做任何比较**。插件可以声明 `>=9.9.9` 照样被加载。

**对你的含义**：插件与宿主的版本契约目前是空的。等 `transcript` 插件的 proto 一旦演进，老插件会静默失败。**建议在动手写插件前先补这个校验**（比 Halo 的 `spec.requires` 简单，一个 semver 比较即可）。

---

## 4. 最大的架构冲突：任务模型不匹配

这一章比上面的断点更重要，因为它不是"改个函数"，而是"两种世界观"。

### 4.1 双方假设对照

| 维度 | modality 的假设 | 你的任务实际 |
|---|---|---|
| 单次调用耗时 | 毫秒～秒（CRUD、通知、工单流转） | **分钟级**：抖音全流程 61s、笔记 114s、CPU medium ~360s |
| 健康检查 | 每 10s `Ping()`，连续 3 次失败 = 崩溃 → **杀进程重启** | ASR 期间进程忙但健康 |
| 超时 | `LaunchTimeout` 15s（握手）、`StopTimeout` 5s | 握手快，但**单次转写远超任何 HTTP 超时** |
| 并发 | 无显式控制，靠 DB 连接池 | **必须串行**（6GB 显存，`_WHISPER_LOCK`） |
| 重启语义 | 崩溃 → 自动重启，最多 5 次 | **重启 = 丢失正在跑的转写**（任务态在内存） |
| 请求上下文 | 同步请求-响应 | 提交 → 轮询 → 取结果（你已有 22 条 REST 路由） |
| 资源 | CPU/内存 | **GPU 独占 + 模型缓存（数 GB）** |

### 4.2 危险点：健康检查会不会杀掉正在干活的插件

我读了 `healthLoop`（`runner.go:282-330`）：它调 `raw.Ping()`，这是 go-plugin 的 **gRPC health check**，由 go-plugin 自动注册的 health service 处理，与业务 handler **不在同一 goroutine**。所以正常情况**不会**因业务忙而误杀。

**但有两个真实风险**：

1. **如果插件主 goroutine 阻塞**（比如 Python 侧 GIL 卡住、或 Go 插件里同步调 Python），gRPC server 仍在跑，`Ping` 仍返回 OK → 检测不到。**健康检查会给出假阳性。**
2. **`restart` 会杀掉正在转写的任务**。`onCrash` 直接 `client.Kill()`。你现在的任务态在内存（`HANDOFF` §8.5 自己承认），一杀就全丢。

结论：**健康检查机制本身够用，但"任务态必须在外部持久化"是硬前提。** 这也正好呼应上一篇的 P2 建议。

### 4.3 正确解法：把"长任务"从 RPC 里拿出来

**错误做法**（最容易踩的坑）：

```
Go 插件 → gRPC 调用 → Python 转写 → 等 60 秒 → 返回文案
```

后果：gRPC 默认无超时但宿主 HTTP 有；上游 `gin` 请求超时；ctx 取消传到插件；一次网络抖动全部白跑。

**正确做法**（你现在的架构本来就是对的）：

```
① Go 插件 createTask  → 立即返回 taskId（<100ms）
② Python 侧异步跑      → 状态写 SQLite / 或回调宿主
③ 前端/宿主编排轮询    → queryTask(taskId)
```

即：**gRPC 只传"控制指令"，不传"长任务结果"。** 这和你现有 22 条路由的 `doTask` / `queryTask` 语义完全一致——**你在 Python 侧已经把正确的模式实现好了，接进 modality 时不要退化成同步 RPC。**

---

## 5. 推荐方案：三段式接入

```
┌─────────────────────────────────────────────────────────────┐
│ modality 宿主（Go / Gin / PostgreSQL）                        │
│                                                              │
│  ┌────────────────────┐   ┌──────────────────────────────┐  │
│  │ plugin 域（已有）    │   │ 新增：transcript 任务域        │  │
│  │ 注册表/启停/UI 挂载  │   │ 表：transcript_task           │  │
│  └────────────────────┘   │ 字段：id/url/status/result/    │  │
│                            │      error/owner_id/created_at │  │
│  ┌────────────────────┐   └──────────────────────────────┘  │
│  │ .mgpn 插件           │                                     │
│  │ transcript-bridge    │  ← Go 二进制约 800 行               │
│  │ 职责：                │                                     │
│  │  · 校验 URL / 权限    │                                     │
│  │  · 调 Python 服务     │                                     │
│  │  · 落库 / 发事件      │                                     │
│  └─────────┬──────────┘                                     │
└────────────┼────────────────────────────────────────────────┘
             │ gRPC（推荐）或 HTTP（更简单）
             ▼
┌─────────────────────────────────────────────────────────────┐
│ Python 算法服务（transcript_tool，本机 / 同 Pod sidecar）      │
│  FastAPI :8000                                               │
│  /prod-api/transcript/doTask   （现有）                       │
│  /prod-api/transcript/queryTask（现有）                       │
│  GPU / whisper / yt-dlp / Playwright / OCR（全保留）           │
└─────────────────────────────────────────────────────────────┘
```

### 5.1 三个角色各自该做什么

| 角色 | 语言 | 职责 | 不做什么 |
|---|---|---|---|
| **宿主 modality** | Go | 用户/权限/租户、任务表、审计、配额、前端页面 | 不做转写，不碰 GPU |
| **bridge 插件** | Go（`.mgpn`） | 参数校验、调 Python、写宿主任务表、发 `transcript.changed` 事件 | 不做重活，不缓存结果 |
| **算法服务** | Python | 抓取/下载/ASR/OCR/纠错/笔记（现有全部能力） | 不管用户体系，不管权限 |

**为什么 bridge 要写成 gopn 插件，而不是直接在宿主里加个 service？**

诚实说：**如果你只有这一个"插件"，直接写进宿主是更省事的**。写成插件的好处只有三个：
1. 借 gopn 的进程隔离——Python 服务挂了不影响宿主（但你本来就有独立进程）；
2. 走 `grants` 声明能力——**但目前 `grants` 不生效（§3.5）**，所以这个好处暂时是空的；
3. 为将来"多个媒体类插件"预留形态。

所以我的建议是分两步：

- **第 1 步（先做，1–2 周）**：先在宿主里加 `transcript` 业务域（service + repo + api + 前端页），直接调 Python HTTP。**这步不需要 gopn，也不需要修 §3 的断点。**
- **第 2 步（等 §3 修完后）**：把这段逻辑抽成 `.mgpn` 插件。此时它才真正获得"可独立启停/可被替换"的价值。

如果反过来先做插件，你会被 4 个断点卡住，且做出来的东西和直接写在宿主里功能一样。

### 5.2 Python 服务的暴露方式：三个选项

| 方案 | 做法 | 优点 | 缺点 | 建议 |
|---|---|---|---|---|
| **A. HTTP** | 插件用 `net/http` 调 `http://127.0.0.1:8000/prod-api/...` | 零改造，今天就能通 | 无类型约束，靠文档 | ✅ **起步用这个** |
| **B. gRPC** | Python 用 `grpcio` 起 gRPC server，共享 `.proto` | 类型安全、流式、可做进度推送 | 需写 proto + 双向代码生成 | 稳定后再换 |
| C. 子进程直调 | 插件 `exec` 调 `python -m ...` | 无需常驻服务 | 每次冷启动 10s+（模型加载），且无法复用 GPU 缓存 | ❌ 不要 |

注意方案 A 的地址必须是**固定值或配置项**，不能让用户填任意 URL——这和你上一篇抄 Halo 的第 6 条（SSRF 防护）是同一个道理。

### 5.3 任务表设计（宿主侧）

现有 `workorder` 域有"固定状态机"的先例，建议照它的写法：

```
transcript_task
  id            bigint pk
  task_no       varchar  -- 对外单号
  source_url    text
  platform      varchar  -- douyin/kuaishou/bilibili/xiaohongshu/...
  status        varchar  -- pending/running/success/failed
  progress      int      -- 0-100
  whisper_model varchar
  text_content  text
  segments      jsonb    -- 分段+时间戳
  error_message text
  owner_id      bigint   -- 谁提交的（对应 fileasset/upms 的归属模型）
  external_id   varchar  -- Python 侧 taskId，用于对齐
  created_at / updated_at / finished_at
```

**关键**：`external_id` 必须存。Python 服务重启会丢任务态（`HANDOFF` §8.5），但宿主有 `external_id` 就能判定"这条任务在 Python 侧已经不存在了"，从而标失败或重投。**这是两侧状态不一致时唯一的锚点。**

### 5.4 权限码

modality 的权限是 `服务.动作` **两段式**（`prd/library/sam.md:37`），且强制两段（`2026-10-25-single-binary-packaging.md` 记录过：曾因三段式批量改名）。所以：

```
transcript.create        -- 提交转写
transcript.query         -- 查询自己的
transcript.query-all     -- 查询全部（管理）
transcript.export        -- 导出
transcript.delete        -- 删除
```

**不要**写成 `transcript.task.create`（三段会被 SAM 拦）。

### 5.5 UI 形态

gopn 的 UI 是"静态文件 + `routes.json` 声明"，宿主把 `/plugins/<name>/` 挂出去，**但前端目前没有任何地方消费 `/plugins/routes`**（我 grep 过 `web/src`，只有 `api/modules/plugin.ts` 里的接口定义，没有视图引用）。

所以插件 UI 有两种落法：
- **A. 独立页面**：插件自带 Vue 构建产物，走 `/plugins/transcript/`。但宿主前端现在不会自动注册这个路由，**用户得手动输 URL**——除非你先补前端动态路由。
- **B. 宿主页面 + 插件只提供 API**：转写页面写成宿主的正常页面（`web/src/views/`），插件只做后端。**推荐这个**，和 §5.1 的"第 1 步"一致。

`static/index.html` 那 1000 行单文件**不要**直接塞进插件——它是无构建的原型，接进 Vue 3 + TDesign 的项目里会很难维护。

---

## 6. 必须先修的宿主侧问题（按优先级）

| 优先级 | 问题 | 位置 | 修法 | 不修的后果 |
|---|---|---|---|---|
| **P0** | 安装必失败（目录名） | `install.go:76` | 改用 `LoadManifestFromArchive` | 上传安装 100% 失败 |
| **P0** | 装完扫不到（目录 vs 文件） | `discover_impl.go:27` | `Discover` 支持目录形态 | 重启后插件消失 |
| **P1** | Windows 可执行位 | `manifest.go:140` | `GOOS==windows` 时跳过 | 只能在 Docker 里联调 |
| **P1** | 无 SetService 调用 | 全仓 | 定义 proto + 注册工厂 | 插件调不通 |
| **P2** | `min_host_version` 不校验 | `manifest.go:117` | 加 semver 比较 | 版本漂移静默失效 |
| **P2** | `grants`/`extensions` 不分发 | `gopn.go` | 补 dispatch + `Authorize` 调用 | 权限形同虚设 |
| **P3** | 前端不消费路由 | `web/src` | 动态 `addRoute` | 插件页面进不去 |

**P0 两个是硬门槛**，不修则任何插件方案都跑不通。P1 的两个决定你能不能在本地开发。

---

## 7. 与上一篇（Halo 分析）的衔接

上一篇我给的路线 A（写 Halo 薄插件接远程服务），在 modality 场景下**升级为主线**——因为 modality 本来就是你要用的宿主。

| 上一篇的结论 | 在 modality 下的对应 |
|---|---|
| ❌ 路线 C 整站迁移为 JVM 插件 | 同样不成立：`.mgpn` 只收 Go |
| ✅ 路线 A 薄插件 + 远程服务 | **就是本文 §5 的三段式** |
| P0 能力注册表（`Fetcher` 接口） | 仍然要做，但它服务的是 **Python 内部**（多平台），与 modality 无关 |
| P1 插件清单/生命周期/配置 | modality 已经给了（`plugin.json` / gopn 生命周期 / Viper），**不用自己造** |
| P2 任务即资源 + 调谐 | **变成必需**：宿主 `transcript_task` 表 + Python 侧持久化 |
| P3 进程外插件协议 | **由 gopn 提供**，但 proto 要你自己定（§3.4） |

**最重要的变化**：上一篇说"借 Halo 的契约"，现在变成"用 modality 的契约"。你的 `README` §架构规划 里写的「Go 宿主 + Python 进程外插件」，**modality 就是那个宿主**——这张图纸不用再画了，直接用。

---

## 8. 三个必须提前想清楚的问题

### 8.1 部署形态：Python 服务和宿主怎么放一起

| 形态 | 做法 | 适用 |
|---|---|---|
| 同机 sidecar | 同一个 compose，Python 容器 + Go 容器，共享 `127.0.0.1` 网络 | ✅ 单机、有 GPU |
| 远程服务 | Python 跑在有 GPU 的机器，宿主通过内网调 | 多租户、GPU 集中 |
| 宿主机进程 | Python 直接跑在宿主机（如现在的 Windows） | 开发期 |

**生产必须有 GPU。** modality 的 Dockerfile 是 alpine + `CGO_ENABLED=0`，**宿主机镜像里不可能跑 whisper**——所以 Python 必须是**独立容器或独立机器**，这是硬约束。

### 8.2 多租户与数据归属

modality 是"企业一站式平台"，有多用户/角色/数据范围。你的工具现在是单机单用户、SQLite、无归属概念。

接入时必答：
- 一条转写记录归谁？（`owner_id`）
- 谁能看别人的？（`transcript.query-all` 权限）
- 导出物存哪？（建议走 `fileasset` 域，而不是继续写 `文案输出/` 本地目录）
- 配额怎么算？（`duration` × 模型 折算积分？）

**这几点不设计清楚，接进去就是个"所有人都能看所有文案"的安全洞。**

### 8.3 .env 里那 40 个变量往哪放

`grep TRANSCRIPT_` 实测：代码里读了 **40 个** `TRANSCRIPT_*` 变量，但 `.env.example` 只声明了 5 个。

接入 modality 后，它们要分流：

| 类型 | 例子 | 去处 |
|---|---|---|
| 敏感凭据 | `TRANSCRIPT_LLM_API_KEY` | 宿主配置的加密项 / 环境变量，**绝不进 `plugin.json`** |
| 运行参数 | `TRANSCRIPT_MODEL_CACHE`、`TRANSCRIPT_BATCH_DELAY` | Python 服务自己的配置 |
| 路径 | `TRANSCRIPT_DB`、`TRANSCRIPT_TASKS_DIR` | 容器卷挂载，**不要用相对路径**（`exec.Command` 不设 `Cmd.Dir`，相对路径会落到宿主进程 cwd） |

---

## 9. 建议的推进顺序

```
第 0 步  修 P0 两个断点（install 目录名 + Discover 目录形态）
         └─ 验收：手工构造一个 .mgpn，能装、能启、重启后还在

第 1 步  宿主加 transcript 业务域（service/repo/api）
         └─ 直接 HTTP 调 Python，不碰 gopn，不碰插件
         └─ 验收：宿主页面提交链接 → 轮询 → 看到文案

第 2 步  补 proto + SetService（解决 §3.4）
         └─ 验收：宿主能通过 gRPC 调插件的一个 echo 方法

第 3 步  把第 1 步的逻辑抽成 .mgpn 插件
         └─ 验收：插件能独立停用，停用后页面优雅报错而非 500

第 4 步  补 min_host_version 校验 + grants 真鉴权
         └─ 验收：版本不匹配的插件被拒绝加载
```

**不要跳步。** 尤其不要在第 0 步之前写插件——你会花时间在"为什么装不上"上，而那和你的业务逻辑无关。

---

## 10. 工作量估算

| 项 | 估算 | 说明 |
|---|---|---|
| P0 两个断点 | 1–2 天 | 改动小，但要补测试；注意现有测试断言了"必然失败"，要同步改 |
| P1 Windows 可执行位 | 0.5 天 | 加 `runtime.GOOS` 分支 |
| 宿主 transcript 域 | 5–8 天 | service + repo + api + 迁移 + 前端页，照 workorder 的写法 |
| proto + SetService | 3–5 天 | 含 swag/生成、错误码、超时 |
| 抽成 .mgpn 插件 | 3–5 天 | 打包脚本（现在没有 `scripts/` 打包工具，要新写） |
| Python 侧改造 | 5–10 天 | 见下 |
| **合计** | **约 4–6 周** | 不含多租户/配额等产品化 |

**Python 侧要改什么**（这部分容易被低估）：
1. 任务态持久化（现在是内存，`HANDOFF` §8.5）
2. 加 `owner_id` / 租户字段
3. 导出物从本地目录改走 `fileasset`
4. 去掉 `static/index.html` 的独立入口（改由宿主渲染）
5. 配置从 `.env` 改为可注入（容器环境变量）

---

## 11. 一句话总结

> **modality 的插件体系是"Go 进程外插件"，你的 Python 项目不能变成插件，只能变成插件背后的服务——而这恰好就是你在 `README` 里已经写好的架构。**
>
> **但先别写插件：modality 现在装不上、扫不到、调不通（§3.1–3.4 实测）。先花两天修宿主，再花两周在宿主里直接跑通业务，最后才谈"抽成插件"。**

---

## 附：实测证据索引

| 结论 | 验证方式 | 结果 |
|---|---|---|
| Windows 上 Go 返回 0666 | 最小 Go 程序 `os.Stat().Mode().Perm()` | `0666`，无执行位 |
| 安装链路目录名冲突 | 复刻 `install.go:76` 调用 | `name: 须与目录名一致` |
| 落盘目录不被 Discover 识别 | 构造 `plugins/<name>/plugin.json` 后 `Discover` | 发现 0 个 |
| 代码库自认此缺陷 | `install_test.go:203-209` 断言文本 | "生产缺陷...此处必然失败" |
| 无 SetService 生产调用 | `grep -rn "SetService" --include=*.go` | 仅 3 处测试 |
| 无 grants 鉴权调用 | `grep -rn "\.Authorize(" --include=*.go` | 0 处 |
| `exec.Command` 不设 Env/Dir | 读 `runner.go:120-136` | 确认未设置 |
| 现有测试失败 | `go test ./library/gopn/manifest/... ./app/service/plugin/...` | 多项 FAIL，均为上述 Windows/目录名问题 |
