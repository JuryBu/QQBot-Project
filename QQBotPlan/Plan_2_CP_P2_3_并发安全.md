# Plan: P2-3 压缩并发安全 — 合并式 Save

## 背景

`compress_if_needed` 在调用 FlashLite API 压缩时（5-10秒），其他请求的新消息可能通过 `append_messages` 写入磁盘。压缩完成后的 save 基于旧快照，会覆盖这些中间消息。

## 方案：压缩期间乐观放行，Save 时合并

### 核心原则
1. **压缩期间不阻塞**：新消息正常写入磁盘，FlashLite/主模型用最新 T 文件
2. **Save 时合并**：重新 load 磁盘最新状态，提取中间到达的消息，追加到保留部分
3. **Save 后不二次检测**：即使合并后仍超限，等下一条消息自然触发

### 时序图

```
请求A: load(10条) → append(A→11条) → compress_if_needed(基于11条快照)
                                            ↓ FlashLite API调用中(5-10s)...
请求B:                                      ↓ append(B→12条, 磁盘写入)
请求C:                                      ↓ append(C→13条, 磁盘写入)
                                            ↓ 压缩结果返回
                                            ↓ Save阶段(锁内):
                                            │  load 最新(13条)
                                            │  mid_arrival = [B, C] (比快照多的)
                                            │  messages = [保留部分] + [B, C]
                                            │  save → 完成
```

### 修改文件

#### checkpoint.py: compress_if_needed Save 阶段

原代码 (L660-709):
```python
remaining_messages = t_file["messages"][original_compress_count:]
t_file["messages"] = remaining_messages
async with self._get_lock(window_key):
    await self.save(window_key, t_file)
```

改为:
```python
remaining_messages = t_file["messages"][original_compress_count:]
pre_compress_msg_count = len(t_file["messages"])  # 记录快照消息数

# ... 更新 T1, metadata ...

# Save 阶段：锁内 load-merge-save（原子操作）
async with self._get_lock(window_key):
    current_t_file = await self.load(window_key)
    current_msgs = current_t_file.get("messages", [])
    mid_arrival = current_msgs[pre_compress_msg_count:]
    t_file["messages"] = remaining_messages + mid_arrival
    await self.save(window_key, t_file)
```

### 安全性分析

1. **死锁风险**: load() 不加锁，在 _get_lock 内调用安全
2. **append_messages**: 使用同一个 _get_lock，与 save 互斥，不会并发写
3. **增量提取兼容**: processed_count = compressed + existing 在合并后仍然正确（数学证明见思考记录）
4. **工具消息**: append_messages 已完整保存 tool_calls/tool_call_id/tool role，_extract_new_messages 按计数提取所有类型消息，不遗漏

### 不修改

- `_extract_new_messages`: 计数策略天然兼容
- `append_messages`: 已有窗口锁保护
- `main.py on_llm_request`: 流程不变
