# Plan_2_2.md - 触发/功能/UI 问题

> 问题 9~12：涉及 FlashLite 触发标准、media_summary、Memory 写入、UI 修改

---

## 问题 9：三模型系统提示词审计（完整）

> 审计文件: `QQBotPlan/提示词审计/Prompt_主模型.md` / `Prompt_FlashLite.md` / `Prompt_工具模型.md`

---

### 9-A: 主模型提示词问题

#### 体系与认知层

**🔴 P0: 主模型缺乏体系认知**
- 没有告知模型"你是在 AstrBot 框架 + FlashLite 中断引擎体系下工作的 QQ Bot"
- 模型不知道自己的输出会被 AstrBot 框架处理后发送到 QQ 消息
- 不知道有 FlashLite 帮它筛选消息，不知道有工具模型帮它执行后台任务
- 缺少"系统知识提示词"——只有角色人格 + 碎片化的注入片段
- 后果：模型每次收到消息都在"盲目理解"，影响输出策略和工具调用决策

**🔴 P0: 主模型没有系统时间**
- FlashLite（`_build_flash_lite_system` L709）和工具模型（L753）都注入了 `datetime.now()`
- 主模型的 inject_parts 中完全没有时间信息
- 后果：无法做时间相关判断（如"深夜关心"、"周末打招呼"）

#### 上下文层

**🔴 P0: 主模型看不到用户原始上下文**
- 主模型的上下文来自 AstrBot 框架 `req.contexts`——只包含与 Bot 直接交互过的历史
- 群友回复的引用消息、非直接交互的群聊内容，主模型**一概看不到**
- Section 2 注入的是 FlashLite 的 `context_summary`（一句话摘要），不是原文
- 这就是为什么用户回复消息时模型好像不知道回复的是什么
- 用户指出的"看不到某些文件"也源于此——文件附件信息丢失在上游

#### 工具层

**🟡 P1: 工具使用场景指南缺失**
- 主模型通过 AstrBot `ToolSet` → Gemini API `functionDeclarations` 获得完整工具定义（name/description/parameters）
- FlashLite Section 4 的「工具分类速查」只是辅助文本（仅名字），**不是模型实际的工具定义**
- **但缺少工具使用场景指南**——什么时候用什么工具、工具组合最佳实践、常见错误
- Section 9 的文件处理有详细指南但其他重点工具（search/memory/web_fetch）缺少

**🟡 P1: Section 9 vs Section 13 矛盾 — OFFICE 文件处理冲突**
- Section 9 (L193): `PDF/Office文件处理链: save_data 保存 → view_file 提取文本(pdfplumber)`
- Section 13 (L275): `收到 .docx/.xlsx/.pptx/.pdf 文件时，直接用 web_fetch 处理，不要用 view_file！`
- 两条指令互相矛盾，模型行为不确定

**🔴 P0: 缺少 wait 和 grep 工具**
- `wait(seconds)` — 等待指定时间后苏醒，用于定时提醒、延迟操作
- `grep(pattern, path)` — Sandbox 内快速搜索文件内容
- 主模型和工具模型都需要这两个工具
- 目前模型无法"等一下再做"也无法快速搜索 Sandbox 内文件

#### 思考与草稿层

**🔴 P0: 草稿纸/思考机制没有说明**
- 主模型有 `agent_draft` 工具但提示词没有说明：
  - 如何用来做思考/规划（类似 sequential-thinking）
  - 命名规范和用途分类
  - 何时使用、何时清理
  - 如何记录中间结果供后续步骤引用
- 工具模型同样缺失

**🟡 P1: 指针系统（source_pointer）未文档化**
- `source_pointer` 出现在 task_set/memory_write 等多个工具中
- 但没有统一文档说明"指针"的含义、格式规范、使用场景
- 文件地址/消息ID/上下文标记的互操作方式没有清晰描述

#### 环境与配置层

**🟡 P1: Section 14 Sandbox 环境信息过于简略**
- 只有 OS/Python 版本/网络状态
- 缺失：内存限制、执行超时限制、磁盘空间、已安装的 pip 包列表

**🟡 P1: persona 被后续 inject 冲淡**
- persona prompt ~1100 字在最前，但后续 inject_parts ~7400 字把它推远
- Section 1 的硬性约束是补丁（重复强调），并非根治方案

#### 输出与缓存层

