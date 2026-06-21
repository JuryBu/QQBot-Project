# QQBotPlan 文档总索引

老板娘 QQBot 项目的全部规划、设计、审查与接班文档索引。Stage0 重构后已按 Plan_N 分层归档，本文件为唯一导航入口。

---

## 一、项目进度速览

| Plan | 主题 | 阶段范围 | 状态 | 总纲入口 |
|------|------|----------|------|----------|
| Plan_1 | 基础架构（三模型 / Sandbox / Memory+Knowledge / Web 控制台） | Stage 1–17 | 已落地 | [Plan_1/Plan_1.md](Plan_1/Plan_1.md) |
| Plan_2 | 问题修复 + CHECKPOINT(CP) 上下文压缩系统 + 提示词重构 | CP 体系 + R1–R14 | 已落地 | [Plan_2/Plan_2.md](Plan_2/Plan_2.md) · [Plan_2/Plan_2_CP.md](Plan_2/Plan_2_CP.md) |
| Plan_3 | KVCache / 成本监控优化（FlashLite 采样 / 主模型 / 工具模型 / 面板） | 17-Stage 体系 | 已落地 | [Plan_3/Plan_3.md](Plan_3/Plan_3.md) |
| Plan_4 | 新 Feature 路线图（多 Feature 调研） | 设计/调研 | 待开发 | [Plan_4/Plan_4.md](Plan_4/Plan_4.md) |
| Plan_5 | 对话机制大升级（上下文压缩升级 / 新机制） | S0–S8 | 设计定稿 / S0 进行中 | [Plan_5/Plan_5.md](Plan_5/Plan_5.md) |

---

## 二、分层导航

### Plan_1 — 基础架构（19 文件）
- 总纲：`Plan_1/Plan_1.md`
- 专题设计（7）：`Plan_1_architecture.md` / `Plan_1_data.md` / `Plan_1_gaps.md` / `Plan_1_memory.md` / `Plan_1_models.md` / `Plan_1_sandbox.md` / `Plan_1_webui.md`
- 缺漏补充（4）：`Plan_1_缺漏_1.md` + `Plan_1_缺漏_1_测试.md` / `Plan_1_缺漏_2.md` + `Plan_1_缺漏_2_测试.md`
- Task：`Task.md`（Plan_1 执行清单，Stage 1–17）
- 测试文档（4）：`Test_Stage3_multimodal.md` / `Test_Stage4_persistence.md` / `Test_Stage5_flashlite.md` / `Test_Stage6_8_integration.md`
- 测试脚本（2）：`test_codex_fixes.py`（Codex Stage 7–10 修复验证） / `test_stage13_e2e.py`（覆盖 Stage 1–12 全链路 e2e）

### Plan_2 — 问题修复 + CHECKPOINT 压缩（31 文件）
- 主线（5）：`Plan_2.md` / `Plan_2_1.md` / `Plan_2_2.md` / `Plan_2_2_ImplPlan.md` / `Plan_2_2_Task.md`
- CHECKPOINT 体系（8）：`Plan_2_CP.md` / `Plan_2_CP_architecture.md` / `Plan_2_CP_T_file.md` / `Plan_2_CP_compression.md` / `Plan_2_CP_integration.md` / `Plan_2_CP_缺漏_P0P1.md` / `Plan_2_CP_缺漏_P2优化.md` / `Plan_2_CP_P2_3_并发安全.md`
- Task（2）：`Task_2_CP.md` / `Task_2_14.md`
- Report（14）：`Report_2_1.md` ~ `Report_2_14.md`
- 讨论记录（2）：`CHECKPOINT机制讨论记录.md` / `Prompt注入讨论记录.md`

### Plan_3 — KVCache / 成本优化（20 文件）
- 总纲：`Plan_3/Plan_3.md`
- 专题设计（4）：`Plan_3_1_FlashLite采样优化.md` / `Plan_3_2_KVCache优化.md` / `Plan_3_3_工具模型KVCache.md` / `Plan_3_4_面板与成本监控.md`
- 讨论原文：`Plan_3系列讨论原始内容.md`
- Task（2）：`Task_3.md` / `Task_对照.md`
- Report（5）：`Report_3_final.md` / `Report_3_Plan3_对照审核.md` / `Report_3_QQBotPlan_整体体系审核.md` / `Report_3_R1_R6_代码修改审核.md` / `Report_3_缺漏_面板链路审计.md`
- 测试脚本（5）：`test_stage1_flashlite.py` / `test_stage3_main_model.py` / `test_stage6_kvcache_all.py` / `test_stage7_9_sampling.py` / `test_stage11_cost_tracker.py`（Stage 编号属 Task_3 的 17-Stage 体系）
- Stage5_6 审核对（2）：`Codex_Review_Stage5_6.md` / `Review_Stage5_6.md`（审的是 Report_3 的 Stage5 存储费 / Stage6 Knowledge T 文件，归 Plan_3）

