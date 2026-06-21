# Plan_2_1.md - 系统核心机制问题

> 问题 4~8：涉及 Knowledge/Memory/FlashLite/QQ_data_original/私聊 核心系统机制

---

## 问题 3.5A：KV Cache 403 认证失败 ✅ 已解决

### 现象
每次调用工具模型和 FlashLite 时都触发 KV Cache 403 错误:
```
KV Cache 创建失败 403: {"error": {"code": 403, "message": "Method doesn't allow unregistered callers..."}}
```
当前为降级处理（跳过缓存 直接请求） 不影响功能但浪费重复 token

### 代码定位
- `kv_cache.py` L25: `GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"`
- `kv_cache.py` L121: `url = f"{GEMINI_API_BASE}/cachedContents?key={self._api_key}"`
- endpoint 格式与[官方文档](https://ai.google.dev/api/caching?hl=zh-cn)一致 URL 构建本身无误

### 已解决
- [x] 用户确认已修复（API Key 权限/配置问题）

---

## 问题 3.5B：media_summary 文件路径不一致 + 缺少指针机制 ✅ 已解决

### 现象
1. media_summary 处理转发消息后返回摘要成功 但主模型想查看完整内容时路径找不到
2. 日志: `view_file(path='Sandbox/workspace/media_logs/media_1775567585.txt')` → 文件不存在

### 已解决
- [x] 修复路径: `_archive_content` 返回无 `Sandbox/` 前缀的相对路径，与 view_file 解析一致
- [x] 统一指针格式: 所有分支（extract_raw + 小/中/大型概括）统一为 `[文件: path]` 格式
- [x] 消除双重前缀 Bug（旧代码 `Sandbox/{archive_path}` 已清除）
- [x] 转发内嵌视频也纳入多模态分析管道（下载≤20MB + Gemini 并发分析 + 分级大小控制）
- [x] 概括模式分析后自动清理临时下载文件


---

## 问题 4：Knowledge 全文发送与格式优化 ✅ 已解决

### 已解决
- [x] 确认 `get_formatted()` 和 `get_prompt_text()` 完全一致（`knowledge.py` L198-200 别名关系）
- [x] 工具模型收到完整 Knowledge 符合设计（工具模型需要全局上下文来执行任务）
- [x] 三种模型都有 Knowledge 注入，覆盖确认

详见 [Report_2_6.md](Report_2_6.md) 和 [提示词审计/00_总览.md](提示词审计/00_总览.md)

---

## 问题 5：QQ_data_original 工具无法正常获取对话 ✅ 已解决

### 已解决
- [x] 数据库表结构确认：双源适配（`qq_messages` 精确匹配 + `message_log` 降级模糊匹配）
- [x] FlashLite 触发后自动注入最近原始消息（Section 3 `flashlite_recent_messages`）
- [x] system prompt Section 15 已有 QQ_data_original 使用场景和调用示例
- [x] 实际验证通过：2026-04-08 00:36 日志确认全链路正常（`@quoted_msg` 解析 + 指针回溯 + 📌锚标 + 数据返回）

详见 [Report_2_6.md](Report_2_6.md)

---

## 问题 6：私聊窗口未接入 FlashLite 语义判断 ✅ 已解决

### 现象
私聊窗口每发一条消息就触发一次主模型回复，没有 FlashLite 语义判断「是否需要回复」的过程。

### 代码定位

**FlashLite 同步/异步触发** (`main.py` L338-630)：
- 同步触发只在群聊中工作（每 N 条消息触发）
- 异步触发：被 @ / 关键词，这些主要是群聊场景

**私聊路径**：
- 私聊消息直接由 AstrBot 的 pipeline 触发主模型
- 只在卡片注入阶段 (L1183-1191) 检查 `msg_type == "private"` 来自动注入本人卡片
- 没有经过 FlashLite 的 `should_trigger` 判断

### 需要做什么
- [x] 为私聊窗口接入 FlashLite 判断流程 → 已实现 `_private_trigger` 方法
- [x] 私聊也应该有「不回复」的能力 → TRIGGER=false 时 `event.stop_event()` 阻断
- [x] 私聊的默认回复概率应该远高于群聊 → FlashLite 判断时私聊 ACTIVE_USERS 只有对话者一人

> **实测验证 (2026-04-08 01:57)**：私聊场景下 TRIGGER=true/false 均正常工作，FlashLite 正确识别了「别回复我」→ TRIGGER=false、「可以继续回复」→ TRIGGER=true。

---

## 问题 7：私聊工具系统使用是否正常 ✅ 已解决

### 疑问
私聊窗口既然没接入 FlashLite，那工具系统是否能正常运作？

### 代码分析

**工具注入** (`main.py` L1141-1411 `inject_flashlite_context`)：
- 此钩子在 **所有** `on_llm_request` 上触发（priority=9000），不区分群聊/私聊
- 因此私聊时工具描述、Knowledge 等都会被正常注入
- 工具通过 AstrBot 的 `@filter.llm_tool` 装饰器注册，全局生效

**结论**：
- 工具注册和注入本身是**全局的**，私聊也能用
- 但问题在于：私聊绕过了 FlashLite 的 CHECKPOINT 压缩和 Knowledge 更新
- 工具调用结果可能不会被 FlashLite 感知和记录

### 需要做什么
- [x] ~~确认私聊环境下 CHECKPOINT 压缩、Knowledge 更新是否也被跳过~~ → 已确认并修复：`_private_trigger` 中包含完整的 CHECKPOINT/Knowledge/Memory/Profile 管道
- [x] ~~如果私聊也要完整功能，需要将 FlashLite 同步触发逻辑扩展到私聊窗口~~ → 已实现 `_private_trigger` 方法，每条私聊消息都经过 FlashLite 判断
- [x] ~~最少要保证 Knowledge 对私聊窗口也有记录~~ → 私聊使用 `FriendMessage:{QQ号}` 作为 window_key

---

## 问题 8：Knowledge 窗口摘要中用户标识不含 QQ 号 ✅ 已解决

### 现象
Knowledge 记载群 ID 到最近发生事情的卡片，但一个人在不同群有不同昵称/不同 ID。建议在 Knowledge 摘要中用户名后附加 QQ 号。

### 代码定位

**卡片系统** (`knowledge.py`)：
- 卡片以 `qq_id` 为 key 存储 (L236)
- 输出格式已包含 `{nick}(QQ:{qq_id})` (L458)，**卡片本身是正确的** ✅

**Knowledge 窗口摘要**（由 FlashLite 生成）：
- FlashLite 返回 `KNOWLEDGE_SUMMARY=<20字内摘要>` (L667)
- 摘要中只有群号，没有用户 QQ 号
- 是由 FlashLite 模型自行生成的自然语言，模型可能只用昵称

### 需要做什么
- [x] 在 FlashLite 的 system prompt 中要求 Knowledge 摘要包含用户 QQ 号 → L1239-1243 已明确要求 `昵称(QQ号)` 格式
- [x] 消息发送者格式改为 `昵称(QQ号)` → ACTIVE_USERS/PROFILE_UPDATE/CONTEXT_SUMMARY 均已要求带 QQ 号
- [x] 昵称自动同步 → `sync_nicknames()` 从 ACTIVE_USERS 自动提取最新昵称更新到卡片

> **额外修复**：knowledge_card 工具增加昵称降级检索、Knowledge 操作时间戳从 `%H:%M` 改为 `%Y-%m-%d %H:%M`
