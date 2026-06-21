# Plan_1 缺漏-2：Flash Lite 引擎 + 核心工具系统深度审计

> 审计时间：2026-04-02（初版）→ 2026-04-02T19:50 补全版  
> 审计范围：`astrbot_plugin_flashlite/` 全部 7 模块 (3135 行) + `Sandbox/` + `BossLady_Console/backend/`  
> 对照文件：`初始讨论记录副本.md` + `Plan_1.md` + `Plan_1_sandbox.md` + `Plan_1_models.md`

---

## 一、总体发现

Flash Lite 三级模型引擎代码 **已全部写好**——7 个 Python 模块共 3135 行：

| 模块 | 行数 | 核心类 | 实现状态 |
|------|------|--------|---------| 
| `main.py` | 945 | `FlashLiteEngine` | ✅ 消息路由 + 9个 @filter.llm_tool 注册 |
| `checkpoint.py` | 355 | `CheckpointManager` | ✅ token 估算 + 压缩 + DB |
| `knowledge.py` | 254 | `KnowledgeCache` | ✅ JSON 持久化 + 窗口 CRUD |
| `kv_cache.py` | 312 | `KVCacheManager` | ✅ cachedContent API + TTL |
| `memory.py` | 309 | `MemoryStore` | ✅ SQLite CRUD + 模糊搜索 |
| `agent.py` | 550 | `AgentRequestBuilder` | ✅ 20 个工具定义（已补全） |
| `sandbox.py` | 411 | `SandboxManager` + `SandboxSecurity` | ✅ 路径安全 + 4 个底层操作 |

---

## 二、已修复的缺漏（本轮完成）

| 编号 | 内容 | 状态 |
|:---:|------|:---:|
| 2A | 插件加载确认（AstrBot 自动扫描 data/plugins/） | ✅ |
| 2B | agent.py TOOL_DEFINITIONS 13→20 | ✅ |
| 2C | env.json tool_count 14→17 | ✅ |
| 2D | main.py 9个 @filter.llm_tool 工具注册 | ✅ |
| 2E | data.py Knowledge 路径修复 | ✅ |
| 2F | limits.json 字段名对齐 sandbox.py | ✅ |

---

## 三、工具执行后端缺失清单（sandbox.py 仅实现 4 个底层操作）

sandbox.py 当前只有 `view_file`、`modify_file`、`list_files`、`exec_code` 四个函数。  
以下工具虽然有 base_tools JSON 注册 + agent.py 定义，但 **没有执行后端**：

---

### 3.1 `QQ_data_original` — 原始消息查询

**设计意图**（初始讨论 L137, L159, L179, L225, L247）：

> 类似你的 conversation_read_original，模型看到一个用户回复了被 CHECKPOINT 压缩的古老内容时，需要查原文。是整个记忆系统能"回忆"的根本基础。

**实现要求**：
- 查询 `QQ_data/messages.db` 的 `qq_messages` 表
- 支持按群号/QQ号、时间范围、关键词搜索
- 返回原始消息（含 content_text, content_raw, sender_name, extra_data）
- 支持分页（避免大量返回）
- 这是模型"翻书看原内容"的能力，是 CHECKPOINT 压缩后能定位原文的关键

**权限**：只读，无限制

---

### 3.2 `web_search` — 网络搜索（工具模型驱动）

**设计意图**（初始讨论 L88, L164-165）：

> web_search 主要是调用工具模型进行大量搜索，反馈回它总结的内容

**实现要求**：
- 调用搜索 API（Tavily / Google / SearXNG）获取搜索结果
- **调用工具模型**（Gemini 3 Flash）对搜索结果进行 chunk 概括总结
- 返回结构化的摘要结果，而非原始搜索 JSON
- 和我们当前 IDE 里的 web_search 工作方式一致：搜索→分块→模型总结→返回

**权限**：需要网络出站权限（limits.json `network.allow_outbound = true`）

---

### 3.3 `web_fetch` — 网页抓取套件

**设计意图**（初始讨论 L164-165）：

> 一整套 MCP 工具，就和我们当前 MCP 差不多

