# Report_2_2: 八问深度调研报告

> 生成时间: 2026-04-07 00:40
> 源码: `main.py` (3830行) + `kv_cache.py` (312行) + `gemini_source.py` (995行)

---

## Q1: 主模型 KVCache — 如何实现？

### 调研结论

**主模型 KVCache 技术可行！** 但需要修改 AstrBot 框架层。

#### 发现链路
```
主模型调用链:
FlashLite.inject_flashlite_context() [on_llm_request钩子]
  → 修改 req.system_prompt（追加 inject_parts）
  → AstrBot 框架调用 gemini_source.py._query()
    → _prepare_query_config() 构建 GenerateContentConfig
    → client.models.generate_content(model, contents, config)
```

#### 关键代码: gemini_source.py L593-597
```python
result = await self.client.models.generate_content(
    model=model,
    contents=cast(types.ContentListUnion, conversation),
    config=config,  # ← GenerateContentConfig
)
```

#### SDK 支持方式
```python
config = types.GenerateContentConfig(
    cached_content=cache_name,  # ← 传入缓存名
    # ⚠️ 使用 cached_content 时不能同时指定:
    # - system_instruction
    # - tools
    # - tool_config
    # 这些必须在 client.caches.create() 时包含
)
```

#### 实现方案: 在 on_llm_request 钩子中注入
由于 inject_flashlite_context 是 priority=9000 的钩子，在 gemini_source._query() 之前执行：
1. 在 FlashLite 插件中创建/管理缓存（用 `genai.Client.caches.create()`）
2. 通过 `req.extra` 传递 `cached_content_name` 给框架
3. 在 gemini_source._prepare_query_config 中检查 extra 字段并注入

**难点**: system_instruction + tools 必须在缓存时绑定，而这些每次可能变化（工具集动态注册）。
**可行解法**: 只缓存 persona + 体系认知 + 固定工具描述，Knowledge/Memory/CHECKPOINT 作为动态 contents。

#### 最低 Token 要求
| 模型系列 | 最低 Token |
|---------|-----------|
| Gemini 2.5 | 2048 |
| Gemini 3 | 4096 |

主模型的 system_prompt (persona ~1100字 + inject_parts ~3000字) ≈ 1500-2000 token，加上工具描述通常能超过 2048。

**结论: 需改 gemini_source.py 才能用，标记为 Plan_2_3 高优先级项。**

---

## Q2: FlashLite 和工具模型 KVCache 当前状态

### FlashLite KVCache — main.py L894-960
```python
# L894-914: KVCache 尝试
_fl_system = self._build_flash_lite_system()  # ~1600字 ≈ 500 token
_cached_name = None
if self._kv_cache:
    try:
        _fixed_system = self._build_flash_lite_system()
        _cached_name = await self._kv_cache.ensure_cache(
            fixed_contents=[{"role": "user", "parts": [{"text": "KV Cache 锚点"}]}],
            system_instruction=_fixed_system,
        )
    except Exception as _e:
        logger.debug(f"KVCache 降级: {_e}")
        _cached_name = None

# L930-960: 两分支 payload
if _cached_name:
    payload = {"cachedContent": _cached_name, "contents": [...], ...}
else:
    payload = {"systemInstruction": {...}, "contents": [...], ...}
```

**状态**: 代码已接入。但 FlashLite system prompt 仅 ~500 token，**低于** Gemini 2.5 的 2048 最低要求。
**预期**: API 会返回 400 错误 → 降级到无缓存模式。除非 Memory 索引足够长凑够 2048+。

### 工具模型 KVCache — main.py L1093-1127
```python
# L1093-1106: 循环外缓存 system prompt
_tool_cached_name = None
if self._tool_kv_cache:
    try:
        _tool_system = self._build_tool_model_system()
        _tool_cached_name = await self._tool_kv_cache.ensure_cache(
            fixed_contents=[...],
            system_instruction=_tool_system,
        )
    except: _tool_cached_name = None

# L1115-1133: 两分支 payload
if _tool_cached_name:
    payload = {"cachedContent": ..., "contents": messages, ...}
else:
    payload = {"systemInstruction": ..., "contents": messages, ...}
```

**状态**: 同样因 token 不足可能降级。工具模型 system prompt ~1717 字 ≈ 600 token。

**两者共同问题**: REST API 的 cachedContents 也有最低 token 限制。实际效果需要启动 Bot 测试（看日志是否有"KVCache 降级"）。

---

## Q3: wait/grep 工具

### wait 工具
- **定义**: `Sandbox/base_tools/wait.tool.json` + `main.py tool_wait()` L3630
- **参数**: `seconds` (1-300, 默认10)
- **用法**: 主模型/工具模型 均可调用
  - 主模型: `wait(seconds=60)` → AstrBot 框架执行 → 返回"已等待60秒 当前时间: 23:45:01"
  - 工具模型: `agent_wait(seconds=60)` → 通过 agent_ 路由执行
