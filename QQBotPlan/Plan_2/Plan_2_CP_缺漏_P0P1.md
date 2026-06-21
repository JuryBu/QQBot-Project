# Plan 2-CP 缺漏清单（第一部分）：必须修复的问题

> 本文档综合了 Report_2_12 审查、Codex 双重 Review（GPT-5.4 xhigh + high）以及主人 Review 的反馈，梳理当前 CHECKPOINT 实现中 **必须修复** 的所有问题。

---

## 🔴 P0-1：参数命名断裂 —— checkpoint_limit vs checkpoint_token_limit

### 问题描述

面板/后端/config.json 全部使用 `checkpoint_limit`，但 main.py 的实际消费端读取的是 `checkpoint_token_limit`。

### 影响链路

```
面板修改 → app.js 发送 checkpoint_limit → 后端 models.py 存入 config.json["checkpoint_limit"]
                                              ↓
                              main.py L2697 读取 checkpoint_token_limit  ← ❌ 不匹配！
                              → _cfg() 找不到 → 回退默认 50000
```

**结果**：主人面板上改的 10000 实际不会生效到 T 文件压缩判断。

### 相关代码位置

| 文件 | 位置 | 当前 key |
|---|---|---|
| `main.py` L160 | CheckpointManager 初始化 | `checkpoint_token_limit` ❌ |
| `main.py` L2697 | compress_if_needed 调用 | `checkpoint_token_limit` ❌ |
| `config.json` L9 | 持久化存储 | `checkpoint_limit` |
| `models.py` L160/178/200 | 后端 API | `checkpoint_limit` |
| `app.js` L1363/1376 | 前端 | `checkpoint_limit` |
| `index.html` L427 | HTML input | `cpTokenLimit` → `checkpoint_limit` |
| `Plan_2_CP_compression.md` L11/21 | 文档 | `checkpoint_token_limit` ❌ |

### 修复方案

1. `main.py` L160 和 L2697：改为读取 `checkpoint_limit`
2. `Plan_2_CP_compression.md`：参数表改为 `checkpoint_limit`
3. 可选：兼容旧名

```python
# main.py L2697 修改
token_limit=self._cfg("checkpoint_limit", self._cfg("checkpoint_token_limit", 50000)),
```

---

## 🔴 P0-2：旧 check_and_compress() 调用残留

### 问题描述

main.py 的群聊同步触发和私聊触发路径仍在调用 `self._checkpoint_mgr.check_and_compress()`，但 CheckpointManager 已不定义此方法。

### 影响

每次消息处理都会触发 `AttributeError`，被 `try/except` 吞掉并写 error log。不影响功能但浪费资源且污染日志。

### 相关代码位置

| 文件 | 位置 | 描述 |
|---|---|---|
| `main.py` L867-882 | 群聊同步触发 | `=== CHECKPOINT 检查 ===` 块 |
| `main.py` L1170-1185 | 私聊触发 | 同上 |
| `checkpoint.py` L765-834 | CheckpointManager | 仅保留 `get_stats()`，无 `check_and_compress()` |

### 修复方案

直接删除 `main.py` 中这两段旧代码块。压缩逻辑已在 `on_llm_request` 中通过 `TFileManager.compress_if_needed()` 统一处理。

---

## 🔴 P0-3：压缩边界 Bug —— T1 消息对切割

### 问题描述

当已有 T1 摘要时，candidate 的前 2 条是 T1 的 `[user(摘要), assistant(ACK)]` 消息对。第二重守卫和 compress_count 计算基于 `len(candidate)` 而不是 `len(t_file["messages"])`，在边界场景下会产生负数索引。

### 复现条件

