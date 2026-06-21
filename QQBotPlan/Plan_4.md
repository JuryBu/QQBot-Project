# 🔮 老板娘 QQ 机器人 —— 未来优化路线图 (Plan_4)

> 版本: v3.0（2026-04-14 架构决策+提示词工程完成版）  
> 状态: ✅ 全部方案闭环 / 待启动开发  
> 前置: Plan_1（Stage 1-9 已完成）、Plan_2（面板+持久化）、Plan_3（性能+成本优化）  

### 📂 附属文档索引
| 文档 | 用途 |
|------|------|
| `Plan_4_Feature1_讨论问题.md` | Feature 1 的 15+1 道讨论问题清单 |
| `Plan_4_Feature1_讨论回答记录.md` | Feature 1 所有问题的决策记录 + Q6 话题切分方案 |
| `Plan_4_Feature4_图像搜索调研.md` | Feature 4 图像搜索 API/插件全景调研报告 |
| `Plan_4系列讨论原始记录.md` | 所有讨论的原始对话记录 |
| `Plan_4_提示词工程变动清单.md` | 各 Feature 对提示词注入的具体改动点（待创建）|

---

## 定位说明

本文件记录**短期收工后的中长期优化方向和待讨论内容**。
每个 Feature 都是独立的增量功能，可按优先级灵活排期。
与 Plan_1 Stage 10-12 中未实现的内容做了整合。

---

## Feature 1：向量化数据库 — 混合检索增强

### 背景
Plan_3 讨论中曾分析过引入向量数据库的可能，当时否决了「先向量库筛一遍再给 FlashLite」的方案，
因为这种管道式过滤等于让缺乏语义理解的向量检索硬卡了一道门，可能丢失关键记忆。

### 新方案：混合推荐（并行而非串行）

核心思路是**两路并行推荐，FlashLite 做最终融合**：

```
┌─────────────────┐     ┌───────────────────┐
│  向量检索引擎     │     │  FlashLite 语义筛选  │
│  相似度 Top-K    │     │  现有推荐机制        │
└────────┬────────┘     └─────────┬─────────┘
         │                       │
         └───────┬───────────────┘
                 ▼
         FlashLite 融合去重
         输出最终 Memory 注入
```

- **向量侧**：纯相似度匹配，补充 FlashLite 可能遗漏的"不那么显眼但语义相关"的记忆条目
- **FlashLite 侧**：维持现有的语义判断机制不变（已有膨胀控制）
- **融合层**：FlashLite 对两批推荐结果做最终筛选和去重

### 应用场景 2：QQ 数据海量搜索升级

- `QQ_data_original` 目前只支持精确搜索（grep 式）
- 向量化后可支持**模糊语义搜索**（如"之前聊过 AI 画图"能匹配到具体对话）
- 适合对超大历史记录做回忆式检索
- 可作为 Sandbox 的 `search` 工具的升级后端

### 应用场景 3：主模型「联想」机制（💭 需深入讨论）

除了辅助 Memory 检索和 QQ 数据搜索，向量库还有一个潜在的高价值应用：
每次主模型请求时，**自动注入一定量的向量检索 Top-K 高相关历史对话片段**，
形成类似"联想"的效果——让主模型天然带有与当前话题相关的历史上下文。

```
用户消息 "今天考完试了好累"
         ↓ embedding
向量库搜索 → Top-3 相关历史片段：
  1. [2周前] "用户说期末考试要来了好紧张"
  2. [1个月前] "用户和群友讨论复习方法"  
  3. [3天前] "用户说在图书馆熬夜复习"
         ↓ 注入到主模型 prompt/context
主模型回复（自然地引用历史上下文）: 
  "考完啦？之前不是说紧张得要命吗..."
```

**核心价值**：让老板娘不需要 FlashLite 刻意标记，也能"自然回忆"起相关的过往对话。
**关键问题**：怎么注入、注入多少、会不会干扰主模型理解、成本影响——需要详细讨论。

> 📋 详细讨论问题见 `Plan_4_Feature1_讨论问题.md`

