# CHECKPOINT 重构 Stage 8-14 独立 Review

## 任务
对 CHECKPOINT 机制重构的 Stage 8-14 进行独立代码审核，检查实现是否与设计文档一致，是否存在逻辑缺陷或遗漏。

## 审核范围

### 需要审核的源代码文件
1. `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py` — FlashLite 核心引擎
2. `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py` — T 文件管理 + 压缩逻辑
3. `BossLady_Console/backend/routers/models.py` — 后端参数校验

### 需要对照的设计文档（原始需求来源）
1. `QQBotPlan/CHECKPOINT机制讨论记录.md` — 原始讨论和核心设计理念
2. `QQBotPlan/Plan_2_CP.md` — 总纲索引
3. `QQBotPlan/Plan_2_CP_architecture.md` — 三系统分立架构
4. `QQBotPlan/Plan_2_CP_T_file.md` — T 文件结构规范
5. `QQBotPlan/Plan_2_CP_compression.md` — 压缩逻辑规范
6. `QQBotPlan/Plan_2_CP_integration.md` — 集成方案
7. `QQBotPlan/Plan_2_CP_缺漏_P0P1.md` — P0P1 缺漏清单
8. `QQBotPlan/Plan_2_CP_缺漏_P2优化.md` — P2 优化清单

## 各 Stage 审核要点

### Stage 8: P0-1 参数命名统一
- 检查 main.py 中 `checkpoint_limit` 与 `checkpoint_token_limit` 的兼容回退写法
- 确认 config.json、后端 models.py、前端 app.js 参数名统一

### Stage 9: P0-2 清理旧 check_and_compress 调用
- grep main.py 确认已无 `check_and_compress` 调用
- 确认同步触发和私聊触发函数的 FlashLite 判断逻辑完整性

### Stage 10: P0-3 压缩边界 Bug 修复
- 检查 checkpoint.py 中 compress_if_needed 的三重守卫逻辑
- 验证 compress_count 计算：基于原始消息数 vs candidate 长度
- 推演边界用例确认无负索引风险

### Stage 11: P1-1 FlashLite 触发判断上下文切到 T 文件
- 确认 5 个 _get_recent_context 调用点已全部切换为 T 文件方式
- 验证 window_key 格式：GroupMessage:{id} / FriendMessage:{id}
- 检查 build_flashlite_context 的返回类型与 _build_judgment_prompt 的兼容性

### Stage 12: P1-2 LLM 回复后回写 T 文件
- 检查 assistant 回复追加逻辑的位置和去重保护
- 确认不会导致重复追加或遗漏

### Stage 13: P1-3 max_tokens 压缩率硬保证
- 检查 _call_flash_lite 的 max_output_tokens 参数传递链
- 验证 compress_if_needed 中的动态 max_tokens 计算（含 Δ 余量）
- 确认 build_compress_prompt 已去除字数限制描述

### Stage 14: P2 批量优化
- 检查 models.py 中 checkpoint_limit 下界校验和 target_min ≤ target_max 自动交换
- 检查系统认知提示词更新是否准确反映三系统架构
- 检查压缩分割语义完整性保护

## 特别关注
1. **三系统分立**：C系统(T文件) 不影响 A系统(AstrBot messages.db) 和 B系统(FlashLite Knowledge)
2. **压缩流程**：T 文件前部 T' → 压缩为 T'_o → 新上下文 = T'_o + T_original(未压缩部分) → 不影响原始对话记录
3. **并发安全**：T 文件操作是否有竞态风险
4. **兼容性**：旧配置格式能否正常回退
