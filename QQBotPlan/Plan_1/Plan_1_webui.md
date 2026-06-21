# 🖥️ 统一 Web 控制台设计 (Plan_1_webui)

> 关联: [Plan_1.md](./Plan_1.md) | [Plan_1_architecture.md](./Plan_1_architecture.md)  
> 关键词: 一键启动, 统一管理, 自有前端, 全面控制

---

## 设计理念

> 「不打开 AstrBot 的 Web，不打开 NapCat 的 Web，一个入口管控一切」

我们的控制台**不是替换 AstrBot/NapCat 的 Web**，而是在它们之上建一层**统一管理层**：
- 通过 AstrBot 的 API/配置文件直接操作其配置
- 通过 NapCat 的 API 管理 QQ 登录
- 加上我们自己的系统（Sandbox、Memory、Knowledge、消息持久化）管理
- 一键 bat 启动所有后台 → 自动弹出我们的 Web

---

## 技术栈

| 层 | 技术 | 理由 |
|----|------|------|
| 前端 | **Vite + React + Tailwind CSS** | 快速开发，现代美观 |
| 后端 | **FastAPI (Python)** | 和 AstrBot 同 Python 生态，可直接操作其 DB/配置 |
| 启动 | **bat 脚本 + Python subprocess** | Windows 原生，一键启动所有服务 |
| 数据库 | **共用 AstrBot SQLite + 自有 SQLite** | 无需额外依赖 |
| 通信 | **WebSocket + REST API** | 实时监控 + 配置管理 |

---

## 页面结构（左侧导航栏）

效仿 AstrBot 的紫色主调 + 左侧导航，但更现代，更有"控制中心"质感：

```
📊 仪表盘        ← 系统状态总览
🤖 Bot 管理      ← QQ 登录/账号/NapCat 状态
🧠 模型配置      ← 三模型选型/API Key/参数
💾 对话内存      ← 消息持久化统计/管理/清理
📝 Memory 系统   ← 记忆查看/管理
📖 Knowledge     ← Knowledge 缓存实时查看
📦 Sandbox       ← Sandbox 空间查看/Review 管理
🔌 插件管理      ← AstrBot 插件开关/配置
💬 人格设定      ← 老板娘角色 Prompt 编辑
⚙️ 系统设置      ← 全局配置/可移植性/导出
```

---

## 各页面功能设计

### 📊 仪表盘（首页）

```
┌─────────────────────────────────────────────────────┐
│  老板娘控制中心 v1.0                                   │
├──────────┬──────────┬──────────┬──────────────────────┤
│ 🟢 Bot   │ 🟢 NapCat│ 🟢 AstrBot│ ⏱ 运行 3h 25m     │
│ 在线      │ 已连接   │ 运行中    │                     │
├──────────┴──────────┴──────────┴──────────────────────┤
│                                                       │
│  📈 今日统计                                           │
│  ├ 总消息: 1,247    ├ Flash Lite 调用: 312             │
│  ├ 主模型调用: 89   ├ 工具模型调用: 15                  │
│  ├ API 总开销: $0.23 ├ 活跃群: 3/5                     │
│  └ CHECKPOINT: 7 次  └ Memory 新增: 12 条              │
│                                                       │
│  📊 实时消息流速（折线图）                               │
│  [██████████████░░░░░░] 2.3 条/秒                     │
│                                                       │
│  🔔 最近事件                                           │
│  14:30 CHECKPOINT #7 压缩 群<GROUP_B> (35%→12K token)  │
│  14:25 Flash Lite 判断触发主模型 (群<GROUP_B> @事件)    │
│  14:20 Memory 写入: "数据库期中考试信息"                 │
└───────────────────────────────────────────────────────┘
```

### 🤖 Bot 管理

| 功能 | 说明 |
|------|------|
| QQ 登录状态 | 显示当前 QQ 号、在线状态、NapCat 版本 |
| 扫码/快速登录 | 内嵌 NapCat 的登录二维码/快速登录 |
| 账号切换 | 支持多 QQ 号配置切换 |
| NapCat 重启 | 一键重启 NapCat 进程 |
| AstrBot 重启 | 一键重启 AstrBot 进程 |
| 连接状态 | WebSocket 连接状态实时监控 |

### 🧠 模型配置