- **结果**: asyncio.sleep 后返回等待时间和当前时间
- **场景**: 定时提醒、等待外部进程、延迟操作

### grep 工具
- **定义**: `Sandbox/base_tools/grep.tool.json` + `main.py tool_grep()` L3645
- **参数**: `pattern`(必填), `path`(默认workspace/), `max_results`(默认20)
- **用法**: 同上，两种模型均可
  - 主模型: `grep(pattern="天气", path="workspace/")` → AstrBot 框架执行
  - 工具模型: `agent_grep(pattern="天气")` → agent_ 路由
- **结果**: 返回匹配的文件路径:行号:行内容，不区分大小写
- **跳过的文件**: .pyc/.exe/.dll/.png/.jpg/.jpeg/.gif/.webp/.mp3/.mp4/.zip/.tar/.gz/.db/.sqlite

---

## Q4: 模型如何操作草稿

### 主模型操作方式
在 Section 8 Sandbox 段中已注入指引（L1770-1777）：
```
- 操作: modify_file(path=workspace/drafts/xx.md, content=内容) 写入
- 操作: view_file(path=workspace/drafts/xx.md) 读取
- 命名: 任务名_日期.md 如 search_report_0406.md
- 用途前缀: plan_(计划) note_(笔记) tmp_(临时) result_(结果)
```

主模型通过 AstrBot 框架注册的 `modify_file` 和 `view_file` 工具来操作草稿。
路径必须在 `workspace/drafts/` 下。

### 工具模型操作方式
工具模型有专用的 `agent_draft` 工具（硬编码在 _call_tool_model L1048-1059）：
```python
{
    "name": "agent_draft",
    "description": "读写子代理专属草稿纸（自动在 agent_drafts 目录下操作）",
    "parameters": {
        "filename": {"type": "string", "description": "草稿文件名"},
        "content": {"type": "string", "description": "写入内容（留空则为读取）"}
    }
}
```
工具模型的草稿存在 `workspace/agent_drafts/{task_id}/` 目录下，与主模型草稿隔离。

### 两者区别
| 项目 | 主模型 | 工具模型 |
|------|--------|---------|
| 草稿路径 | workspace/drafts/ | workspace/agent_drafts/{task_id}/ |
| 操作工具 | modify_file + view_file | agent_draft |
| 隔离性 | 共享 drafts 目录 | 每个 task 独立目录 |

---

## Q5: Memory 机制完整流程

### 触发流程
```
1. 用户发消息到 QQ 群聊
   ↓
2. FlashLite 引擎接收消息 (_on_group_message)
   ↓
3. _build_memory_mini_index() 构建索引（L1680-1730）
   - 调用 self._memory.list_all() 获取 所有 Memory 条目
   - pinned 优先 + 更新时间降序
   - 上限 100 条
   - 输出: "[1] "张三的饮食偏好" [pinned] #用户信息"
   ↓
4. 索引注入 FlashLite system prompt 末尾
   ↓
5. FlashLite 判断需要回复 → 输出 MEMORY_HINT=1,3,7
   ↓
6. 解析 MEMORY_HINT → 按序号精确召回
   - 从 sorted_entries 中取对应索引
   - 调用 self._memory.read(entry_id) 获取完整内容
   ↓
7. 召回内容注入主模型的 Section 3（Memory 召回段）
   ↓
8. 主模型收到 Memory → 可以引用/回复
   ↓
9. 主模型决定写入新 Memory → 调用 memory_write 工具
```

### Write 流程（tool_memory_write L2149-2177）
```python
async def tool_memory_write(self, event, title, content, tags="", category="general", workspace=""):
    # 解析 tags（逗号或空格分隔）
    # 调用 self._memory.write(title, content, tags, category, workspace)
    # 返回写入确认
```

### Read 流程（tool_memory_read L2227-2253）
```python
async def tool_memory_read(self, event, id):
    # 调用 self._memory.read(id) → 返回完整内容
```

### Query 流程（tool_memory_query L2178-2225）
```python
async def tool_memory_query(self, event, query="", workspace="", tags="", limit=10):
    # 调用 self._memory.query(query, workspace, tags, limit)
    # 返回匹配条目列表
```

### 状态确认
- ✅ memory_write: 主模型可直接调用
- ✅ memory_read: 主模型可直接调用
- ✅ memory_query: 主模型可直接调用（scope=memory 的 search 也会路由到这里）
- ✅ _build_memory_mini_index: FlashLite 每次调用时动态构建
- ⚠️ 依赖: self._memory 在 __init__ 中初始化为 MemoryStore()，需确认 MemoryStore 的后端存储正常

---

## Q6: 主模型工具发现与渐进式披露

### 当前状态
主模型提示词中有这一段：
```
【动态段: _agent_builder._build_tool_section("brief") — 工具集说明】
```

但实际代码中 **没有 `_agent_builder` 也没有 `_build_tool_section`**——这个函数不存在！

