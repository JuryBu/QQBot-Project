# 审核报告：运行完整性审计

**审核时间**: 2026-04-02 20:20 CST
**审核范围**: `start_bosslady.bat`、`BossLady_Console/backend/*`、`AstrBot/main.py`、`AstrBot/data/plugins/astrbot_plugin_flashlite/*`
**整体评价**: 当前目录中的一套实例已经在运行，但“一键重启/重新拉起”的实现完整性不足，存在组件选错目录、核心插件启动失败、启动脚本误报成功等问题，不能认定为稳定可复现地“能跑起来”。

## 🔴 严重问题（必须修复）

### 问题 1：启动脚本拉起的 NapCat 与控制台管理的 NapCat 不是同一套目录
- **位置**：`start_bosslady.bat:28`、`start_bosslady.bat:44`、`BossLady_Console/backend/routers/bot.py:15`、`BossLady_Console/backend/routers/bot.py:37`、`BossLady_Console/backend/routers/bot.py:148`
- **描述**：
  - 启动脚本只会扫描 `NapCat.*.Shell` 子目录并启动 `NapCatWinBootMain.exe`。
  - 控制台后端 `_find_napcat_dir()` 则优先选择根目录下带 `config/webui.json` 的目录。
  - 当前实测中，`6099` 端口对应的实际进程路径是 `NapCat.Shell.Windows.OneKey\NapCat.39038.Shell\QQ.exe`，但 `/api/bot/napcat/status` 返回的 `napcat_dir` 却是 `NapCat_v4.17.53`。
  - 这意味着控制台展示的账号、Token、重启对象，可能都不是正在运行的那一套 NapCat。
- **修复建议**：
  - 抽出统一的 NapCat 定位逻辑，启动脚本与后端共用同一规则。
  - 最稳妥的方案是以“实际启动目标”为准，固定到一个明确路径，避免同时保留多套 `NapCat*` 时发生歧义。
  - 若必须支持多安装目录，控制台应展示“当前运行进程路径”和“配置来源路径”，并允许用户显式选择。

### 问题 2：Flash Lite 核心插件在 AstrBot 启动阶段加载失败
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:11`、`AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:17`、`AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:23`
- **描述**：
  - 该插件配置 schema 使用了 `type: "str"`。
  - AstrBot 当前配置解析器只接受 `string/text/list/object/...`，不接受 `str`。
  - 实测运行 `AstrBot\.venv\Scripts\python.exe main.py` 时，插件在启动期直接报错：
    `TypeError: 不受支持的配置类型 str`
  - 虽然 AstrBot 主体仍可继续启动，但 Flash Lite 正是 BossLady 方案里的关键中断引擎，这会直接导致相关能力缺失。
- **修复建议**：
  - 将 `_conf_schema.json` 中所有 `type: "str"` 改为 `type: "string"`。
  - 修复后重新完整启动 AstrBot，确认启动日志不再出现 `astrbot_plugin_flashlite 载入失败`。
  - 最好补一条最小化启动测试，校验插件 schema 能被 AstrBot 成功解析。

### 问题 3：`start_bosslady.bat` 会在组件未成功监听时直接回报“启动成功”
- **位置**：`start_bosslady.bat:43`、`start_bosslady.bat:45`、`start_bosslady.bat:58`、`start_bosslady.bat:61`、`start_bosslady.bat:74`、`start_bosslady.bat:77`
- **描述**：
  - 脚本对 NapCat/AstrBot 只是 `start /B` 后立刻打印 `[OK]`，没有做端口探测、进程存活检查或退出码校验。
  - 实测再次启动时，AstrBot 因 `6185` 已被占用而抛出异常，BossLady Console 因 `8090` 已被占用而绑定失败，但脚本前面仍会显示 “All services started in this window!”。
  - 这会把“已有实例占用端口”“实际启动失败”“脚本真的拉起成功”三种状态混在一起，用户很难判断当前到底是什么情况。
- **修复建议**：
  - 启动前先检查 `6099/6185/8090/6199`，发现占用时明确提示“已运行”还是“被其他程序占用”。
  - 启动后轮询健康端点，例如：
    - NapCat: `http://127.0.0.1:6099`
    - AstrBot: `http://127.0.0.1:6185`
    - Console: `http://127.0.0.1:8090`
  - 只有在健康检查通过后再输出 `[OK]` 和总成功提示。

