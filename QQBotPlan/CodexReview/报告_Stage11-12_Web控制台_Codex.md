# 审核报告：Stage 11-12 Web 控制台

**审核时间**: 2026-04-02
**审核范围**: `BossLady_Console/**/*`、`start_bosslady.bat`，以及用于核对真实结构的 `AstrBot/data/cmd_config.json`、`AstrBot/data/data_v4.db`、`AstrBot/data/plugins/astrbot_plugin_flashlite/*`、`AstrBot/astrbot/core/persona_mgr.py`
**整体评价**: 当前控制台已经具备页面与路由骨架，但 Stage 11/12 被标记为“完成”的多项能力在当前仓库中实际上不可用，且存在未鉴权暴露敏感接口的安全问题。

## 🔴 严重问题（必须修复）

### 问题 1：控制台默认对外暴露且完全未鉴权，敏感写接口和 NapCat Token 可被直接访问
- **位置**：`BossLady_Console/backend/main.py:50-55`、`BossLady_Console/backend/main.py:102-103`、`start_bosslady.bat:27-30`、`BossLady_Console/backend/routers/bot.py:73-89`
- **描述**：后端直接监听 `0.0.0.0:8090`，同时没有任何登录态、中间件或权限校验；现有路由里已经包含配置写入、记忆增删改、消息清理、人格修改、数据导出等写操作接口。更严重的是 `/api/bot/napcat/webui-url` 会把 NapCat 的原始 token 直接返回给前端。只要端口暴露到局域网，其他机器就能直接拿到管理权限。
- **修复建议**：默认只绑定 `127.0.0.1`；在所有 `/api/*` 之前增加鉴权层；把变更类接口放到受保护路由；不要向浏览器回传 NapCat 原始 token，改为后端代理或短时一次性凭证。

### 问题 2：消息浏览器、仪表盘统计、系统导出都还在读取旧版 `QQ_data/messages.db`，Stage 12 核心数据页在当前仓库里不可用
- **位置**：`BossLady_Console/backend/routers/dashboard.py:13-16`、`BossLady_Console/backend/routers/dashboard.py:62-83`、`BossLady_Console/backend/routes/messages.py:16-18`、`BossLady_Console/backend/routes/messages.py:35-42`、`BossLady_Console/backend/routes/messages.py:86-99`、`BossLady_Console/backend/routes/system.py:115-125`、`BossLady_Console/backend/routes/system.py:138-151`
- **描述**：代码把消息持久化源硬编码为 `QQ_data/messages.db`，并分别假定存在 `messages` / `qq_messages` 表。但当前仓库并不存在 `QQ_data` 目录，真实消息数据在 `AstrBot/data/data_v4.db`，表名是 `platform_message_history`。我直接调用 `message_stats()` 与 `list_windows()` 时，前者只返回 `{"total":0}`，后者直接报错，说明这不是“缺测试数据”，而是 schema 假设已经脱离实际项目结构。
- **修复建议**：抽出统一的 `astrbot_bridge` / 数据访问层，优先适配 `data_v4.db`，必要时兼容旧表；不要在多个路由各自硬编码路径和表结构；导出逻辑也应跟随统一数据源，否则会漏掉真实消息数据。

### 问题 3：模型配置页读取的是过时的 `cmd_config.json` 结构，Flash Lite 配置还写到了一个运行时根本不会读取的文件
- **位置**：`BossLady_Console/backend/routers/models.py:13-15`、`BossLady_Console/backend/routers/models.py:45-63`、`BossLady_Console/backend/routers/models.py:90-134`、`BossLady_Console/backend/routers/models.py:141-179`、`BossLady_Console/backend/routers/models.py:186-221`
- **描述**：当前实现假定 API Key 在 `provider[*].key`、主模型参数在 `provider[*].model_config`，但真实 `cmd_config.json` 已经是 `provider_sources + provider + provider_settings` 三段式结构，Key 存在 `provider_sources[*].key`，模型在 `provider[*].model`。我直接调用接口时，`/api/models/api-key` 返回空 key，`/api/models/main-model` 返回空 model，`/api/models/available` 直接报“未配置 API Key”。此外，Flash Lite 路由读写的是 `AstrBot/data/plugins/astrbot_plugin_flashlite/config.json`，而插件真实运行时是通过 `_conf_schema.json` + AstrBot 插件配置注入的，`main.py` 读取的字段也是 `sync_trigger_interval`、`thinking_level`、`checkpoint_token_limit` 等，不是当前路由使用的 `sync_interval`、`thinkingBudget`、`checkpoint_limit`。
- **修复建议**：按当前 AstrBot v4 配置 schema 重写模型路由；主模型配置应修改 `provider` / `provider_sources` 的真实字段；Flash Lite 配置要走 AstrBot 插件配置源，而不是私自创建一个 `config.json`；保存后应补充一次端到端回读验证。

