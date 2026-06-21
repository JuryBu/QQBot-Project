# Plan_2.md - 实际使用问题修复总纲

> 本系列记录主人在实际使用中发现的 Bug/体验问题，逐条整理并关联代码定位。

---

## 问题 1：特殊消息解析缺失

### 现象
- QQ 表情显示为 `[表情:325]` 原始文本，未被解析为可理解的内容（参考图一）
- 视频 mp4 无法解析
- 分享卡片无法解析
- 合并转发等特殊消息类型解析不正常

### 代码定位

**AstrBot 消息组件层** (`astrbot/core/message/components.py`)：
- `Face` L106 — QQ 表情组件，只有 `id` 字段（如 `325`），无语义描述
- `Video` L223 — 视频组件，有 `file` 字段
- `Share` L352 — 分享卡片组件，有 `url`/`title` 字段
- `Json` L662 — JSON 卡片（小程序/分享卡片的底层格式）
- `Forward` L574 — 合并转发组件

**FlashLite 插件层** (`astrbot_plugin_flashlite/main.py`)：
- FlashLite 没有对 `Face`、`Video`、`Share`、`Json`、`Forward` 等组件做专门解析
- 消息经 AstrBot 转为 `message_str` 时，这些特殊组件可能被丢弃或转为无意义的占位文本
- 唯一有视频处理的是 `media_summary` 工具 (L2364)，但需要模型主动调用，不是自动解析

**参考资料**：初始讨论记录副本.md 中 Stage 3 "多模态视觉" 和 讨论点 5 都提到需要处理多模态消息

### 需要做什么
- [x] 在 FlashLite 消息接收链路中拦截各类特殊消息组件 ✅ 已在 aiocqhttp_platform_adapter.py 修复
- [x] QQ 表情 `Face(id=325)` → 转为 `[QQ表情:xxx名称]` 文本描述 ✅ 表情中文名映射
- [ ] 视频 → 自动触发视频 URL/路径提取，视情况调用 `media_summary` 做摘要
- [ ] 分享卡片 `Share`/`Json` → 提取 title + url + description
- [ ] 合并转发 `Forward` → 展开获取内容或做摘要
- [ ] 研究 AstrBot 的 `context_enhancer` 插件如何处理这些组件（可参考其代码逻辑）

---

## 问题 2：文件下载/查看能力缺失 + 模型工具使用指引

### 现象
- 用户发送 MD 文件，Bot 无法下载到 Sandbox 进行 `view_file`（参考图二图三）
- Bot 说"奇怪的绝对路径打不开"——因为 NapCat 本地路径在 Sandbox 外，严格不可访问
- PDF 文件 Bot 说"没发给我"——File 组件可能未正确传递
- 知乎卡片链接看到了标题但无法读取内容——工具通路或知乎防爬墙
- 缺失对 md、PDF 等各类型文件的下载查看能力

### 根因分析

**Sandbox 隔离问题（核心）**：
- `File.get_file()` 返回的是 **NapCat 的本地绝对路径**（如 `C:\NapCatData\temp\xxx.md`）
- `build_main_agent` 将这个路径拼入 `extra_user_content_parts` → 模型收到的是本地路径
- 但模型 Sandbox 严格隔离，**无法访问 Sandbox 之外的任何本地文件** → 路径无意义
- 模型自己也无法"下载到 Sandbox"，因为没有能从本地路径复制的工具

**模型认知缺失（关联）**：
- 即使工具通路可用，模型也不知道：
  - 收到 `[File Attachment: ...]` 时应该怎么操作
  - 收到 `[Card Message: url=...]` 时应该用什么工具访问链接
  - 需要在**系统提示词**中明确指引文件/链接的处理流程

### 代码定位

**FlashLite 现有工具** (`main.py`)：
- `view_file` (L1429) — 只能查看 Sandbox 内已存在的文件
- `modify_file` (L1444) — 只能修改 Sandbox 内文件
- `save_data` (L2656) — 可以保存数据到 Sandbox
- `upload_data` (L2585) — 从 Sandbox 上传/发送文件

**缺失的关键能力**：
- ❌ 没有将消息附件文件复制到 Sandbox 的机制
- ❌ 模型不知道如何使用工具访问注入的链接/路径
- AstrBot 的 `File` 组件有 `file_` 和 `url` 字段，但路径对 Sandbox 不可达

### 设计方案

#### A. 文件访问策略（安全边界设计）

> **原则：仅允许消息附件文件被复制进 Sandbox，其他情况保持严格隔离**