**实现要求**：
- 参考 `C:\Users\<user>\.gemini\antigravity\mcp-web-fetcher` 的设计
- 核心能力：URL 抓取→正文提取→Markdown 转换
- 支持截图、HTML 获取、链接提取
- 可选：调用工具模型对长页面进行 ai_summary

**权限**：需要网络出站权限

---

### 3.4 `generate_image` — 图片生成

**设计意图**（初始讨论 L159）：

> 调用生成图模型 generate_image

**实现要求**：
- 调用 Gemini 的图片生成 API（具有 `image` 能力的模型）
- **需要在控制台模型配置页新增"生图模型"卡片**：
  - 模型选择下拉列表只显示具有 `imageGeneration` 能力的模型
  - 保存选中的生图模型名到配置
- 请求参数：prompt + 尺寸 + 风格等
- 返回生成的图片（base64 或保存到 Sandbox 后返回路径）

**权限**：需要 API 调用权限

> [!IMPORTANT]
> 需要在 BossLady_Console 的模型配置页面新增第四个模型卡片"🎨 生图模型"

---

### 3.5 `upload_data`（原 `import_data`）— 从 Sandbox 发送文件

**设计意图**（初始讨论 L167）：

> Agent 可以 import/save data 保存自己想要的内容到本地，可以勾选文件发送

**实现要求**（用户纠正：方向是从 Sandbox 往外发）：
- 从 Sandbox/workspace/ 内取文件
- 通过 QQ 消息发送给指定群聊/个人
- 支持文件类型限制（不发 .exe 等危险文件）
- 返回发送结果

**权限**：Sandbox 内读取 + QQ 消息发送权限

> [!NOTE]
> 建议重命名为 `upload_data`，语义更清晰——"从 Sandbox 上传/发送"

---

### 3.6 `save_data` — 保存数据到 Sandbox

**设计意图**：`upload_data` 的反向操作

**实现要求**：
- 接收结构化数据（JSON/CSV/文本/Markdown）
- 写入 Sandbox/workspace/ 指定路径
- 格式化保存（自动缩进 JSON、CSV header 等）
- 底层调用 `SandboxManager.modify_file()`

**权限**：Sandbox workspace 写权限（不可写 base_tools/config/）

---

### 3.7 `system_report` — Sandbox 自检维护报告

**设计意图**（初始讨论 L169，**核心设计**）：

> 工具模型本身会定期 launch 一次 Sandbox 内 Review，检查，整理 Sandbox 内部状态并写维护报告到固定区域。这个维护报告在基础工具文件夹内部的 system_report 文件夹里，**正常下模型不可操作，只可读，只有定期 launch 这种 review 的时候 system_report 接口才对主持的工具模型开放，允许写入新日志**。这种定期主要是安全检查，内部文件整理。

**实现要求**：
- 写入路径：`Sandbox/base_tools/system_report/`
- **权限切换机制**：
  - 默认状态：`system_report/` 对模型**只读**
  - Review 模式：由系统定期 launch 触发，临时开放写入权限
  - Review 结束：恢复只读
- 报告内容：workspace 文件统计、磁盘用量、异常文件检测、工具使用统计
- 日志格式：`report_YYYYMMDD_HHMMSS.md`

**权限**：特殊——默认只读，Review 模式时临时开放写入

---

### 3.8 `media_summary`（合并 `forward_summary` + `video_summary`）— 多媒体消息摘要

**设计意图**（初始讨论 L159, L313）：

> 进行某个群聊中转发记录的 summary。转发内容也有 video，做成一个综合工具更好。

**实现要求**：
- 统一处理多种特殊消息类型：
  - **转发消息**：解析合并转发 JSON → 提取各条消息文本/图片 → 调用工具模型生成摘要
  - **视频消息**：提取视频元信息（时长、分辨率、文件名）→ 如有封面图则描述
  - **文件消息**：提取文件名、大小、类型
  - **语音消息**：如有转文字则返回，否则标注"语音消息未转写"