### 问题 4：Memory / Knowledge 接口在独立启动控制台时会导入失败，Stage 12 的两个页面实际上无法工作
- **位置**：`BossLady_Console/backend/routes/data.py:21-24`、`BossLady_Console/backend/routes/data.py:31-49`、`BossLady_Console/backend/routes/data.py:52-63`、`BossLady_Console/backend/routes/data.py:118-132`
- **描述**：这里仅把插件目录塞进了 `sys.path`，然后直接 `import memory` / `import knowledge`。但这两个模块内部都依赖 `astrbot.api`，而控制台启动命令是在 `BossLady_Console` 目录下执行的，`AstrBot` 包路径根本不在 `sys.path`。我直接调用 `list_memories()`、`read_memory()`、`get_knowledge()`，全部返回 `No module named 'astrbot'`。也就是说页面不是“数据为空”，而是后端接口本身会炸。
- **修复建议**：至少把 `PROJECT_ROOT / "AstrBot"` 加入 Python 路径，确保 `astrbot` 包可导入；更稳妥的做法是为控制台提供独立 service 层，不要直接依赖插件模块的运行环境副作用。

### 问题 5：NapCat 的发现与启动逻辑互相不一致，当前仓库下基本拿不到真实 WebUI
- **位置**：`start_bosslady.bat:9-16`、`BossLady_Console/backend/routers/bot.py:15-19`、`BossLady_Console/backend/main.py:24-28`
- **描述**：启动脚本只会在根目录寻找 `NapCat*\\napcat.bat`，但当前仓库里真正的 `napcat.bat` 位于 `NapCat.Shell.Windows.OneKey\\...\\napcat.bat` 的嵌套目录中，因此这段逻辑会直接跳过 NapCat 启动。后端 `_find_napcat_dir()` 又只是“取第一个以 NapCat 开头的目录”，在当前仓库里实际命中的是 `NapCat.Shell.Windows.OneKey`，而不是带 `config/webui.json` 的 `NapCat_v4.17.53`。我直接调用 `napcat_status()` 时拿到的就是错误目录，`get_napcat_webui_url()` 则直接 404。
- **修复建议**：统一 NapCat 路径发现策略，优先选择同时具备运行脚本和 `config/webui.json` 的目录；如果存在多个候选目录，应显式配置或在 UI 中要求用户确认，而不是依赖 `iterdir()` 的随机顺序。

### 问题 6：人格设定页读取了错误的数据结构，保存时写入的字段也不是 AstrBot 实际使用的来源
- **位置**：`BossLady_Console/backend/routes/system.py:62-69`、`BossLady_Console/backend/routes/system.py:79-95`
- **描述**：读取逻辑把 `cmd_config.json` 的 `persona` 当作字典处理，但当前配置里它是列表；真实生效的人格来源是 `provider_settings.default_personality` 对应的 Persona 记录，而 Persona 内容保存在数据库中，由 `persona_mgr` 读取 `personas.system_prompt`。我直接调用 `get_persona()` 得到的错误就是 `'list' object has no attribute 'get'`。与此同时，保存逻辑写入的是 `provider_settings.persona_prompt`，但当前 AstrBot 代码使用的是 `default_personality` + Persona DB，这个字段不会让当前人格真正变更。
- **修复建议**：不要自己发明 Persona 配置格式，直接复用 AstrBot 已有的人格管理接口或数据库访问层；读取时先解析 `default_personality`，再定位对应 Persona；保存时更新 `personas.system_prompt`，必要时同步默认人格选择。