### ✅ 技术选型（已确定）

| 项目 | 决策 | 备注 |
|------|------|------|
| 向量模型 | **Gemini text-embedding-004** | m3e-base 作 fallback（需重新下载）|
| 向量维度 | **768** | Gemini 默认 |
| 存储引擎 | **ChromaDB** | 持久化 + metadata 过滤（按群/用户/时间）|
| Memory 切分 | **逐条向量化** | 单条即单主题，不需 tag |
| QQ 原文切分 | **话题摘要** | FlashLite 触发时顺便输出群话题摘要，向量化摘要而非原文 |
| 索引策略 | **增量实时** | 参考 FlashLite 计数+时间窗口采样减压 |
| 融合策略 | **互补模式** | 向量只推 FlashLite 没推的，默认 5 条 |
| 联想注入位置 | **user 消息上方** | 走 conversation 通道不影响 KV Cache |
| 联想注入量 | **默认 1-2 条** | 面板可调 |
| 联想触发 | **每次搜索 + 阈值判断** | 阈值面板可调 |
| 多模态 | ✅ 支持 | 图片数据也建向量索引 |

### 灰度策略

- 在 BossLady 面板增加开关：`向量检索增强: 开/关`
- 可配参数（**全部专家模式**）：
  - 模型选择 / Top-K 数量 / 相似度阈值
  - 覆盖范围（Memory / QQ_data / 两者）
  - 联想注入条数、触发阈值
- 关闭时完全走现有路径，零侵入

### 待实现
- [ ] ChromaDB 环境搭建 + Memory 向量化入库
- [ ] FlashLite `recent_topics_summary` 输出字段设计
- [ ] 向量检索 → 联想注入 pipeline 实现
- [ ] BossLady 面板配置项 UI

---

## Feature 2：Sandbox 工具生态扩展 — MCP/SKILL 导入体系

### 背景
目前 Sandbox 已有 22 个基础工具（browser_agent, generate_image, grep, search 等），
支撑了老板娘的日常能力。但缺少**用户自定义工具的导入管理**能力。

### 目标
支持 MCP Server 和 SKILL 技能包的导入、管理和卸载，让老板娘的能力可以灵活扩展。

### ✅ 方案确定

**实现思路：在 BossLady Sandbox 页面增加两个管理卡片区块**

#### 目录结构（已确定）

```
Sandbox/
├── base_tools/          # 只读内建 (22个 .tool.json) — 不可修改
├── extensions/          # 🆕 扩展工具区（面板管理导入，模型只读）
│   ├── mcp_servers/     # MCP Server 配置 + 进程管理
│   ├── skills/          # SKILL 技能包
│   └── group_admin/     # 群管理工具集 (F3)
├── config/              # env.json, limits.json
└── workspace/           # 可写自定义
    └── custom_tools/    # AI 自建工具
```

> 权限策略：`base_tools/` 和 `extensions/` 均**只读**，模型只能使用不能修改。
> `workspace/custom_tools/` 可写，模型可自行创建工具。

#### MCP Server 管理卡片
- JSON 配置编辑器（支持 MCP 协议标准格式，参考 Antigravity `mcp_config.json`）
- 启用/禁用开关 + 进程状态指示灯
- 进程生命周期管理：启动/停止/重启
- 参考实现：`C:\Users\<user>\.gemini\antigravity\mcp_config.json`

#### SKILL 技能包管理卡片
- 文件夹导入（ZIP/路径/Git URL）
- SKILL.md 预览 + 启用/禁用开关
- 纯文件无进程，仅需读取和注入

#### 渐进式披露扩展（三层架构）

| 层级 | 内容 | Token 成本 |
|------|------|------------|
| **L0 概览** | 4大类数量 — "22个基础工具 / N个MCP / M个SKILL / K个自定义" | ~50 token |
| **L1 分类列表** | 按 category 列出名 + 一行描述（现有 brief 模式）| ~500-800 token |
| **L2 完整参数** | 具体工具的全部参数 + 用法示例（现有 full 模式）| 按需展开 |

