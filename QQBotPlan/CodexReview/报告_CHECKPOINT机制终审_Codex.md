# CHECKPOINT 机制终审报告（Codex）

**审核时间**: 2026-04-11  
**审核范围**: `QQBotPlan/Plan_2_CP*`、`QQBotPlan/CHECKPOINT机制讨论记录.md`、`AstrBot/data/plugins/astrbot_plugin_flashlite/{checkpoint.py,main.py,agent.py}`、`BossLady_Console` 前后端参数链路  
**整体结论**: **核心 CHECKPOINT 重构已落地（T 文件三系统分立、压缩主链路、面板参数链路基本完成），但在“同窗口并发数据完整性”和“严格压缩率保证”上仍有关键缺口，当前结论为“可用但未达到终审关闭标准”。**

---

## 一、设计文档逐项核对

| 设计文档 | 核对结论 | 说明 |
|---|---|---|
| `QQBotPlan/CHECKPOINT机制讨论记录.md` | **部分实现** | 三系统分立与 T 文件主链路已实现；但“消息不丢失/不重复”的并发场景仍有缺口（见 High-1/High-2）。 |
| `QQBotPlan/Plan_2_CP.md` | **部分实现** | 决策 1~8、10 基本落地；决策 9“压缩率严格保证”仍为软校验（见 Medium-2）。 |
| `QQBotPlan/Plan_2_CP_architecture.md` | **已实现** | FlashLite/主模型上下文均切到 T 文件；`req.contexts` 在 `on_llm_request` 被替换。 |
| `QQBotPlan/Plan_2_CP_T_file.md` | **部分实现** | T 文件结构、原子写入、per-window lock 已实现；但增量提取未做内容对齐/指纹兜底。 |
| `QQBotPlan/Plan_2_CP_compression.md` | **部分实现** | 三重守卫、前比例压缩、动态 `maxOutputTokens` 已实现；下限/区间仍非硬约束。 |
| `QQBotPlan/Plan_2_CP_integration.md` | **部分实现** | `on_llm_request` 替换上下文、触发判断改读 T 文件已实现；“回复后回写 T”仍是启发式补录，鲁棒性不足。 |
| `QQBotPlan/Plan_2_CP_缺漏_P0P1.md` | **部分实现** | P0 关键项已基本处理（参数命名兼容、旧调用移除、边界切割修复）；P1 数据完整性项未彻底收口。 |
| `QQBotPlan/Plan_2_CP_缺漏_P2优化.md` | **部分实现** | 并发合并式 Save、参数校验等已实现；增量提取健壮化与部分清理项未完成。 |
| `QQBotPlan/Plan_2_CP_P2_3_并发安全.md` | **部分实现** | “压缩期间中间消息合并”已实现；但“同窗口并发进入 load→extract→append”仍可重复追加。 |

---

## 二、问题分级（Critical/High/Medium/Low）

## Critical

- 本轮未发现可直接导致服务不可启动或必现崩溃的 Critical 问题。

## High

### High-1：同窗口并发请求可导致 T 文件消息重复追加（破坏“不重复”）
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2667`、`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2670`、`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:351`
- **描述**：`on_llm_request` 先 `load` 再 `_extract_new_messages`，提取结果基于旧快照；并发请求会拿到同一增量，随后 `append_messages` 在锁内只做 append 不做幂等检查，导致重复写入。
- **复现实验结果**：并发两次 append 相同增量后 `final_len=14`（预期 12），尾部出现 `m10,m11,m10,m11` 重复。
- **修复建议**：
  1. 用 window 级总锁包裹 `load → extract → append → compress → build_contexts` 整个事务链。  
  2. 或在 `append_messages` 增加尾部指纹去重（`role+content+tool_call_id+timestamp`）作为二次防线。

### High-2：增量提取仅靠长度差，history 截断/重置后可永久漏记新消息（破坏“不丢失”）
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3026`
- **描述**：`_extract_new_messages()` 仅在 `len(contexts) > processed_count` 时返回增量；一旦外部截断导致 `len(contexts) < processed_count`，函数持续返回空，后续新消息在较长时间内无法写入 T 文件。
- **修复建议**：
  1. 增加“降级对齐”分支：当 `len(contexts) < processed_count` 时基于尾部内容/指纹重同步。  
  2. 为消息写入指纹并维护最近 N 条索引，按指纹定位增量而非纯计数。