### Plan_4 — 新 Feature 路线图（5 文件）
- 总纲：`Plan_4/Plan_4.md`
- 调研/讨论（4）：`Plan_4_Feature1_讨论问题.md` / `Plan_4_Feature1_讨论回答记录.md` / `Plan_4_Feature4_图像搜索调研.md` / `Plan_4系列讨论原始记录.md`

### Plan_5 — 对话机制大升级（4 文件）
- 总纲：`Plan_5/Plan_5.md`
- 设计/讨论（2）：`Plan_5_上下文压缩升级_讨论记录.md` / `Plan_5_新机制设计_主人原文备份.md`
- Task：`Task_5.md`

---

## 三、横切文档

### CodexReview/（57 篇，原位保留）
Codex 模型对各 Plan 代码与设计的独立审查报告。命名规约：
- `报告_xxx_Codex.md` — 正式版审查报告
- `R_xxx.md` / `Review_xxx.md` / `codex_review_xxx.md` — 过程稿 / 不同 effort（high / xhigh）档位的中间产物
- `Codex_xxx.md` / `R1_xxx.md` — 专项/复审稿

### 提示词审计/（7 篇，原位保留）
三模型提示词全文 + 总览 + 修改 Task。**这是可入库的 RP 人格留存载体**（运行态人格在 data_v4.db，已 gitignore 不入库）：
`00_总览.md` / `Prompt_主模型.md` / `Prompt_主模型_Part2.md` / `Prompt_FlashLite_判断.md` / `Prompt_FlashLite_压缩.md` / `Prompt_工具模型.md` / `Task_提示词修改.md`

### 辅助/（6 篇）
跨 Plan 通用的外部经验与参考材料：
- `Suggestion_Kaleidoscope_1/2/3.md` — 外部 Kaleidoscope 项目经验
- `初始讨论记录副本.md` — 项目最早讨论
- `参考材料_Gemini_API_定价表.md` / `参考材料_Gemini_Context_Caching_API.md` — Gemini API 参考（Plan_1/3 都引用）

### _接班理解/（接班心智模型，原位保留，下划线置顶）
给下一个接班窗口的全景理解文档：
- 顶层全景（00–06）：`00_综合全景.md` / `01_Plan_1 体系...` / `02_Plan_2 体系...` / `03_Plan_3+Plan_4+提示词审计...` / `04_AstrBot 定制代码...` / `05_配套应用...` / `06_运行时真相...`
- `stage0_整理准备.md` — S0 整理总方案
- 子目录：`Plan5_调研/`（7）/ `Plan5找茬/`（6）/ `体系bug审查/`（5）/ `S0准备/`（S0 盘点过程报告，含本次重构映射）/ `对话导出/`（接班窗口完整对话历史快照）

> 注：`_接班理解/对话导出/` 含完整 API Key + 明文密码，已列入 `.gitignore` 不入库。

---

## 四、Stage0 重构备注

- **重构日期**：2026-06-21（Plan_5 S0 文档整理）
- **重构动作**：原 QQBotPlan/ 顶层 85 个平铺文档按所属 Plan 分层归入 `Plan_1/`~`Plan_5/` 与 `辅助/`；`CodexReview/`、`提示词审计/`、`_接班理解/` 原位保留。
- **引用已批量更新**：CodexReview 57 篇 + _接班理解 导航报告（01–05）+ Plan 互引中所有跨子目录的 `QQBotPlan/旧文件` 路径已更新为 `QQBotPlan/Plan_N/新路径`。同一子目录内的互引保持无前缀（相对引用），不改动。
- **死链顺手修正**：CodexReview 内若干 `QQBotPlan/报告_xxx_Codex.md` 绝对路径死链（报告实际已在 CodexReview/）已补全为 `QQBotPlan/CodexReview/报告_xxx.md`。
- **保真不改的区域**：
  - `_接班理解/对话导出/`（历史对话快照，gitignore 不入库，保持一字不改）
  - `_接班理解/S0准备/`（S0 盘点过程报告，其中的 `QQBotPlan/旧路径` 是「重构前状态描述 + 行号定位脱敏盘点」，改之破坏记录语义，故保留原样）
  - `Plan_1/` 下 `Plan_1.md` / `Plan_1_data.md` / `Plan_1_gaps.md` / `Plan_1_architecture.md` 内的目录树 ASCII 示意图（结构图非真链接，未做前缀替换）
- **归属修正（已据文件头注核实）**：
  - `test_codex_fixes.py` / `test_stage13_e2e.py` → Plan_1（覆盖 Plan_1 体系 Stage 测试，非辅助/Plan_3）
  - `Codex_Review_Stage5_6.md` / `Review_Stage5_6.md` → Plan_3（审 Report_3 Stage5/6，非 Plan_2）
  - `test_stage1/3/6/7_9/11_*.py` → Plan_3（Stage 编号属 Task_3 的 17-Stage 体系）
