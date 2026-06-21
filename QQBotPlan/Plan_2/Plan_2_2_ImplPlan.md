# Plan_2_2 问题 9：Prompt 系统重构 — Implementation Plan

> 本文件是 `Plan_2_2_Task.md` 的详细实施方案
> 核心代码：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`（3479 行）
> 辅助代码：`kv_cache.py`（312 行）、`sandbox.py`、`memory.py`

---

## Stage 1: 主模型提示词基础修复

### 1.1 新增 Section 0 — 体系认知说明

修改位置：`on_llm_request` 钩子中的 `inject_parts` 列表（约 L1385 起）

在所有 Section 之前（即 Section 1 风格约束之前）插入新 Section 0：

```python
# Section 0: 体系认知（最高优先级）
inject_parts.insert(0, f"""## 系统架构认知

你是"老板娘"——一个运行在 AstrBot 框架 + FlashLite 中断引擎体系中的 QQ Bot。

**你的运行环境**：
- 你的文字输出 → AstrBot 框架处理 → 发送到 QQ 消息窗口
- 你的 function_call → AstrBot 自动执行工具 → 结果注入继续对话
- FlashLite 中断引擎在你之前运行，帮你筛选群聊消息，只把需要你回复的消息转给你
- 你有一个工具模型（子代理），可以通过 task_set 工具派它执行后台任务

**当前时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ({['周一','周二','周三','周四','周五','周六','周日'][datetime.now().weekday()]})

**你收到的上下文**：
- Section 2: FlashLite 整理的近期群聊摘要（非全文）
- Section 3: Memory 召回结果（如有）
- Section 4-5: Knowledge + 对话 CHECKPOINT
- 实际对话历史: AstrBot 维护的你与用户的直接交互记录
""")
```

关键要点：
- `datetime.now()` 解决了系统时间缺失问题
- 明确了输出路由（文字→QQ，function_call→框架执行）
- 告知 FlashLite 和工具模型的存在

### 1.2 系统时间注入

已包含在 1.1 的 Section 0 中。如果 Section 0 方案不被采用，单独注入：

```python
inject_parts.insert(0, f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S %A')}")
```

### 1.3 增强上下文注入

修改位置：`_notify_main_model`（L1145-1164）和 `_build_judgment_prompt` 中的 `context_summary` 生成

当前问题：Section 2 只注入 FlashLite 的一句话摘要。

方案：让 FlashLite 在 `CONTEXT_SUMMARY` 字段中包含更丰富的信息：

```
CONTEXT_SUMMARY=最近5分钟群聊: 张三(123456) 问了明天的计划, 李四(789012) 分享了一个文件链接 http://xxx.pdf, 
王五(345678) 回复了张三说"我也想知道"。当前话题焦点: 明天的安排。
```

修改 FlashLite 提示词中 `CONTEXT_SUMMARY` 的生成指引：
- 从"一句话概括" → "列出关键发言者(QQ号) + 核心内容 + 附件信息"
- 特别注意回复引用链的传递

### 1.4 统一 OFFICE 处理规范

修改位置：`on_llm_request` 中 Section 9（L191-195）和 Section 13（L274-279）

操作：
1. 删除 Section 9 中的 `PDF/Office处理链: save_data 保存 → view_file 提取文本(pdfplumber)`
2. 保留 Section 13 的 `web_fetch` 方案
3. 在 Section 9 文件处理流程中统一指向：
```
OFFICE/PDF 文件 → web_fetch(url) 直接处理（支持 docx/xlsx/pptx/pdf）
纯文本文件 → view_file 直接读取
图片文件 → 直接在消息中查看
```

### 1.5 丰富 Section 14

修改位置：`on_llm_request` 中 Section 14 的 Sandbox 状态注入（约 L290）

从：
```
系统: Win11 | Python 3.12 | 网络: 可用
```

改为动态生成：
```python
sandbox_info = (
    f"系统: Win11 | Python 3.12 | 网络: 可用\n"
    f"Sandbox 空间: {self._sandbox.get_disk_usage() if self._sandbox else 'N/A'}\n"
    f"执行超时: 默认 30s, 上限 300s\n"
    f"内存限制: 无硬限, 建议单次 <500MB\n"
    f"核心包: aiohttp, PIL, pdfplumber, openpyxl, pandas, matplotlib"
)
```

---

## Stage 2: FlashLite 消息上下文与身份修复

### 2.1 修改消息拼接格式

修改位置：`_get_recent_context`（L1220-1222）

```python
# 当前代码（L1220-1222）:
# lines.append(f"[{time_str}] {name}: {text}")

# 改为:
sender_id = msg.get('sender_id', '')
if sender_id == 'bot':
    lines.append(f"[{time_str}] {name} [BOT]: {text}")
else:
    lines.append(f"[{time_str}] {name}({sender_id}): {text}")
```

同时修改 Bot 消息识别——确认 DB 查询结果中 `sender_id` 字段可用：
- 群消息：`sender_id` 是 QQ 号
- Bot 消息：`sender_id` 是 `'bot'`（L1260 存入时的值）

### 2.2 更新 FlashLite 提示词说明

修改位置：`_build_flash_lite_system`（L674 起）

在开头 `## 角色` 段落后追加：

```
## 消息格式说明
- 普通消息格式：`[时间] 昵称(QQ号): 内容`
- Bot（老板娘）消息：`[时间] 老板娘 [BOT]: 内容`——这是你的主模型的回复
- QQ号是跨群唯一标识——同一用户在不同群的昵称可能不同，但QQ号相同
- 判断"和老板娘有关"时，看 [BOT] 标记而非名字
```

### 2.3 统一两套 prompt 输出格式

修改位置：`_build_flash_lite_system`（L674）和 `_build_judgment_prompt`（L1037）

现状：
- `_build_flash_lite_system` 要求标记行格式（`TRIGGER_MAIN=true`）
- `_build_judgment_prompt` 要求 JSON 格式

统一方案：两处都使用标记行格式（更简单、token 更少）。

如果 `_build_judgment_prompt` 有独立的用途场景需要 JSON，则在其中明确说明"此处使用 JSON 格式，与标记行格式无关"以避免混淆。

### 2.4 收紧触发规则

修改位置：`_build_flash_lite_system` 中的触发判断指引

原文："有人直接向老板娘说话/提问" → 改为：
```
触发条件（满足任一即触发）：
- 消息中 @老板娘 或使用唤醒词
- 消息直接呼叫"老板娘"三个字
- 消息明确回复了 [BOT] 标记的消息
- 消息直接向老板娘提问（含疑问句+称呼）

不触发条件：
- 群友之间的闲聊（即使提到"老板娘"但不是在和她说话）
- 表情包/纯图片/纯链接分享（无对话意图）
- 已经有 [BOT] 回复过的同一话题（避免重复触发）
```

---

## Stage 3: Memory 迷你索引注入（思路 C）

### 3.1 迷你索引构建函数

新增方法到 `FlashLiteEngine` 类：

```python
async def _build_memory_mini_index(self) -> str:
    """构建 Memory 迷你索引供 FlashLite 使用"""
    if not self._memory:
        return ""
    
    entries = await self._memory.list_all()  # 获取所有 Memory 条目
    if not entries:
        return ""
    
    # 排序：pinned 优先，然后按更新时间降序
    entries.sort(key=lambda e: (not e.get('pinned', False), -e.get('updated_at', 0)))
    
    # 超过 100 条时截断
    MAX_INDEX = 100
    if len(entries) > MAX_INDEX:
        pinned = [e for e in entries if e.get('pinned')]
        recent = [e for e in entries if not e.get('pinned')][:MAX_INDEX - len(pinned)]
        entries = pinned + recent
    
    lines = ["## Memory 索引（共 {} 条）".format(len(entries))]
    for i, entry in enumerate(entries, 1):
        pin = " [pinned]" if entry.get('pinned') else ""
        cat = entry.get('category', 'general')
        tags = entry.get('tags', [])
        tag_str = " ".join(f"#{t}" for t in tags[:3]) if tags else ""
        lines.append(f"[{i}] \"{entry['title']}\"{pin} #{cat} {tag_str}")
    
    lines.append("\n输出 MEMORY_HINT 时用序号精确指定，如 MEMORY_HINT=1,3,7")
    return "\n".join(lines)
```

### 3.2 注入到 FlashLite

修改位置：`_build_flash_lite_system`（L674 起）末尾 或 `_call_flash_lite` 调用前

在 system prompt 末尾追加 Memory 索引（动态内容）：

```python
# 在 _call_flash_lite 中：
system_prompt = self._build_flash_lite_system()
memory_index = await self._build_memory_mini_index()
if memory_index:
    system_prompt += f"\n\n{memory_index}"
```

同时更新 FlashLite 提示词中 MEMORY_HINT 的说明：
```
Memory 召回：查看上方 Memory 索引，如果当前对话涉及索引中的某条记忆，
输出 MEMORY_HINT=序号1,序号2（精确指定序号，不要猜关键词）
如果没有相关记忆，不要输出 MEMORY_HINT
```

### 3.3 修改召回代码

修改位置：`_call_flash_lite` 后的 MEMORY_HINT 解析逻辑（约 L489-490）

```python
# 当前代码:
# result = await self._memory.query(query=keyword, limit=3)

# 改为:
memory_hint = parsed.get('memory_hint', '')
if memory_hint:
    indices = [int(x.strip()) for x in memory_hint.split(',') if x.strip().isdigit()]
    entries = await self._memory.list_all()
    # 按索引构建时的排序规则获取对应条目
    sorted_entries = sorted(entries, key=lambda e: (not e.get('pinned', False), -e.get('updated_at', 0)))
    results = []
    for idx in indices:
        if 1 <= idx <= len(sorted_entries):
            entry = sorted_entries[idx - 1]
            full = await self._memory.read(entry['id'])
            results.append(full)
    # 注入主模型
    if results:
        memory_text = "\n---\n".join(
            f"**{r['title']}** ({r['category']})\n{r['content']}" for r in results
        )
        # 注入到 Section 3
```

### 3.4 边界处理

在 3.1 的函数中已包含：
- Memory 为空 → 返回空字符串，不注入
- 超过 100 条 → 保留 pinned + 最近 N 条
- 解析时无效序号 → `if 1 <= idx <= len(sorted_entries)` 自动忽略


---

## Stage 4: 工具模型架构升级

### 4.1 主模型工具注入工具模型

修改位置：`_call_tool_model`（L842-984）中的 `tool_declarations` 列表

当前只有 3 个内联工具。需要动态加载 `Sandbox/base_tools/*.tool.json`：

```python
# 在 _call_tool_model 中，构建 tool_declarations 之后追加：
import json as _json

# 加载 base_tools 工具定义
base_tools_dir = os.path.join(self._sandbox_root, "base_tools")
excluded_tools = {"task_set"}  # 排除不适合子代理的工具
for tool_file in sorted(os.listdir(base_tools_dir)):
    if not tool_file.endswith(".tool.json"):
        continue
    with open(os.path.join(base_tools_dir, tool_file), encoding="utf-8") as f:
        tool_def = _json.load(f)
    if tool_def.get("name") in excluded_tools:
        continue
    tool_declarations.append({
        "name": f"agent_{tool_def['name']}",  # 加前缀避免冲突
        "description": tool_def.get("description", ""),
        "parameters": tool_def.get("parameters", {"type": "object", "properties": {}})
    })
```

### 4.2 扩展工具路由

修改位置：`_execute_agent_tool`（L986-1020）

当前只处理 3 个 `agent_xxx` 工具。需要扩展路由：

```python
async def _execute_agent_tool(self, name: str, args: dict, draft_dir: str) -> str:
    try:
        if name == "agent_view_file":
            # ... 现有逻辑
        elif name == "agent_modify_file":
            # ... 现有逻辑
        elif name == "agent_draft":
            # ... 现有逻辑
        # === 新增路由 ===
        elif name.startswith("agent_") and hasattr(self, f"tool_{name[6:]}"):
            # 通用路由：agent_search → tool_search, agent_memory_write → tool_memory_write
            real_name = name[6:]
            tool_func = getattr(self, f"tool_{real_name}")
            # 读取超时配置
            timeout = self._get_tool_timeout(real_name)
            result = await asyncio.wait_for(
                tool_func(event=None, **args),  # event=None 因为子代理无消息上下文
                timeout=timeout / 1000  # ms → s
            )
            return str(result)[:2000]
        else:
            return f"未知工具: {name}"
    except asyncio.TimeoutError:
        return f"工具 {name} 执行超时"
    except Exception as e:
        return f"工具执行错误: {str(e)[:200]}"
```

### 4.3 超时保护

新增辅助方法：

```python
def _get_tool_timeout(self, tool_name: str) -> int:
    """读取 base_tools/*.tool.json 中的 timeout_ms，默认 30000"""
    tool_file = os.path.join(self._sandbox_root, "base_tools", f"{tool_name}.tool.json")
    try:
        with open(tool_file, encoding="utf-8") as f:
            return json.load(f).get("timeout_ms", 30000)
    except (FileNotFoundError, json.JSONDecodeError):
        return 30000
```

在 `_execute_agent_tool` 的所有分支都包装 `asyncio.wait_for`。

### 4.4 max_agent_steps 可配置

修改位置：`_call_tool_model`（L848）

```python
# 从：
max_agent_steps = 10

# 改为：
async def _call_tool_model(self, prompt: str, max_tokens: int = 4096, 
                            task_id: str = "", max_steps: int = 0) -> str:
    max_agent_steps = max_steps if max_steps > 0 else self._cfg("tool_max_agent_steps", 30)
```

### 4.5 Task 管理增强

修改位置：`tool_task_set`（L2473 起）

4.5.1 支持自定义 name：
```python
async def tool_task_set(self, event, action, task_description="", task_id="",
                        task_name="",  # ← 新增
                        source_pointer="", steps="[]", wake_condition=""):
```

在 create 时：
```python
tid = f"task-{FlashLiteEngine._task_counter:04d}"
display_name = task_name or task_description[:30]
meta["display_name"] = display_name
```

4.5.2 改进 list 展示：
```python
line = f"  {tid} [{display_name}]: {status}"
```

---

## Stage 5: 新工具 + 使用规范文档

### 5.1 新增 wait 工具

在 `FlashLiteEngine` 类中新增：

```python
async def tool_wait(self, event: AstrMessageEvent, seconds: int = 10):
    \'\'\'等待指定时间后返回。用于定时提醒、延迟操作。

    Args:
        seconds(number): 等待秒数（1-300，默认10）
    \'\'\'
    seconds = max(1, min(seconds, 300))
    await asyncio.sleep(seconds)
    return f"已等待 {seconds} 秒，当前时间: {datetime.now().strftime('%H:%M:%S')}"
```

同时在 `Sandbox/base_tools/` 下创建 `wait.tool.json`。

### 5.2 新增 grep 工具

```python
async def tool_grep(self, event: AstrMessageEvent, pattern: str = "", 
                    path: str = "workspace/", max_results: int = 20):
    \'\'\'在 Sandbox 内搜索文件内容。

    Args:
        pattern(string): 搜索模式（支持简单文本匹配）
        path(string): 搜索路径（相对 Sandbox 根，默认 workspace/）
        max_results(number): 最大结果数（默认20）
    \'\'\'
    if not pattern:
        return "错误: pattern 不能为空"
    results = []
    search_root = os.path.join(self._sandbox_root, path)
    for root, dirs, files in os.walk(search_root):
        for fname in files:
            if fname.endswith(('.pyc', '.exe', '.dll', '.png', '.jpg')):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding='utf-8', errors='ignore') as f:
                    for line_num, line in enumerate(f, 1):
                        if pattern.lower() in line.lower():
                            rel = os.path.relpath(fpath, self._sandbox_root)
                            results.append(f"{rel}:{line_num}: {line.strip()[:100]}")
                            if len(results) >= max_results:
                                break
            except: pass
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    return "\n".join(results) if results else f"未找到匹配 '{pattern}' 的内容"
```

### 5.3-5.6 使用规范文档

在 `_build_tool_model_system` 和主模型 inject_parts 中追加规范：

**草稿纸使用规范**：
```
## 草稿纸（drafts）使用指南
- 用途：思考规划、中间结果记录、多步任务笔记
- 位置：workspace/drafts/ 或 agent_drafts/{task_id}/
- 命名：{用途}_{日期}.md（如 plan_20260406.md）
- 清理：任务完成后可删除临时草稿
- 思考模式：遇到复杂问题先写草稿组织思路再行动
```

**base_tools 使用规范**：
```
## 工具定义（base_tools）
- 位置：base_tools/（只读）
- 格式：.tool.json — 含 name/description/parameters/timeout_ms
- 用途：参考格式创建自定义工具（写入 workspace/my_tools/）
- 自定义工具需要 .tool.json 定义文件 + 同名 .py 脚本
```

**指针系统规范**：
```
## 指针系统（source_pointer）
- 格式：文件路径 | 消息ID | 上下文标记
- 示例：workspace/report.md | msg:1234567 | ctx:group_<GROUP_B>
- 用途：追溯任务来源、关联 Memory 条目原始出处
```

---

## Stage 6: KVCache 激活 + 输出优化

### 6.1 FlashLite KVCache 激活

修改位置：`_call_flash_lite` 方法

当前 FlashLite 每次调用都重复发送完整 system prompt。接入 KVCache：

```python
async def _call_flash_lite(self, ...):
    # 固定内容 = system prompt 基础部分 + Knowledge
    fixed_content = self._build_flash_lite_system()  # 不含动态 Memory 索引
    
    # 动态内容 = Memory 索引 + 消息上下文
    memory_index = await self._build_memory_mini_index()
    dynamic_parts = memory_index + "\n\n" + message_context
    
    # 确保 cache
    cache_name = await self._kv_cache.ensure_cache(
        fixed_content=fixed_content,
        model=self._flash_lite_model
    )
    
    # 构建 payload
    payload = {
        "cachedContent": cache_name,  # ← 使用缓存
        "contents": [{"role": "user", "parts": [{"text": dynamic_parts + user_prompt}]}],
        "generationConfig": {...}
    }
```

注意：需要确认 `KVCacheManager.ensure_cache()` 的接口是否匹配，可能需要适配。

### 6.2 工具模型 KVCache

工具模型也有固定的 system prompt，可以独立缓存：

```python
# 在 __init__ 中为工具模型创建独立 KVCacheManager
self._tool_kv_cache = KVCacheManager(
    api_key_getter=self._get_tool_api_key,
    model=self._tool_model or "gemini-3-flash-preview"
)
```

### 6.3 主模型 KVCache

主模型走 AstrBot 框架 `gemini_source.py`，需要在框架层支持 `cachedContent`。

如果框架不支持：记录为未来改进，不在本轮实现。

### 6.4 分轮续发

推荐方案 B：移除"分多轮说"提示，改为精确的长度约束：
```
输出控制：
- 单次回复不超过 300 字
- 如果内容超长，优先用工具（modify_file/agent_draft）存储完整内容，口头指向文件路径
- 不要说"我分多轮说"——你只有一次输出机会
```

### 6.5 persona 加固

将 Section 1 的核心风格约束提取，追加到 persona prompt 末尾（在 DB 层面修改 persona 文本），使其不被 inject_parts 推远。

---

## 验证计划

### 每 Stage 的自检

| Stage | 验证方法 |
|-------|---------|
| 1 | 查看日志确认 Section 0 注入、时间显示、OFFICE 处理统一 |
| 2 | 发送群消息后检查 FlashLite 收到的 message_str 格式 |
| 3 | 写入测试 Memory → 确认索引注入 → 发相关消息确认精确召回 |
| 4 | 创建 task → 确认工具模型可调用 search/memory 等工具 |
| 5 | 测试 wait/grep 工具 → 审阅草稿规范文档 |
| 6 | 对比 KVCache 前后 API 延迟 → 确认 cache 命中率 |

### 全面回归

- 各窗口类型（群聊/私聊）的 FlashLite 触发
- Memory 召回准确率（准备 5 条测试 Memory + 5 条测试消息）
- 工具模型 Task 的多步编排
- 主模型输出风格是否符合 persona 设定