```
已有 T1 摘要（candidate 包含 2 条 T1 消息）
len(t_file["messages"]) = keep_recent + 1 = 11
len(candidate) = 2 + 11 = 13 > keep_recent(10) → 通过第二重守卫

compress_count = max(1, int(13 * 0.7)) = 9
compress_count = min(9, 13 - 10) = 3  ← 只压 3 条

to_compress 包含 T1 的 2 条 + 1 条原始消息
t1_msg_count = 2
original_msgs_compressed_count = 3 - 2 = 1  ← 正常

# 但如果 len(candidate) 只比 keep_recent 多 1：
# len(candidate) = 11, keep_recent = 10
# compress_count = max(1, int(11 * 0.7)) = 7
# compress_count = min(7, 11 - 10) = 1  ← 只压 1 条！
# t1_msg_count = 2
# original_msgs_compressed_count = 1 - 2 = -1  ← 负数！
# remaining_messages_start = 1 - 2 = -1  ← Python 负索引！
# t_file["messages"] = t_file["messages"][-1:]  ← 几乎全部消息被裁掉！
```

### 修复方案

第二重守卫应基于 `len(t_file["messages"])` 而非 `len(candidate)`：

```python
# 当前（有 Bug）
if len(candidate) <= keep_recent:
    return t_file, None

# 修复后
if len(t_file["messages"]) <= keep_recent:
    return t_file, None
```

同时 `compress_count` 计算也要排除 T1：
```python
# 可压缩的消息数（排除 T1 的 2 条消息 + 保留 keep_recent）
t1_count = 2 if has_existing_t1 else 0
available_for_compress = len(candidate) - t1_count - keep_recent
if available_for_compress <= 0:
    return t_file, None

compress_count = t1_count + max(1, int(available_for_compress * compress_front_ratio))
```

---

## 🔴 P1-1：FlashLite 触发判断上下文必须切到 T 文件

### 背景说明

**注意区分两个概念**：
- **CHECKPOINT 压缩逻辑**操作的是 T 文件数据 → 已正确实现 ✅
- **FlashLite 触发判断**（判断要不要唤醒主模型）使用的是 `messages.db` → 仍是原系统

根据 CHECKPOINT 讨论记录（关键点 D）和 Plan_2_CP.md 决策 #5（"主模型和 FlashLite 都使用 T"），FlashLite 触发判断**必须**改用 T 文件上下文。这是三系统分立架构的核心承诺之一，不是可选优化。

### 当前状态

- `checkpoint.py` 中 `build_flashlite_context()` **已实现**但**无调用点**
- 同步/异步/私聊三条入口的 `_get_recent_context()` 仍从 `messages.db` 读取
- Plan_2_CP_integration.md 第 3 节已规划了此修改

### 修复方案

按 Plan_2_CP_integration.md L175-189 执行：

```python
# 原来的
recent_context = await self._get_recent_context(group_id)

# 改为
window_key = f"GroupMessage:{group_id}"
t_file = await self._t_file_mgr.load(window_key)
recent_context = self._t_file_mgr.build_flashlite_context(t_file, max_tokens=8000)
```

### 影响范围

- `main.py` 同步触发 (L740)
- `main.py` 异步触发 (L905)
- `main.py` 私聊触发 (L1048)
- Knowledge 上下文（间接通过 FlashLite prompt 影响）

---

## 🟡 P1-2：LLM 回复后回写 T 文件

### 问题描述

当前只能等下一轮 `on_llm_request` 从 `req.contexts` 增量提取时才补录上一轮的 assistant/tool 消息。Plan_2_CP_integration.md 第 4 节设计了在延迟持久化逻辑中同步回写 T 文件。

### 当前实现

`main.py:L2546-L2597` 延迟持久化逻辑只写 `messages.db`（`_persist_bot_reply()`），不写 T 文件。

功能效果上，新消息会在"下一轮请求开始时"通过 `_extract_new_messages()` 间接补齐（因为 AstrBot 的 `req.contexts` 包含上一轮 assistant 回复）。

### 风险

- 若 AstrBot 在下次请求前截断了 assistant 回复，T 文件可能永久丢失该条
- 工具调用链 `assistant → tool_call → tool_result → assistant` 可能因增量提取的时序不稳定而部分丢失

### 修复方案

在 on_llm_request 后处理或延迟持久化中追加：

```python
if window_key and t_file:
    for msg in reversed(req.contexts):
        if msg.get("role") == "assistant":
            await self._t_file_mgr.append_messages(window_key, [msg])
            break
```