ToolRegistry 扫描目录扩展为：
```python
SCAN_DIRS = ["base_tools", "extensions", "workspace"]
```

#### 🔌 首批导入计划：反重力 SKILL 适配

确定从 Antigravity Skills 中适配导入以下高价值技能包（独立部署，不共用数据）：

| SKILL | 来源 | 改造要点 | 价值 |
|-------|------|---------|------|
| **pptx** | `~/.gemini/antigravity/skills/pptx/` | 输出路径→Sandbox→发QQ | 老板娘能做PPT |
| **docx** | `~/.gemini/antigravity/skills/docx/` | 同上 | 老板娘能写Word文档 |
| **xlsx** | `~/.gemini/antigravity/skills/xlsx/` | 同上 | 老板娘能处理表格 |
| **pdf** | `~/.gemini/antigravity/skills/pdf/` | 同上 | 老板娘能处理PDF |
| **canvas-design** | `~/.gemini/antigravity/skills/canvas-design/` | 输出→upload_data发QQ | 海报/设计能力 |
| **algorithmic-art** | `~/.gemini/antigravity/skills/algorithmic-art/` | 同上 | 生成艺术趣味 |
| **theme-factory** | `~/.gemini/antigravity/skills/theme-factory/` | 适配主题系统 | 主题美化 |

首批导入 MCP：

| MCP | 说明 | 改造量 |
|-----|------|--------|
| **sequential-thinking** | 深度推理链 | 无需改造，npx 直接用 |

> 不导入的：memory-store（老板娘已有 Memory 系统）、sandbox（已有）、web-fetcher（已有类似 web_fetch 工具，按需增强）

### 提示词变动（⭐⭐⭐ 最多）

需要修改 `main.py` 的以下 inject_parts Section：

| Section | 变动内容 |
|---------|----------|
| **0 体系认知** | `你身边的协作系统` 增加 MCP/SKILL 说明 |
| **4 工具集(brief)** | ToolRegistry.get_brief() 扩展支持 MCP/SKILL 分类 |
| **8 Sandbox 工作空间** | 增加 `extensions/` 目录说明 + 只读策略 |
| **9 自定义工具** | 区分 custom_tools(可写) vs extensions(只读) |
| **11 工具速查** | 新增 MCP/SKILL 分类行 |
| **KV Cache 固定区** | 工具模型也需感知 MCP/SKILL |

### 待实现
- [ ] 前端：Sandbox 页面新增 MCP/SKILL 管理卡片 UI
- [ ] 后端：CRUD API 路由 + 文件系统操作
- [ ] ToolRegistry 扩展：支持 extensions/ 目录扫描 + MCP/SKILL 解析
- [ ] MCP Server 进程管理逻辑（Sandbox 内托管）
- [ ] 渐进式披露三层架构实现（L0/L1/L2）
- [ ] 提示词注入适配（6 个 Section 修改）
- [ ] SKILL 适配改造：pptx/docx/xlsx/pdf/canvas-design/algorithmic-art/theme-factory
- [ ] sequential-thinking MCP 部署

---

## Feature 3：经典群机器人功能升级（Plan_1 Stage 10 回归）

### 原始设计
Plan_1 Stage 10 定义了签到系统、小游戏、群管理增强等传统功能，至今未实现。

### 新思路：与 FlashLite 深度集成

不做传统的「命令式」签到和游戏，而是让这些功能成为**老板娘人格的自然延伸**：

#### 3.1 群管理能力（✅ API 已调研）

老板娘作为群主/管理员时，可以通过 FlashLite 的语义判断**主动参与群管理**：

**实现基础**：AstrBot 已有完整的 `self.bot.call_action()` 调用机制（见 `aiocqhttp_message_event.py`）

