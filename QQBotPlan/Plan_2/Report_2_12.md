# Report_2_12: CHECKPOINT 重构实现状态深度审查报告

**审查时间**: 2026-04-10  
**审查人**: Antigravity + GPT-5.4 (xhigh) + GPT-5.4 (high)  
**审查方法**: 针对主人提出的 6 个核心问题，逐条对照 Plan_2_CP 系列文档与真实代码实现，标注匹配/差异/缺漏

---

## 问题 1: 三系统分立架构与 T 文件持久化

> 目前是否是三系统？T 文件存储在 QQ_data/checkpoints/ 下？能持久化？重启后读取？用 req.contexts 增量提取新消息？

### ✅ 已确认实现

**三系统分立架构完全落地：**

| 系统 | 代码位置 | 状态 |
|---|---|---|
| A：`req.contexts` | AstrBot 框架管理的 `conversation.history` | ✅ 未触碰 |
| B：`messages.db` | FlashLite 的 SQLite `qq_messages` 表 | ✅ 未触碰 |
| C：Per-window T 文件 | `checkpoint.py:269-272` `_file_path()` | ✅ 已实现 |

**T 文件存储位置** (`checkpoint.py:L252-L254`)：
```python
CHECKPOINTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "QQ_data", "checkpoints"
)
```
生成路径如 `QQ_data/checkpoints/GroupMessage_<GROUP_B>.json`，符合 Plan_2_CP_architecture.md 设计 ✅

**持久化保证** (`checkpoint.py:L317-L335`)：
- 每次写操作使用"先写临时文件(.tmp) → 再原子重命名(os.replace)"模式
- 文件在磁盘上，重启不丢失
- JSON 损坏时回退空 T (`checkpoint.py:L303-L307`)

**读取逻辑** (`checkpoint.py:L278-L307`)：
- `load(window_key)` 自动检测文件是否存在，不存在则创建空 T
- 文件存在则读取 JSON → 返回完整 T 文件字典

**增量提取** (`main.py:L3015-L3032`)：
```python
def _extract_new_messages(self, contexts, t_file):
    existing_count = len(t_file.get("messages", []))
    compressed_count = t_file["T1"].get("original_msg_count", 0)
    processed_count = compressed_count + existing_count
    if len(contexts) > processed_count:
        return contexts[processed_count:]
    return []
```
从 `req.contexts` 中基于计数增量提取新消息，追加到 T 文件底部 ✅

### ⚠️ Codex 发现的潜在问题

`_extract_new_messages()` **假设历史长度只增不减**。如果 AstrBot 框架自己做了 `truncate_turns` 导致 `len(contexts) < processed_count`，则此后所有新消息都不会被检测到。

**影响评估**：在我们的架构下，由于 AstrBot 的截断是兜底机制（文档决策 #10），且我们用 C 替换了 req.contexts 发送给 LLM，AstrBot 的截断对最终输出无影响。但 T 文件确实可能在极端场景下漏记几条消息。后续可考虑加入尾部内容比对作为保险。

---

## 问题 2: CHECKPOINT 对 A/B 系统的影响关系

> CHECKPOINT 不会影响 A 系统和 B 系统？不会被 AstrBot 的 A 系统压缩截断影响？

### ✅ 已确认：C 系统完全独立

1. **不影响 A**：`on_llm_request` 中 `req.contexts = self._t_file_mgr.build_llm_contexts(t_file)` 是替换操作（`main.py:L2710`），只影响本次请求，AstrBot 底层的 `conversation.history` 继续自行维护不被修改。

2. **不影响 B**：T 文件管理不操作 `messages.db`（仅在压缩后会调用 `_save_to_db` 向 `checkpoint_history` 表写统计记录，不改动 `qq_messages` 表）。

3. **不被 A 系统截断影响**：
   - AstrBot 的 `truncate_turns` / `enforce_max_turns` 作用在 `req.contexts` 上
   - 我们在 `on_llm_request`（AstrBot 构建完 contexts 之后运行）中**整体替换** `req.contexts`
   - 替换后 AstrBot 不会二次压缩，因为插件钩子是最后一道关卡（`main.py:L2489` 之后直接发送给 LLM）
   - 即使 AstrBot 之后有自己的压缩，C 系统的 T 文件数据不会被删改
   
   > Plan_2_CP.md 决策 #10: "与 AstrBot 框架自带压缩平行：FlashLite 先压缩，框架兜底" ✅