| 场景 | 策略 | 工具 |
|------|------|------|
| NapCat 本地已有文件 | **复制**到模型指定的 Sandbox 位置 | `fetch_attachment` |
| 文件为 URL（未下载到本地） | **下载**到模型指定的 Sandbox 位置 | `fetch_attachment` |
| 其他本地路径 | ❌ 严格禁止访问 | — |

实现：新增 `fetch_attachment` 工具，接受消息中注入的路径/URL，复制或下载到 Sandbox 内。  
安全：仅当路径来源是消息附件（通过白名单/标记验证）时才允许操作。

#### B. 系统提示词指引

在 FlashLite 的 System Prompt 中添加：
- 收到 `[File Attachment: name=xxx, path=yyy]` → 用 `fetch_attachment` 获取到 Sandbox，再用 `view_file` 查看
- 收到 `[Card Message: url=xxx]` → 用浏览器工具或 `web_browse` 访问链接
- 收到 `[Video/Audio Attachment: url]` → 用 `media_summary` 分析
#### C. 调试发现（02:21 测试日志分析）

**完整调用链还原**：
1. `02:21:31` — 模型第一轮调用 `media_summary(content=知乎URL, media_type="forward")` → 返回 `TRIGGER_MAIN=true`（触发主模型，未读取实际内容）
2. `02:22:26` — 第二轮请求出现 **`_query 收到 tools=None!`** → 接着报错 `处理图片描述失败: 'NoneType' object has no attribute 'tool_calls'`
3. `02:22:28` — 恢复后重新注入工具（26个），第三轮正常
4. `02:22:34` — 模型调用 `media_summary(content="QQ用户:[图片]想你了 小胡子...", media_type="mixed")` → **成功获取转发内容**
5. `02:22:54` — 最终回复包含"小胡子"信息 ✅

**关键 Bug: `tools=None` 间歇性工具丢失**：
- `openai_source._query` 在某些轮次收到 `tools=None`，导致模型完全失去工具调用能力
- 伴随错误：`'NoneType' object has no attribute 'tool_calls'`（图片描述处理）
- 可能原因：`_modalities_fix` 或 `on_llm_request` 在某些条件下未正确传递 tools 参数
- **影响**：工具通路间歇性中断，模型只能凭上下文复读而非实际调用工具

> [!WARNING]
> 此 bug 影响所有工具调用场景，不仅限于文件处理。需要在 `openai_source.py` 中排查 tools 参数传递链路。

#### D. 第二轮测试发现（02:53 测试日志分析）

**✅ 问题1修复验证**：
- `[文件:初始讨论记录副本.md]` — message_str 文件名修复成功
- `[文件:讲解稿.pdf]` — PDF 文件名也正确出现
- `[表情:可怜]` — 表情中文名修复成功（event_bus 和 message_str 一致）
- 引用回复 message_str 也成功携带了 `[回复 Jury_鸽姬布: [文件:初始讨论记录副本.md]]`

**❌ PDF 文件模型说"没发过"的根因**：
- PDF 确实被解析了（`MSG_PARSE_DEBUG message_str='[文件:讲解稿.pdf]'`）
- 用户回复了 PDF 消息时，message_str 成功携带了 `[回复 Jury_鸽姬布: [文件:讲解稿.pdf]]还有这个`
- **模型确实看到了文件名**，但仍回复"你根本就没发给老板娘我"
- 根因：模型不理解 `[回复 ... [文件:xxx]]` 意味着用户在指向一个真实文件附件
- 解决：需要在系统提示词中明确告知：看到 `[文件:xxx]` 标记时，文件确实存在，应用 `fetch_attachment` 获取

**❌ 模型工具调用认知不足**：
- MD 文件：模型知道路径是本地的、要上传 Sandbox，但**不知道用什么工具操作**
- 需要在系统提示词中明确告知 `fetch_attachment` 工具的使用方法

**❌ 工具调用错误**：
- `send_image` 报错 `'MessageChain' object has no attribute 'add'`（历史bug，与问题1无关）
- `tools=None` 再次出现（02:51 别的用户图片消息触发）

### 需要做什么
- [x] ~~新增 `fetch_attachment` 工具~~ → 改为 `save_data(local_path=)` 模式实现 ✅ 白名单安全复制
- [x] 安全校验：只允许消息附件来源的路径被操作 ✅ NapCat/QQ/TEMP 白名单
- [x] 改进 `view_file`：支持自动检测文件类型，对 PDF 做文本提取 ✅ pdfplumber + 魔数检测 + 分页
- [x] 在 FlashLite System Prompt 中添加文件/链接处理流程指引 ✅ fallback 链路 + 主动性原则
- [x] 考虑文件大小限制 ✅ 50MB 上限
- [x] sandbox_exec 增强：command 双模式 + bash/shell/cmd 支持 ✅ 对标 MCP sandbox
- [x] save_data 下载后魔数校验 ✅ 假 PDF/图片检测 + 醒目警告
- [x] web_engine file:// 路径修复 + PDF 无 Playwright 降级 ✅