| 能力 | 场景示例 | API 接口 | 调用方式 |
|------|---------|----------|----------|
| 设置专属头衔 | "给我整个酷酷的头衔" | `set_group_special_title` | `call_action("set_group_special_title", group_id=..., user_id=..., special_title="...")` |
| 设为精华消息 | 老板娘觉得消息很有价值 | `set_essence_msg` | `call_action("set_essence_msg", message_id=...)` |
| 修改群名片 | "帮我改个名片" | `set_group_card` | `call_action("set_group_card", group_id=..., user_id=..., card="...")` |
| 全员禁言/解禁 | 特定场景群管操作 | `set_group_whole_ban` | `call_action("set_group_whole_ban", group_id=..., enable=True)` |
| 欢迎新人 | 新人入群自动欢迎 | `group_member_increase` | 监听事件 → 自动触发人格化欢迎 |
| 群公告 | 代发群公告 | `_send_group_notice` | `call_action("_send_group_notice", group_id=..., content="...")` |
| 设置群待办 | "帮我记一下明天开会" | `set_group_todo` | 需确认 NapCat 扩展支持 |

> ⚠️ **原则**：不做踢人、永久禁言等不可逆操作。只做"有益管理"类动作。

#### 3.2 签到 & 小游戏（与人格融合）

- 签到不是冰冷的 `/签到`，而是自然对话："老板娘，早上好" → 老板娘傲娇回应 + 签到记录
- 小游戏由老板娘主动发起（根据群活跃度和时间判断）
- 积分系统绑定到 Memory 系统（老板娘会记得每个人的积分和排名）

### ✅ 工具封装方案（已确定）

**选定方案 A：封装为 `base_tools/*.tool.json`**

| 方案 | 评估 |
|------|------|
| ✅ A: base_tools JSON | 渐进式披露自动生效，提示词改动最少，每个 API 一个 `.tool.json` |
| ❌ B: 裸暴露 call_action | 需要大段 system_prompt 说明，污染 KV Cache |

具体工具列表：
- `group_set_title.tool.json` — 设置专属头衔
- `group_set_essence.tool.json` — 设为精华消息
- `group_set_card.tool.json` — 修改群名片
- `group_set_ban.tool.json` — 全员禁言/解禁
- `group_send_notice.tool.json` — 发群公告

每个 `.tool.json` 的 description 中内嵌使用约束（如"仅当群成员直接请求时使用"）。

### 提示词变动（⭐⭐ 中等）

| Section | 变动内容 |
|---------|----------|
| **0 体系认知** | 新增"群管理能力"段：原则+判断标准 |
| **11 工具速查** | 新增 【群管理】 分类行 |

Section 0 新增段落示例：
```
## 群管理能力
你在部分群中拥有管理员权限，可以设置专属头衔、精华消息等。
原则：只做有益管理，不做踢人/永久禁言等不可逆操作。
群友开玩笑说"禁言他" → 不执行，用傲娇语气拒绝。
群友真诚请求"给我整个头衔" → 自然回应 + 调用工具。
```

### 待实现
- [ ] 封装群管理 API 为 base_tools `.tool.json`（5-6个工具）
- [ ] 对应 handler 实现（`call_action` 包装）
- [ ] 新人入群事件监听 + 人格化欢迎
- [ ] FlashLite 判断层：什么时候触发群管操作
- [ ] 权限校验：确认老板娘 QQ 号是否为管理员
- [ ] 签到数据存储（复用 Memory / SQLite）
- [ ] 提示词注入适配（2 个 Section 修改）
- [ ] NapCat 完整 API 清单参考：https://napcat.apifox.cn/

---

## Feature 4：图片/图像搜索功能（Plan_1 Stage 11 回归）

> 📋 详细调研报告见 `Plan_4_Feature4_图像搜索调研.md`

### ✅ 技术方案（已确定）

#### 4.1 搜索引擎矩阵

