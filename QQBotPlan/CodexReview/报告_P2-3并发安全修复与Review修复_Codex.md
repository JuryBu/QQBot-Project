# 审核报告：P2-3 并发安全修复 + 本轮 Review 修复

**审核时间**: 2026-04-10  
**审核范围**:  
- `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py`  
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`  
- `BossLady_Console/backend/routers/models.py`  
- `AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py`  
**整体评价**: 本轮修复覆盖了“压缩期间 append 覆盖写”的主路径，但仍存在“并发压缩 + 中间 append”场景下的消息丢失竞态，尚未达到完全并发安全。

## 🔴 严重问题（必须修复）

### 问题 1：合并式 Save 在“并发压缩”下仍会覆盖中间新消息
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:507-727`（关键点：`671`, `711-719`）
- **描述**：当前合并逻辑假设压缩期间只有 `append_messages` 在写入，即通过  
  `mid_arrival_msgs = current_msgs[pre_compress_msg_count:]` 提取中间新增消息。  
  但当同窗口并发触发两次 `compress_if_needed()` 时，先完成的一次压缩会先把 `current_msgs` 变短；后完成的压缩再用旧的 `pre_compress_msg_count` 做切片会得到空集，从而覆盖掉两次压缩之间到达的新消息。
- **复现场景（已实测）**：
  1. 请求 A/B 基于同一快照并发进入压缩；
  2. B 先完成并保存（消息被裁剪）；
  3. 请求 C 追加一条新消息；
  4. A 后完成保存，`current_msgs[旧快照长度:]` 为空，C 的消息被覆盖丢失。
- **修复建议**：
  1. 增加“窗口级压缩互斥门闩”（独立于 append 锁）：同窗口同一时刻只允许一个 `compress_if_needed` 执行，后续压缩请求直接跳过或排队。  
  2. 在 T 文件中引入版本号/epoch，保存阶段先校验快照基线；若基线变化（发生过他人压缩），则放弃本次保存或按新基线重算 `remaining_messages`。  
  3. 补充并发回归测试：覆盖 “A/B 并发压缩 + C append”。

## 🟡 建议改进

### 问题 2：关键并发修复缺少自动化测试，现有测试与实现已脱节
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py:275-287`，以及测试整体未覆盖 `compress_if_needed` 并发路径
- **描述**：
  - 当前测试未覆盖 P2-3 的 `load-merge-save` 并发安全路径；
  - `test_build_compress_prompt` 仍断言旧 Prompt 字数参数，已与当前实现不一致，执行会失败。
- **修复建议**：
  1. 新增 `compress_if_needed` 的并发单测（至少覆盖 append-only 与 double-compress 两类）；  
  2. 更新/重写 `test_build_compress_prompt` 断言，改为校验当前 Prompt 关键约束文本而非旧字数占位符。

## 🟢 微调建议

### 问题 3：assistant 补录去重仅按 content，可能误判 `content=None` 的工具调用消息
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2701-2708`
- **描述**：`_is_dup` 仅比较 `assistant.content`。当 `assistant` 为 tool-call 消息（常见 `content=None`）时，不同调用可能被判重。
- **修复建议**：去重签名改为 `(role, content, tool_calls)` 或至少在 `content is None` 时附加比较 `tool_calls`。

## ✅ 做得好的地方

- `main.py` 已按要求在替换 `req.contexts` 前保存 `_original_contexts`，避免引用丢失导致补录源错误。  
- 去重逻辑已收敛为“仅与最后一条 assistant 比较”，避免旧实现对历史短句的误伤。  
- `BossLady_Console/backend/routers/models.py` 已实现旧键名回退：  
  `config.get("checkpoint_limit", config.get("checkpoint_token_limit", 50000))`，兼容性修复到位。
