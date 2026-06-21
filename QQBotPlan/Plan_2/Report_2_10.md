# Report_2_10：系统性检查与深度调研报告

> 调研时间：2026-04-08 | 基于实际代码审计

---

## A. 消息拦截存储机制

### 当前实现

消息拦截由 `astrbot_plugin_persistence` 插件实现（`main.py`），核心流程：

```
QQ消息 → intercept_all_messages (priority=9999，最高优先级)
       → 解析消息字段(文本/图片/表情/回复/转发/卡片/语音/视频/文件)
       → 异步放入写入队列 (asyncio.Queue, max=1000)
       → _batch_writer 后台任务批量写入 SQLite (batch=20, timeout=2s)
```

存储位置：`QQ_data/messages.db`（SQLite WAL 模式）

### 定期清理

> [!CAUTION]
> **系统设置面板的「消息持久化策略」（热存储/冷存储/归档天数）目前只有配置字段，没有实际执行代码。** persistence 插件只读取了 `hot_data_days=7`、`cold_data_days=30`、`enable_cold_cleanup=True` 三个配置，但整个插件中 **没有任何自动清理/分级管理的定时任务代码**。

这意味着：面板上设置的 7天/30天/90天 分级策略 **不会自动执行**，消息会无限增长直到手动清理。

---

## B. 「消息持久化策略」vs「数据清理」区别

| 项目 | 系统设置 → 消息持久化策略 | 对话内存 → 数据清理 |
|------|-------------------------|---------------------|
| **位置** | 系统设置页面 | 对话内存页面 |
| **功能** | 热存储(7天)/冷存储(30天)/归档(90天) | 清理 N 天前的旧消息 |
| **后端实现** | 仅保存配置到 JSON，**无执行代码** | 直接 `DELETE FROM qq_messages WHERE created_at < 截止时间` |
| **效果** | ⚠️ **无实际效果**（配置悬空） | ✅ **立即生效**，物理删除数据库记录 |

### 设计意图 vs 实际状态

系统设置的「消息持久化策略」**设计意图**是分级管理：
- **热存储**(7天)：完整保留，快速查询
- **冷存储**(30天)：可能做字段裁剪/压缩存储
- **归档**(90天)：超此天数自动清理

但这套分级管理的 **定时任务从未被实现**。persistence 插件只做了「写入」没做「清理」。

对话内存页面的「数据清理」是一个手动操作按钮，通过 `DELETE /api/messages/cleanup?days=30` 直接删除指定天前的所有消息，是一刀切的物理删除。

> [!WARNING]
> **结论**：两个面板管理的是 **同一份数据**（`qq_messages` 表），但系统设置的分级策略是空壳，只有对话内存的手动清理按钮真正有效。

---

## C. 图片缓存管理——去重与防过大机制

### 当前机制 ✅ 充分

| 机制 | 实现方式 | 代码位置 |
|------|---------|---------|
| **MD5 去重** | 每张图下载后计算 `hashlib.md5(data).hexdigest()`，与 `_hash_index.json` 比对，已存在则复用 | persistence L416-426 |
| **Hash 索引持久化** | `QQ_data/images/_hash_index.json` 记录 `{md5_hash: filename}` 映射 | persistence L464-482 |
| **容量上限** | 配置 `image_cache_max_mb=500`，每次下载前检查总量 | persistence L484-516 |
| **超限清理** | 按时间排序删除最旧文件，清理到 80% 容量 | persistence L505 |
| **下载超时** | 单张图片下载 10s 超时 | persistence L401 |
| **失败降级** | 下载失败保留 `cdn:` 前缀的原始 URL | persistence L407,412,453 |

**结论**：图片缓存管理已有完善的去重（MD5）和防膨胀（max_mb + LRU 清理）机制。

> [!NOTE]
> 但缺少单张图片大小限制——如果某张图片特别大（比如 50MB），不会被过滤。不过 QQ 图片服务器通常会压缩，实际风险较低。

---

## D. 内存存储 vs QQ_data_original 的关系

**是的，`QQ_data_original` 工具查询的就是 persistence 插件写入的 `qq_messages` 表。**

数据流：
```
QQ消息 → persistence 插件拦截 → 写入 QQ_data/messages.db (qq_messages 表)
                                         ↑
QQ_data_original 工具查询 ←──────────────┘
```

`QQ_data_original` 的可获取范围 = `qq_messages` 表中所有未被清理的记录，包含：
- 所有群聊和私聊的文本消息
- 图片 URL（本地路径或 CDN）
- 表情、回复、转发、卡片、语音、视频、文件等元数据
- 撤回标记和原始内容

---

## E. 对话 vs 内存——两套独立系统

**正确，对话和内存是完全不同的两套系统。**

| 维度 | 「对话」(Conversation) | 「内存」(Memory/qq_messages) |
|------|----------------------|---------------------------|
| **管理者** | FlashLite + AstrBot 主模型 | persistence 插件 |
| **存储位置** | AstrBot 内部会话管理（内存中） | QQ_data/messages.db (磁盘) |
| **生命周期** | 每个窗口(群/私聊)独立维护 | 全量持久化，跨窗口 |
| **压缩机制** | CHECKPOINT 压缩 | 无（原始记录） |
| **用途** | 发给 AI 模型的上下文 | 历史查询、数据分析 |

### 实际发送给主模型的内容

主模型收到的请求体（`on_llm_request` 注入）：

```
系统层固定人格（"老板娘"身份）
+ FlashLite 注入的 Knowledge 卡片
+ CHECKPOINT 压缩摘要（如果有）
+ 工具集说明
+ AstrBot 会话管理器维护的最近对话记录（平台层管理）
```

