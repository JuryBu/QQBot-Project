# Report_2_5: media_summary 内部逻辑审计报告

> 审计时间: 2026-04-07 23:00
> 审计对象: `main.py` L3509-3632 (`tool_media_summary`) + L236-358 (`_fetch_forward_content`)
> 审计依据: 逐条对照实际代码

---

## 1. 解析视频的逻辑

**代码位置**: L3542 (`media_type == "video"` 分支) → L3634-3679 (`_summarize_video`)

### 实际行为

| 步骤 | 代码行 | 行为 |
|------|--------|------|
| 入口判断 | L3542 | `media_type == "video"` 进入视频分支 |
| URL视频 | L3638 | `video_info.startswith("http")` → 用 `fileData` 方式发给 Gemini |
| 本地视频 | L3640-3655 | Sandbox 路径解析 → base64 编码 → `inlineData` 发给 Gemini |
| 大小限制 | L3651 | 硬限 20MB |
| API 调用 | L3658-3670 | Gemini 2.5 Flash，`mediaResolution=LOW` 降分辨率 |
| Sandbox 前缀 | L3644-3646 | ✅ 已加 `Sandbox/` 前缀剥离守护 |

### 诚实评价

- ✅ URL 和本地文件两种来源都能处理
- ✅ 20MB 硬限保护
- ⚠️ **MIME 类型硬编码为 `video/mp4`**，不支持 webm/mkv/avi 等格式自动识别（虽然 Gemini 可能能处理，但 MIME 不匹配可能出问题）
- ⚠️ **视频不走三级分片**，直接一次性发给 Gemini——超长视频（如1小时讲座）可能因 token 限制失败
- ❌ **视频处理不存档**——没有像 forward 那样存档原文/结果到 Sandbox

---

## 2. 原文提取模式（extract_raw）

**代码位置**: L3543-3551

### 实际行为

```python
if extract_raw:
    archive_path = self._archive_content(content, media_type)
    if archive_path:
        return f"📄 原文已提取并保存到: {archive_path}\n..."
```

### 诚实评价

- ✅ 跳过 AI 总结，直接存原文到 `workspace/media_logs/{media_type}_{timestamp}.txt`
- ✅ 返回文件路径指针，模型可用 `view_file` 后续查看
- ✅ 对转发消息：先经过 `_resolve_quoted` + `_fetch_forward_content` 完整解析后再存档——所以**嵌套聊天记录会被递归展开后存档**
- ✅ 图片/视频/文件等多媒体在转发内容解析阶段已标注为 `[图片: URL]` `[视频: URL]` 等文本描述
- ⚠️ **纯文本存储**——不会下载图片/视频文件本身到 Sandbox，只存文本描述

---

## 3. 概括模式对嵌套聊天记录和多模态的处理

**代码位置**: L3554-3610 (三级分片) + L236-358 (`_fetch_forward_content`)

### 嵌套聊天记录

| 行为 | 代码行 | 说明 |
|------|--------|------|
| 递归深度 | L238-239 | `max_depth=5`，超过返回占位提示 |
| 嵌套展开 | L293-300 | `seg_type == "forward"` 时取 `data.id` 递归调用 |
| 缩进标记 | L274, L297 | `indent = "  " * depth`，嵌套内容有层级缩进 |
| 分隔标记 | L297 | `--- 嵌套转发(层N) ---` 开头和结尾标记 |

### 多模态内容处理

| segment 类型 | 代码行 | 解析结果 |
|-------------|--------|---------|
| text | L288 | 纯文本提取 |
| image | L290 | `[图片: URL]`（含 URL 前 80 字符） |
| video | L302 | `[视频: URL]` |
| file | L305 | `[文件: 文件名, url=...]` |
| face/mface | L308-312 | 表情描述文本 |
| json | L316-324 | `[卡片: 标题]`（解析 JSON 提取 prompt/title） |
| share | L313-315 | `[分享: 标题, URL]` |
| record | L312 | `[语音消息]` |
| at | L326 | `@QQ号` |
| reply | L328 | `[回复消息]` |

### 诚实评价

- ✅ 嵌套转发递归展开到 5 层
- ✅ 图片和视频提取了 URL（可供模型后续用 web_fetch 查看）
- ✅ JSON 卡片消息会尝试解析标题
- ⚠️ **图片/视频只标注 URL，不下载也不分析内容**——AI 总结拿到的是 `[图片: https://...]` 文本，不是真正的图片内容
- ⚠️ **语音消息只标记占位符**，不转录
- ⚠️ **文件只标记名称和 URL**，不下载也不解析内容