搜索结果：
- `_build_tool_section` 在整个 AstrBot 代码库中 **0 次出现**
- `_agent_builder` 在 main.py 中也没有实例

**真实情况**：主模型的工具描述来自 AstrBot 框架的 `ToolSet.get_func_desc_google_genai_style()`（gemini_source.py L209），这只是 `functionDeclarations` 列表，不是 system prompt 中的文本描述。

也就是说：
1. 主模型看到的工具 = AstrBot 框架注册的 `functionDeclarations`（模型通过 function calling 看到工具名+参数）
2. 主模型 **没有** 在 system prompt 中看到工具的文字说明
3. 模型要了解工具 → 只能靠 `functionDeclarations` 中的 `description` 字段（一句话）

### 渐进式披露问题
**目前不存在渐进式披露机制**：
- 模型无法"先看列表再深入看方法"
- 所有工具一次性通过 functionDeclarations 全部暴露
- 工具的 description 是一句话，没有详细的使用示例

### 建议方案（Plan_2_3）
1. 在 inject_parts 中加入"工具速查表"（当前已有 Section 12 工具分类速查）
2. 增加 `tool_help` 工具：模型调用 `tool_help(name="search")` 获取详细用法
3. 或在 system prompt 中加入"关键工具示例"段——不需要渐进式，直接在 prompt 里写清楚

---

## Q7: 工具模型的工具发现

### 当前状态
工具模型的工具来自两个地方（main.py L1022-1091）：

**1. 硬编码三件套**（L1023-1060）：
- agent_view_file, agent_modify_file, agent_draft
- 有完整的 description + parameters

**2. 动态加载**（L1062-1091）：
```python
base_tools_dir = os.path.join(os.path.dirname(__file__), "Sandbox", "base_tools")
for tool_file in sorted(os.listdir(base_tools_dir)):
    if tool_file.endswith(".tool.json"):
        tool_def = json.load(f)
        if hasattr(self, f"tool_{tname}"):  # 只加载有实现的
            tool_declarations.append({
                "name": f"agent_{tname}",
                "description": tool_def.get("description", ""),
                "parameters": tool_def.get("parameters", {})
            })
```

**关键过滤**: `hasattr(self, f"tool_{tname}")` 确保只有在 main.py 中有 `tool_xxx` 方法的工具才会被加载。

### 渐进式问题
与主模型类似——所有工具一次性通过 functionDeclarations 暴露。
但工具模型有额外优势：
- system prompt 中有"工具使用场景指南"（L830-854）描述了分类和用法
- 有 base_tools 规范说明

### 与主模型共享情况
| 项目 | 主模型 | 工具模型 |
|------|--------|---------|
| 工具来源 | AstrBot 框架注册 | base_tools/*.tool.json + 硬编码三件套 |
| 命名 | 原名(search, memory_write...) | agent_ 前缀(agent_search, agent_memory_write...) |
| 底层实现 | **共享** 同一个 tool_xxx 方法 | 通过 agent_ 路由到同一个 tool_xxx |
| 工具描述 | functionDeclarations.description | tool.json.description |
| 详细文档 | ❌ 无渐进式 | system prompt 有场景指南 |

**结论: 底层实现已共享，但描述和发现机制各自独立。**

---

## Q8: Knowledge 更新格式 + Memory QQ号规范

### 当前 Knowledge 更新问题
FlashLite 输出 KNOWLEDGE_UPDATE 时，active_users 字段格式不统一。

需要在 FlashLite 提示词中明确要求：
```
用户标识格式: 昵称(QQ号,QQ昵称 原始昵称)
示例: 柚子(<ADMIN_QQ>,QQ昵称 Jury_鸽姬布)
绝对不能只写昵称——同一用户可能有多个群昵称但QQ号唯一
```

### 需要修改的位置
1. **FlashLite 提示词** `_build_flash_lite_system()`: KNOWLEDGE_UPDATE 的 active_users 格式说明
2. **主模型 Memory 提示词**: memory_write 时的用户标识规范

### 修改内容（将立即实施）

---

## 行动清单

| # | 问题 | 状态 | 行动 |
|---|------|------|------|
| Q1 | 主模型 KVCache | 🟡 需改框架 | Plan_2_3 最高优先级 — 改 gemini_source.py |
| Q2 | FL/工具 KVCache | ✅ 已接入 | 运行时可能因 token 不足降级 需测试 |
| Q3 | wait/grep | ✅ 已实现 | 两模型均可用 |
| Q4 | 草稿操作 | ✅ 已有指引 | 主模型 modify_file 工具模型 agent_draft |
| Q5 | Memory 机制 | ✅ 流程完整 | 需确认 MemoryStore 后端 |
| Q6 | 主模型工具披露 | ⚠️ 缺失 | 当前无渐进式 立即补工具速查段 |
| Q7 | 工具模型披露 | 🟡 基本够用 | system prompt 有场景指南 但可增强 |
| Q8 | Knowledge QQ号 | ⚠️ 需修改 | 立即修复 FlashLite + Memory 提示词 |