**🔴 P0: KVCache 死代码——初始化但从未调用**
- `self._kv_cache = KVCacheManager(...)` 于 L164 初始化
- **后续 0 处调用**——`_kv_cache.ensure_cache()` / `_kv_cache.build_fixed_contents()` 从未被使用
- KVCacheManager 实现完整（312 行），支持创建/更新/清理 Gemini cachedContent
- 三个模型都没有使用 KVCache：
  - 主模型走 AstrBot 框架 `gemini_source.py`，无 cachedContent 参数
  - FlashLite 直接 HTTP POST，无 cachedContent
  - 工具模型也是直接 HTTP POST，无 cachedContent
- 启用 KVCache 可显著减少重复的 system prompt 处理开销

**🟡 P1: "分轮说"无实际机制**
- 提示词说"复杂内容分多轮说"
- 但 `max_segments` 硬限（默认 3 段）是**对已生成内容做合并裁剪**，不是让模型"主动发起下一轮"
- 模型没有"继续发送"的能力——一次生成就是一次输出
- 需要设计"分轮续发"机制（如模型输出特定标记触发框架续发）

**需要做什么** ✅ 全部完成（详见 Plan_2_2_Task.md Stage 1~6）
- [x] 编写体系认知说明（"你是什么/在什么系统里/你的输出如何到达用户"），注入主模型
- [x] 注入系统时间到主模型 inject_parts
- [x] 解决上下文不可见问题——注入更多群聊原文或增强 FlashLite 的摘要细度
- [x] 补充工具使用场景指南（什么时候用什么工具）
- [x] 修复 Section 9/13 OFFICE 处理矛盾（合并为一条统一规范）
- [x] 新增 `wait` 和 `grep` 工具（双模型共享）
- [x] 编写草稿纸/思考机制使用文档
- [x] 统一文档化指针系统（source_pointer 规范）
- [x] 丰富 Section 14 的环境信息
- [x] **激活 KVCache**——接入 `_kv_cache.ensure_cache()` 到三个模型的调用链
- [x] 设计"分轮续发"机制或移除误导性提示
- [x] 考虑将 Section 1 的硬性约束移到 persona prompt 内部

---

### 9-B: FlashLite 中断引擎提示词问题

**🔴 P0: FlashLite 不知道"自己"是谁 + 跨窗口 ID 不一致**
- 消息拼接格式是 `[时间] 名称: 内容`（`_get_recent_context` L1220-1222）
- Bot 回复以 `sender_name='老板娘'`（L1260 硬编码）存入 DB
- 但 FlashLite **提示词中没有标注「名称=老板娘的消息是 Bot 自己发的」**
- 同一用户在不同群昵称不同（如群A叫"柚子"、群B叫"游戏社长"），仅用昵称无法跨窗口关联
- DB 已存 `sender_id`（QQ号），但 `_get_recent_context` 只取了 `sender_name`

**✅ 已确定修复方案**：
- 消息拼接格式改为：`[19:30:05] 柚子(1135909899): 今天吃什么`
- Bot 回复标记为：`[19:30:12] 老板娘 [BOT]: 碳基生命不需要充电的吗`
- QQ号 10 位数字仅增加 3-4 token/条，对 2M context 模型无注意力影响
- 提示词中补充说明：`[BOT] 标记的消息是你的主模型回复`
- 修改位置：`_get_recent_context` L1220-1222 + `_build_judgment_prompt` 前置说明

**🔴 P0: Memory 召回机制是"盲猜"**
- FlashLite 输出 `MEMORY_HINT=关键词` → 代码用关键词做 `memory.query()` → 结果注入主模型
- **但 FlashLite 完全不知道 Memory 里存了什么** — 它只能根据对话内容"猜测"可能相关的关键词
- 结果：
  - 如果关键词猜准了 → 召回成功
  - 如果关键词偏了 → 召回到不相关的内容
  - 如果 Memory 里没有相关内容 → FlashLite 不知道别猜了，白白浪费一次 query

**✅ 已确定方案: Memory 迷你索引注入（思路 C）**

Memory DB 结构（`memory.py` L261-268）：
| 字段 | 说明 |
|------|------|
| `title` | 标题（必填），通常 ≤20 字 |
| `content` | 正文（必填，上限 15KB） |
| `category` | `general`/`problem-solution`/`technical-note`/`conversation` |
| `tags` | 标签 JSON 数组 |
| `search_summary` | 搜索摘要 |
| `auto_summary` | FlashLite 自动摘要 |
| `pinned` | 是否置顶 |

