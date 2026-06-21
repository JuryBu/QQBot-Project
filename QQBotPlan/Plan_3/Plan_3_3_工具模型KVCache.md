# Plan_3_3_工具模型KVCache.md — 工具模型 静态/动态分离

> 优先级：P0 | 预估收益：工具模型缓存命中率 ↑
> 前置依赖：Plan_3_2 Stage 1 完成后可并行 | 影响范围：main.py `_build_tool_model_system` + `_call_tool_model`
> 最后更新：2026-04-13 | 状态：方案定稿

---

## 一、当前结构

### `_build_tool_model_system()` (L1295-1396)

```
systemInstruction:
  ├─ # 身份与体系认知                           ← 纯静态
  ├─ # 工作环境
  │   ├─ Sandbox 根目录: {sandbox_path}          ← 准静态（首次启动后不变）
  │   └─ 其余目录说明                            ← 纯静态
  ├─ # 可用工具分类
  │   ├─ 核心三件套                              ← 纯静态
  │   └─ 扩展工具: {tool_list}                   ← 准静态（极少变化）
  ├─ # base_tools 规范                           ← 纯静态
  ├─ # 系统维护工具                              ← 纯静态
  ├─ # 定期 Review 职责（7步维护流程）            ← 纯静态
  ├─ # system_report 日志格式                    ← 纯静态
  ├─ # 工具使用场景指南                          ← 纯静态
  ├─ # 工作原则                                  ← 纯静态
  ├─ # 当前 Knowledge 概况: {knowledge_snapshot}  ← 动态 ⚠️
  └─ # 系统时间: {now}                           ← 动态 ⚠️
```

### KV Cache（已有）

工具模型已经有 KV Cache (L1618-1630)，但因为 system prompt 末尾的 Knowledge 和时间是动态的，缓存经常失效。

---

## 二、改造方案

与 Plan_3_2 的 FlashLite 改造手法完全一致：

### 2.1 改造后结构

```
systemInstruction:  ← 完全不变，100% 缓存命中
  ├─ # 身份与体系认知
  ├─ # 工作环境（含 sandbox_path 和 tool_list，视为静态）
  ├─ # 可用工具分类
  ├─ # base_tools 规范
  ├─ # 系统维护工具
  ├─ # 定期 Review 职责
  ├─ # system_report 日志格式
  ├─ # 工具使用场景指南
  └─ # 工作原则

contents[user]:
  ├─ # 当前 Knowledge 概况                       ← 动态前缀
  ├─ # 系统时间                                  ← 动态前缀
  ├─ ---
  └─ {原始 task prompt}                          ← 不变
```

### 2.2 代码改动

#### A. `_build_tool_model_system()` 移除动态部分

```python
def _build_tool_model_system(self) -> str:
    # sandbox_path 和 tool_list 保留在 system prompt（视为静态）
    sandbox_path = str(getattr(self._sandbox, '_root', 'Sandbox/'))
    tool_list = ...  # 保持原有逻辑

    return (
        "# 身份与体系认知\n..."
        f"- Sandbox 根目录: {sandbox_path}\n..."
        f"{tool_list}\n..."
        "# 工作原则\n..."
        # ❌ 不再拼接 Knowledge 和时间
    )
```

#### B. `_call_tool_model()` 动态内容拼到 user prompt 前缀

```python
async def _call_tool_model(self, prompt, ...):
    _tool_system = self._build_tool_model_system()  # 纯静态

    # 动态前缀
    _dynamic_prefix = ""
    knowledge_snapshot = self._knowledge.get_prompt_text() or "暂无"
    _dynamic_prefix += f"# 当前 Knowledge 概况\n{knowledge_snapshot}\n\n"
    _dynamic_prefix += f"# 系统时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n---\n\n"

    # 拼接：动态前缀 + inject_context(可选) + 原始 prompt
    _effective_prompt = _dynamic_prefix
    if context_text:
        _effective_prompt += f"## 当前对话上下文\n{context_text}\n\n---\n\n"
    _effective_prompt += prompt

    messages = [{"role": "user", "parts": [{"text": _effective_prompt}]}]
    ...
```

### 2.3 Token 凑够要求

移除 Knowledge 和时间后，纯静态部分的 token 数需要评估。工具模型的 system prompt 相当长（身份说明 + Review 7步流程 + 日志格式 + 工具指南 + 工作原则），预估 **1500-2000 tokens**，大概率足够 1024 阈值。

如果不够（小概率），可在"工具使用场景指南"中补充更多示例。

---

## 三、改动清单

### Stage 1：工具模型静态/动态分离
- [ ] `_build_tool_model_system()`: 移除末尾的 Knowledge 快照和系统时间
- [ ] `_call_tool_model()`: Knowledge+时间作为动态前缀拼到 user prompt
- [ ] 确保 `context_text` 注入逻辑不受影响（在动态前缀之后）
- [ ] 验证静态部分 token 数 ≥ 1024（用 countTokens API）
- [ ] 如不够，在工具使用场景指南中补充示例

### Stage 2：验证
- [ ] 定期 Review 模式正常工作（system_report 权限切换不受影响）
- [ ] Checkpoint 审阅模式不受影响（它用不同的 system prompt）
- [ ] 多轮工具调用循环正常（agent loop 无异常）
- [ ] usageMetadata 中 cached_content_token_count > 0 确认缓存命中

---

## 四、注意事项

1. **sandbox_path 和 tool_list 视为静态**：虽然理论上可变，但实际运行中：
   - sandbox_path 在插件加载时确定，永不变化
   - tool_list 仅在新增/删除 base_tools/*.tool.json 时变化，极其罕见
   - 即使偶尔变化，缓存只是被刷新一次，不影响后续命中

2. **不引入额外优化机制**：不做思考预算按任务调节、不做智能终止、不做模型降级。这些参数已可在面板调整，保持简单。

3. **与 Plan_3_2 的关系**：手法完全一致（移出动态 → 拼 user prefix），但改动文件和函数不同，独立执行不冲突。
