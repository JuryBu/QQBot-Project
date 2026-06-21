# 🎀 老板娘 QQ 机器人 —— 升级总纲领 (Plan_1)

> 版本: v1.1（2026-04-02 讨论更新）  
> 创建时间: 2026-04-01  
> 项目路径: `c:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\`

---

## 项目概述

「老板娘」是为群友打造的傲娇 QQ 机器人，基于 AstrBot + NapCat + Gemini 架构。因长期荒废需全面升级。本 Plan 定义了从基础设施恢复到高级 Agentic 能力的完整升级路线。

---

## 已完成项（Stage 0 - 基础设施恢复）

| 项目 | 升级前 | 升级后 | 状态 |
|------|--------|--------|------|
| AstrBot | v4.0.0 | **v4.22.2** | ✅ 完成 |
| Python | 3.9.13 | **3.12.13** (uv) | ✅ 完成 |
| AI 模型 | 中转站已挂 | **Gemini 2.5 Flash（Google AI Studio 原生）** | ✅ 完成 |
| NapCat | 4.8.109 (build 39038) | **v4.17.53** (已部署) | ✅ 待启动验证 |
| QQ | 9.9.7 build 21484 | **9.9.26 build 44343** | ✅ 完成 |
| 数据备份 | — | `水桶理论聊天\备份\` | ✅ 完成 |

---

## Stage 分期计划

### Stage 1：NapCat 启动验证 + 连通性测试
- 以管理员权限启动 NapCat v4.17.53
- 验证 QQ 登录（可能需扫码）
- 确认 OneBot 11 反向 WS 连接到 AstrBot 6199 端口
- 在群里发一条测试消息验证端到端通信
- **验证方式**：群内收发消息正常

### Stage 2：人格重塑 + 分段回复
- 重写 System Prompt（从 ~150字 扩展到 800-1200字）
  - 身世故事、性格层级、说话风格、社交习惯
  - 记忆管理指令、情绪系统、禁止事项
- 扩充预设对话（从 3 对到 15-20 对）
- 开启 `segmented_reply`（分段回复）
- 调整回复延迟和最大字数
- 修复分段空行、改为引用回复、配置唤醒词
- **验证方式**：在群内对话测试老板娘风格是否自然 ✅ 已通过

### Stage 3：多模态视觉能力
- 确认 provider modalities 包含 `["text", "image"]`（已配）
- 验证 NapCat 图片消息解析正常
- 测试发图让老板娘识图
- 配置上下文增强器的 image_caption 指向正确 provider
- **验证方式**：发图后老板娘能正确描述图片内容

### Stage 4：智能对话跟踪（零开销连续对话）

> **核心目标**：解决「每次都要@或说唤醒词才能对话」的问题，实现自然的群聊连续对话

**方案设计**（轻量级状态机 + 语义判断混合）：

1. **对话态状态机**（纯本地逻辑，零 API 开销）
   - 被 @/唤醒词触发后，该用户进入「对话态」（默认 90s 超时）
   - 对话态内，同一用户的后续消息自动判断是否需要回复
   - 超时 / 用户明确结束（"好的""谢了"等）时退出对话态
   - 其他用户@也会让那个用户进入独立的对话态

2. **意图判断层**（解决「不是在跟老板娘说话」的问题）
   - 对话态内的消息先做**轻量级本地意图检测**：
     - 消息明确回复/引用老板娘 → 直接触发 ✅
     - 消息包含「老板娘」关键词 → 直接触发 ✅
     - 消息是@别人的 → 不触发 ❌
     - 消息是纯表情/图片/链接 → 不触发 ❌
     - 其他：根据上下文语义相关性判断（可选用 Heartflow 的小模型轻判断，但频率受限）
   - 考虑**对话温度**：连续回复 3 轮后逐渐降低触发概率，避免刷屏

3. **与现有系统集成**
   - 不依赖 Heartflow/group_chat 插件，独立实现为新插件
   - 复用 context_enhancer 的上下文收集能力
   - 设计为 AstrBot 插件，以 `@filter.event_message_type(GROUP_MESSAGE, priority=999)` 高优先级拦截

- **验证方式**：@老板娘后，后续不@也能自然继续对话

### Stage 5：Gemini KV Cache 优化（API 成本优化）

> **核心目标**：利用 Gemini API 的 context caching 特性，大幅降低重复 token 开销

**天然优势分析**：
- 聊天场景上文高度一致（System Prompt + 预设对话 ≈ 2000 token 每次重复发送）
- Gemini `cachedContent` API 可缓存这些固定内容，后续调用只传增量
- 缓存的 token 计费比实时 token 便宜 75%

**实现要点**：
1. **缓存层级**
   - L1：System Prompt + 预设对话（几乎不变，长期缓存）
   - L2：近期对话历史的滚动窗口（随对话推进增量更新）

2. **撤回消息处理**（你提到的重点）
   - 消息撤回时需要**重建缓存**——从撤回点之后的上下文重新构建
   - `recall_cancel` 插件已能捕获撤回事件，可在此 hook 里触发缓存失效
   - 设计缓存版本号机制：每次上下文变化（撤回/编辑）递增版本，不匹配时重建

3. **AstrBot Provider 层适配**
   - 需要修改 `openai_chat_completion` provider，添加 Gemini CachedContent 支持
   - 或在 provider 外层做一个缓存代理中间件

- **验证方式**：对比启用前后的 API 调用 token 数和延迟

### Stage 6：深度记忆系统（🔥 大讨论项）

> **核心目标**：让老板娘拥有真正的长期记忆，比简单的「截断历史消息」方案强大得多

**问题分析**：
- 现有 `long_term_memory` 内置插件太基础（只做简单的消息存取）
- 截断消息历史长度 → 截多了丢失重要信息，截少了 token 爆炸
- 没有自动整理/总结/遗忘机制

**理想架构**（参考 Antigravity IDE 的 memory-store 设计，要做得更好）：

1. **三层记忆体系**
   - 🔴 **工作记忆**（Working Memory）：当前对话的最近 N 条消息（原始保留）
   - 🟡 **短期记忆**（Episodic Memory）：近期对话的**自动总结**（每 N 轮压缩一次）
   - 🟢 **长期记忆**（Semantic Memory）：用户画像 + 话题知识 + 关键事件（持久化存储）

2. **自动总结与遗忘**
   - Agentic 方式：对话结束后自动调用 LLM 总结本次对话要点
   - 渐进式遗忘：短期记忆随时间衰减，重要的提升为长期记忆
   - 去重：相似内容合并而非重复存储

3. **用户画像系统**
   - 记住每个群友的：昵称偏好、说话风格、常聊话题、重要事件
   - 主动在对话中引用（"你上次不是说想学 Python 吗"）
   - 好感度系统（基于互动频率和内容）

4. **技术实现选项**
   - 方案 A：自写 MCP memory-store（类似我们现在用的）
   - 方案 B：SQLite + 向量检索（复用已有的 FAISS）
   - 方案 C：AstrBot 插件 + JSON/SQLite 持久化

- **验证方式**：老板娘能记住群友说过的话、喜好，并主动提及
- **⚠️ 此项需要大讨论确定具体方案后再进入执行**

### Stage 7：MCP 工具生态
- 在 WebUI 配置 MCP 服务器：
  - web-search（网络搜索）
  - 自定义 memory-store（接入 Stage 6 的记忆系统）
  - filesystem（有限文件操作）
- 验证工具调用可用
- **验证方式**：@老板娘 搜索 xxx → 返回搜索结果

### Stage 8：Agent 沙盒环境 (Shipyard Neo)
- 评估是否需要 Docker 部署
- 如果资源不足可改用轻量级沙盒方案
- 配置沙盒权限（仅限 /workspace 目录）
- **验证方式**：让老板娘执行简单代码并返回结果

### Stage 9：SubAgent 编排
- 设计子 Agent 架构：
  - 图片搜索员、代码助手、网络搜索员、娱乐助手、记忆管理员
- 配置 SubAgent orchestrator
- 为不同子 Agent 分配不同模型（Flash vs Pro）
- **验证方式**：复杂任务被正确分派到子 Agent

### Stage 10：经典群机器人功能
- 签到系统（每日签到 + 积分 + 排行）
- 小游戏（猜数字/21点等）
- 群管理增强（欢迎新人、定时提醒等）
- **验证方式**：群友可使用签到和游戏功能

### Stage 11：图片/色图功能优化
- 升级 setu / pixiv_search 到最新版
- 结合多模态实现识图
- 配置 Pixiv Refresh Token
- 考虑合规性限制
- **验证方式**：搜图命令正常返回

### Stage 12：主动型 Agent + 高级功能
- 配置定时任务（早安/晚安推送等）
- 自动推送能力（新闻、天气等）
- Skills 模块化能力扩展
- **验证方式**：定时消息按时发送

---

## 关键路径与依赖

```
Stage 1 (NapCat) ──→ Stage 2 (人格) ──→ Stage 3 (多模态)
                  │
                  ├──→ Stage 4 (对话跟踪) ──→ Stage 6 (记忆系统)
                  │                             ↓
                  ├──→ Stage 5 (KV Cache) ──→ Stage 7 (MCP)
                  │                             ↓
                  ├──→ Stage 8 (沙盒) ──→ Stage 9 (SubAgent)
                  │
                  └──→ Stage 10/11/12 (并行)