```
┌───────────────────────────────────────────┐
│ 模型配置                                    │
├───────────────────────────────────────────┤
│                                            │
│ 🔑 API Key                                 │
│ ┌──────────────────────────────────┐      │
│ │ AIza••••••••••••••••••••••       │ ✅ 有效│
│ └──────────────────────────────────┘      │
│ [验证 Key] [刷新模型列表]                    │
│                                            │
│ 🔴 主模型                                  │
│ ┌──────────────────────────────────┐      │
│ │ ▼ gemini-2.5-flash              │      │
│ └──────────────────────────────────┘      │
│ 思考: [██████████░░] Budget: 8192          │
│                                            │
│ 🟢 Flash Lite 模型                         │
│ ┌──────────────────────────────────┐      │
│ │ ▼ gemini-3.1-flash-lite-preview │      │
│ └──────────────────────────────────┘      │
│ 思考: [████░░░░░░░░] Level: MEDIUM         │
│ 同步触发间隔: [5] 条  CP上限: [50000] token │
│                                            │
│ 🟡 工具模型                                │
│ ┌──────────────────────────────────┐      │
│ │ ▼ gemini-3-flash-preview        │      │
│ └──────────────────────────────────┘      │
│ 思考: [████████░░░░] Level: HIGH           │
│                                            │
│ [🔍 探测模型参数支持] [💾 保存配置]          │
└───────────────────────────────────────────┘
```

**关键功能**：
- API Key 输入 + 一键验证
- 模型下拉选择（从 API 模型列表动态获取）
- 思考参数调节（thinkingBudget 滑条 / thinkingLevel 选择器）
- 三层优先级参数探测（直接调用 Suggestion 的 ModelProber）
- Flash Lite 触发间隔和 CHECKPOINT 上限配置

### 💾 对话内存

| 功能 | 说明 |
|------|------|
| 窗口列表 | 所有群聊/私聊窗口列表，含消息数统计 |
| 消息浏览器 | 按窗口浏览历史消息（QQ_data_original 的可视化版） |
| CHECKPOINT 历史 | 查看每次压缩的摘要和压缩率 |
| 消息搜索 | 全局关键词搜索 |
| 存储统计 | 每个窗口的消息数、磁盘占用、冷热分布 |
| 清理管理 | 手动触发清理、设置清理策略（保留天数、是否清理图片缓存） |
| 撤回记录 | 查看被撤回的消息列表 |

### 📝 Memory 系统

| 功能 | 说明 |
|------|------|
| 记忆列表 | 按工作区/标签浏览所有记忆 |
| 记忆详情 | 查看/编辑单条记忆 |
| 搜索 | 全文搜索 + 标签过滤 |
| 用户画像 | 查看每个用户的画像积累 |
| 批量管理 | 批量删除/导出/导入 |
| 统计 | 总条数、工作区分布、增长趋势 |

### 📖 Knowledge

| 功能 | 说明 |
|------|------|
| 实时 Knowledge | JSON 美化展示当前 Knowledge 缓存 |
| 各窗口摘要 | 展开查看每个窗口的 summary/mood/active_users |
| 更新日志 | Knowledge 更新的时间线 |
| 手动刷新 | 强制触发 Flash Lite 更新 Knowledge |

### 📦 Sandbox

```
┌───────────────────────────────────────────┐
│ Sandbox 空间                               │
├───────────────────────────────────────────┤
│                                            │
│ 📁 文件浏览器                               │
│ ├── base_tools/     [14 工具] [🔒 只读]    │
│ │   ├── view_file.tool                     │
│ │   ├── modify_file.tool                   │
│ │   └── ...                                │
│ ├── workspace/      [2.3 MB / 512 MB]     │
│ │   ├── custom_tools/ [3 自定义工具]        │
│ │   ├── drafts/                             │
│ │   │   ├── plan.md [查看]                  │
│ │   │   └── task.md [查看]                  │
│ │   ├── files/                              │
│ │   └── scripts/                            │
│ └── config/         [🔒 只读]              │
│                                            │
│ 📊 资源使用                                 │
│ 存储: [████░░░░░░] 2.3 / 512 MB (0.4%)    │
│ 自定义工具: 3                               │
│ 上次 Review: 2 小时前                       │
│                                            │
│ 🔧 操作                                    │
│ [🔍 Launch Review] [📋 查看最新报告]         │
│ [🗑️ 清理临时文件] [📤 导出 workspace]        │
└───────────────────────────────────────────┘
```