**方案详情**：
1. 构建迷你索引：每条 Memory 取 `序号 + title + category + pinned状态`
   ```
   [1] "柚子的生日是9月3日" [pinned] #用户信息
   [2] "群<GROUP_B>周五狼人杀约定" [general] #群活动
   [3] "主人喜欢KEY社和�的音乐" [pinned] #用户偏好
   ```
2. 注入 FlashLite 提示词的 `_build_flash_lite_system` 末尾
3. FlashLite 输出 `MEMORY_HINT=1,3`（用序号精确指定）
4. 代码根据序号精确读取完整 content 注入主模型
5. 估算：几十条 Memory → 索引 500-1500 字；几百条 → 索引 ~5000 字（仍可控）

**vs 被否决的方案**：
- 方案 A（2000字 Summary）→ 损失细节，膨胀后频繁压缩
- 方案 B（工具搜索）→ 对 FlashLite 要求过高，模糊搜索不准
- 主模型自调 memory_query → 影响响应时间，否决

**🔴 P0: 触发规则过于模糊**
- "有人直接向老板娘说话/提问" — 太主观
- "话题与老板娘相关" — FlashLite 可能过度解读
- @ 触发是强制覆盖的（L653-655），这个设计正确

**✅ 已确认保留: 唤醒机制维持 event 标志位方案**
- 初始设计是工具管道唤醒，实际用 `event.is_at_or_wake_command=True`（L1152）
- 用户确认当前方案比管道唤醒更好，不改
- FlashLite 保持纯标记行输出 + 代码执行模式，不给工具调用能力

**✅ 已确认: FlashLite 按窗口独立触发**
- 每条消息按 `group_id` 独立计数（`self._msg_counters[group_id]`）
- `_get_recent_context(group_id)` 只查该窗口消息
- Knowledge 按窗口维护（`self._knowledge_cache[group_id]`）
- 不是全局响应，每个群/私聊窗口完全独立

**🟡 P1: 两套 prompt 共存**
- `_build_flash_lite_system`（L674，标记行格式）用于 systemInstruction
- `_build_judgment_prompt`（L1037，JSON 格式）用于 user prompt
- 两套输出格式要求不一致（一个要标记行，一个要 JSON），FlashLite 可能混淆
- 需要统一

**需要做什么** ✅ 全部完成（详见 Plan_2_2_Task.md Stage 2~3）
- [x] 在消息上下文中标注 Bot 身份（`老板娘 [BOT]` 或前置说明）
- [x] 讨论并确定 Memory 索引暴露方案（A/B/C 或融合方案）→ 采用思路 C 迷你索引
- [x] 收紧触发规则措辞，减少 FlashLite 主观误判
- [x] 统一两套 prompt 的输出格式要求
- [x] 评估是否给 FlashLite 工具调用能力 → 确认保持标记行+代码执行模式

---

### 9-C: 工具模型提示词与架构问题

**🔴 P0: 工具模型无法共享主模型工具库**
- 工具模型通过 Gemini function calling（L912）调用工具，但只定义了 **3 个内联工具**：
  - `agent_view_file` — 读 Sandbox 文件
  - `agent_modify_file` — 写 Sandbox 文件
  - `agent_draft` — 读写草稿纸
- 系统提示词列了 20+ 工具名（search/memory_write/web_fetch 等），但**实际都不可调用**
- `Sandbox/base_tools/` 下的 19 个 `.tool.json` 是**主模型工具定义**，工具模型读不到也用不了
- **设计意图**：工具模型应该是"除了不参与 QQ 消息发送交互外功能完全一致的代理"，至少对 Sandbox 权限一致
- **现状**：工具模型能力严重受限，无法执行搜索、Memory 写入、图片生成、代码执行等操作
- 需要将主模型工具注册到工具模型的 `functionDeclarations` 或在 mini agent loop 中增加工具路由

**🔴 P0: drafts / base_tools / workspace 缺使用规范**
- `workspace/drafts/` — 目录存在但为空，提示词只说"复杂任务使用草稿纸"
  - 缺失：命名规范、格式约定、用途分类（计划/临时笔记/中间结果）、何时清理
- `base_tools/` — 提示词说"只读"
  - 缺失：文件格式说明、如何参考写自定义工具、`.tool.json` schema 文档
  - 实际内容是 JSON 工具定义（含 name/description/parameters/timeout_ms/builtin 字段）
- 工具模型不知道这些目录是干什么的，自己要怎么用它们协作来完成任务
- 需要在 `_build_tool_model_system` 中补充详细的使用指南

