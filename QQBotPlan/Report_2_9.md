# Report 2-9：问题13硬化完成 — 系统状态总览

> 日期：2026-04-08 | 关联：Plan_2_2.md Issue #13

---

## 一、当前运行的插件及作用

| 插件名称 | 状态 | 作用说明 |
|---------|------|---------|
| **astrbot_plugin_flashlite** | ✅ 启用 | Flash Lite 中断引擎。CPU 中断处理器，负责 Knowledge 更新、CHECKPOINT 压缩、主模型触发决策、表情包内化发送 |
| **astrbot_plugin_chatsummary** | ✅ 启用 | 基于 LLM 的历史聊天记录总结插件，为 FlashLite 知识系统提供上下文 |
| **astrbot_plugin_persistence** | ✅ 启用 | QQ 消息持久化，支持消息写入 SQLite、冷热数据管理、重启恢复 |
| **astrbot_plugin_recall_cancel** | ✅ 启用 | 撤回取消插件。用户撤回消息后仍保留记录，防止 LLM 回复后消息被撤走导致上下文断裂。纯事件级管控，不注入提示词 |
| **Pixiv 图片搜索** | ✅ 启用 | 通过标签在 Pixiv 上搜索图片，支持 R18 过滤和数量控制 |
| **astrbot_plugin_setu** | ✅ 启用 | AstrBot 色图插件，支持自定义配置与标签指定 |
| **letai_sendemojis** | ⛔ 禁用 | 原表情包发送插件。功能已内化到 FlashLite 的 `_emoji_manager`，保留文件但禁用 |

### 已卸载的插件

| 插件名称 | 卸载理由 |
|---------|---------|
| **context_enhancer** | "直接回复该用户"隐性对抗简短约束 + 功能与 FlashLite Section 3 重叠 |
| **heartflow** | FlashLite 中断引擎已覆盖群聊触发判断 |
| **group_chat** | 绕过 persona/FlashLite，拥有独立回复引擎直接生成回复 |
| **knowledge_base** | 未配置实际知识库，空跑浪费资源 |

---

## 二、三模型架构与 FlashLite 触发情况

### 模型职责

| 角色 | 模型 | 职责 |
|------|------|------|
| **主模型** | `cmd_config.json` 中启用的 provider（当前: gemini-2.5-flash） | 老板娘核心人格，处理用户直接对话、工具调用、图像生成等 |
| **Flash Lite（中断引擎）** | gemini-3.1-flash-lite-preview | CPU 中断处理器，负责群聊监控、Knowledge 更新、触发判断、CHECKPOINT 压缩 |
| **工具模型** | FlashLite 配置中的 `tool_model.model` | 用于高级搜索（scope=web）等需要独立推理的工具场景 |

### FlashLite 触发情况列表

| 触发类型 | 条件 | 说明 |
|---------|------|------|
| **同步触发** | 群聊每 N 条消息（`sync_trigger_interval`，默认 5） | 定期扫描群聊上下文，更新 Knowledge 摘要 |
| **同步时间门控** | 距上次同步 ≥ `sync_time_interval` 秒（默认 60） | 防止消息密集时过于频繁触发 |
| **@触发** | 用户在群聊中 @老板娘 | 立即唤醒主模型回复 |
| **唤醒词触发** | 消息中包含 `wake_keywords` 中的关键词 | 当前建议仅保留 `"/"` |
| **CHECKPOINT 超限** | token 数超过 `checkpoint_token_limit`（默认 50000） | 触发 CHECKPOINT 压缩，精简历史上下文 |
| **工具反馈** | 后台 Task 完成后的待唤醒队列 | `_pending_task_wakes` 触发主模型处理工具结果 |
| **表情包触发（新增）** | `on_decorating_result` 钩子（priority=9000） | **主模型回复后**才触发：扫描回复文本中的关键词 → 匹配到则延迟发送本地表情包。FlashLite 仅负责初始化时扫描目录建立关键词映射表，不参与实际触发 |

---

## 三、主模型提示词注入去除情况

### 注入链路概览（从 system_prompt 构建到最终发送）

```
persona_mgr → 插件 on_llm_request → FlashLite Section 注入 → [TOOL_CALL_PROMPT] → [LLM_SAFETY_MODE] → 发送
                                                                  ↑ 已移除            ↑ 待关闭
```

### 去除/保留状态

| 提示词 | 状态 | 位置 | 说明 |
|-------|------|------|------|
| **Persona（老板娘人格）** | ✅ 保留 | `persona_mgr.py` L423 | 老板娘核心人格，与 FlashLite 互补 |
| **FlashLite Section 1-16** | ✅ 保留 | `astrbot_plugin_flashlite/agent.py` | 完整的行为规范、工具使用规范、简短约束 |
| **TOOL_CALL_PROMPT** | 🗑️ 已移除 | `astr_main_agent.py` L1399 | "briefly summarize result" 与简短输出约束冲突。FlashLite Section 7-16 已自带完整工具指令 |
| **TOOL_CALL_PROMPT_SKILLS_LIKE_MODE** | 🗑️ 随 TOOL_CALL_PROMPT 一并移除 | 同上 | skills_like 模式的变体，同样冲突 |
| **LLM_SAFETY_MODE_SYSTEM_PROMPT** | ⚠️ 待关闭 | `astr_main_agent.py` L1367 | 通过 AstrBot WebUI → `provider_settings.llm_safety_mode` 设为 `false` 关闭。"promote constructive content" 消耗 token 且无实际作用 |
| **SANDBOX_MODE_PROMPT** | ✅ 保留 | `astr_main_agent_resources.py` L53 | 沙箱模式提示，仅一句话，无干扰 |
| **LIVE_MODE_SYSTEM_PROMPT** | ✅ 保留 | `astr_main_agent.py` L1403 | 仅在直播场景触发，不影响日常对话 |
| **context_enhancer 提示词** | 🗑️ 已卸载 | — | "直接回复该用户"隐性对抗简短约束 |
| **heartflow 触发指令** | 🗑️ 已卸载 | — | 独立设置 `is_at_or_wake_command`，功能重叠 |
| **group_chat 独立引擎** | 🗑️ 已卸载 | — | 绕过 persona/FlashLite 的独立回复引擎 |

### 待操作项

1. **LLM_SAFETY_MODE** → AstrBot WebUI → 服务提供商设置 → `llm_safety_mode` 设为 `false`
2. **唤醒词清理** → AstrBot WebUI → 基本设置 → 唤醒词列表，移除所有自定义唤醒词仅保留 `"/"`
