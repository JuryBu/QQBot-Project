# 审核报告：QQBotPlan 整体体系审核 (xhigh)

**审核时间**: 2026-04-13
**审核范围**:
- 规划文档：`QQBotPlan/Task.md`、`QQBotPlan/Plan_1_sandbox.md`、`QQBotPlan/Plan_1_memory.md`
- 核心实现：`AstrBot/data/plugins/astrbot_plugin_flashlite/{main.py,sandbox.py,web_engine.py,memory.py}`
- 控制台后端：`BossLady_Console/backend/{main.py,routers/models.py,routers/bot.py,routes/messages.py,routes/system.py}`
**整体评价**: 当前系统在“安全边界与数据隔离”上仍存在体系级缺口，尤其是控制台鉴权、Sandbox 外部隔离、Memory 工作区隔离和文件路径安全，需优先修复。

## 🔴 严重问题（必须修复）

### 问题 1：控制台“已支持密码”的安全假象，实际所有高危 API 无鉴权
- **位置**：
  - `BossLady_Console/backend/main.py:42-73`
  - `BossLady_Console/backend/routes/system.py:358-381`
  - `BossLady_Console/backend/routers/models.py:71-83,125-146,202-252`
  - `BossLady_Console/backend/routers/bot.py:145-205`
  - `BossLady_Console/backend/routes/messages.py:242-266`
- **描述**：
  - 后端仅设置了 CORS，并未注册任何鉴权中间件或 `Depends` 权限校验。
  - `/api/system/password` 仅写入 `password_hash`，但全项目没有读取/校验该字段的逻辑（`password_hash` 仅在 `system.py` 写入/删除）。
  - 结果是：任何可访问本机 8090 端口的调用方都能直接改模型配置、重启进程、清理消息、导入导出数据。
- **修复建议**：
  - 增加统一鉴权中间件（推荐签发短期 session token 或 API token）。
  - 将 `password_hash` 改为“登录校验入口 + 会话态校验”，并在所有 `/api/*` 路由强制校验。
  - 对高危操作（重启、清理、导入）增加二次确认和审计日志。

### 问题 2：Sandbox 边界失效，可读取/执行 Sandbox 外部资源（与设计硬约束冲突）
- **位置**：
  - 设计约束：`QQBotPlan/Plan_1_sandbox.md:60-63,68,77-81`
  - 执行链路：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:641-679,693-708,750-766`
  - 本地文件读取：`AstrBot/data/plugins/astrbot_plugin_flashlite/web_engine.py:246-279,288-293`
  - 工具暴露：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3903-3917`
- **描述**：
  - `sandbox_exec` 的 `code` 模式可执行任意 Python 代码，且解释器可回退到项目/系统 Python，天然可读写 Sandbox 外部文件。
  - `web_fetch` 支持 `file://`，`_fetch_local_file()` 未做“必须位于 Sandbox 内”的路径校验，可读取任意本地文件。
  - 这与 Plan 中“Sandbox 外部绝对禁止 AI 触碰”的硬性要求直接冲突。
- **修复建议**：
  - 对 `sandbox_exec` 增加进程级隔离（容器/受限用户/Job Object + ACL + 禁网）而非仅路径约定。
  - `web_fetch(file://)` 强制 `resolve_path` 到 Sandbox 根下，禁止绝对路径和跨目录解析。
  - 运行时禁止回退系统解释器，只允许 `Sandbox/base_tools/runtimes` 白名单。

### 问题 3：Memory 工作区隔离被绕过，跨窗口读写风险真实存在
- **位置**：
  - 设计要求：`QQBotPlan/Plan_1_memory.md:36-39`
  - 全局召回：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:920-927,1059-1066,1209-1216,1316-1333`
  - 工具读写：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3627,3678,3791`
  - 存储层兜底缺失：`AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py:672-680,733-736,756-764`
- **描述**：
  - 多处逻辑使用 `_get_workspace_entries(None)` 和 `read(id)`，直接跨工作区读取。
  - `memory_update/delete/read` 在未传 workspace 时按 `id` 全局操作，导致“知道 mem_id 即可跨窗口操作”。
  - `search(scope=memory/all)` 默认也未绑定当前窗口工作区。
