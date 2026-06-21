# 老板娘 QQ 机器人工程

「老板娘」是一个运行在真实 QQ 群里的傲娇人格 AI 群机器人，基于 **AstrBot + NapCat + Gemini** 架构，并在其上构建了一套「类操作系统级 AI Agent」体系：FlashLite 中断引擎 + 主模型 + 工具模型 三模型协作、CHECKPOINT/record 上下文压缩、Memory/Knowledge 双记忆、Sandbox 工具运行时、统一 Web 控制台。

## 架构

```
QQ → NapCat(OneBot11) → AstrBot(aiocqhttp 平台「老板娘」) → Gemini → 分段回复 + 表情包 → 群
```

定制代码集中在三处（其余为 AstrBot 框架原版 / 市场第三方插件）：
- `AstrBot/data/plugins/astrbot_plugin_flashlite/` — 核心「大脑」（中断引擎 + 三模型 + 上下文压缩 + Memory/Knowledge + Sandbox + 工具集）
- `AstrBot/data/plugins/astrbot_plugin_persistence/` — 全量 QQ 消息持久化
- `AstrBot/astrbot/core/provider/sources/gemini_source.py` — Gemini 显式 KVCache 改造

配套独立服务：
- `BossLady_Console/` — 统一运维控制台（FastAPI :8090）
- `QQAnalysisApp/` — QQ 用户分析独立工具（自建 OneBot 端口 6299，与主 bot 的 6199 **互斥使用**：同一时刻 NapCat 只能反连一个）

## 启动

主力一键启动：**`start_bosslady.bat`**（依次拉起 NapCat → AstrBot → BossLady_Console）。
- AstrBot Dashboard: http://localhost:6185
- NapCat WebUI: http://localhost:6099
- BossLady Console: http://localhost:8090

## 首次配置

敏感配置（API Key / 密码 / jwt_secret）**不入库**，需本地填写：
1. 复制 `AstrBot/data/cmd_config.example.json` → `AstrBot/data/cmd_config.json`
2. 填入 `<GEMINI_API_KEY>`（Google AI Studio 的 key）、dashboard 密码、管理员 QQ 号等占位项
3. 人格 System Prompt 存于 `data_v4.db`（不入库）；可供 RP 参考的提示词留存见 `QQBotPlan/提示词审计/Prompt_主模型*.md`

## AstrBot 框架基线

本仓库整库包含 AstrBot 框架（已删除其嵌套 `.git`，由本仓库统一版本管理）。
- fork upstream: `AstrBotDevs/AstrBot.git`，branch `master`，基线 commit `4d9dce18`
- 如需同步上游框架，按此 hash 关联

## 文档

项目规划与设计文档在 **`QQBotPlan/`**（按 `Plan_1/`~`Plan_5/` 分层 + `INDEX.md` 总索引）。
当前进行中：**Plan_5** 对话机制大升级（record 机制 / BPC 背景预压缩 / 多人并发集体回复，见 `QQBotPlan/Plan_5/`）。

## 不入库内容（本地保留）

`.gitignore` 排除：`venv/`、NapCat 协议端、运行数据（`QQ_data/`、`Memory/`、`Knowledge/`、`Sandbox/`、`*.db`）、cmd_config 真身、`_接班理解/`（接班分析过程文档）、大文件等。

---
> 基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）二次开发。