- 调用工具模型（Flash）进行内容总结
- 返回结构化的摘要文本

**权限**：需要调用工具模型

> [!TIP]
> 建议将 `forward_summary.tool.json` 和 `video_summary.tool.json` 合并为 `media_summary.tool.json`

---

### 3.9 `task_set` — 任务进程管理（核心中的核心）

**设计意图**（初始讨论 L157，**工具系统核心**）：

> 主模型设置 task 进程：主模型主动创立一个 task，里面有为什么创立这个 task，相关标记信息源头指针，task 内容。主模型会设置命令形式的 task 内容，包括先并行创立多个工具调用/工具模型任务调用，自己什么时候被苏醒 call check，等于设置一个 task 列表调用工具模型设置子代理简单任务和工具调用的集合，这是复杂任务的集合体。

**实现要求**：
- **create**：创建任务
  - 任务描述（why + what）
  - 源头指针（引用来源的地址/消息ID）
  - 步骤列表（可并行的工具调用 + 工具模型子代理任务）
  - 唤醒条件（什么时候 call 主模型 check）
- **check**：检查任务状态
  - 返回各步骤完成情况
  - 未完成步骤的当前进度
- **kill**：终止任务
  - 清理关联的子进程和工具模型调用
- 底层用 `asyncio.Task` 管理并发
- 任务状态持久化到 `Sandbox/workspace/tasks/`

**权限**：完整 Sandbox 操作权限

---

### 3.10 `browser_agent` — 有头浏览器子代理

**设计意图**（初始讨论 L164）：

> 有头浏览器子代理插件

**当前状态**：暂不实现，需要 Playwright/Puppeteer 等重依赖

**思路**：
- 启动无头 Chromium 实例
- 接收任务描述 → 自动操作（导航、点击、截图、提取）
- 超时管控 + 沙盒隔离
- 可参考 AstrBot 生态是否有现成的浏览器插件

---

## 四、操作权限模型（对照初始讨论 L155-169, L366）

### 4.1 Sandbox 空间分区

```
Sandbox/
├── base_tools/          # 🔒 只读（系统级保护）
│   ├── *.tool.json      # 基础工具注册
│   └── system_report/   # 🔒 默认只读，Review 时临时开放
├── config/              # 🔒 只读（系统配置）
│   ├── env.json
│   └── limits.json
├── runtimes/            # 🔒 只读（Python/Node/GCC 运行时）
├── workspace/           # ✅ 可读写（模型工作空间）
│   ├── drafts/          # 模型草稿纸（类似 implementation_plan）
│   ├── tasks/           # task_set 任务持久化
│   ├── custom_tools/    # 模型自己创建的工具！
│   └── (自由目录)       # 模型可自由创建子目录
└── (根级)               # ❌ 不可创建新根级目录
```

### 4.2 权限矩阵

| 操作 | base_tools/ | config/ | system_report/ | workspace/ | Sandbox 外部 |
|------|:-----------:|:-------:|:--------------:|:----------:|:------------:|
| 读取 | ✅ | ✅ | ✅ | ✅ | ❌ 绝对禁止 |
| 写入 | ❌ | ❌ | 🔶 仅 Review 时 | ✅ | ❌ 绝对禁止 |
| 删除 | ❌ | ❌ | ❌ | ❌（也不允许删除） | ❌ 绝对禁止 |
| 重命名 | ❌ | ❌ | ❌ | ❌ | ❌ 绝对禁止 |
| 执行 | — | — | — | ✅（sandbox_exec） | ❌ 绝对禁止 |

> [!CAUTION]
> **Sandbox 外部绝对不可让 AI 有任何触碰权限**——这是系统级声明（初始讨论 L366）  
> sandbox.py 的 SandboxSecurity.validate_path() 已实现路径逃逸防护

### 4.3 渐进式工具披露（初始讨论 L175）

> 虽然工具都在 Sandbox 两个文件夹里，但采用渐进式披露提高效率

agent.py 中 TOOL_DEFINITIONS 的 `brief` 字段就是实现这个设计：
- 首次请求只发送 brief（一行描述）
- 模型决定使用时才发送 full（完整 schema）