---

## 问题 3: 各模型收到的上下文来源

> FlashLite 和主模型都收到 C 系统上下文？FlashLite 有截断？工具模型呢？

### 主模型 ✅ 
`main.py:L2710`：
```python
req.contexts = self._t_file_mgr.build_llm_contexts(t_file)
```
主模型收到的是 C 系统（T 文件）构建的完整上下文：T1 压缩摘要 + 未压缩原文消息 ✅

### FlashLite ⚠️ 部分实现
**当前实际情况**：FlashLite 触发判断上下文**仍然来自 messages.db**，未切到 T 文件。

代码证据：
- `main.py:L740`：`recent_context = await self._get_recent_context(group_id)` — 同步触发
- `main.py:L905`：同上 — 异步触发  
- `main.py:L1048`：同上 — 私聊触发

`_get_recent_context()` (`main.py:L2274-L2338`) 从 `messages.db` 的 `qq_messages` 表读取最近 N 条消息。

**`build_flashlite_context()` 已实现但无调用点**：`checkpoint.py:L436-L470` 中实现了从 T 文件构建 FlashLite 上下文的方法（含 max_tokens 截断），但 main.py 中没有任何地方调用它。

**截断**：`build_flashlite_context` 实现了从尾部向前截断到 `max_tokens=8000` 的策略 (`checkpoint.py:L456-L465`)，所以 FlashLite 不会收到过长的上下文。

**文档承诺**：Plan_2_CP.md 决策 #5 明确要求"主模型和 FlashLite 都使用 T"，但这一承诺**尚未兑现到 FlashLite 侧**。

### 工具模型 ✅
工具模型默认不带聊天上下文。`main.py:L1548-L1557` 中 `_call_tool_model()` 仅以任务 prompt 构造请求，不自动拼接 T 上下文。

符合 Plan_2_CP.md 决策 #4: "工具模型默认不带上下文" ✅

---

## 问题 4: T 文件结构是否符合规范

> T 文件结构是否如 Plan_2_CP_T_file.md 所示？

### ✅ 完全一致

**`_create_empty_t_file()` (`checkpoint.py:L105-L134`) 生成结构**：

```json
{
  "version": 1,
  "window_key": "GroupMessage:<GROUP_B>",
  "window_type": "group",  
  "window_id": "<GROUP_B>",
  "T1": {
    "compressed_summary": "",
    "token_count": 0,
    "compression_ratio": 0.0,
    "original_msg_count": 0,
    "compression_count": 0,
    "last_compress_time": "",
    "compress_history": []
  },
  "messages": [],
  "metadata": {
    "created_at": "2026-04-10T08:00:00",
    "updated_at": "2026-04-10T08:00:00",
    "total_messages_ever": 0,
    "total_compressions": 0,
    "avg_compression_ratio": 0.0
  }
}
```

**与 Plan_2_CP_T_file.md 逐项对比**：

| 文档规范 | 代码实现 | 一致 |
|---|---|---|
| `version/window_key/window_type/window_id` | ✅ `L107-L113` | ✅ |
| `T1.compressed_summary/token_count/compression_ratio` | ✅ `L115-L120` | ✅ |
| `T1.original_msg_count/compression_count` | ✅ `L121-L122` | ✅ |
| `T1.last_compress_time/compress_history` | ✅ `L123-L124` | ✅ |
| `messages` 数组支持 role/content/timestamp/meta/tool_calls/tool_call_id | ✅ `append_messages L359-L383` | ✅ |
| `metadata.created_at/updated_at/total_messages_ever/total_compressions` | ✅ `L126-L132` | ✅ |

**压缩后 T1 更新** (`checkpoint.py:L666-L674`)：含 compressed_summary、token_count、compression_ratio、original_msg_count（累加）、compression_count（+1）、last_compress_time、compress_history（限 20 条）✅

---

## 问题 5: 压缩规则逻辑审查

> 压缩规则是否如 Plan_2_CP_compression.md 和讨论记录所示？压缩率如何保证？

### 三重守卫 ✅

`checkpoint.py:L541-L567` 严格实现：

