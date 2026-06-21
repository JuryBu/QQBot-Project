# 审核报告：CHECKPOINT 重构 Stage 8-14

**审核时间**: 2026-04-10
**审核范围**: 
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py`
- `BossLady_Console/backend/routers/models.py`
- 设计文档：`QQBotPlan/Plan_2/CHECKPOINT机制讨论记录.md`、`Plan_2_CP*.md`、`Plan_2_CP_缺漏_P0P1.md`、`Plan_2_CP_缺漏_P2优化.md`

**整体评价**: Stage 8-14 主干需求基本落地（参数统一、旧调用清理、压缩边界修复、T 文件上下文切换、max_tokens 链路、P2 参数校验/提示词/语义分割均已实现），但仍存在 1 个并发一致性严重风险与 1 个 Stage12 回写时序缺陷。

## 🔴 严重问题（必须修复）

### 问题 1：同窗口并发请求下，T 文件存在“覆盖写”风险（可能丢消息/丢压缩结果）
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2659-2679`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:339-383`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:507-735`
- **描述**：
  - `on_llm_request` 的流程是 `load -> extract -> append -> compress -> save/replace contexts`，但没有把整段包在同一个 `window_key` 锁内。
  - `append_messages()` 虽然加锁，但 `compress_if_needed()` 的“计算 + FlashLite 调用 + 最终保存”基于传入快照 `t_file` 执行；并发请求时，后到请求可能先写入新消息，先到请求随后用旧快照保存，覆盖掉后到请求的新内容。
  - 该行为违反了设计文档对“同窗口单时刻互斥操作 T 文件”的约束，直接影响三系统分立中 C 系统的数据完整性。
- **修复建议**：
  1. 在 `main.py` 的 T 文件处理段对同一 `window_key` 加总锁（整个事务锁）。
  2. 避免锁重入死锁：将 `append_messages` / `compress_if_needed` 拆为“已持锁版本”（unlocked helper）供事务内调用。
  3. 事务顺序建议：`load_latest -> append_new -> compress_if_needed -> build_llm_contexts`，并确保最终保存只发生一次。

## 🟡 建议改进

### 问题 2：assistant 补录发生在 `req.contexts` 替换之后，当前轮仍可能丢失上轮 assistant 上下文
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2685-2711`
- **描述**：
  - 当前先执行 `req.contexts = build_llm_contexts(t_file)`，再做“补录上一轮 assistant”。
  - 若命中补录分支（`len(new_msgs)==0` 且 T 末尾非 assistant），补录结果不会进入当前轮 `req.contexts`，只能在下一轮生效。
  - 与 Stage 12 “回复后回写 T 文件并避免遗漏”的目标不完全一致。
- **修复建议**：
  1. 将补录逻辑前移到 `build_llm_contexts` 之前。
  2. 补录后更新本地 `t_file`（或重新 load）再构建 `req.contexts`。

### 问题 3：assistant 补录去重条件过于宽松，存在“同内容不同轮次”被误判重复的风险
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2703-2706`
- **描述**：
  - 去重仅比较“最近 3 条中是否存在相同 assistant content”。
  - 当模型多轮回复相同短句（例如“好的”）时，可能误判为重复并跳过，导致遗漏。
- **修复建议**：
  1. 去重条件改为“仅与最后一条 assistant 完全相同才跳过”，减少误伤。
  2. 若可行，加入 `timestamp/tool_calls/tool_call_id` 参与去重指纹。

## 🟢 微调建议

### 问题 4：后端读取 FlashLite 配置未对旧键名做回退，控制台兼容性不完整
- **位置**：`BossLady_Console/backend/routers/models.py:160`
- **描述**：
  - 运行时（`main.py`）已兼容 `checkpoint_limit <- checkpoint_token_limit` 回退；
  - 但控制台读取接口只读 `checkpoint_limit`，旧配置文件若仅有 `checkpoint_token_limit`，面板会显示默认值 `50000`。
- **修复建议**：
  - 读取时改为：`config.get("checkpoint_limit", config.get("checkpoint_token_limit", 50000))`。
  - 可选：保存时执行一次键名迁移并移除旧键，避免双键并存。

## ✅ 做得好的地方

1. **Stage 8（参数命名统一）**
- `main.py` 关键入口已采用 `checkpoint_limit` 并保留 `checkpoint_token_limit` 回退兼容。
- `config.json`、`models.py`、`frontend/app.js` 主链路均统一为 `checkpoint_limit`。

2. **Stage 9（旧调用清理）**
- `main.py` 已无 `check_and_compress()` 调用残留。
- 群聊同步/异步/私聊触发链路完整，FlashLite 判断路径正常。

3. **Stage 10（压缩边界修复）**
- `compress_if_needed()` 实现了三重守卫。
- 第二重守卫与压缩计数基于原始消息数（`t_file["messages"]`）而非 candidate 长度，负索引风险已被规避。

4. **Stage 11（FlashLite 上下文切换到 T 文件）**
- `main.py` 中 `_get_recent_context` 调用点已切换为 `load + build_flashlite_context`（共 5 处）。
- `window_key` 规范使用 `GroupMessage:{id}` / `FriendMessage:{id}`。
- `build_flashlite_context()` 返回 `str`，与 `_build_judgment_prompt(context: str)` 兼容。

5. **Stage 13（max_tokens 压缩率硬保证）**
- `_call_flash_lite` 已支持 `max_output_tokens` 形参并传递至 `generationConfig.maxOutputTokens`。
- `compress_if_needed` 已实现 `raw_max + Δ` 动态上限并传参调用。
- `build_compress_prompt` 已移除“强制字数区间”描述，改为“尽量详细 + API 上限控制”。

6. **Stage 14（P2 批量优化）**
- `models.py` 已实现 `checkpoint_limit` 下界/上界约束与 `target_min <= target_max` 自动交换。
- 系统认知提示词已更新为“T 文件上下文来源”。
- 压缩分割已增加 user-assistant 对话对完整性保护。

## 备注
- 尝试执行自动化验证：`python -m pytest -q QQBotPlan/Plan_1/test_codex_fixes.py QQBotPlan/Plan_1/test_stage13_e2e.py`
- 当前环境未安装 `pytest`（`No module named pytest`），因此本次结论基于静态审查。