## Medium

### Medium-1：`_compressing` 无全链路 `finally` 保护，异常路径可能残留互斥标记
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:574`、`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:757`、`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:760`
- **描述**：窗口压缩标记在多处分支手动 `discard`，但缺少“从 add 到 return 全流程 finally”；若在未覆盖路径抛异常（如保存阶段 I/O 异常），该窗口后续会长期被判定为“压缩中”。
- **修复建议**：将 `self._compressing.add(window_key)` 到函数结束包进 `try/finally`，在 finally 中统一 `discard`。

### Medium-2：压缩率区间仍是软约束，未达到“严格区间保证”
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:629`、`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:655`
- **描述**：虽已动态计算 `maxOutputTokens`，但上限采用 `target_max + Δ`，且越界仅 warning 不阻断提交；下限同样仅告警，不重试/不回滚。
- **修复建议**：
  1. 明确“硬约束定义”（例如允许 `target_max + ε`）。  
  2. 对越界结果至少做一次自适应重试（调低/调高 max tokens）或拒绝覆盖 T1。

### Medium-3：CHECKPOINT 回归测试已失效，无法覆盖当前提示词策略
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py:279`、`AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py:286`
- **描述**：测试仍断言旧版 prompt 中的字数区间文本（`1500/3000`），与当前“去字数硬约束 + API 上限控制”实现不一致，执行失败。
- **修复建议**：更新测试断言到现行策略（检查“输出要求/maxOutputTokens 调用路径”），并将 emoji 打印改为兼容控制台编码。

## Low

### Low-1：遗留注释/兼容桩与现实现状态不完全一致
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py:6`、`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2259`、`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1239`
- **描述**：`agent.py` 顶部公式仍写“CHECKPOINT 摘要 + 最近消息”，`_get_recent_context()` 已基本废弃但仍保留，system 文案仍提 `CheckpointManager` 自动触发。
- **修复建议**：统一文案与死代码清理，减少后续维护误导。

---

## 三、针对任务审核要求的结论

| 审核要求 | 结论 | 说明 |
|---|---|---|
| 设计-实现一致性 | **部分通过** | 主架构已落地，细节收口不足（并发增量与严格压缩率）。 |
| 并发安全 | **部分通过** | `_compressing`、合并式 Save 已有，但同窗口并发追加仍可重复。 |
| 数据完整性 | **未通过** | 当前仍存在“重复追加”和“截断后漏记”两类风险。 |
| 压缩质量 | **部分通过** | Prompt + 动态 max tokens + 比率校验已实现，但区间非硬保证。 |
| 面板参数联动 | **通过** | 前端 `app.js`、后端 `models.py`、`config.json` 与 `main.py` 已打通（含兼容旧 key）。 |
| 边界条件 | **部分通过** | 空 T/首次压缩/压缩中新消息合并已覆盖；同窗口并发一致性未闭环。 |
| 回归风险 | **中等** | Knowledge/触发主链路未见回退，但自动化测试未跟进当前策略。 |

---

## 四、验证记录

- `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py AstrBot/data/plugins/astrbot_plugin_flashlite/main.py BossLady_Console/backend/routers/models.py`：**通过**。  
- `python AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py`：**失败**（旧断言与新 prompt 策略不一致）。

---

## 五、建议收口顺序

1. 先修 High-1/High-2（事务级并发与增量重同步），把“消息不丢失不重复”闭环。  
2. 再修 Medium-1（`_compressing` finally 化），避免异常后窗口长期卡死。  
3. 最后收口 Medium-2/Medium-3/Low-1（压缩率硬保证策略与测试、文案一致性）。
