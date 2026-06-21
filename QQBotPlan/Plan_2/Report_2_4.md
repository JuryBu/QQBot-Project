# Report_2_4: system_report 安全性与 Review 流程深度审计

> 审计时间: 2026-04-07 16:21
> 审计范围: main.py (4073行), sandbox.py (734行全文)
> 审计方式: 逐行追踪完整调用链

---

## 问题 1: 工具模型被 Review 唤醒时是否知道要做 Review？

### 结论: ⚠️ 部分知道，但不够精确

**唤醒方式** (main.py L381-430):
```python
# L408: 定期 Review 启动
result = await self._call_tool_model(f"执行以下任务并返回结果:\n{review_desc}")
```

工具模型收到的 user prompt 是 `review_desc`（L397-403），明确写了 5 步：
1. 列出 workspace/ 下文件
2. 清理临时文件
3. 检查报告
4. 统计磁盘
5. 调用 system_report 写日志

**问题在于**: 工具模型收到的 **system prompt** 是固定的 `_build_tool_model_system()`——Review 任务和普通 task_set 任务用的是**同一套 system prompt**（L1130-1172）。区别只在于 **user prompt 不同**。

所以：
- ✅ 工具模型知道"这次要做 Review"——因为 user prompt 里说得很清楚
- ⚠️ 但 system prompt 中的 Review 说明只是一般性描述，没有标注"当前唤醒类型是 Review"
- ⚠️ 工具模型**无法区分**自己是被 Review 唤醒还是普通 task

---

## 问题 2: Review 流程是完全后台自动的？前端不会被要求操作？

### 结论: ✅ 是的，完全后台自动

```python
# L405-430
import asyncio
async def _run_review():
    result = await self._call_tool_model(...)  # 工具模型执行
    await self.tool_system_report(...)          # 系统直接写日志
task = asyncio.create_task(_run_review())       # 异步后台运行
```

- ✅ Review 是 `asyncio.create_task()` 创建的后台异步任务
- ✅ 完成后自动调用 `system_report` 写日志
- ✅ 前端不会收到任何交互请求
- ✅ 控制面板只能看到日志更新（base_tools/system_report/ 下的文件）
- ⚠️ **system_report 的调用者是主进程代码**（L411-416），不是工具模型！

**重要发现**: Review 日志实际上是主进程的 `_run_review()` 闭包直接调用 `self.tool_system_report()` 写入的，**不是工具模型调用 system_report 工具写入的**。工具模型只负责执行清理/统计，返回文本结果。

---

## 问题 3: 不同唤醒的工具模型收到同一套 system prompt + 不同指示？

### 结论: ✅ 没错，但有细微差异

**System Prompt**: 固定调用 `_build_tool_model_system()` (L836-916)
- 所有唤醒场景共用
- 包含工作环境、工具分类、Review 职责说明（我们刚加的）

**User Prompt**: 因场景不同
- 定期 Review: L408 `"执行以下任务并返回结果:\n{5步清单}"`
- 普通 task_set: L2942 `"执行以下任务并返回结果:\n{用户指定描述}"`
- media_summary: L2616 `"总结以下内容..."`