---

## 五、优先级排序（更新后）

| 优先级 | 工具 | 工作量 | 核心依赖 |
|:------:|------|:------:|---------|
| P0 | `QQ_data_original` | 中 | messages.db 已存在 |
| P0 | `task_set` | 大 | 工具系统核心 |
| P0 | `media_summary` | 中 | 需要工具模型调用 |
| P1 | `web_search` | 中 | 需要搜索 API + 工具模型 |
| P1 | `web_fetch` | 中 | 可参考 mcp-web-fetcher |
| P1 | `system_report` | 中 | 需要权限切换机制 |
| P1 | `save_data` | 小 | 底层已有 modify_file |
| P1 | `upload_data` | 中 | 需要 QQ 消息发送接口 |
| P2 | `generate_image` | 中 | 需要控制台新增模型卡片 |
| P3 | `browser_agent` | 大 | 留后面 |

---

## 六、控制台新增需求

### 6.1 生图模型卡片

模型配置页面需要新增第四个卡片"🎨 生图模型"：
- 模型下拉列表只显示具有 `imageGeneration` 能力的模型
- 保存配置到 `config.json`
- generate_image 工具读取此配置决定用什么模型

### 6.2 media_summary 合并

前端 Sandbox 工具列表需要反映合并后的工具名。

---

## 七、Memory 系统差距分析（对照 mcp-memory-store）

> 对照源码：`C:\Users\<user>\.gemini\antigravity\mcp-memory-store\src\` (store.ts 609行 + search.ts 432行 = 1041行)  
> 我们的实现：`memory.py` (310行)

### 7.1 能力对比矩阵

| 能力 | mcp-memory-store | 我们的 memory.py | 差距 |
|------|:----------------:|:----------------:|:----:|
| CRUD (write/read/update/delete) | ✅ | ✅ | — |
| 工作区隔离 | ✅ SHA256 hash 映射 | ✅ workspace 字段 | ≈ |
| 搜索：模糊匹配 | ✅ Fuse.js 多维度评分 | ⚠️ SQL LIKE | 🔴 大差距 |
| 搜索：CJK 优化 | ✅ 子串 + 前缀匹配 | ❌ | 🔴 |
| 搜索：多词联合 | ✅ 覆盖率 70% + 质量 30% | ❌ 单关键词 LIKE | 🔴 |
| 全文 grep 搜索 | ✅ 正文搜索跳过 frontmatter | ❌ | 🟡 |
| 去重检测 | ✅ Fuse.js 相似度 + 子串 | ❌ | 🟡 |
| 原子写入 | ✅ tmp + rename | ❌ 直接写 | 🟡 |
| 并发锁 | ✅ 进程内索引锁 | ❌ | 🟡 |
| LRU 缓存 | ✅ 索引缓存 | ❌ | 🟢 |
| autoSummary | ✅ Flash 自动生成 | ❌ | 🟡 |
| pinned 置顶 | ✅ | ✅ | — |
| source_pointer | ❌ | ✅ | 我们更好 |
| 批量操作 | ✅ batch | ❌ | 🟡 |
| 归档机制 | ✅ archive/unarchive | ❌ | 🟢 |
| 导入导出 | ✅ export/import zip | ❌ | 🟢 |
| 统计 stats | ✅ 详细 | ✅ 基础 | 🟢 |

### 7.2 必须修复的核心差距（P0）

**搜索引擎**是最大的差距——SQL LIKE 在中文环境几乎不可用：
- `LIKE '%老板娘%'` 可以，但 `LIKE '%老板%'` 也会匹配"老板娘"，无法精确控制
- 多关键词搜索 "群聊 老板娘 生日" 只能 AND 三个 LIKE，效率极差
- 无法做模糊匹配：打错字就搜不到

**建议方案**：引入 Python 版 Fuse.js 等价物（如 `thefuzz` 或 `rapidfuzz`），或用 SQLite FTS5 全文索引

### 7.3 应该实现的增强（P1）

| 增强 | 实现方案 |
|------|---------|
| 原子写入 | `aiosqlite` 本身有 WAL 模式（已开） |
| 并发锁 | `asyncio.Lock` per workspace |
| autoSummary | 写入时调用 Flash Lite 自动生成搜索摘要 |
| 去重检测 | write 时查询相似记忆，返回提醒 |
| 批量操作 | 新增 `batch()` 方法 |

---

## 八、media_summary 分片合并机制设计

### 8.1 问题分析

QQ 群聊中的复杂内容可能触发以下超限场景：

| 场景 | 具体问题 | 频率 |
|------|---------|:----:|
| 多层转发嵌套 | 转发中套转发，3-5 层嵌套，展开后上万字 | 中 |
| 多图轰炸 | 单条转发含 20-50 张图片 | 高 |
| 长视频 | 视频直接喂给模型会超限 | 中 |
| 混合类型 | 转发中同时含图片+视频+文件+语音 | 中 |
| 超大文件 | 视频/文件超出 API 处理上限 | 低 |

### 8.2 分片策略

```
输入消息
    │
    ▼