## 🟡 建议改进

### 问题 4：未命中的 `/api/*` 路由会返回 `200 + index.html`，掩盖真实接口错误
- **位置**：`BossLady_Console/backend/main.py:89`
- **描述**：
  - 当前 SPA fallback 会吞掉所有未命中的路径，包括 `/api/not-found-test` 这类本应返回 404 的 API 请求。
  - 实测访问不存在的 `/api/not-found-test` 返回状态码仍为 `200`，内容是前端首页 HTML。
  - 这会让前端把接口错误误判成 JSON 解析异常，排障成本很高。
- **修复建议**：
  - 在 fallback 中排除 `/api/` 前缀。
  - 对未注册 API 返回标准 404 JSON。

### 问题 5：控制台的 NapCat“重启”实现与当前实际进程模型不匹配
- **位置**：`BossLady_Console/backend/routers/bot.py:153`、`BossLady_Console/backend/routers/bot.py:163`
- **描述**：
  - 代码只尝试 `taskkill /IM NapCat.Shell.exe /F`，但当前运行中的实际进程名是 `QQ.exe`。
  - 同时它会在选中的根目录上直接取第一个 `*.bat` 启动，目录歧义存在时，重启行为不可预测。
- **修复建议**：
  - 先按真实进程路径或 PID 停止当前运行实例。
  - 启动脚本应绑定到同一条“已确认的 NapCat 安装目录 + 明确的启动文件”。

## 🟢 微调建议

### 问题 6：运行态日志已经提示 AstrBot WebUI 版本落后于主程序版本
- **位置**：`AstrBot/main.py:82`
- **描述**：
  - 实测启动日志显示 `WebUI 版本 (v4.0.0) 与当前 AstrBot 版本 (v4.22.2) 不符`。
  - 这不一定阻止启动，但会带来界面与后端接口不匹配的潜在风险。
- **修复建议**：
  - 更新 `AstrBot/data/dist` 到与当前 `AstrBot` 版本一致的前端资源。
  - 至少在版本不一致时，在控制台页显式提示用户。

## ✅ 做得好的地方

- 当前环境下 `6099`、`6185`、`6199`、`8090` 均已有实例监听，说明这套组合在现有机器上已经被成功跑起来过。
- `BossLady_Console` 后端通过 `uvicorn backend.main:app` 方式导入时是可启动的，说明 Python 包结构本身是通的。
- 前端主要页面调用的核心接口大多已经打通，控制台首页、Bot 状态页、模型配置页的基础数据可正常返回。

## 验证记录

```powershell
# 已确认当前环境中存在运行实例
Get-NetTCPConnection -State Listen -LocalPort 6099,6185,6199,8090

# AstrBot 冷启动验证（复跑）
& '.\AstrBot\.venv\Scripts\python.exe' main.py

# BossLady Console 启动验证（复跑）
& '..\AstrBot\.venv\Scripts\python.exe' -m uvicorn backend.main:app --host 127.0.0.1 --port 8090 --no-access-log

# 实测接口
Invoke-RestMethod 'http://127.0.0.1:8090/api/dashboard/status'
Invoke-RestMethod 'http://127.0.0.1:8090/api/bot/napcat/status'
Invoke-WebRequest 'http://127.0.0.1:8090/api/not-found-test'
```

## 结论

当前项目在这台机器上“已有一套实例正在运行”这件事成立，但从实现完整性看，还不能认为“一键启动链路已经稳定闭环”。如果只看最终页面能打开，会高估当前质量；从代码和复跑结果看，至少应先修掉 NapCat 目录歧义、Flash Lite 插件 schema 错误、启动脚本误报成功这三项，再谈可稳定交付。