```python
# ① token 超限：total_tokens > token_limit
# ② 消息数足够：len(candidate) > keep_recent  
# ③ 冷却期已过：(now - last_compress_time) > cooldown_seconds
```
三个条件必须**同时满足**才触发压缩 ✅

### 压缩范围计算 ✅

```python
compress_count = max(1, int(len(candidate) * compress_front_ratio))
compress_count = min(compress_count, len(candidate) - keep_recent)
```
确保至少保留 keep_recent 条消息不被压缩 ✅

### 压缩率保证 ⚠️

**当前实现**：压缩率超出 `[target_min, target_max]` 区间时**只打 warning，仍然接受结果** (`checkpoint.py:L620-L635`)。

Plan_2_CP.md 决策 #9 要求"压缩率必须严格保证"，当前实现属于"soft guarantee"：

- 通过 Prompt 明确目标字数/token 区间（`build_compress_prompt` L170-L176）
- 后验证记录 warning/info
- **但不拒绝/重试超出区间的结果**

**评价**：对于 Flash Lite 这种小模型，Prompt 的长度约束已经能大幅改善之前 0-1% 的极端情况。强制拒绝可能导致"压缩永远不成功→对话上下文无限增长"的更严重问题。当前实现是务实的折中。

### ❌ Codex 发现的边界 Bug

当已有 T1 摘要（占 candidate 前 2 条消息）且 `len(messages)` 刚好只比 `keep_recent` 多一点时：

- `compress_count` 可能 = 1（只压 T1 的第一条 user 消息）
- 但 `t1_msg_count = 2`，导致 `original_msgs_compressed_count = -1`
- `remaining_messages_start = 1 - 2 = -1`
- `t_file["messages"] = t_file["messages"][-1:]` — 大量原文被误删

**修复方案**：第二重守卫应基于 `len(t_file["messages"])` 而非 `len(candidate)`，确保"至少有 1 条原始消息可被压缩"才执行。

### 下次压缩更新流程 ✅

压缩完成后 (`checkpoint.py:L653-L689`)：
1. 新 T1 覆盖旧 T1（含新摘要、token 数、压缩率、累加的原始消息数、压缩次数+1、时间戳）
2. `messages` 保留未被压缩的部分
3. 压缩历史追加（限 20 条）
4. 元数据更新
5. 原子保存 T 文件

下次触发时：
- T1 中包含上次压缩摘要 → 作为 candidate[0:2] 参与新的 candidate
- 新消息从 req.contexts 增量追加
- 旧 T1 和新消息一起被 re-compress → RNN 遗忘效应自然实现 ✅

---

## 问题 6: 压缩时 FlashLite 收到的完整 Prompt

> 压缩判断是系统做的？call FlashLite 时传的完整提示词？

### 触发流程

压缩不是 FlashLite 主动触发的，而是 `on_llm_request` 钩子中 `TFileManager.compress_if_needed()` 自动判断 (`main.py:L2693-L2703`)：

```
用户发消息 → AstrBot 构建 req.contexts → FlashLite on_llm_request 钩子
  → 加载 T 文件 → 提取新消息追加 → compress_if_needed() 检查三重守卫
  → 三重守卫通过？ →（是）→ call FlashLite 进行压缩
                      →（否）→ 跳过压缩
  → req.contexts = T 文件上下文 → 发送给主模型
```

### FlashLite 压缩时收到的完整 Prompt

压缩时系统调用 `self._call_flash_lite(prompt)`，这个 prompt 由 `build_compress_prompt()` (`checkpoint.py:L141-L194`) 构建。

**完整 Prompt 模板**（以实际值填充示例）：