---

## 问题 2.5：模型配置界面参数硬编码

### 现象
- BossLady Console 模型配置页面中，各卡片的参数输入框完全硬编码在 HTML 中
- 主模型卡片只有 Max Tokens + Temperature，即使选了支持思考的模型（如 gemini-3-flash-preview）也不显示思考预算
- Flash Lite 卡片固定显示 同步间隔 + CP上限 + 思考预算，无论选什么模型
- 工具模型卡片固定显示 思考级别 + 思考预算
- 图像模型列表硬编码了两个 imagen 模型，没有从 API 动态获取
- 选择模型后没有任何能力探测、参数组自适应

### 原始设计要求（参考 初始讨论记录副本.md）
1. 输入保存 API Key 后 → **自动获取**此 Key 支持的模型列表
2. 获取模型支持的参数组 → 列表中**选择模型时参数显示内容自动变化**供选择
3. 图像模型 → **只显示带 image 字段**的模型
4. 工具模型卡片 → 允许输入多个 Key 减轻并发压力（已实现 ✅）
5. 整个界面 → **没有硬编码，完全自动智能**

### 参考资料
- `Suggestion_Kaleidoscope_1.md` — REST API 字段名映射、`models.get` 元数据
- `Suggestion_Kaleidoscope_2.md` — 三层能力发现架构（探测→注册表→推断）
- `Suggestion_Kaleidoscope_3.md` — 参数探测试错法

### 代码定位

**前端** (`BossLady_Console/frontend/index.html`):
- L186-208: 🔴主模型 — 硬编码 Max Tokens + Temperature
- L210-235: 🟢Flash Lite — 硬编码 同步间隔 + CP上限 + 思考预算
- L238-269: 🟡工具模型 — 硬编码 思考级别 + 思考预算
- L272-297: 🎨图像模型 — 硬编码 imagen 列表 + 宽高比

**前端 JS** (`BossLady_Console/frontend/app.js`):
- L325-370: `loadModelConfig()` — 固定读取、固定写入 DOM
- L373-410: `refreshAvailableModels()` — 图像模型只按名字过滤 `image/imagen`
- L426-460: `saveMainModel()/saveFlashLite()/saveToolModel()` — 固定字段提交

**后端** (`BossLady_Console/server.py`):
- `/api/models/list` — 已有，返回模型列表
- ❌ 缺少 `/api/models/capabilities` — 不存在模型能力查询接口

### 设计方案

#### A. 后端：模型能力注册表
利用 Gemini API `models.get` 返回的元数据构建能力表：
- `thinking: boolean` → 是否支持思考预算/级别
- `supportedGenerationMethods` 包含 `generateContent` → 可用于对话
- 模型名含 `image`/`imagen` → 图像生成能力
- 对 temperature、top_p、top_k 等通用参数用 `models.get` 的 `temperature` 默认值判断

新增 API: `GET /api/models/capabilities?model=xxx` → 返回该模型支持的参数列表

#### B. 前端：动态参数渲染
- `<select>` 的 `onchange` → 调用 `/api/models/capabilities?model=xxx`
- 根据返回的能力清单，动态创建/显示/隐藏参数控件
- 保存时只提交实际显示的参数

#### C. 图像模型列表自动化
- 从 `/api/models/list` 中过滤支持图像生成的模型
- 不再硬编码 imagen 列表

### 需要做什么
- [x] 后端新增模型能力查询接口 ✅ `/api/models/capabilities` 基于 models.get 元数据
- [x] 前端主模型/Flash Lite/工具模型卡片参数动态渲染 ✅ 基于 capabilities JSON
- [x] 前端图像模型列表从 API 动态获取 ✅ 过滤 image 能力模型
- [x] 保存 API Key 后自动刷新模型列表 ✅
- [x] 测试验证各模型参数显示正确 ✅ 保存后参数回退 bug 已修复

---

## 问题 3：消息过多 + 延迟机制体验不佳 ✅ 至臻

### 现象
- Bot 说话太多，回复内容过长，像连珠炮一样发多条消息（参考图四图五）
- 分段发送延迟不够明显，感觉像机器人连发
- 需要说得更少一点或分得更自然一点

### 已实施的解决方案（技术层面完成 ✅）

