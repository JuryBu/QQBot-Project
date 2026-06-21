# Stage 5-6 审核任务

## 审核目标
对 Report_3 Stage 5（存储费计入）和 Stage 6（Knowledge T文件同步修复）的代码变更进行质量审核。

## 审核范围

### Stage 5: 存储费
- `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`
  - `_calc_cost()` 方法：改为返回 `(total_cost, storage_cost)` 元组
  - `record()` 方法：解构元组，在 JSON 记录中新增 `storage_cost_usd` 字段
  - PRICING 中 `storage` 字段（$/M tokens/hour）被正确使用
  
- `BossLady_Console/backend/routers/cost.py`
  - `summary` 端点新增 `storage_cost_usd` 聚合

- `BossLady_Console/frontend/index.html`
  - 成本明细区域新增 Cache Storage 卡片

- `BossLady_Console/frontend/app.js`
  - loadCostPage 中填充 `costStorageFee` 元素

### Stage 6: Knowledge T文件同步
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
  - `route_message()` 群聊路径 L676 附近：新增每条消息实时追加到 T 文件
  - `route_message()` 私聊路径 L738 附近：同上
  - 关键问题：T 文件之前为空(messages=0)，因为只在 on_llm_request 才写入

## 重点检查
1. _calc_cost() 返回元组后，所有调用方是否都正确解构？是否有遗漏的调用点仍按 float 处理？
2. storage 费计算逻辑是否合理？按最低 1 小时计确实是 Gemini 的计费方式吗？
3. route_message 中的 T 文件追加是否会和 on_llm_request 中的 _extract_new_messages 产生重复消息？
4. 追加到 T 文件的消息格式 `[sender_name] content` 和 on_llm_request 中 req.contexts 的消息格式是否兼容？
5. 高频群消息场景下，每条消息都 await append_messages 是否有性能问题（文件锁竞争）？

## 输出
将审核结果写入 `QQBotPlan/Review_Stage5_6.md`