**Tool Declarations**: 也是固定的
- 核心三件套 + 动态加载的 base_tools/*.tool.json
- 包括 `system_report`（没有被排除！）

---

## 问题 4: system_report 是否只有 Review 期间才可调用？

### 结论: ❌ 不是！存在安全漏洞！

当前 `system_report` 的"守卫"只有**一层参数检查**:

```python
# main.py L3976-3977
if not review_mode:
    return "错误: system_report 仅在 Review 模式下可写入。请设置 review_mode=true"
```

**漏洞分析**:

| 调用场景 | 能否调用 system_report？ | 能否设 review_mode=true？ | 能否写入？ |
|---------|------------------------|--------------------------|-----------|
| 定期 Review（主进程调用） | ✅ | ✅ 代码硬编码 true | ✅ 设计意图 |
| Review 中的工具模型 | ✅ 工具可见 | ⚠️ 如果它选择传 true | ⚠️ 能写入 |
| **非 Review 的工具模型** | ✅ 工具可见！ | ⚠️ **如果它选择传 true** | ⚠️ **能写入！** |
| **主模型**（via AstrBot） | ✅ @filter.llm_tool 注册 | ⚠️ **如果它传 true** | ⚠️ **能写入！** |

### 漏洞 1: review_mode 是工具参数而非系统状态

`review_mode` 作为函数参数暴露给所有调用方。任何能调用 `system_report` 的模型（包括主模型）都可以自行传 `review_mode=True` 绕过守卫。

**正确做法应该是**: 不暴露 `review_mode` 参数，而是检查内部状态（如 `self._current_task_type == "review"`）

### 漏洞 2: system_report 对主模型可见

`system_report` 通过 `@filter.llm_tool(name="system_report")` 注册，这意味着**主模型也能看到并调用它**。虽然 Prompt 里标注了"主模型不直接调用"，但这只是软约束。

### 漏洞 3: 工具模型在任何场景都能看到 system_report

`_call_tool_model` 的 L1095-1124 从 `base_tools/*.tool.json` 动态加载工具声明，`excluded_tools` 只排除了 `task_set` 和 `knowledge_update`，**`system_report` 没有被排除**。所以无论普通 task 还是 Review，工具模型都能看到并调用它。

---

## 问题 5: base_tools/system_report/ 的只读保护

### 结论: ⚠️ 有保护，但有特殊放行路径

sandbox.py L84-93 的写入权限检查逻辑：

```python
# sandbox.py L84-93
if allow_write:
    rel_parts = Path(rel).parts
    if rel_parts and rel_parts[0] == "base_tools":
        # base_tools 只读（system_report 例外，Review 时开放）
        if len(rel_parts) < 2 or rel_parts[1] != "system_report":
            return False, "base_tools 目录为只读"
        # ↑ 注意: base_tools/system_report/ 路径总是通过验证！
    if rel_parts and rel_parts[0] == "config":
        return False, "config 目录为只读"
```

**分析**:
- ✅ `base_tools/` 下除 `system_report/` 外都是真正只读
- ⚠️ `base_tools/system_report/` 路径在 `validate_path(allow_write=True)` 时**总是返回 valid**
- ⚠️ 真正的保护在 `modify_file` L424-429 的 `_review_mode` 检查
- ⚠️ 但 `_review_mode` 由 `tool_system_report()` 的参数控制

**保护链**:
```
validate_path → base_tools/system_report/ 放行
              → 其他 base_tools/ 拒绝

modify_file → validate_path 返回 invalid?
            → 检查 _review_mode → True = 放行 / False = 拒绝
```

实际上 `base_tools/system_report/` 的写入保护形同虚设——`validate_path` 对它直接放行，根本不走 `_review_mode` 控制。

**验证**: 工具模型调用 `agent_modify_file(path="base_tools/system_report/xxx.md", content="任意内容")` 时：
- `validate_path("base_tools/system_report/xxx.md", allow_write=True)` → **(True, 路径)** ← 直接通过！
- 完全绕过 `_review_mode` 检查

---

## 总结

| 问题 | 结论 | 严重度 |
|------|------|--------|
| 工具模型知道要做 Review？ | ⚠️ 通过 user prompt 知道，但无法区分唤醒类型 | 低 |
| 前端不被要求操作？ | ✅ 完全后台 | - |
| 同一 system prompt + 不同指示？ | ✅ 正确 | - |
| system_report 仅 Review 期间可调用？ | ❌ **参数级守卫可被绕过** | 🔴 高 |
| 日志位于只读文件夹？ | ❌ **base_tools/system_report/ 被特殊放行，validate_path 级别不受保护** | 🔴 高 |

### 需要修复的安全漏洞

1. **review_mode 参数应改为内部状态检查** — 不暴露给调用方
2. **system_report 应从主模型的 llm_tool 中隐藏** — 或在工具函数内检查调用链来源
3. **system_report 应加入 excluded_tools** — 防止非 Review 的工具模型直接调用
4. **sandbox.py validate_path 对 base_tools/system_report/ 的放行应增加 _review_mode 条件**