**🔴 P0: Task 管理机制（tool_task_set）缺陷**

| 问题 | 现状 | 目标 |
|------|------|------|
| 无法命名任务 | `task_id` 自动递增 `task-0001` | 支持自定义 `name` 用于识别 |
| 无法主动查看状态 | `action=check` 但无 `task_list` 概览 | 像 Codex 一样支持 `list` + `check` |
| 默认单任务假设 | 忽略了同窗口多 task 并发场景 | 支持按窗口/全局多任务管理 |
| 轮数上限已调整 | 原 `max_agent_steps=10` 硬编码 | ✅ 默认 20，可通过 `max_steps` 参数覆盖 |
| 单步无超时反馈 | `_execute_agent_tool` 无超时保护 | 需要单步 timeout + 超时回报 |

**单步超时调查结论**：
- `_execute_agent_tool`（L986-1020）执行 3 个内联工具时**无任何超时保护**——直接 `await`
- `sandbox_exec` 工具有 `timeout_ms`（默认 30000，上限 300000），但仅主模型调用 `tool_sandbox_exec` 时生效
- mini agent loop 的 HTTP 请求有 `aiohttp.ClientTimeout(total=30)`，但那是 API 请求超时，不是工具执行超时
- base_tools `.tool.json` 里的 `timeout_ms` 字段（5000~300000）只是元数据，**代码中没有读取和应用**

**需要做什么** ✅ 全部完成（详见 Plan_2_2_Task.md Stage 4~5）
- [x] 将主模型工具库注入工具模型的 `functionDeclarations`（或增加工具路由代理）
- [x] 编写 workspace/drafts/base_tools 的使用规范文档，注入工具模型 systemInstruction
- [x] Task 管理：支持自定义命名、list 概览、多任务并发
- [x] `max_agent_steps` 改为可配置参数（默认 30）
- [x] 为 `_execute_agent_tool` 增加 `asyncio.wait_for` 超时保护
- [x] 读取 base_tools `.tool.json` 的 `timeout_ms` 字段并应用到实际执行

---

## 问题 10：media_summary 工具和特殊消息仪表盘显示异常 ✅ 已解决

### 现象
media_summary（概括视频/聊天记录的工具）运作不正常，且视频/转发等消息无法被仪表盘获取到。

### 已解决

**persistence 层根因修复**：
- [x] `_batch_writer()` INSERT SQL 缺少 `extra_data` 字段 → 所有视频/语音/卡片/文件的元数据虽提取了但没写入 DB（`_flush_remaining` 有但主写入路径漏了）

**后端 API 增强**：
- [x] 返回 `video_url`/`voice_url`/`card_title`/`files` 结构化字段
- [x] 新增 `_extract_card_title()` 从 JSON 卡片数据提取标题

**仪表盘前端渲染**：
- [x] 视频：`<video>` 标签（封面+点击播放/暂停），URL 过期降级显示
- [x] 语音：`<audio>` 播放器（▶️ 按钮点击播放/暂停），过期降级
- [x] 文件：类型图标（28 种扩展名）+ 文件名 + 大小格式化
- [x] 卡片：展示标题（截取前 30 字，hover 完整）
- [x] 转发：📨 badge
- [x] 消息搜索页面同步更新特殊消息渲染

**media_summary 多模态增强**：
- [x] 转发内嵌视频纳入并发分析管道（下载→Gemini分析→清理临时文件）
- [x] `_analyze_media_files` 支持 video 类型（MIME 自动检测+大小分级+120s 超时）
- [x] `_download_media_to_sandbox` Content-Type 增加视频格式推断
- [x] 概括模式分析完毕后自动清理临时下载的媒体文件

---

## 问题 11：Memory 记忆系统无法真正写入 ✅ 已解决

### 现象
参考图二：Memory 页面显示 0 条记忆，模型声称"写入了记忆"但实际 DB 为空。

### 代码定位

**Memory DB 验证**：
```
Memory/memory.db → memories 表：0 行
```

**`MemoryStore.write()`** (`memory.py` L324-391)：
- 代码逻辑本身是正确的：生成 ID → 去重检测 → INSERT → commit
- 调用链：`tool_memory_write` (L1497) → `self._memory.write()`

**`MemoryStore.__init__`** (`memory.py` L246)：
```python
def __init__(self, api_key: str = "", flash_lite_model: str = ""):
```

**插件初始化** (`main.py` L157)：
```python
self._memory = MemoryStore()  # ← 无参调用！api_key 和 model 都为空
```