| 引擎 | 用途 | 来源 | 免费额度 |
|------|------|------|----------|
| **SauceNAO** | 插画作者溯源 | img_rev_searcher 提取 | 6次/30s（免注册够用）|
| **trace.moe** | 动漫截图 → 番名+集数 | 直接 API | 无限（有频率限制）|
| **ascii2d** | 二次元搜图补充 | cq-picsearcher 参考 | 免费 |
| **AnimeTrace** | 动漫角色识别 | img_rev_searcher 提取 | 免费 |
| **Google Lens** | 通用反向搜图 | img_rev_searcher 提取(selenium) | 免费 |
| **Bing/Baidu/Yandex** | 通用搜图 | img_rev_searcher 提取 | 免费 |
| **ExHentai** | 本子搜索 | img_rev_searcher 提取 | 需 E-Hentai Cookie |
| **pixivpy3** | Pixiv 标签搜索 | 已装插件复用 | 需 Refresh Token |
| **Lolicon API** | 色图获取 | 已装插件复用 | 免费 |

#### 4.2 集成方式
- **D1 决策**：B — 提取 `img_rev_searcher` 的引擎封装代码
- **D3 决策**：Google Cloud Vision 暂不纳入（效果弱于 Google Lens）
- 参考项目：`cq-picsearcher-bot`（1.6k⭐）的交互模式设计
- **工具形态决策**：封装为 **MCP Server**（非 base_tools）
  - 理由：需要管理 API Key、ExHentai Cookie、SauceNAO 频率限控（6次/30s），MCP 进程内统一管理
  - 作为 F2 MCP 框架的**第一个自研 MCP Server 实战测试用例**
  - 依赖 F2 的 MCP 基础设施（进程管理 + ToolRegistry 扩展）

#### 4.3 反审查策略（参考 setu 插件）

| 策略 | 说明 | 面板控制 |
|------|------|----------|
| `image_obfus` | 修改 3 个像素 RGB±1，破坏 QQ 哈希黑名单 | ✅ 开关 |
| 合并转发 (Node) | NSFW 结果用转发节点包装发送 | ✅ 开关 |

#### 4.4 Gemini 多模态联动
- 收到图片 → Gemini 识图描述 → 调用搜索工具精确查找来源
- FlashLite/主模型通过 tool calling 自然调用

### 提示词变动（⭐⭐ 中等）

| Section | 变动内容 |
|---------|----------|
| **5 工具调用规范** | 新增搜图示例（用户发图+说"搜一下" → tool_call）|
| **11 工具速查** | 新增 【搜图】 分类行 |

### 待实现
- [ ] 实现 image_search MCP Server（封装全部搜索引擎）
- [ ] MCP 内集成：API Key 管理、Cookie 持久化、频率限控
- [ ] Pixiv Refresh Token 获取（Playwright 脚本 + 用户浏览器登录）
- [ ] ExHentai Cookie 配置（用户找回 E-Hentai 账号后）
- [ ] image_obfus + 转发模式集成到搜图结果发送
- [ ] BossLady 面板搜图配置项
- [ ] 提示词注入适配（2 个 Section 修改）

---

## 开发顺序（✅ 方案 B+ 已确定）

| 阶段 | 内容 | 提示词变动量 | 说明 |
|------|------|-------------|------|
| **1st** | F3 群管理 | ⭐⭐ (2 Section) | base_tools 封装，最小改动热身 |
| **2nd** | F2 MCP 框架 | ⭐⭐⭐ (6 Section) | ToolRegistry 扩展 + extensions/ + 进程管理 + 面板 UI |
| **2.5** | SKILL 实战导入 | — | 导入 pptx/docx/xlsx/pdf/canvas-design 等，验证框架 |
| **3rd** | F4 搜图 MCP | ⭐⭐ (2 Section) | 第一个自研 MCP Server，验证 MCP 集成 |
| **4th** | F1 向量检索 | ⭐ (最小) | ChromaDB + 联想机制，独立于工具系统 |

### 方案 B+ 的设计理由

1. **F3 先行**：改动最小（base_tools JSON），作为提示词工程的热身
2. **F2 框架先于 F4**：F4 搜图选定为 MCP Server 形态，依赖 F2 的 MCP 基础设施
3. **2.5 SKILL 导入**：用反重力的成熟 SKILL 验证 F2 框架，比空框架更有说服力
4. **F4 实战测试**：作为第一个自研 MCP Server，验证整个 MCP 集成链路
5. **F1 最后**：开发量最大且独立于工具系统，不影响其他 Feature

