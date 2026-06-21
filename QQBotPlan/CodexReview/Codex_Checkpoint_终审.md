# Codex Review 任务：CHECKPOINT 机制终审

## 审核目标
对 CHECKPOINT（检查点/上下文压缩）机制进行最终全面审核，确认所有设计文档中的要求已在代码中正确实现，没有遗漏或偏差。

## 审核范围

### 设计文档（必须逐一核对）
- `QQBotPlan/Plan_2/CHECKPOINT机制讨论记录.md` — 原始设计讨论，包含所有需求和决策
- `QQBotPlan/Plan_2/Plan_2_CP.md` — CHECKPOINT 总纲
- `QQBotPlan/Plan_2/Plan_2_CP_architecture.md` — 三系统分立架构
- `QQBotPlan/Plan_2/Plan_2_CP_T_file.md` — T 文件机制
- `QQBotPlan/Plan_2/Plan_2_CP_compression.md` — 压缩机制
- `QQBotPlan/Plan_2/Plan_2_CP_integration.md` — 集成方案
- `QQBotPlan/Plan_2/Plan_2_CP_缺漏_P0P1.md` — P0/P1 缺漏修复
- `QQBotPlan/Plan_2/Plan_2_CP_缺漏_P2优化.md` — P2 优化项
- `QQBotPlan/Plan_2/Plan_2_CP_P2_3_并发安全.md` — 并发安全修复

### 代码文件
- `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py` — CHECKPOINT 核心实现
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py` — FlashLite 引擎主文件（T文件集成、压缩调用、上下文替换等）
- `BossLady_Console/` 下的相关面板参数配置

### 提示词审计文档（辅助参考）
- `QQBotPlan/提示词审计/Prompt_FlashLite_压缩.md` — 压缩模式完整提示词
- `QQBotPlan/提示词审计/Prompt_FlashLite_判断.md` — 判断模式完整提示词

## 审核要求

1. **设计-实现一致性**：逐条核对设计文档中的每个要求，确认代码是否已实现
2. **并发安全**：验证 `_compressing` 互斥集合、锁内合并式 Save、冷却期等机制
3. **数据完整性**：验证 T 文件读写的原子性，消息不丢失、不重复
4. **压缩质量**：验证压缩 Prompt、压缩率验证、maxOutputTokens 动态计算
5. **面板参数**：验证 checkpoint_token_limit、cooldown_seconds、压缩率等参数的前后端联动
6. **边界条件**：空 T 文件、首次压缩、多窗口并发、压缩中新消息到达等场景
7. **回归风险**：确认 CHECKPOINT 引入不影响原有的 Knowledge、FlashLite 判断、主模型调用等机制

## 输出格式

请输出结构化审核报告，按以下格式：
- 每个设计文档的核对结果（已实现/未实现/部分实现 + 具体说明）
- 发现的问题按严重程度分级（Critical/High/Medium/Low）
- 每个问题提供文件名、行号、问题描述、建议修复方案