```

> **重点路径**：Stage 4（对话跟踪）和 Stage 5（KV Cache）可以并行开发，Stage 6（记忆系统）需要大讨论后再执行

## 文件结构

```
AstrBotLauncher-0.1.5.6/
├── QQBotPlan/
│   ├── Plan_1.md                ← 本文件（总纲领 + Stage 路线图）
│   ├── Plan_1_architecture.md   ← 🆕 系统总架构（两层对话管理 + CHECKPOINT）
│   ├── Plan_1_models.md         ← 🆕 三模型分工（Flash Lite / 主模型 / 工具模型）
│   ├── Plan_1_sandbox.md        ← 🆕 Sandbox 空间设计（安全模型 + 工具系统）
│   ├── Plan_1_memory.md             ← 🆕 Memory + Knowledge 双系统
│   ├── Plan_1_data.md               ← 🆕 数据层真相 + API 参数参考 + 可移植性
│   ├── Plan_1_gaps.md               ← 🆕 Review GAP 补充（Stage重编号+7项细节）
│   ├── Plan_1_webui.md              ← 🆕 统一 Web 控制台设计
│   ├── Task.md                      ← 详细 Stage 执行清单
│   ├── Test_Stage3_multimodal.md    ← 🆕 多模态视觉测试场景
│   ├── Test_Stage4_persistence.md   ← 🆕 消息持久化测试场景
│   ├── Test_Stage5_flashlite.md     ← 🆕 Flash Lite 中断引擎测试
│   └── Test_Stage6_8_integration.md ← 🆕 CHECKPOINT+KVCache+Memory+全链路集成
├── AstrBot/                     ← v4.22.2 主程序
├── AstrBot_old/                 ← v4.0.0 旧代码（备用）
├── NapCat_v4.17.53/             ← 新版 NapCat Shell
├── NapCat.Shell.Windows.OneKey/ ← 旧版 NapCat（备用）
└── QQ9.9.26.44343_x64.exe      ← QQ 安装包
```

## 技术备忘

- WebUI: http://localhost:6185 (astrbot / astrbot)
- OneBot WS: ws://127.0.0.1:6199/ws
- QQ 号: <BOT_QQ> (星泰理绪)
- 管理员 QQ: <ADMIN_QQ>
- Gemini API: OpenAI 兼容格式 → generativelanguage.googleapis.com
- Model: gemini-2.5-flash (text + image)
- NapCat 启动需管理员权限
