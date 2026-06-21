# CHECKPOINT P2-3 并发安全修复 Review

## 任务
对刚完成的 P2-3 并发安全修复 + 本轮所有 Review 修复进行独立代码审核。

## 本轮修复清单（需审核）
1. **P2-3 合并式 Save**：`checkpoint.py` compress_if_needed Save 阶段改为锁内 load-merge-save
2. **S12 Bug 修复**：`main.py` assistant 补录改用 `_original_contexts` 而非替换后的 `req.contexts`
3. **去重逻辑改进**：`main.py` 仅与 T 文件最后一条 assistant 比较，避免误伤短句
4. **旧键名回退**：`models.py` GET 接口增加 `checkpoint_token_limit` 回退读取

## 审核重点

### checkpoint.py 合并式 Save（核心）
- 位置：约 L660-727
- 检查 `pre_compress_msg_count` 记录时机是否正确（应在 compress 前记录）
- 检查 `mid_arrival_msgs = current_msgs[pre_compress_msg_count:]` 的边界安全性
- 验证锁内 load → merge → save 的原子性
- 推演并发场景：请求A压缩中，请求B/C append → A的 save 是否正确合并 B/C

### main.py assistant 补录
- 位置：约 L2685-2715
- 检查 `_original_contexts` 保存时机（req.contexts 替换前）
- 检查去重逻辑：`_is_dup` 只与最后一条 assistant 比较

### models.py 旧键名兼容
- 位置：约 L160
- 检查 `config.get("checkpoint_limit", config.get("checkpoint_token_limit", 50000))`

## 对照设计文档
- `QQBotPlan/Plan_2/Plan_2_CP_P2_3_并发安全.md` — 刚固化的并发安全方案
- `QQBotPlan/Plan_2/CHECKPOINT机制讨论记录.md` — 原始设计理念
- `QQBotPlan/Plan_2/Plan_2_CP_compression.md` — 压缩逻辑规范

## 额外检查
- 工具消息（tool_calls/tool/tool_call_id）在 append_messages 和 _extract_new_messages 中是否正确处理
- _extract_new_messages 的计数策略在压缩+合并后是否仍然数学正确
- 是否存在死锁风险（compress save 锁内调用 load）