┌─────────────────────┐
│  Step 1: 类型检测    │  识别消息类型（转发/视频/图片/文件/语音/JSON）
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  Step 2: 深度展开    │  递归展开嵌套转发（最大深度 MAX_FORWARD_DEPTH=5）
│  + 尺寸预估          │  估算总 token 数、图片数量、视频时长
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  Step 3: 分片决策    │  根据预估决定处理策略
└─────────┬───────────┘
          │
    ┌─────┼─────┐
    ▼     ▼     ▼
  小型   中型   大型
```

### 8.3 分级处理规则

| 级别 | 判定条件 | 处理策略 |
|:----:|---------|---------|
| **小型** | 文本 ≤ 2000 token + 图片 ≤ 3 + 无视频 | 直接整体喂给工具模型一次总结 |
| **中型** | 文本 ≤ 8000 token + 图片 ≤ 10 + 视频 ≤ 1 | 分 chunk 处理：文本按 2000 token 切片，图片按 3 张一组描述，视频降 resolution 直喂 |
| **大型** | 超出中型任意条件 | 多轮分片：文本多轮+合并，图片采样，视频降帧+降 resolution |

### 8.4 各类型处理详情

#### 图片处理
```
图片数量 ≤ 3    →  直接发送给工具模型描述
图片数量 4-10   →  每 3 张一组，分批描述，合并结果
图片数量 > 10   →  采样策略：首 2 张 + 尾 1 张 + 均匀抽样 2 张 = 最多 5 张
                    其余标注"(另有 N 张图片未详细描述)"
```

#### 视频处理（使用 Gemini mediaResolution 参数）

> [!IMPORTANT]
> Gemini 原生支持 `mediaResolution` 参数控制视频处理精度，应**直接喂视频给模型而非仅取元信息**

```
Gemini API 视频参数：
  generationConfig.mediaResolution: "MEDIA_RESOLUTION_LOW"  // 降低 token 消耗
  videoMetadata.fps: 0.5  // 降低采样帧率（默认 1fps，改为每2秒1帧）

处理策略：
  视频 < 20MB   →  直接发送 + mediaResolution=LOW + fps=0.5
  视频 20-50MB  →  mediaResolution=LOW + fps=0.25（每4秒1帧）
  视频 > 50MB   →  仅封面+元信息 + "[大视频，可用 QQ_data_original 获取]"
