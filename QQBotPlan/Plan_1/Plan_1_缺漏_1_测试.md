# Plan_1 缺漏补充 #1 - 测试计划

> 对应计划文档：Plan_1_缺漏_1.md 第四至六章

## Stage 缺漏-1A：图片本地缓存

### 自动测试

```python
# test_image_cache.py - 在 sandbox 中运行

# 1. 验证 QQ_data/images/ 目录自动创建
assert os.path.isdir("QQ_data/images/")

# 2. 发送带图片的测试消息后，检查图片文件是否下载
# SELECT image_urls FROM qq_messages WHERE has_image=1 ORDER BY id DESC LIMIT 1
# 返回的路径应以 QQ_data/images/ 开头而非 https://

# 3. 通过 API 验证图片可访问
# GET /api/messages/image/{local_path} → 200 + image/jpeg

# 4. 路径穿越安全测试
# GET /api/messages/image/../../etc/passwd → 403
```

### 手动验证

- [ ] 在 QQ 群发送一张图片
- [ ] 5 秒后刷新仪表盘消息流，检查新消息是否显示缩略图
- [ ] 点击缩略图，确认大图 modal 正常弹出
- [ ] 重启项目后，历史图片依然可查看（本地文件未丢失）
- [ ] 检查 `QQ_data/images/` 目录，确认图片文件确实存在

## Stage 缺漏-1B：消息流信息增强

### 自动测试

```python
# 1. 调用 /api/messages/search?limit=5
# 验证返回 JSON 包含 sender_id、window_id 字段

# 2. 验证前端渲染（截图检查）
# 消息流中每条消息应显示群名
```

### 手动验证

- [ ] 仪表盘消息流中每条群消息显示群名
- [ ] hover 发送者名字时，tooltip 显示 QQ号
- [ ] 私聊消息和群聊消息用不同图标区分

## Stage 缺漏-1C：Memory 页面修复

### 自动测试

```python
# 1. GET /api/memory/list (或对应路径) → 200
# 2. 验证返回 JSON 不含 error 字段
```

### 手动验证

- [ ] Memory 页面"记忆列表"不再显示红色 `Failed to fetch`
- [ ] 统计区"总记忆"和"工作区"显示正确数字或 0（非 `-`）
- [ ] 目前无记忆数据时显示"暂无记忆"而非错误

## 执行优先级

1. **缺漏-1C**（Memory 页面修复）— 最简单，可能只是 API 路径错误
2. **缺漏-1B**（消息流增强）— 纯前端改动
3. **缺漏-1A**（图片本地缓存）— 涉及插件 + 后端 + 前端三层改动

## Stage 缺漏-1D：消息类型分级处理

### 自动测试

```python
# test_message_types.py

import sqlite3, json

db = sqlite3.connect("QQ_data/messages.db")
db.row_factory = sqlite3.Row

# 1. 验证 extra_data 列存在
cols = [row[1] for row in db.execute("PRAGMA table_info(qq_messages)")]
assert "extra_data" in cols, "extra_data 列不存在"

# 2. 发送转发消息后，验证 forward_content 不为空
rows = db.execute(
    "SELECT extra_data FROM qq_messages WHERE content LIKE '%转发消息%' ORDER BY id DESC LIMIT 1"
).fetchall()
if rows:
    extra = json.loads(rows[0]["extra_data"]) if rows[0]["extra_data"] else {}
    assert "forward_content" in extra, "转发消息缺少 forward_content"

# 3. 发送视频后，验证 video_url 不为空
rows = db.execute(
    "SELECT extra_data FROM qq_messages WHERE content LIKE '%视频%' ORDER BY id DESC LIMIT 1"
).fetchall()
if rows:
    extra = json.loads(rows[0]["extra_data"]) if rows[0]["extra_data"] else {}
    assert "video_url" in extra, "视频消息缺少 video_url"

# 4. 发送文件后，验证 files 数组
rows = db.execute(
    "SELECT extra_data FROM qq_messages WHERE content LIKE '%文件%' ORDER BY id DESC LIMIT 1"
).fetchall()
if rows:
    extra = json.loads(rows[0]["extra_data"]) if rows[0]["extra_data"] else {}
    assert "files" in extra and len(extra["files"]) > 0
```

### 手动验证

- [ ] 在 QQ 群发送一条合并转发消息，DB 中 `extra_data` 含 `forward_content`
- [ ] 在 QQ 群发送一段视频，DB 中 `extra_data` 含 `video_url`
- [ ] 在 QQ 群发送一个文件，DB 中 `extra_data` 含 `files` 数组（含文件名/大小/URL）
- [ ] 在 QQ 群发送 JSON 卡片（如音乐分享），DB 中 `extra_data` 含 `json_data`
- [ ] 语音消息 `extra_data` 含 `voice_url`

## Stage 缺漏-1E：Sandbox 工具 JSON 注册补全

### 自动测试

```python
# test_sandbox_tools.py

import json, os

base_tools = r"Sandbox\base_tools"

# 1. 验证所有规划工具 JSON 存在
expected = [
    "view_file", "modify_file", "sandbox_exec", "browser_agent",
    "web_fetch", "search", "web_search", "import_data", "save_data",
    "task_set", "QQ_data_original", "generate_image", "memory_store",
    "system_report", "forward_summary", "video_summary"
]
for name in expected:
    f = os.path.join(base_tools, f"{name}.tool.json")
    assert os.path.isfile(f), f"缺失工具 JSON: {name}"
    with open(f, encoding="utf-8") as fp:
        data = json.load(fp)
    assert "name" in data
    assert "parameters" in data

# 2. 验证 API 能返回全部工具
import requests
resp = requests.get("http://localhost:8090/api/data/sandbox/tools")
tools = resp.json().get("base_tools", [])
tool_names = [t["name"] for t in tools]
for name in expected:
    assert name in tool_names, f"API 缺失工具: {name}"
```

### 手动验证

- [ ] Sandbox 页面"基础工具"区显示 16 个工具卡片（原14 + forward_summary + video_summary）
- [ ] forward_summary 卡片显示正确描述和参数数量
- [ ] video_summary 卡片显示正确描述和参数数量