**1. 短句合并** — `respond/stage.py`
- 相邻 Plain comp 字数和 ≤ merge_threshold 时自动用空格合并为一条
- `merge_threshold` 可在 BossLady Console 设置界面调整（当前值：60）

**2. 自适应延迟** — `respond/stage.py`
- 新增 `adaptive` 延迟模式（默认启用）
- 短句(≤15字) 0.8-1.5s / 中句(16-40字) 1.5-3.0s / 长句(>40字) 2.5-4.5s
- 可在 BossLady Console 切换 adaptive/random/log 三种模式

**3. 表情包延后** — 表情包插件 `main.py`
- 动态计算等待时间 = 分段数 × 2.0 + 1.0s
- 确保表情包在所有文字消息发送完毕后才出现

**4. MD 格式清洗** — `cmd_config.json`
- `content_cleanup_rule` 设为 `[*#\`>~_|\\-]{1,3}` 清除 MD 标记符
- 在 `result_decorate/stage.py` 中通过 `re.sub` 自动清洗

**5. BossLady Console 设置界面** — 系统设置新增「消息分段设置」卡片
- 延迟模式选择、短句合并阈值、MD 清洗正则 三项可配置
- 后端 API: `GET/POST /api/models/segmented-reply`

**6. AstrBot Schema 注册** ✅ (2026-04-05)
- `default.py` 中注册 `merge_threshold`/`adaptive_delays`/`emoji_delay` 到 DEFAULT_CONFIG 和 CONFIG_METADATA_3
- 解决了配置键被 AstrBot 启动校验机制自动删除的根因

### ✅ 子问题 3-A：Bot 还是话太多

**根因**：上述技术方案（短句合并/自适应延迟）运作正常，但问题不在发送端——**LLM 本身输出的内容就太长太多**。每段分段的文本长度都超过 merge_threshold（60字），所以合并根本无法减少条数。

典型场景：用户问一句简单的话，Bot 回复 5-6 条消息，每条都是完整的长句子。合并机制只能处理"两条各20字的短句可以合并为一条40字"的情况，对"5条各50+字的长句"无能为力。

**解决方案（三层）**：

| 层级 | 方案 | 效果 | 实施难度 |
|-----|------|------|---------|
| 🔴 提示词层 | 在人格/系统提示词中限制回复长度：「每次回复控制在1-3句话，像微信聊天一样简短」 | 从源头减少输出 | 低（修改人格配置） |
| 🟡 硬限分段数 | `respond/stage.py` 中加 `max_segments` 参数，超出的强制合并到末段 | 兜底保护 | 中（代码+UI配置） |
| 🟢 max_tokens | 降低主模型 max_tokens（300-500） | 物理限制 | 低（配置调整） |

### ✅ 子问题 3-B：回复消息引用引导性不足

**根因**：`result_decorate/stage.py` L418 中 `Reply(id=event.message_obj.message_id)` 只在分段的第一条消息中携带引用。后续分段消息"裸奔"——在群聊中看不出这些消息是对谁的回复、关于什么话题，容易混乱。

`respond/stage.py` L302：`header_comps.clear()` 在第一条发送后清除 Reply/At，后续分段不再携带。

**方案比较**：

| 方案 | 优点 | 缺点 |
|------|------|------|
| A. 每条都引用用户原消息 | 明确归属 | QQ 显示太吵（每条都有引用框） |
| B. 后续引用前一条 Bot 消息 | 形成自然消息链 | 需拿到 send() 返回的 message_id |
| C. 从源头减少条数（推荐） | 最优解，与3-A联动 | 需提示词+硬限双管齐下 |
| D. 后续带承接标记（如 ↳） | 简单实现 | 视觉上不够自然 |

**推荐**：**C + B 组合** — 提示词+硬限将条数控制在2-3条（C），同时尝试让后续分段引用前一条已发送消息（B），如果 aiocqhttp 的 `send()` 返回值包含 `message_id` 的话。

### 修改文件清单
- `AstrBot/astrbot/core/pipeline/respond/stage.py` — adaptive延迟 + 短句合并
- `AstrBot/astrbot/core/config/default.py` — Schema 注册新配置键
- `AstrBot/data/cmd_config.json` — 默认配置更新
- `AstrBot/data/plugins/astrbot_plugin_letai_sendemojis/main.py` — 表情包动态延后
- `BossLady_Console/backend/routers/models.py` — 后端 API + BOM 兼容修复
- `BossLady_Console/frontend/index.html` — 前端 UI 卡片
- `BossLady_Console/frontend/app.js` — 前端加载/保存逻辑

