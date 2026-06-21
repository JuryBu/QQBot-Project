# 🤖 三模型分工设计 (Plan_1_models)

> 关联: [Plan_1_architecture.md](./Plan_1_architecture.md)

---

## 模型阵列

| 角色 | 模型 | 定位 | Think |
|------|------|------|-------|
| 🔴 主模型 | `gemini-2.5-flash` 或 `gemini-3-pro-preview` | 实际对话和决策 | ✅ 带思考 |
| 🟢 Flash Lite | `gemini-3.1-flash-lite-preview` | 中断事件处理器 | ✅ 中等思考额度 |
| 🟡 工具模型 | `gemini-3-flash-preview` | 子代理/工具任务执行 | ✅ 可选（主模型可在 task 中指定 no-thinking） |

### Gemini API 可用模型清单（2026-04-02 查询）

| 模型 | 输入 | 输出 | CachedContent |
|------|------|------|---------------|
| `gemini-3.1-flash-lite-preview` | 1,048,576 | 65,536 | ✅ |
| `gemini-3-flash-preview` | 1,048,576 | 65,536 | ✅ |
| `gemini-2.5-flash` | 1,048,576 | 65,536 | ✅ |
| `gemini-2.5-pro` | 1,048,576 | 65,536 | ✅ |
| `gemini-3.1-pro-preview` | 1,048,576 | 65,536 | ✅ |

> 所有核心模型均支持 `createCachedContent`（KV Cache），1M 上下文窗口。

---

## 🟢 Flash Lite 模型——"CPU 中断事件"

### 地位
Flash Lite 是整个系统的**核心调度器**，如同 CPU 的中断处理机制。它不负责"聊天"，只负责：
1. 维护对话（压缩 + Knowledge 更新）
2. 决定是否唤醒主模型

### 触发方式

| 类型 | 触发条件 | 频率 |
|------|----------|------|
| 同步触发 | 每隔 ~5 条群消息 | 高频（群活跃时每分钟多次） |
| 异步触发 | @老板娘 / 唤醒词匹配 | 事件驱动 |
| 异步触发 | CHECKPOINT token 上限 | 事件驱动 |
| 异步触发 | 工具反馈结果 | 事件驱动 |

### 执行任务（按序）

1. **CHECKPOINT 压缩**：检查当前窗口是否超过 token 上限
   - 有 → 调用自身对上限之前的内容进行 10-35% 压缩
   - 无 → 跳过
2. **Knowledge 更新**：根据压缩前的上下文更新 Knowledge 中本窗口对应部分
   - 排出旧内容 + 加入新内容
3. **响应判断**：判断是否需要通知主模型响应
   - @/关键词触发 → 强制通知
   - 语义相关 → 基于上下文判断
   - 不相关 → 静默
4. **工具判断**：判断是否需要进一步工具调用
   - 需要 → 调用工具模型
   - 不需要 → 直接反馈主模型

### 请求体构成

```
Flash Lite 请求体 = knowledge（全局）
                  + 系统环境说明
                  + 任务要求内容（压缩/判断指令）
                  + 系统维护前的 C'（当前窗口上下文）
                  + 工具系统内容
```

---

## 🔴 主模型——"实际看聊天参与对话的人"

### 地位
主模型是一个**时不时被 Flash Lite 喊过来看聊天记录的人**。它拥有完整的工具知识和 Sandbox 操作权限。

### 触发条件
- **当且仅当** Flash Lite 触发 + 系统触发
- 不可被直接触发（@事件先经 Flash Lite 判断后由系统触发）

### 能力清单
- ✅ 回复消息（文字、图片、文件）
- ✅ 工具 Task 创建和管理
- ✅ 设置子代理 Agent 任务
- ✅ Check 工具进程状态
- ✅ Sandbox 空间操作（文件、代码、命令）
- ✅ Memory 记忆写入/读取
- ✅ QQ_data_original 查看原始消息
- ✅ generate_image 图片生成
- ✅ search/web_search 搜索
- ✅ 浏览器子代理操作

### "草稿纸"机制
主模型在 Sandbox 空间内拥有**自己的工作区**：
- 类似 Antigravity 的 `implementation_plan.md` 和 `task.md`
- 用于规划复杂任务、记录思路、管理进度
- 持久化在 Sandbox 中

### 请求体构成

```
主模型请求体 = knowledge（全局）
             + 系统环境说明
             + 角色设定内容（老板娘人格 Prompt）
             + 工具系统 resource 说明（渐进式披露）
             + 系统维护的 C'（CHECKPOINT 压缩 + 最近消息）
             + 工具调用/结果内容
```

---

## 🟡 工具模型——"子代理"

### 地位
工具模型是主模型的**执手**，负责执行具体任务。

### 触发方式

| 方式 | 说明 |
|------|------|
| 简单工具调用 | 主模型直接调用 Skills/MCP/内置工具，接口进接口出 |
| 子代理模式 | 调用工具模型 + 设置任务 → 在 Sandbox 内活动操作 → 完成任务 |
| Task 进程模式 | 主模型创建 Task（包含：创建原因、标记信息源头指针、任务内容）→ 并行创建多个工具调用/子代理任务 → 设置 check 时机 |

### Task 进程结构

```json
{
  "task_id": "task_001",
  "created_by": "main_model",
  "reason": "用户要求搜索并总结某个话题",
  "source_pointer": "GroupMessage:<GROUP_B>#msg_12345",
  "steps": [
    {"type": "tool_call", "tool": "web_search", "params": {...}, "parallel": true},
    {"type": "sub_agent", "task": "summarize_results", "parallel": true},
    {"type": "check", "after": "all_above", "notify": "main_model"}
  ]
}
```

### 请求体构成

```
工具模型请求体 = knowledge（全局）
              + 系统环境说明
              + Task 内容 / 任务内容
              + 工具系统内容
              + 工具 resource 说明
```

---

## 定期维护

工具模型会**定期 launch Sandbox 内 Review**：
- 安全检查（排查异常文件/权限问题）
- 内部文件整理
- 写维护报告到 `system_report/` 目录（只读区域，仅 Review 时开放写入）
- 报告内容：空间使用、异常检测、清理建议