- **修复建议**：
  - 在工具入口层强制注入当前 `workspace`（群/私聊窗口），默认禁止跨工作区。
  - 存储层将 `workspace` 设为必填校验项；`read/update/delete` 无 workspace 时直接拒绝。
  - 跨工作区检索单独做受控能力（显式 `scope=global` + 权限位 + 审计）。

### 问题 4：表情包管理接口存在路径穿越，可触发越权读写/删除
- **位置**：
  - `BossLady_Console/backend/routers/models.py:616-625,636-648,651-658,673-683`
- **描述**：
  - `filename`/`old_name`/上传文件名直接参与 `EMOJI_DIR / xxx` 拼接，无 `resolve + relative_to` 约束。
  - 在 Windows 下通过反斜杠路径可实现目录穿越，造成任意文件读取、移动、删除或覆盖风险。
- **修复建议**：
  - 对所有文件名参数执行严格白名单（仅允许 `[a-zA-Z0-9 _.-]`，拒绝 `..`、`/`、`\`、盘符）。
  - 拼接后统一 `resolved = path.resolve(); resolved.relative_to(EMOJI_DIR.resolve())` 校验。
  - 上传时使用服务端生成文件名，不信任客户端原始文件名。

## 🟡 建议改进

### 问题 5：Sandbox 限制配置与代码读取结构不一致，部分安全参数实际未生效
- **位置**：
  - 配置：`AstrBot/data/plugins/astrbot_plugin_flashlite/Sandbox/config/limits.json:2-18`
  - 读取：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:588,615-620,631`
- **描述**：
  - `limits.json` 是平铺字段（`max_execution_time_seconds` 等），但代码按 `limits.execution.*` 和 `limits.network.allow_outbound` 读取。
  - 导致实际运行大量回退默认值（如超时、并发、输出限制、网络策略），配置面板认知与真实行为不一致。
- **修复建议**：
  - 统一配置 schema（建议采用嵌套结构）并加启动时 schema 校验。
  - 配置无效时显式告警并拒绝启动，避免“静默降级”。

### 问题 6：FlashLite 配置命名存在新旧键混用，长期维护成本高
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:2`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:116`
  - `BossLady_Console/backend/routers/models.py:159,209-212`
- **描述**：
  - `sync_trigger_interval` 与 `sync_interval` 并行存在；控制台只写旧键，核心逻辑兼容双读。
  - 短期可运行，但会持续制造“字段真实来源不清晰”的排障成本。
- **修复建议**：
  - 做一次配置迁移（读旧写新），并在一个版本窗口后移除旧键写入。
  - 前后端统一展示同一字段名。

## 🟢 微调建议

### 问题 7：安全能力分布在多个模块，缺少统一安全基线自检
- **位置**：`BossLady_Console/backend/main.py`、`AstrBot/data/plugins/astrbot_plugin_flashlite/{sandbox.py,web_engine.py,memory.py}`
- **描述**：当前安全约束由多模块各自实现，缺少启动时集中“安全基线体检”（鉴权、路径、隔离、网络）。
- **修复建议**：增加一个启动自检清单（fail-fast），不满足基线直接拒绝服务。

## ✅ 做得好的地方

- `CHECKPOINT` T 文件链路的并发保护与合并式保存思路较完整（锁 + merge-save），相比早期版本明显增强。
- `sandbox.py` 的路径前缀校验已从简单 `startswith` 改到更严格的规范化路径判断，方向正确。
- `system import` 路径安全（Zip Slip 防护 + 白名单 + resolve 二次校验）实现较扎实。

## 建议修复优先级（执行顺序）

1. 先补控制台鉴权（问题1）和 Emoji 路径安全（问题4）。
2. 同步封堵 Sandbox 外部读取/执行（问题2）。
3. 最后收口 Memory 工作区强隔离和配置 schema 统一（问题3/5/6）。