**关键功能**：
- 可视化文件浏览器（base_tools 灰色锁定，workspace 可交互）
- 查看主模型的草稿纸（plan.md / task.md）内容
- 手动触发 Sandbox Review
- 查看最新的 Review 报告
- 资源使用统计

### 🔌 插件管理

直接读取/操作 AstrBot 的插件配置：
- 插件列表（卡片式展示，效仿 AstrBot UI）
- 启用/禁用切换
- 配置项编辑
- 不需要打开 AstrBot 的 Web 也能管理

### 💬 人格设定

- 老板娘的 System Prompt 编辑器（Markdown 编辑 + 实时预览）
- 人格文件管理（支持多人格切换）
- 唤醒词配置

### ⚙️ 系统设置

| 功能 | 说明 |
|------|------|
| 消息持久化策略 | 热/冷/归档的天数配置 |
| CHECKPOINT 策略 | token 上限、压缩率范围 |
| Flash Lite 参数 | 同步触发间隔、Knowledge 更新频率 |
| KV Cache 管理 | 缓存 TTL、缓存状态 |
| 导出系统 | 一键打包导出（排除 API Key） |
| 导入系统 | 从 zip 恢复 |
| 日志查看 | AstrBot/NapCat/我们系统 的合并日志流 |
| 安全设置 | Web 控制台密码、访问控制 |

---

## 一键启动脚本 (start_bosslady.bat)

```bat
@echo off
chcp 65001 >nul
echo ====================================
echo    老板娘控制中心 - 启动中...
echo ====================================

:: 1. 启动 NapCat（后台）
echo [1/3] 启动 NapCat...
start /B "" "%~dp0NapCat_v4.17.53\napcat.bat"
timeout /t 3 /nobreak >nul

:: 2. 启动 AstrBot（后台）
echo [2/3] 启动 AstrBot...
cd /d "%~dp0AstrBot"
start /B "" python -m astrbot
cd /d "%~dp0"
timeout /t 5 /nobreak >nul

:: 3. 启动控制台后端 + 前端
echo [3/3] 启动控制中心...
cd /d "%~dp0BossLady_Console"
start /B "" python -m uvicorn backend.main:app --port 8090
timeout /t 2 /nobreak >nul

:: 4. 打开浏览器
echo.
echo ✅ 全部启动完成！正在打开控制台...
start http://localhost:8090

echo.
echo 按任意键停止所有服务...
pause >nul

:: 停止所有进程
taskkill /f /im python.exe 2>nul
taskkill /f /im node.exe 2>nul
echo 已停止所有服务
```

---

## 后端 API 设计

```
BossLady_Console/
├── backend/
│   ├── main.py                  ← FastAPI 入口
│   ├── routers/
│   │   ├── dashboard.py         ← 仪表盘数据
│   │   ├── bot.py               ← Bot 管理（NapCat API 代理）
│   │   ├── models.py            ← 模型配置（读写 cmd_config.json）
│   │   ├── messages.py          ← 消息持久化查询
│   │   ├── memory.py            ← Memory 系统接口
│   │   ├── knowledge.py         ← Knowledge 缓存接口
│   │   ├── sandbox.py           ← Sandbox 文件浏览/Review
│   │   ├── plugins.py           ← AstrBot 插件管理
│   │   ├── persona.py           ← 人格设定
│   │   └── system.py            ← 系统设置/导出导入
│   ├── services/
│   │   ├── astrbot_bridge.py    ← AstrBot 配置/DB 操作桥接
│   │   ├── napcat_bridge.py     ← NapCat API 调用
│   │   ├── model_prober.py      ← 参考 Kaleidoscope 的参数探测
│   │   └── process_manager.py   ← 进程启停管理
│   └── websocket/
│       ├── log_stream.py        ← 实时日志 WebSocket
│       └── status_stream.py     ← 实时状态 WebSocket
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/               ← 各页面组件
│   │   ├── components/          ← 通用组件
│   │   └── hooks/               ← 自定义 hooks
│   ├── tailwind.config.js
│   └── vite.config.ts
└── package.json
```

---

---

## 🔴 核心整合方案（最重要）

> [!IMPORTANT]
> 这是整个控制台设计的核心目标——**不打开 AstrBot Web（:6185）、不打开 NapCat WebUI（:6099），所有操作都在我们的控制台（:8090）完成**。