其中 AstrBot 的会话管理器负责维护每个窗口的对话历史，它有自己的 token/条数限制。

---

## F. CHECKPOINT 机制工作原理

### 核心流程 ✅ 正常工作

```
FlashLite 每次处理消息（同步/异步触发后）
  → 调用 CheckpointManager.check_and_compress()
    → 从 qq_messages 表读取该窗口所有未撤回消息
    → 估算总 token 数（中文 1.5字/token + 英文 4字符/token + 图片 258token/张）
    → 如果 total_tokens > token_limit (默认 50000)
      → 保留最近 keep_recent=10 条不压缩
      → 其余消息构建压缩 prompt 发给 Flash Lite
      → Flash Lite 返回摘要，写入 checkpoint_history 表
```

### 发送给主模型时

`on_llm_request` 钩子中（L2392-2411）：
```python
checkpoint_text = await self._agent_builder._get_checkpoint_summary(window_key)
if checkpoint_text:
    # 注入 "## CHECKPOINT 历史压缩摘要" 到 system prompt
```

`build_context_for_main_model()` 构建 **C' = 最新 CHECKPOINT 摘要 + 最近 N 条原始消息**。

**结论**：CHECKPOINT 压缩后确实将压缩结果拼入主模型请求体，机制正常。

---

## G. CHECKPOINT 判断权——谁说了算？

> [!IMPORTANT]
> **CHECKPOINT 的触发判断完全由系统自动执行，FlashLite AI 的提示词中写的「CHECKPOINT 判断」只是形式上的——实际不依赖 AI 的判断。**

### 实际触发机制（代码层面）

```python
# checkpoint.py L94-133
async def check_and_compress(self, window_id, window_type, flash_lite_caller):
    # 1. 从 DB 读取该窗口所有消息
    # 2. 估算 total_tokens
    # 3. if total_tokens < self.token_limit: return None  ← 不需要压缩
    # 4. 超限则自动触发压缩
```

这个方法在 FlashLite 的 `_sync_trigger`（L866-881）和 `_private_sync_trigger`（L1169-1184）中被 **无条件调用**——每次处理完消息就检查一次。

### FlashLite 提示词中的描述

```
2. CHECKPOINT 判断：评估当前上下文 token 量是否接近上限
```

这只是让 FlashLite 在输出中增加一个 `CHECKPOINT_NEEDED` 字段（true/false），但代码中 **并未使用这个字段的值来决定是否压缩**。压缩完全由 `CheckpointManager` 的 token 估算自动决定。

### 压缩标准

| 参数 | 默认值 | 面板可调 |
|------|--------|---------|
| `token_limit` | 50000 | ✅ 系统设置 → CHECKPOINT 策略 → Token 上限 |
| `keep_recent` | 10 | 代码硬编码 |
| 压缩率区间 | 10%-35%（目标~22.5%） | ✅ 面板 → 压缩率范围 |

**结论**：已修复——将 FlashLite 提示词中的「CHECKPOINT 判断」移出核心职责列表，改为独立的「CHECKPOINT 说明」段落，明确标注由系统自动管理，AI 无需判断。

---

## H. 系统设置 CHECKPOINT 面板有效性

### ✅ 有效

Console 面板通过 API 链路：

```
前端面板输入 → POST /api/models/flashlite → 保存到 flashlite.json
                                              ↓
FlashLite 初始化时读取 → self._cfg("checkpoint_token_limit", 50000)
                        → CheckpointManager(token_limit=...)
```

| 面板字段 | API 字段 | 后端读取 | 生效方式 |
|---------|---------|---------|---------|
| Token 上限 | `checkpoint_limit` | `_cfg("checkpoint_token_limit", 50000)` | 重启 AstrBot |
| 压缩率范围 | `compression_range` | `_cfg("checkpoint_compression_range", "0.2-0.4")` | 重启 AstrBot |

**结论**：面板配置有效，修改后重启 AstrBot 即可生效。

---

## 1. 工具模型 Review 模式检查

### 已更新内容

Review 模式提示词已扩充（`_build_tool_model_system` L1376-1397），现包含 7 步：

| # | 职责 | 说明 |
|---|------|------|
| 1 | 文件清单 | 列出 workspace/ 下所有文件和目录 |
| 2 | **重复文件合并** | 扫描同目录下内容相同但名字不同的文件，合并为一个 |
| 3 | **位置整理** | 错位文件移动到正确目录 |
| 4 | **临时垃圾清除** | 删除确认无用的临时文件、超7天 drafts、空/损坏文件 |
| 5 | 报告归档检查 | task_reports/ 中未归档报告 |
| 6 | 异常文件检查 | 超大/不该存在的文件 |
| 7 | 写入维护日志 | system_report 包含对应段落 |

### Review 周期面板联动

✅ **有效**：面板设置 → `review_interval_hours` → FlashLite `_review_interval_hours` → L653 按时间差判断是否触发，范围 1-168 小时。

---

## 遗留问题汇总

| # | 问题 | 严重程度 | 处理状态 |
|---|------|---------|----------|
| 1 | 消息持久化策略的分级清理未实现 | ⚠️ 中 | ✅ 已实现——persistence 插件每6小时自动执行分级清理（归档删除 + 冷存储裁剪），面板配置联动完整 |
| 2 | FlashLite 提示词中 CHECKPOINT 判断描述不准确 | 低 | ✅ 已修复——改为「系统自动管理」独立说明段 |