```

**REST API 字段名**（参考 Suggestion_Kaleidoscope_1.md）：
```json
{
  "generationConfig": {
    "mediaResolution": "MEDIA_RESOLUTION_LOW"
  },
  "contents": [{
    "parts": [{
      "inlineData": { "mimeType": "video/mp4", "data": "<base64>" },
      "videoMetadata": { "fps": 0.5 }
    }]
  }]
}
```

可用 resolution 值：`LOW` → `MEDIUM` → `HIGH` → `ULTRA_HIGH`（per-part Gemini3+）

#### 嵌套转发处理
```
深度 1         →  正常展开所有内容
深度 2-3       →  每层递归，内层的媒体用上述策略处理
深度 4-5       →  内层转发只提取文本摘要，不展开媒体
深度 > 5       →  截断，标注"[嵌套层级过深，已截断]"
```

### 8.5 分片合并流水线（核心改进）

> [!IMPORTANT]
> 所有分组结果必须经过工具模型合理拼接成一份摘要，原始分组内容保存在 Sandbox 并有指针链接

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  分片 1 结果  │     │  分片 2 结果  │     │  分片 N 结果  │
│  (chunk_1)   │     │  (chunk_2)   │     │  (chunk_N)   │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       │  ── 并发执行，受 MAX_CONCURRENT 限制 ──  │
       │                    │                    │
       ▼                    ▼                    ▼
┌──────────────────────────────────────────────────────────┐
│  各分片结果保存到 Sandbox:                                │
│  workspace/media_summary/{task_id}/chunk_1.md            │
│  workspace/media_summary/{task_id}/chunk_2.md            │
│  workspace/media_summary/{task_id}/chunk_N.md            │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│  工具模型 合并调用：                                      │
│  读取所有 chunk_*.md → 拼接为一份完整摘要                 │
│  输出 → workspace/media_summary/{task_id}/summary.md     │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
                    返回给主模型的内容：
                    summary.md 的内容
                    + 指针: "详细分片内容见 Sandbox:workspace/media_summary/{task_id}/"
```

### 8.6 并发控制与 API KEY 池

> [!TIP]
> 分片处理的并发性能取决于工具模型的并发上限，建议在控制台支持多 API KEY 轮询

| 配置项 | 说明 |
|--------|------|
| `MAX_CONCURRENT_TOOL_CALLS` | 工具模型最大并行调用数（建议 3-5） |
| `TOOL_MODEL_API_KEYS[]` | API KEY 池，多个 KEY 轮询避免单 KEY 限流 |
| `TOOL_MODEL_RPM_LIMIT` | 单 KEY 每分钟请求上限 |

**控制台需求**：工具模型面板新增"并发 API KEY"配置区：
- 支持添加多个 API KEY
- 显示每个 KEY 的使用状态/配额
- 自动轮询/负载均衡

### 8.7 合并输出格式

```markdown
## 📦 转发消息摘要 (共 N 条)

### 概要
XXX 发了一段关于 YYY 的讨论，共 N 条消息，含 M 张图片、K 个视频。

### 内容
1. [张三 12:30] 文本内容...
2. [李四 12:31] [图片: 一只猫坐在桌上]
3. [王五 12:32] [视频: 15秒, 720p - 内容描述]
...

### 图片详情 (共 M 张，已展示 P 张)
- 图1: 描述...
- 图3: 描述...
- (另有 N-P 张图片未详细描述)

### 嵌套转发 (第2层)
- [内层转发摘要]: XXX

---
> 📁 完整分片内容: `Sandbox:workspace/media_summary/{task_id}/`
```

### 8.8 安全参数

| 参数 | 默认值 | 说明 |
|------|:------:|------|
| `MAX_FORWARD_DEPTH` | 5 | 最大展开嵌套层数 |
| `MAX_IMAGES_PER_CALL` | 3 | 单次工具模型调用最多处理图片数 |
| `MAX_TOTAL_IMAGES` | 50 | 单条消息最大处理图片总数 |
| `MAX_TEXT_TOKENS` | 8000 | 单次文本 chunk 上限 |
| `MAX_VIDEO_SIZE_MB` | 50 | 视频直接处理上限（降 resolution 后可更大） |
| `SAMPLE_IMAGES_COUNT` | 5 | 大量图片时的采样数 |
| `CHUNK_SIZE_TOKENS` | 2000 | 文本分片大小 |
| `MAX_CONCURRENT_TOOL_CALLS` | 3 | 分片并发上限 |
| `DEFAULT_VIDEO_FPS` | 0.5 | 视频默认采样帧率 |
| `DEFAULT_MEDIA_RESOLUTION` | `LOW` | 视频默认分辨率级别 |