### 整合 1：NapCat QQ 登录管理

#### 当前 NapCat 登录架构
```
NapCat WebUI (localhost:6099)
├── token 认证: "23ccc039107e" (webui.json)
├── autoLoginAccount: "<BOT_QQ>"
├── 扫码登录: WebUI 页面点击 → 显示 QRCode → 手机扫码
├── 快速登录: 扫码成功后自动保存登录态，下次自动登录
└── 二维码文件: cache/qrcode.png
```

#### 整合方案：反向代理 + API 桥接

```
用户浏览器 → 我们的控制台 (:8090)
                  │
                  ├─ 方案A (推荐): iframe 内嵌
                  │   └─ /napcat/login 页面内嵌 NapCat WebUI iframe
                  │      src="http://localhost:6099/webui"
                  │      通过 token 自动认证
                  │
                  ├─ 方案B: 反向代理
                  │   └─ FastAPI 代理所有 /napcat/* 请求到 localhost:6099
                  │      自动注入 token 认证
                  │
                  └─ 方案C (最优): 直接调用底层
                      ├─ 读取 NapCat cache/qrcode.png 在我们的 UI 显示
                      ├─ 通过 WebSocket 监听 NapCat 登录状态变化
                      ├─ 修改 webui.json 的 autoLoginAccount 切换账号
                      └─ 通过进程管理重启 NapCat 触发快速登录
```

#### 推荐实现：方案 A + C 混合

| 功能 | 实现方式 | 说明 |
|------|----------|------|
| **扫码登录** | iframe 内嵌 NapCat WebUI | 最稳定，无需重新实现 QQ 协议 |
| **快速登录** | 读 autoLoginAccount + 重启 NapCat | 修改 config → subprocess restart |
| **账号切换** | 修改 webui.json + onebot11 配置 + 重启 | 支持多账号配置保存 |
| **登录状态** | 轮询 NapCat log 或 WebSocket | 实时显示在线/离线/重连 |
| **二维码获取** | 直接读取 cache/qrcode.png | 在我们的 UI 中展示 |

#### 关键配置文件操作
```python
# 读写 NapCat 配置
NAPCAT_DIR = "NapCat_v4.17.53"

# 1. 获取/修改 WebUI token
webui_config = json.load(open(f"{NAPCAT_DIR}/config/webui.json"))
token = webui_config["token"]  # "23ccc039107e"
auto_login = webui_config["autoLoginAccount"]  # "<BOT_QQ>"

# 2. 切换账号
webui_config["autoLoginAccount"] = "新QQ号"
json.dump(webui_config, open(f"{NAPCAT_DIR}/config/webui.json", "w"))

# 3. 修改 OneBot11 连接配置
onebot_config = json.load(open(f"{NAPCAT_DIR}/config/onebot11_xxx.json"))
onebot_config["network"]["websocketClients"][0]["url"] = "ws://127.0.0.1:6199/ws"

# 4. 重启 NapCat 进程
subprocess.Popen([f"{NAPCAT_DIR}/launcher.bat"], shell=True)
```

---

### 整合 2：AstrBot 配置管理

#### 当前 AstrBot API 架构
```
AstrBot Dashboard (localhost:6185)
├── Flask 路由体系（20+ Route 类）
│   ├── AuthRoute        ← 登录认证
│   ├── ConfigRoute      ← 全局配置（cmd_config.json）
│   ├── PluginRoute      ← 插件管理
│   ├── PlatformRoute    ← 消息平台管理
│   ├── PersonaRoute     ← 人格设定
│   ├── ConversationRoute ← 对话管理
│   ├── ToolsRoute       ← 工具管理
│   ├── SubAgentRoute    ← 子代理管理
│   ├── StatRoute        ← 统计数据
│   ├── LogRoute         ← 日志流
│   ├── BackupRoute      ← 备份
│   └── ...
├── API 认证: Cookie 或 API Key
└── 数据库: data_v4.db (SQLite)
```

#### 整合方案：API 代理 + 直接文件操作