```
你是一个对话压缩引擎。将以下对话内容压缩为结构化摘要。

注意：输入内容开头有一段 [对话历史压缩摘要]，这是之前轮次的压缩结果。
请将其与后续新消息融合为一份统一的新摘要，旧摘要中的信息可以适当精简但不要完全丢弃。

## 输出长度要求（关键！）
- 目标长度：约 4500 个中文字（3000~6000 字范围内）
- 原始内容约 10000 tokens
- 你的输出必须在 3000 到 6000 字之间
- 过短（< 3000 字）或过长（> 6000 字）都是失败

## 压缩原则
1. 按话题/时间段分块，用简洁的标题标注每个话题段
2. 保留所有参与者名字和 QQ 号
3. 保留关键事实：人名、地名、数字、日期、结论、决定
4. 保留情感倾向和关系动态
5. 用「」包围重要原文引用
6. 去除：重复内容、纯表情、日常闲聊（你好/再见）、无信息量的应答
7. 如涉及图片/文件/工具调用，注明 [图片] [文件] [工具:名称→结果摘要]

## 输出格式
直接输出摘要，不要输出其他说明文字。格式参考：

【话题：xxx（时间段）】
参与者A 和 B 讨论了...关键信息:「原文引用」

## 原始内容（15000 字）
[对话历史压缩摘要]
（上次压缩的摘要文本...）

老板娘 [BOT]: 好的，我已了解之前的对话历史。
[18:30:00] 张三(1234567890): 今天天气真好
[18:30:05] 老板娘 [BOT]: 是呀 天气确实不错呢
[18:31:00] 老板娘 [BOT]: [调用工具: web_search]
[18:31:02] [工具结果 call_001]: 南京今日晴 25°C...
[18:31:05] 老板娘 [BOT]: 刚帮你查了 今天南京25度呢
...
```

**参数来源链路**（以 `target_min=0.20, target_max=0.40` 为例）：
```
target_mid = 0.30
target_tokens = original_tokens × 0.30
target_chars = target_tokens × 1.5 （中文约 1.5 字/token）
min_chars = original_tokens × 0.20 × 1.5
max_chars = original_tokens × 0.40 × 1.5
```

**消息序列化**由 `serialize_messages_for_compress()` (`checkpoint.py:L197-L241`) 完成：
- user 消息：`[时间] 发送者名(QQ号): 内容`
- assistant 消息：`[时间] 老板娘 [BOT]: 内容`
- 工具调用：附加 `[工具调用: 工具名]`
- 工具结果：`[工具结果 call_id]: 内容`

### "注意"段落

当 `to_compress` 中包含旧的 T1 摘要（检测 `[对话历史压缩摘要]` 前缀，`checkpoint.py:L583-L588`）时，Prompt 会加上 `summary_note` 提示 FlashLite"请将旧摘要与新消息融合为统一新摘要，旧信息可适当精简但不要完全丢弃"。

---

## Codex Review 综合发现（3个严重问题 + 3个改进建议）

### ❌ 严重问题

#### 1. `checkpoint_limit` vs `checkpoint_token_limit` 命名断裂

- **面板/后端/config.json** 全部使用 `checkpoint_limit`
- **main.py:L160, L2697** 读取的是 `checkpoint_token_limit`
- **结果**：面板保存的 Token 上限不会传到压缩逻辑，运行时回退默认 50000

> 主人面板上改的 10000 实际**不会生效**到 T 文件压缩判断中！

#### 2. 旧 `check_and_compress()` 调用残留

- `main.py:L869-L882`（群聊同步触发）和 `L1172-L1185`（私聊触发）仍在调用 `self._checkpoint_mgr.check_and_compress()`
- 但 `CheckpointManager` 已不定义此方法
- 每次运行都会抛 `AttributeError`，被 try/except 吞掉

#### 3. FlashLite 上下文未切到 T 文件

- 计划文档明确要求 FlashLite 使用 T 文件上下文
- 但同步/异步/私聊三条判断入口仍从 `messages.db` 读取
- `build_flashlite_context()` 已实现但无调用

### ⚠️ 改进建议

1. **压缩边界 Bug**：第二重守卫应基于 `len(t_file["messages"])` 而非 `len(candidate)`
2. **增量提取健壮性**：增加 `len(contexts) < processed_count` 的降级处理
3. **压缩率越界处理**：考虑越界时拒绝覆盖 T1（保留原 T）

---

## 修复优先级建议

1. 🔴 **P0**: 统一 `checkpoint_limit` 参数名，否则面板调参无意义
2. 🔴 **P0**: 删除两处失效的 `check_and_compress()` 旧调用
3. 🟡 **P1**: FlashLite 上下文切到 T 文件（完成三系统分立承诺）
4. 🟡 **P1**: 修复压缩边界 Bug（T1 消息对切割问题）
5. 🟢 **P2**: 增量提取健壮化 + 并发保护 + 压缩率越界处理
