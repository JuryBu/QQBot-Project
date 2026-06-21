# Plan_1 缺漏补充 #1：消息类型处理策略 & 连接问题

> 来源：2026-04-02 对话讨论中用户提出的关键意见

## 一、消息类型分级处理策略（用户确认）

### 1. 合并转发消息（forward / Nodes）
- **❌ 绝对不能展开**。嵌套可达多层，单层最多 100 条消息，展开会导致请求体爆炸
- **正确做法**：persistence 层只做标记存储 `[合并转发]` + 原始 JSON
- **调用时机**：主模型认为需要时，**自主调用 summary 工具**让辅助模型摘要转发内容
- 对应初始讨论记录中已设计的「转发消息 summary 工具」

### 2. 视频消息
- **❌ 不直接传原始视频**。Gemini 虽支持视频输入，但 QQ 视频普遍过大
- **正确做法**：persistence 层标记 `[视频]` + 保存 URL/文件路径
- **调用时机**：设计 summary 服务（降帧 / 关键帧提取），主模型需要时调用

### 3. 文件消息（分级）
| 文件类型 | 处理策略 |
|---------|---------|
| PDF | 可直接送给模型 |
| 纯文本 (.txt/.md/.csv 等) | 可直接送给模型 |
| Office (.docx/.pptx/.xlsx) | 需调用工具转换（web_fetch / 并行 web_fetch） |
| 代码文件 | 可直接送给模型（作为文本） |
| 其他格式 (zip/exe/音频等) | 仅标记文件名和大小，不送内容 |

### 4. 图片消息
- 下载 → base64 → Gemini inlineData，可直接送

### 5. 文本型消息（@/表情/回复引用/戳一戳/JSON卡片等）
- 转成对应文本描述即可

## 二、NapCat → AstrBot WS 连接失败问题

### 现象（2026-04-02 确认）
- NapCat 终端持续报错：`反向WebSocket (ws://127.0.0.1:6199) 连接错误`，每 5 秒重试
- AstrBot 终端无 `aiocqhttp(OneBot v11) 适配器已连接` 日志
- AstrBot 终端只有 HTTP GET 请求日志（来自我们的仪表盘健康检查）
- persistence 插件初始化成功，但 messages.db 0 条记录

### 影响
- 消息根本没有从 NapCat 流入 AstrBot
- persistence 插件 handler 从未被触发
- 所有依赖消息流的功能全部失效

### ✅ 根因已确认（2026-04-02 16:10）

**NapCat 的 WS Client URL 路径错误！**

| 项目 | 原值 | 正确值 |
|-----|------|-------|
| NapCat WS URL | `ws://127.0.0.1:6199/` | `ws://127.0.0.1:6199/ws/` |

**技术原因**：aiocqhttp 库的 Quart 服务器注册了以下路由：
- `POST /` → HTTP 事件接收（**不是 WebSocket！**）
- `WS /ws` → WebSocket 反向连接
- `WS /ws/event` → 事件专用 WS
- `WS /ws/api` → API 调用专用 WS

NapCat 试图以 WebSocket 连接 `/` 路径，但该路径只注册了 HTTP POST，
导致 Quart 返回 **405 Method Not Allowed**，NapCat 反复重试失败。

**修复**：已将 NapCat 配置 `onebot11_<BOT_QQ>.json` 中的 URL 从
`ws://127.0.0.1:6199/` 改为 `ws://127.0.0.1:6199/ws/`。

> ⚠️ **重要**：需要重启 NapCat 才会重新读取配置。AstrBot 密码也已改为 `<DASHBOARD_PASSWORD>`，需重启 AstrBot 生效。

## 三、AstrBot 密码修改

- 用户名：`astrbot`（保持）
- 密码：`<DASHBOARD_PASSWORD>`（MD5: `<PWD_HASH>`）
- 存储位置：`AstrBot/data/cmd_config.json` → `dashboard.password`
- 需重启 AstrBot 生效

## 四、图片本地缓存（新增 2026-04-02 18:05）

### 现状

- persistence 插件 `_extract_content()` 第 259-264 行：图片 segment 仅提取 CDN URL 存入 `image_urls`
- CDN URL 格式：`https://multimedia.nt.qq.com.cn/download?...&rkey=xxx`
- `rkey` 是临时签名 token，**有时效性**，过期后 URL 失效 → 历史图片无法查看
- `content_text` 只存 `[图片]` 占位符

### 修改方案

#### 1. persistence 插件改造（`main.py`）