```
我们的控制台后端 (FastAPI :8090)
    │
    ├─ 路线1: 通过 AstrBot HTTP API
    │   ├─ POST /api/auth/login → 获取 session
    │   ├─ GET  /api/config     → 读取配置
    │   ├─ POST /api/config     → 写入配置
    │   ├─ GET  /api/plugin     → 插件列表
    │   ├─ POST /api/plugin/toggle → 开关插件
    │   ├─ GET  /api/stat       → 统计数据
    │   └─ ...
    │
    └─ 路线2: 直接读写文件/数据库（更快更灵活）
        ├─ cmd_config.json → 全局配置（模型、API Key 等）
        ├─ data_v4.db → 对话数据、统计
        ├─ plugins/*/config.json → 插件配置
        └─ persona/*.yaml → 人格文件
```

#### 模型配置整合细节

| 操作 | 数据源 | 读写方式 |
|------|--------|----------|
| **查看/修改 API Key** | cmd_config.json → `provider` 数组 | 直接读写 JSON |
| **选择主模型** | cmd_config.json → `provider[].model_config.model` | 直接读写 |
| **选择 Flash Lite** | 我们自己的配置 | 独立配置文件 |
| **选择工具模型** | 我们自己的配置 | 独立配置文件 |
| **thinkingBudget/Level** | 需要扩展 AstrBot provider | API 请求参数注入 |
| **安全设置** | generationConfig.safetySettings | API 请求参数注入 |
| **模型列表刷新** | Gemini API listModels 接口 | 实时 API 调用 |
| **参数探测** | ModelProber（Kaleidoscope 方案） | 我们自己的服务 |

#### AstrBot cmd_config.json 关键路径
```json
{
  "provider": [
    {
      "id": "gemini_flash",
      "type": "google_genai",
      "enable": true,
      "key": ["AIzaSy..."],                    // ← API Key
      "model_config": {
        "model": "gemini-2.5-flash-preview",   // ← 主模型选择
        "max_tokens": 4096,
        "temperature": 0.7
      },
      "enable_system_prompt": true,
      "enable_multi_modal": true
    }
  ],
  "platform": [...],                            // ← NapCat 连接配置
  "wake_prefix": ["老板娘", ...],               // ← 唤醒词
  "reply_with_quote": true,                     // ← 回复方式
  "t2i_model_config": {...}                     // ← 图片生成配置
}
```

#### 工具管理整合

```
AstrBot 内置工具 (ToolsRoute)
├── 已注册工具列表: GET /api/tools
├── 启用/禁用: POST /api/tools/toggle
└── 工具配置: POST /api/tools/config

我们的 Sandbox 工具 (额外)
├── base_tools/ 内的工具清单
├── workspace/ 内自定义工具
└── 工具权限矩阵管理
```

**一体化展示**: 在同一个"工具管理"页面同时展示 AstrBot 原生工具和 Sandbox 工具，统一管理。

---

## 与 AstrBot 的交互方式

| 需求 | 实现方式 |
|------|----------|
| 读取/修改配置 | 直接读写 `cmd_config.json` |
| 操作 DB | 直接操作 `data_v4.db` SQLite |
| 插件管理 | 读取插件目录 + 修改 config.json |
| 重启 AstrBot | subprocess kill + restart |
| 获取运行状态 | AstrBot 的 `/api/status` 接口 |
| 人格编辑 | 直接操作 persona 文件 |

---

## Stage 集成

在 Plan_1.md 的 Stage 路线图中新增：

| Stage | 内容 |
|-------|------|
| **Stage 11** | **Web 控制台 MVP** — 仪表盘 + Bot管理 + 模型配置 + 一键启动 |
| **Stage 12** | **Web 控制台完整版** — 消息浏览器 + Memory/Knowledge + Sandbox + 插件管理 |
| **Stage 13** | **全链路联调** — 控制台 + Agent 系统 + 所有 Stage 端到端测试 |

> [!NOTE]
> Web 控制台在功能上是**锦上添花**，不阻塞核心 Agent 系统开发。建议在 Stage 4-10 期间并行开发控制台 MVP，Stage 10 后集成完整版。

---

## UI 风格参考

- **主色调**：继承 AstrBot 的紫色系（#6B5CF5 + #5C6AC4）
- **背景**：浅灰/暗色可切换
- **布局**：左侧固定导航 + 右侧内容区
- **卡片**：圆角卡片 + 阴影 + 半透明效果
- **字体**：思源黑体 / Inter
- **动效**：页面切换淡入、数据加载骨架屏、图表平滑过渡
- **名称**：「老板娘控制中心」/ 「BossLady Console」
