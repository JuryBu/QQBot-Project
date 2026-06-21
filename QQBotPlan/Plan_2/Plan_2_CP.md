# Plan 2-CP：CHECKPOINT 压缩系统重构 —— 总纲领

## 项目背景

FlashLite 插件的 CHECKPOINT 压缩机制存在严重设计缺陷：
- 操作对象（`messages.db`）与实际需要管控的对象（LLM 请求体上下文）完全分离
- 压缩后不标记/删除原消息 → 每条消息都重复触发压缩
- 压缩率无保证（实际 0-1%，远低于目标 20-40%）
- 结果只注入 system_prompt 文本，不影响实际 contexts

## 重构目标

引入 **三系统分立** 架构，为每个对话窗口独立维护一份智能压缩的对话请求体上下文（T 文件），取代 AstrBot 框架的 `req.contexts` 作为实际发送给 LLM 的上下文。

## 核心架构：三系统分立

| 标识 | 系统 | 管理者 | 存储 | 用途 |
|---|---|---|---|---|
| **A** | `req.contexts` | AstrBot 框架 | `conversation.history` (JSON in DB) | 框架的原始对话历史（不触碰） |
| **B** | `messages.db` | FlashLite | SQLite `qq_messages` 表 | QQ 消息流水持久化 |
| **C** | **Per-window T 文件** | **FlashLite CHECKPOINT** | `QQ_data/checkpoints/*.json` | **实际发送给 LLM 的请求体上下文** |

**核心原则**：
- A 和 B 完全不动，不影响现有功能
- C 是新增的独立系统，由 FlashLite CHECKPOINT 模块维护
- 在 `on_llm_request` 钩子中，用 C 的内容 **替换** `req.contexts`
- 三个模型角色（FlashLite / 主模型 / 工具模型）各自通过 C 获取所需上下文

## 子文档索引

| 文件 | 内容 |
|---|---|
| [Plan_2_CP_architecture.md](Plan_2_CP_architecture.md) | 三系统分立详细架构设计 |
| [Plan_2_CP_T_file.md](Plan_2_CP_T_file.md) | T 文件格式规范、生命周期、读写时机 |
| [Plan_2_CP_compression.md](Plan_2_CP_compression.md) | 压缩策略、Prompt 工程、压缩率保证机制 |
| [Plan_2_CP_integration.md](Plan_2_CP_integration.md) | 集成点：on_llm_request / FlashLite / Knowledge / tools |
| [Plan_2_CP_缺漏_P0P1.md](Plan_2_CP_缺漏_P0P1.md) | 🔴🟡 必须/应该修复的问题清单（含 Codex Review 发现） |
| [Plan_2_CP_缺漏_P2优化.md](Plan_2_CP_缺漏_P2优化.md) | 🟢 优化改进建议（含 max_tokens 压缩率方案、并发安全等） |
| [Report_2_11.md](Report_2_11.md) | 原始问题分析报告 |
| [Report_2_12.md](Report_2_12.md) | 深度审查报告（6 问题逐条核查 + Codex 综合发现） |

## 已确认决策清单

> [!IMPORTANT]
> 以下全部为用户-AI 讨论后明确确认的决策，不可更改

1. ✅ **三系统分立**：A / B / C 完全独立
2. ✅ **T1 放 contexts 中**：作为 `{role: "user"}` 消息注入上下文开头，不放 system_prompt
3. ✅ **T 文件持久化到磁盘**：`QQ_data/checkpoints/` 目录下 per-window JSON 文件，重启不丢失
4. ✅ **工具模型默认不带上下文**：主模型调用工具模型时可选参数传入上下文，默认只传任务
5. ✅ **主模型和 FlashLite 都使用 T**：FlashLite 用 T 进行触发判断 + Knowledge 更新，主模型用 T 聊天
6. ✅ **工具调用全过程记录在 T 中**：assistant → tool_call → tool_result → assistant 完整嵌入
7. ✅ **每个窗口独立的 T**：互不影响
8. ✅ **LLM 请求最终发 C 不发 A**：`on_llm_request` 中 `req.contexts = our_T`
9. ✅ **压缩率必须严格保证**：Prompt 中明确目标 token/字数 + 后验证 + warning
10. ✅ **与 AstrBot 框架自带压缩平行**：FlashLite 先压缩，框架作兜底

## 待讨论/待确认

- 消息增量来源选择（从 `req.contexts` 提取 vs 从 `messages.db` 提取）
  - 用户倾向 messages.db，但认为两者差别不大
  - bot 回复也在 messages.db 中（需确认字段格式）
  - **暂定：选项 1（从 req.contexts 增量提取），因格式最兼容**
