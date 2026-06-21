# 审核报告：全面差异审计

**审核时间**: 2026-04-02  
**审核范围**: `BossLady_Console/`, `QQAnalysisApp/`, `AstrBot/data/`, `NapCat_v4.17.53/config/`, `QQ_data/`, `Memory/`, `Knowledge/`, `export/`, 根目录启动脚本与仓库产物  
**整体评价**: 当前差异不是常规补丁，而是一次 3.14 GB / 71,002 文件的整目录引入；其中同时混入了可远程调用的无鉴权服务、真实凭证与用户数据，以及至少一处任意文件写入漏洞，现状不适合直接提交、共享或发布。

## 差异概览

| 项目 | 结论 |
|------|------|
| Git 视角 | `AstrBotLauncher-0.1.5.6/` 基本整体新增，缺少可审的历史提交级差分 |
| 差异体量 | 约 `3137.91 MB`，`71002` 个文件 |
| 高风险区域 | `QQAnalysisApp/`、`BossLady_Console/`、`AstrBot/data/cmd_config.json`、`NapCat_v4.17.53/config/webui.json` |
| 明显污染项 | `.venv/`、`venv/`、安装包、可执行文件、IDE 配置、日志、数据库、导出 zip |

## 🔴 严重问题（必须修复）

### 问题 1：`QQAnalysisApp` 默认对外暴露完整管理面，且无任何鉴权
- **位置**：`QQAnalysisApp/backend/main.py:33-39`, `QQAnalysisApp/backend/main.py:123-157`, `QQAnalysisApp/backend/main.py:224-267`, `QQAnalysisApp/backend/main.py:274-286`, `QQAnalysisApp/backend/main.py:489-560`, `QQAnalysisApp/backend/main.py:560-561`
- **描述**：服务开启 `allow_origins=["*"]`，绑定 `0.0.0.0:8000`，同时暴露启动/停止 NapCat、导出用户数据、撤回消息、发送消息、保存 LLM 配置等接口，未见任何登录态、令牌、来源校验或权限分层。任何能访问该端口的人都可以直接操作 QQ 机器人和读取分析数据。
- **修复建议**：默认仅绑定 `127.0.0.1`；删除 `allow_origins=["*"]`；为所有 `/api/*` 引入统一鉴权；至少将“发送消息 / 撤回 / 启停 / 导出”设为受保护操作。

### 问题 2：`QQAnalysisApp` 的 OneBot WebSocket 服务同样对外开放，任何客户端都可伪装为上游
- **位置**：`QQAnalysisApp/backend/onebot_server.py:48`, `QQAnalysisApp/backend/onebot_server.py:91-97`, `QQAnalysisApp/backend/onebot_server.py:110-124`
- **描述**：内部 `FastAPI` 对 `/{path:path}` 接受任意 WebSocket 连接，并默认监听 `0.0.0.0:6199`。代码中没有 token、secret、签名或来源校验。攻击者可以伪造事件、劫持消息流，甚至影响后续 API 回包逻辑。
- **修复建议**：仅监听 `127.0.0.1`；限定固定路径如 `/ws`；强制校验 OneBot access token / shared secret；拒绝未授权连接并记录审计日志。

### 问题 3：`BossLady_Console` 的导入接口存在 Zip Slip，可写出工作目录外任意文件
- **位置**：`BossLady_Console/backend/routes/system.py:232-275`
- **描述**：`import_data()` 直接把 zip 内 `member` 拼到 `BASE_DIR / member`，没有做 `resolve()` + 根目录约束，也没有拒绝绝对路径、盘符路径或 `..`。构造恶意 zip 后可覆盖项目外文件。
- **修复建议**：解包前对每个成员做 `target = (BASE_DIR / member).resolve()`，并强制 `target` 落在允许根目录内；拒绝绝对路径、盘符路径、`..`；最好只允许白名单顶层目录如 `QQ_data/`, `Memory/`, `Sandbox/`。

### 问题 4：仓库差异中直接包含真实凭证、控制台密钥和用户数据
- **位置**：`AstrBot/data/cmd_config.json:45-57`, `AstrBot/data/cmd_config.json:68-79`, `AstrBot/data/cmd_config.json:220-225`, `NapCat_v4.17.53/config/webui.json:2-6`, `NapCat_v4.17.53/config/webui.json:252-256`, `QQAnalysisApp/backend/debug.log:26`, `QQAnalysisApp/backend/debug.log:30`, `QQ_data/messages.db`, `Memory/memory.db`, `Knowledge/knowledge_cache.json`, `export/*.zip`
- **描述**：当前差异包含真实 Gemini API Key、AstrBot dashboard 的 `jwt_secret` 与密码哈希、NapCat WebUI token、自动登录账号、历史日志中的完整 WebUI URL，以及聊天库/记忆库/导出包。再结合 `NapCat_v4.17.53/config/webui.json` 的 `host="::"` 和 `accessControlMode="none"`，这已经不是“测试数据”，而是实质性的凭证与隐私泄露。
- **修复建议**：立刻轮换 API Key、JWT secret、NapCat token；移除并重写所有敏感配置与日志；把运行态数据目录、DB、导出包、日志、安装包和可执行文件从版本差异中剥离；新增项目根 `.gitignore` 或发布白名单。