> **结论**：概括模式对嵌套聊天记录的**文本内容**处理完善，对多模态内容只做**标注+URL提取**，不做实际内容分析。

---

## 4. media_summary 是并发过程吗？

**代码位置**: L3571-3581 (中型分片的 `asyncio.gather`)

### 实际行为

| 分支 | 并发？ | 说明 |
|------|--------|------|
| 小型 (≤2000字, ≤3条) | ❌ 串行 | 一次性发给 FlashLite，单次调用 |
| 中型 (≤8000字, ≤10条) | ✅ 并发 | `asyncio.gather(*tasks)` 并行处理所有 chunk |
| 大型 (>8000字) | ❌ 串行 | 采样后一次性发给 FlashLite |

### 诚实评价

- ✅ 中型分片确实用了 `asyncio.gather` 并发处理
- ⚠️ 大型分支虽然也分了采样段，但只做了一次 API 调用（采样后拼接发送），**不是并发**
- ⚠️ 并发的是**多次 FlashLite API 调用**，不是多个工具模型进程

---

## 5. media_summary 是并发的多个工具模型概括子代理进程吗？

### 实际行为

**不是。**

- `media_summary` 是一个**单体工具函数**，在 agent tool loop 中被调用
- 内部调用 `self._call_flash_lite(prompt)` 做 AI 概括——这是**直接调 Gemini API**，不是启动工具模型子代理
- 中型分片的并发是 `asyncio.gather` 并发多个 HTTP 请求——不是多进程
- **不会对记录内的视频/图片自动调用其他工具**（如 `web_fetch`）来分析

### 诚实评价

- ❌ **对记录内的视频/图片不会调用工具**——它只看到 `[图片: URL]` 文本，不会自动用 `web_fetch` 去看图片内容
- ❌ **不是子代理架构**——是单函数内的异步并发 HTTP 调用
- 如果要支持对转发记录内的图片/视频做深度分析，需要额外实现：检测到 `[图片: URL]` 时自动调用 `web_fetch` 或 Gemini 多模态 API

---

## 6. 主模型和工具模型的提示词注入

### 已更新内容

**工具模型**（依赖 docstring 自动注入）——L3511-3518:
```
统一媒体内容摘要工具——合并转发消息+视频+图文混合的三级分片处理。
支持递归嵌套转发（最深5层），自动识别图片URL、视频URL、文件名、
卡片消息等多媒体内容。合并转发消息会自动通过NapCat API拉取完整内容。
所有forward类型摘要均自动存档原文到Sandbox供后续view_file查看。

Args:
    content: 合并转发使用@quoted_forward（自动解析）或纯数字ID
    media_type: forward(默认)/video/mixed  
    media_count: 消息/媒体条数
    duration: 视频时长(秒)
    extract_raw: true跳过AI总结，存原文到Sandbox返回路径指针
```

**主模型**——L2285 工具导航:
```
【媒体】generate_image, media_summary(转发消息/视频/图文摘要, 
       extract_raw=true提取原文到Sandbox), web_fetch(12种模式)
```

**主模型**——L2310-2315 新增「合并转发消息处理」小节:
```
## 合并转发消息处理
- 收到合并转发消息时，message_str 中有 [Forward Message: id=xxx]
- media_summary(content='@quoted_forward', media_type='forward') 自动拉取并AI总结
- media_summary(content='@quoted_forward', extract_raw=true) 提取原文到Sandbox
- 支持最深5层嵌套转发递归展开
- @quoted_forward 未注册时自动从消息组件中提取转发ID
```

### 诚实评价

- ✅ docstring 明确说明了何时使用、参数含义、两种模式
- ✅ 主模型提示词有独立小节指导合并转发消息处理
- ✅ 工具导航一行内能看到 extract_raw 功能
- ⚠️ docstring 没有说明**图片/视频只标注不分析**的局限性——模型可能以为它能分析图片内容

---

## 改进建议（非本次修改范围）

| 优先级 | 建议 | 说明 |
|--------|------|------|
| P2 | 视频 MIME 自动检测 | 根据文件扩展名推断 MIME 类型 |
| P2 | 转发内图片深度分析 | 检测 `[图片: URL]` 时自动调 Gemini 多模态看图 |
| P3 | 视频结果存档 | 与 forward 一致，视频总结也存到 Sandbox |
| P3 | 语音转录 | 语音消息自动调 STT 转文本 |