### 问题 7：`/api/data/sandbox/tree` 的路径校验退回成了前缀匹配，存在同前缀兄弟目录逃逸风险
- **位置**：`BossLady_Console/backend/routes/data.py:143-149`
- **描述**：这里的安全检查使用的是 `str(target.resolve()).startswith(str(SANDBOX_ROOT.resolve()))`。如果外部传入 `..\\Sandbox_evil` 这类路径，并且宿主机上存在同前缀兄弟目录，就会被误判为位于 `Sandbox` 内。`sandbox/file` 已经在 `181` 行补了 `+ os.sep`，但 `sandbox/tree` 这里又回到了旧写法，属于已知安全问题回归。
- **修复建议**：改用 `Path.resolve().is_relative_to(SANDBOX_ROOT.resolve())`；如果要兼容更旧版本 Python，也至少使用 `startswith(root + os.sep)`，并统一封装为共享校验函数。

## 🟡 建议改进

### 问题 1：Stage 12 被勾选为“完成”的多项能力，前端实际上仍是只读展示或压根没有入口
- **位置**：`QQBotPlan/Plan_1/Task.md:157-163`、`QQBotPlan/Plan_1/Plan_1_webui.md:126-163`、`QQBotPlan/Plan_1/Plan_1_webui.md:218-229`、`BossLady_Console/frontend/index.html:203-289`、`BossLady_Console/frontend/app.js:359-510`
- **描述**：任务文档把 Memory 编辑/导入导出、插件开关/配置、系统导入/安全设置、Knowledge 更新日志、Sandbox Launch Review 等都标成已完成，但前端现在只有查看、搜索、导出、日志读取等有限动作。比如 Memory 页从未调用后端的 `POST/PUT/DELETE /api/data/memory`，系统页没有导入和安全设置入口，插件页只有列表，没有开关/配置入口。
- **修复建议**：如果这些功能还没做完，应先把 Task 勾选状态回退；如果计划继续保留“已完成”，就需要把交互入口、表单与后端逻辑补齐，避免后续联调阶段误判完成度。

### 问题 2：插件列表只会解析 JSON 元数据，当前仓库里的大多数 `metadata.yaml` 信息都被丢掉了
- **位置**：`BossLady_Console/backend/routes/system.py:37-49`
- **描述**：代码会尝试读取 `metadata.yaml`，但后续只在 `suffix == ".json"` 时才解析内容。因此像 `astrbot_plugin_flashlite/metadata.yaml`、`astrbot_plugin_group_chat/metadata.yaml` 里的 `description`、`version`、`author` 都不会显示，UI 只能看到插件名，失去“管理页面”应有的可读性。
- **修复建议**：引入 YAML 解析（如 `PyYAML`），统一兼容 `metadata.yaml` 与 `_metadata.star.json`；读取失败时把错误信息记录到日志，而不是静默吞掉。

## 🟢 微调建议

### 问题 1：仪表盘 `uptime` 用的是 `time.process_time()`，显示的是 CPU 时间而不是真实运行时长
- **位置**：`BossLady_Console/backend/routers/dashboard.py:46`
- **描述**：`time.process_time()` 统计的是当前进程消耗的 CPU 时间，不是服务启动后的 wall-clock 运行时长。页面上如果以后展示“运行时长”，会和用户理解明显不一致。
- **修复建议**：在应用启动时记录 `time.time()` 或 `perf_counter()`，接口里返回真实经过秒数。

## ✅ 做得好的地方

- `main.py` 的 SPA 回退和静态资源挂载结构清晰，FastAPI 骨架搭建没有明显组织性问题。
- 前端对消息内容使用了 `escapeHtml()`，至少在消息搜索列表里主动考虑了 XSS 风险。
- Stage 12 已经把 Memory / Knowledge / Sandbox / Settings 的页面壳搭出来了，后续补真实数据接入时不需要推倒重来。

## 验证记录

- 直接调用 `BossLady_Console` 路由函数，确认 `/api/models/*` 在当前仓库下返回空模型、空 Key 或默认值。
- 直接调用 `list_memories()` / `read_memory()` / `get_knowledge()`，确认错误为 `No module named 'astrbot'`。
- 检查本地数据库，确认真实消息库为 `AstrBot/data/data_v4.db`，核心消息表为 `platform_message_history`，并不存在 `QQ_data/messages.db`。
- 检查 `cmd_config.json` 与 `astrbot_plugin_flashlite/_conf_schema.json`，确认当前控制台使用的字段名已与真实 schema 脱节。