### 问题 5：BossLady 控制台的“主模型配置”写入旧字段，页面修改不会驱动真实运行时
- **位置**：`BossLady_Console/backend/routers/models.py:90-134`, `AstrBot/data/cmd_config.json:68-79`, `AstrBot/astrbot/core/utils/migra_helper.py:99-109`, `AstrBot/astrbot/core/provider/manager.py:475-496`, `AstrBot/astrbot/core/provider/sources/openai_source.py:395-396`
- **描述**：控制台读取和写入的是 `provider[*].model_config`，但 AstrBot 迁移逻辑已经把真实模型字段提升到 `provider[*].model`，其余参数移入 `custom_extra_body`。运行时 provider 也是直接取合并后配置里的顶层 `model`。当前实际配置里 `provider.model = gemini-2.5-flash`，而 `model_config.model = gemini-flash-latest` 已经互相矛盾，说明 UI 展示和实际生效值已脱节。
- **修复建议**：控制台统一读写 AstrBot v4 的真实字段：`provider.model`、`provider.custom_extra_body` 以及 `provider_sources`；不要再把 `model_config` 作为权威数据源。

### 问题 6：控制台“密码保护”是伪功能，写了 hash 但整个后端没有任何校验链路
- **位置**：`BossLady_Console/frontend/app.js:866-885`, `BossLady_Console/backend/routes/system.py:305-325`, `BossLady_Console/backend/main.py:42-72`
- **描述**：前端允许用户设置“控制台密码”，后端也会把 `password_hash` 写到 `BossLady_Console/config.json`，但路由注册时没有任何鉴权依赖、中间件或登录流程。也就是说用户会误以为控制台受密码保护，实际所有接口仍然可直接访问。
- **修复建议**：如果保留该设置，必须补完整的登录/会话/JWT 校验；否则应立即移除 UI 与接口，避免制造错误安全预期。

## 🟡 建议改进

### 问题 7：`QQAnalysisApp` 写死绝对路径，项目一旦换目录或换机器就会直接失效
- **位置**：`QQAnalysisApp/backend/napcat_manager.py:16-19`
- **描述**：NapCat 目录、配置目录都硬编码到当前机器的桌面绝对路径。README 又把这个应用描述为可直接启动的本地工具，这与实际可移植性冲突。
- **修复建议**：改为相对项目根目录发现；必要时允许环境变量或配置文件覆盖路径。

### 问题 8：差异集中混入大量环境产物和二进制，导致审计与交付都失去边界
- **位置**：`AstrBot/.venv/`, `AstrBot_old/venv/`, `QQAnalysisApp/backend/venv/`, `venv/`, `NapCat.Shell.Windows.OneKey/`, `QQ9.9.26.44343_x64.exe`, `NapCat_v4.17.53_Shell.zip`, `.idea/`, `.vscode/`
- **描述**：当前新增目录约 3.14 GB / 71,002 文件，其中仅 `NapCat.Shell.Windows.OneKey` 就约 1.59 GB，`AstrBot/.venv` 与 `AstrBot_old/venv` 合计约 910 MB。这样的差异几乎不可评审，也会把构建环境、IDE 状态和第三方安装包永久耦合进版本历史。
- **修复建议**：把“源码/配置模板”和“运行时/安装包/缓存/日志/数据库”彻底拆开；在项目根补 `.gitignore`；为 NapCat、QQ、Python 环境改用下载脚本或安装说明，而不是直接入差异。

## 🟢 微调建议

### 问题 9：`start_app.bat` 与旧服务仍保留在主项目入口层，容易让使用者误启动到高风险旧实现
- **位置**：`start_app.bat:13-19`, `QQAnalysisApp/README.txt`
- **描述**：根目录同时存在 `start_app.bat` 与 `start_bosslady.bat`，且旧服务 README 仍面向终端用户。对于当前项目而言，这会把未收口的旧实现继续暴露出去。
- **修复建议**：明确区分“当前入口”和“废弃入口”；至少在根目录 README 里标记 `QQAnalysisApp` 为 legacy，并默认隐藏或移除启动脚本。

## ✅ 做得好的地方

- `start_bosslady.bat:74` 已把 BossLady 控制台收口到 `127.0.0.1:8090`，比早期默认外网监听安全得多。
- `BossLady_Console/backend/routers/bot.py:61-67` 与 `BossLady_Console/backend/routers/bot.py:85-101` 已不再向前端直接下发 NapCat 原始 token，只返回脱敏信息。
- `BossLady_Console/backend/routes/messages.py:17-29` 已兼容 `QQ_data/messages.db` 与 `AstrBot/data/data_v4.db` 的候选路径，说明部分旧桥接问题已有修补。
