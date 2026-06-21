# CHECKPOINT 重构 Review 任务（Codex A - 深度审核）

## 审核目标
对 FlashLite CHECKPOINT 压缩机制重构的全部代码和设计文档进行深度审核，评估架构一致性、逻辑正确性、边界情况处理。

## 审核范围

### 核心代码文件
1. `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py` — T 文件管理核心类
2. `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py` — on_llm_request 集成（搜索 `compress_if_needed`、`_t_file_mgr`、`_extract_new_messages` 相关代码）
3. `AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py` — 旧 CHECKPOINT 代码清理（确认 `_get_checkpoint_summary` 和 `build_contents` 是否干净清理）
4. `BossLady_Console/backend/routers/models.py` — 面板后端（checkpoint_* 参数的 GET/POST）
5. `BossLady_Console/frontend/app.js` — 面板前端（saveCheckpointStrategy / loadSettingsPage 中 CP 加载）
6. `BossLady_Console/frontend/index.html` — CHECKPOINT 策略卡片 UI

### 设计文档
7. `QQBotPlan/Plan_2/Plan_2_CP.md` — 总纲
8. `QQBotPlan/Plan_2/Plan_2_CP_architecture.md` — 三系统分立架构
9. `QQBotPlan/Plan_2/Plan_2_CP_T_file.md` — T 文件格式规范
10. `QQBotPlan/Plan_2/Plan_2_CP_compression.md` — 压缩策略
11. `QQBotPlan/Plan_2/Plan_2_CP_integration.md` — 集成点清单
12. `QQBotPlan/Plan_2/CHECKPOINT机制讨论记录.md` — 原始讨论

## 审核维度

1. **架构一致性**：代码实现是否完全符合 Plan_2_CP 系列文档中"三系统分立"架构的设计？有无偏离？
2. **参数命名一致性**：前端 HTML ID ↔ 后端模型字段 ↔ config.json key ↔ checkpoint.py 函数参数 ↔ main.py _cfg() 调用，全链路参数名是否一致？
3. **压缩逻辑正确性**：
   - 三重守卫（token 超限 + 消息数 > keep_recent + 冷却期已过）是否严格同时满足？
   - compress_front_ratio 计算逻辑是否正确？
   - 压缩率验证是否做了？
   - RNN 遗忘效应（T1 被反复压缩）是否自然实现？
4. **T 文件管理**：
   - 原子写是否正确实现（先临时文件再 rename）？
   - JSON 损坏恢复是否有兜底？
   - per-window 锁是否正确？
5. **增量消息提取**：`_extract_new_messages` 的逻辑是否可靠？当 AstrBot 自行截断历史时会不会出问题？
6. **旧代码清理**：agent.py 中旧的 CHECKPOINT 注入是否完全移除？有无残留引用？
7. **面板链路**：前端保存 → 后端校验（clamp）→ config.json 持久化 → main.py _cfg() 读取 → checkpoint.py 使用，全链路是否通畅？
8. **边界情况**：空 T 文件、首次对话、压缩冷却期内、token 未超限但消息极多等场景是否正确处理？

## 输出要求
请将审核报告写入指定的 outputFile，格式为 Markdown，按维度分节，每个发现标记 ✅（无问题）/ ⚠️（潜在风险）/ ❌（必须修复）。