### 根因分析
1. `MemoryStore()` 无参初始化 → `api_key=""` → autoSummary 不会生成（这不影响写入）
2. 写入本身应该能工作（不依赖 api_key）
3. **但 DB 0 行意味着 `write()` 从未被成功调用**

**可能原因**：
- 模型说"写入了记忆"但工具调用实际上失败了（异常被 try-except 吃掉了）
- `tool_memory_write` 的异常返回 `f"错误: {e}"` (L1524)——但模型可能不会展示这个错误
- 需要开调试看工具调用的实际返回值
- `aiosqlite` 依赖可能未安装

### 需要做什么
- [x] 检查 `aiosqlite` 是否已安装在 AstrBot 的 venv 中 → 已安装
- [x] 手动调用 `tool_memory_write` 测试实际返回值 → ✅ 写入成功 `mem_1775584637566_8d8924`
- [x] 添加更详细的日志到 `tool_memory_write` 中 → Tool Result 日志已有
- [x] 检查 AstrBot 日志中是否有 "Memory 写入" 或 "错误" 相关日志 → 已确认正常

> **根因修复**：`tags` 参数传入纯字符串（如 `'用户信息'`）时 `json.loads()` 失败导致写入异常。已增加 fallback 容错（JSON 解析失败 → 逗号分割降级），与 `memory_update` 一致。
>
> **实测验证 (2026-04-08 01:57)**：`memory_write(tags='用户信息')` → `✅ 记忆已写入: mem_1775584637566_8d8924`，`memory_query/read` 均正常返回数据。

---

## 问题 12：BossLady Console UI 移除 Emoji ✅ 已解决

### 现象
参考图三图四，侧边栏/标题栏的头像 Emoji 👩‍💼 需要移除或替换为纯文字/图片。

### 代码定位

**`index.html`**：
- L11: favicon — 使用 avatar.webp
- L19: 侧边栏 logo — `<img>` 圆形裁剪

### 需要做什么
- [x] L19: 替换为公孙离头像图片（`avatar.webp`），圆形裁剪 `border-radius:50%`
- [x] L11: favicon 改为 `avatar.webp`
- [x] 使用老板娘的自定义头像图片（离恨烟_公孙离_1.webp）


---

## 问题 13：AstrBot 原生平台 Prompt/机制/插件对系统的干扰审查 ✅ 已解决

### 现象
1. **标点符号/分段约束执行不严格**：尽管 FlashLite 注入的 Prompt 已明确要求使用空格替代中文全角标点(。！？，)并控制分句长度，但主模型输出仍频繁违反，疑似被 AstrBot 原生平台层的系统设定覆盖或冲突
2. **唐突参与对话**：老板娘在不相关的群聊中主动发言，初步判断可能是其他插件（如 persistence、letai_sendemojis 等）的触发机制导致，而非 FlashLite 本身的问题
3. **角色设定冲突**：主模型始终记得"老板娘"身份，但在标点/格式/工具使用等细节上不遵循 FlashLite 注入的约束，说明 AstrBot 原生的 persona 设定可能与我们注入的 system prompt 产生优先级冲突

### 已解决内容

#### A. AstrBot 原生平台 Prompt 机制
- [x] 查看 AstrBot 控制面板中配置的原生 system prompt / persona 内容
- [x] 确认 FlashLite 的 `on_llm_request` 注入是追加还是覆盖 AstrBot 原生设定
- [x] 确认 system prompt 的注入顺序和优先级（谁先谁后 是否会被截断）
- [x] 检查 AstrBot 的 `astr_main_agent` 或 `tool_loop_agent_runner` 是否有内置的格式化/后处理逻辑

#### B. 现有插件的干扰排查
- [x] 列出当前所有已加载插件及其 hook 类型（on_llm_request / on_llm_response 等）
- [x] 检查 `astrbot_plugin_persistence` 是否会注入额外的 system prompt
- [x] 检查 `astrbot_plugin_letai_sendemojis` 的触发逻辑——是否会影响消息格式或主动发言
- [x] 检查是否有插件在 `on_llm_response` 阶段修改了输出内容（如添加标点、格式化等）

#### C. Prompt 冲突解决方案
- [x] 根据审查结果 确定 FlashLite 注入的约束是否被其他层覆盖
- [x] 如果存在冲突 制定优先级策略（禁用冲突的原生设定 或调整注入方式）
- [x] 考虑将关键约束（标点/分段/格式）从 system prompt 层提升到 on_llm_response 后处理层强制执行