- 在 `_extract_content()` 中新增图片下载逻辑
- 下载到 `QQ_data/images/{msg_id}_{idx}.jpg`
- `image_urls` 列改存**本地相对路径**而非 CDN URL
- 下载失败时 fallback 存原始 CDN URL（标注 `cdn:` 前缀）
- 使用 `aiohttp` 异步下载，不阻塞消息队列

#### 2. 后端 API（`routes/messages.py`）

- 新增 `/api/messages/image/{path}` 端点，读取本地图片返回
- 安全校验：路径必须在 `QQ_data/images/` 下，防止路径穿越

#### 3. 前端消息流（`app.js`）

- `loadRecentMessages()` 中检测 `image_urls`，渲染为 `<img>` 缩略图
- 点击大图查看（modal）

#### 4. 系统设置（已有"消息持久化策略"区块）

- 增加配置：图片缓存开关、磁盘上限（默认 500MB）
- 超限时自动清理最旧图片

## 五、消息流信息增强

### 现状

- `sender_id`（QQ号）、`window_id`（群号）、`group_name`（群名）均已存入 DB
- 仪表盘消息流只展示 `sender_name` + `content_text`，缺少群名和 QQ号

### 修改方案

#### 1. 后端 search API 补充字段

- `/api/messages/search` 返回结果增加 `sender_id`、`window_id` 字段（已在 SELECT 中）

#### 2. 前端消息流增强

- 每条消息增加群名显示（从 `content_raw` 解析或新建群名映射表）
- sender 区域增加 QQ号 tooltip
- 区分群聊 👥 / 私聊 👤 图标（已有）

## 六、Memory 页面 "Failed to fetch" 修复

### 现象

- Memory 系统页面"记忆列表"显示红色 `加载失败: Failed to fetch`
- 统计区"总记忆"和"工作区"显示 `-`

### 可能原因

- Memory 页面调用的 API 端口或路径与实际后端不匹配
- 需要检查 `app.js` 中 `loadMemory()` 函数调用的 API 路径

## 七、启动脚本优化记录（已完成 ✅）

- 4 窗口合并为 1 窗口 ✅
- NapCat `start /B` 后台启动 ✅
- AstrBot `pushd + start /B` 确保工作目录正确 ✅
- `chcp 65001` 解决 UTF-8 乱码 ✅
- CRLF 编码确保 bat 兼容性 ✅

## 八、消息类型分级处理实现（新增 2026-04-02 18:57）

### 现状

persistence 插件 `_extract_content()` 已处理 10 种 segment 类型，但多数**只做纯文本标记**，缺少关键数据保存：

| segment 类型 | 当前处理 | 缺失 |
|-------------|---------|------|
| `forward` | `[转发消息]` | ❌ 未保存原始 JSON（`content` 字段含嵌套消息） |
| `video` | `[视频]` | ❌ 未保存视频 URL/file 路径 |
| `file` | `[文件:name]` | ❌ 未保存文件 URL/大小/类型 |
| `json` | `[JSON卡片]` | ❌ 未保存 JSON data 内容（含小程序/音乐/链接信息） |
| `record` | `[语音]` | ❌ 未保存语音 URL/file |

### 修改方案

在 `_extract_content()` 返回值中新增 `extra_data` 字典，按 segment 类型保存完整元数据：

```python
# 返回值变更: (content_text, has_image, image_urls) → (content_text, has_image, image_urls, extra_data)
extra_data = {
    "forward_content": [...],  # forward 原始 JSON nodes
    "video_url": "...",        # 视频 URL
    "files": [{"name": ..., "url": ..., "size": ...}],
    "json_data": "...",        # JSON 卡片原始数据
    "voice_url": "...",        # 语音 URL
}
```

DB `qq_messages` 表新增 `extra_data TEXT`（JSON 序列化存储）。

## 九、Sandbox 基础工具注册缺口（新增 2026-04-02 18:57）

### 现状

Plan_1_sandbox.md 规划的 14 个基础工具 JSON 占位全部到齐，但初始讨论中提到的两个工具**连 JSON 注册都未创建**：

| 缺失工具 | 来源 | 功能 |
|---------|------|------|
| `forward_summary` | 初始讨论第159行 | 转发消息摘要：接收 forward JSON nodes，用辅助模型生成摘要 |
| `video_summary` | 缺漏-1 第14行 | 视频摘要：降帧/关键帧提取，用模型生成内容描述 |

### 修改方案

在 `Sandbox/base_tools/` 下创建对应的 `.tool.json` 占位文件。