### 开发量排序

| 等级 | Feature | 估算工作量 |
|------|---------|----------|
| 最轻 | F3 群管理 | 5-6 个 `.tool.json` + handler + 提示词 2 Section |
| 中等 | F2 MCP/SKILL | ToolRegistry 扩展 + 前端卡片 UI + 后端 CRUD + 进程管理 + SKILL 适配 |
| 中等 | F4 搜图 MCP | MCP Server 实现 + 引擎封装 + Cookie/限流管理 |
| 最重 | F1 向量检索 | ChromaDB + embedding pipeline + FlashLite 联想 + 面板 |

---

## 提示词工程总览

### 现有注入架构

主模型 `on_llm_request` 中有两条独立注入通道：

| 通道 | 变量 | 注入位置 | KV Cache |
|------|------|---------|----------|
| **static** | `inject_parts[]` | → `system_prompt` 尾部 | ✅ 稳定命中 |
| **dynamic** | `dynamic_parts[]` | → `contents` 第一条 user message 前缀 | ❌ 每次变化（不影响 system cache）|

### inject_parts Section 清单（static → system_prompt）

| # | Section | 内容 | F1 | F2 | F3 | F4 |
|---|---------|------|----|----|----|----|
| 0 | 体系认知 | 老板娘身份 + 协作系统 | — | ✏️ +MCP/SKILL | ✏️ +群管理段 | — |
| — | 输出风格 | 1-3句话硬约束 | — | — | — | — |
| 4 | 工具集(brief) | ToolRegistry.get_brief() | — | ✏️ 三层渐进 | — | — |
| 5 | 回复格式+工具规范 | 聊天风格 + function call | — | — | — | ✏️ +搜图示例 |
| 6 | Memory 系统 | 何时读写记忆 | — | — | — | — |
| 7 | Knowledge 说明 | 全局对话概览 | — | — | — | — |
| 7.5 | 文件链接处理 | view_file/web_fetch | — | — | — | — |
| 8 | Sandbox 工作空间 | workspace 使用原则 | — | ✏️ +extensions/ | — | — |
| 9 | 自定义工具 | .tool.json 编写标准 | — | ✏️ 区分读写权限 | — | — |
| 10 | Task 系统 | 后台任务说明 | — | — | — | — |
| 11 | 工具速查 | 分类导航 + 示例 | ✏️ QQ_data | ✏️ +MCP/SKILL | ✏️ +群管理 | ✏️ +搜图 |

### 其他注入点

| 注入点 | F1 | F2 | F3 | F4 |
|--------|----|----|----|----|  
| QQ_data_original `.tool.json` description | ✏️ +向量搜索 | — | — | — |
| KV Cache 工具模型固定区 | — | ✏️ +MCP/SKILL资源 | — | — |
| dynamic_parts（联想注入） | ✏️ 新增 | — | — | — |
| 新 base_tools `.tool.json` | — | — | ✏️ 5-6个 | — |
| 新 MCP Server 工具声明 | — | — | — | ✏️ image_search |

> 全部四个 Feature 方案已闭环，提示词变动点已明确，可按方案 B+ 顺序启动开发。

---

## 修订记录

| 日期 | 内容 |
|------|------|
| 2026-04-14 | v1.0 初版——四大 Feature 讨论记录 |
| 2026-04-14 | v1.1 Feature 1 新增「联想机制」应用场景 + 15 题讨论问题清单 |
| 2026-04-14 | v2.0 全面更新——全部选型完成、全部 API 调研完成、F2/F3 方案明确化、E-Hentai 账号已找回 |
| 2026-04-14 | v3.0 架构决策+提示词工程完成——工具封装形态确定、extensions/ 目录结构、方案B+开发顺序、SKILL导入计划、提示词变动矩阵 |
