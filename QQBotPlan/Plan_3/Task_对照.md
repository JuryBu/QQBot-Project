# Task_对照.md — Plan_3 系列逐 Stage 原始意图 vs 实际效果对照 + 修复计划

> 创建时间：2026-04-13 09:18 | 最后更新：2026-04-13 10:25
> 目的：逐一比对每个 Stage 的原始 Plan 设计意图与当前代码实现，列出偏差和修复方案

---

## 检查标注说明

- ✅ 已实现，与 Plan 一致
- ⚠️ 部分实现 / 有偏差
- ❌ 未实现
- 📝 降级/方案变更（合理且有原因）

---

## Stage 1: FlashLite 静态/动态分离 — system prompt 改造

**来源**: Plan_3_2 第二章

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| `_build_flash_lite_system()` 移除末尾 Knowledge 快照和系统时间 | 已移除，Knowledge/时间已迁移到 user prefix | ✅ |
| 新增"任务执行指南"段落（群聊/私聊判断规则） | 已包含完整群聊/私聊场景判断规则 | ✅ |
| 新增"Memory 召回指南"段落 | 已包含 MEMORY_HINT 用法+排序规则 | ✅ |
| `_build_judgment_prompt()` 移除 `chat_rules` 和 `你的任务` 段落 | 已清除，仅保留纯数据 | ✅ |
| Knowledge/时间/Memory 迷你索引拼到 user prompt 前缀 | 已在 `_call_flash_lite` 中实现 | ✅ |

**结论**: ✅ 完全一致，无偏差

---

## Stage 2: FlashLite KVCache 验证

**来源**: Plan_3_2 第二章 + Task_3 T2

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 验证 system prompt hash 稳定性 | 测试脚本验证 hash 在多次调用间一致 | ✅ |
| 验证 cache 命中率 ≥ 80% | test_stage6_kvcache_all.py 综合测试覆盖 | ✅ |
| 验证静态 system token 数 ≥ 1024 | countTokens API 验证通过 | ✅ |

**结论**: ✅ 完全一致

---

## Stage 3: 主模型 inject_flashlite_context 改造

**来源**: Plan_3_2 第三章

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| `inject_flashlite_context` 静态内容放 systemInstruction | personality/通用规则等静态 Section 已固化 | ✅ |
| 动态内容拼到 user message 前缀 | 已通过 user_prefix_sections 实现 | ✅ |
| Sandbox 环境当动态处理（保守策略） | Sandbox Section 16 放入 user prefix | ✅ |

**结论**: ✅ 完全一致

---

## Stage 4: 主模型 KVCache + gemini_source 适配

**来源**: Plan_3_2 第三章

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| gemini_source.py 支持 system_instruction 的 KV Cache | 框架已支持隐式缓存 | ✅ |
| 验证主模型缓存命中 | 测试脚本验证 cached_tokens > 0 | ✅ |

**结论**: ✅ 完全一致

---

## Stage 5: 工具模型 静态/动态分离

**来源**: Plan_3_3

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 工具模型 system prompt 静态化 | `_build_tool_system` 已固化静态部分 | ✅ |
| 动态上下文拼到 user prefix | 工具特定上下文在 user 前缀 | ✅ |
| 不降级模型（维持 flash-preview） | 未引入模型切换逻辑 | ✅ |

**结论**: ✅ 完全一致

---

## Stage 6: 三模型综合 KVCache 测试

**来源**: Task_3 Stage 6

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 三模型同时验证 cache 命中率 | test_stage6_kvcache_all.py 综合验证 | ✅ |
| system prompt hash 稳定性 | 多次调用 hash 一致性验证通过 | ✅ |

**结论**: ✅ 完全一致

---

## Stage 7: FlashLite 采样面板参数化

**来源**: Plan_3_1 + Plan_3_4 第四章

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| sync_interval 可面板配置 | _conf_schema.json + BossLady Console 均支持 | ✅ |
| 时间兜底间隔/最低消息数 可配置 | sync_time_min_msgs 已实现 | ✅ |
| 采样模式（固定/动态）可切换 | sampling_mode 字段 + UI 下拉框 | ✅ |

**结论**: ✅ 完全一致

---

## Stage 8: 智能动态采样实现

**来源**: Plan_3_1 第二/三章

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 4级活跃度阈值 + 对应间隔 | dynamic_sampling.thresholds/intervals 已实现 | ✅ |
| 滑动窗口（10分钟）检测活跃度 | window_minutes 可配置，默认 10 | ✅ |
| 活跃群少采样，不活跃群多采样 | 阈值越高间隔越大，逻辑正确 | ✅ |

**结论**: ✅ 完全一致

---

## Stage 9: 每群独立配置

**来源**: Plan_3_1 + Plan_3_4 §4.2

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 群号级别覆盖 sync_interval | group_overrides 机制已实现 | ✅ |
| 是否启用 FlashLite 开关 | group_overrides.enabled 字段 | ✅ |
| 优先级：群覆盖 > 动态 > 全局 | 代码逻辑验证一致 | ✅ |
| 每群独立配置 UI（下拉+滑块+删除） | BossLady Console 未实现独立 UI | ❌ |

**偏差 [DEV-A]**: 后端 API 支持 group_overrides 字段读写，但 BossLady Console 前端没有制作表格式配置界面（下拉选群号+滑块设置间隔+删除按钮），用户只能通过 AstrBot 原生面板或直接编辑 config.json。

---

## Stage 10: 采样优化前后端链路验证

**来源**: Task_3 Stage 10

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| _conf_schema.json 面板配置验证 | AstrBot 面板测试通过 | ✅ |
| BossLady Console 采样配置区域 | 已实现固定/动态模式+参数+保存 | ✅ |

**结论**: ✅ 完全一致

---

## Stage 11: 成本监控数据采集层

**来源**: Plan_3_4 §2 + §5 Stage 1

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 每次 API 调用后提取 usageMetadata | FlashLite/工具模型/主模型三条路径均记账 | ✅ |
| 异步写入不阻塞主流程 | debounce 5s 批量 + asyncio.to_thread | ✅ |
| 内置定价表 PRICING | cost_tracker.py 含定价表 | ✅ |
| 主模型通过 @filter.on_llm_response() 钩子记账 | 已实现 Provider 层钩子 | ✅ |
| **成本归因到正确窗口** | **依赖全局 `_current_window_key`，并发下会串窗** | ⚠️ |

**偏差 [DEV-E]**: （严重）成本记账依赖全局状态 `self._current_window_key`，详见下方修复计划。

---

## Stage 12: 成本监控统计层 + API

**来源**: Plan_3_4 §5 Stage 2

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| CostTracker 聚合统计（按模型/窗口/时间） | cost_tracker.py 实现完整 | ✅ |
| 按天归档，保留 90 天 | JSON 按天文件 + cleanup 方法 | ✅ |
| 缓存命中率计算 | cached_tokens / prompt_tokens | ✅ |
| 汇率配置 USD→CNY（默认 7.2） | 构造函数参数支持自定义 | ✅ |
| 后端 API 路由 | cost.py 5 个端点 | ✅ |

**结论**: ✅ 完全一致（统计层本身无偏差，数据准确性依赖 Stage 11 的 DEV-E 修复）

---

## Stage 13: 成本监控前端面板

**来源**: Plan_3_4 §3

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 新增"成本监控"面板页 | BossLady Console page-cost 已添加 | ✅ |
| 概览卡片区（6个指标） | 4 卡片 + Token 明细区 | ⚠️ |
| 按模型分类统计表格 | 已实现 | ✅ |
| 按窗口分类统计表格 | 已实现 | ✅ |
| 可视化图表（折线/饼/柱/面积图） | CSS 条形图代替，未引入 Chart.js | ⚠️ |
| 图表维度切换控件 | 未实现 | ❌ |
| 自动刷新（30s 轮询） | 未实现 | ❌ |
| 今日/本周/本月切换 | period 下拉选择已实现 | ✅ |

**偏差 [DEV-B]**: Chart.js 图表 — 用纯 CSS 条形图代替
**偏差 [DEV-C]**: 自动刷新 — 未实现 30s 轮询
**偏差 [DEV-D]**: 维度切换控件 — 未实现

---

## Stage 14: 采样配置面板 UI

**来源**: Plan_3_4 §4

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 固定/动态模式切换 | 下拉框 + 子参数区域显隐 | ✅ |
| 固定模式参数 | 两个 input 字段 | ✅ |
| 动态模式参数 | 窗口/阈值/间隔输入框 | ✅ |
| 每群独立配置 UI | 未实现 | ❌ |
| `_conf_schema.json` 覆盖全部 Plan_3 新参数 | 缺 dynamic_sampling/group_overrides/review_interval_hours 等 | ⚠️ |

**偏差 [DEV-A]**: 每群独立配置 UI 未做（同 Stage 9）
**偏差 [DEV-F]**: `_conf_schema.json` 未补齐 Plan_3 新参数

---

## Stage 15: 前后端整合链路验证

| 原始意图 | 实际效果 | 状态 |
|---------|---------|------|
| 启动后端服务验证 | BossLady Console 启动成功 | ✅ |
| MCP 截图 | 截图验证通过 | ✅ |
| 原有功能不变 | 未受影响 | ✅ |
| 模拟数据注入测试 | 6条数据注入渲染OK | ✅ |

**结论**: ✅ 完全一致

---

## Stage 16: Codex Review

**结论**: ✅ 三轮 Review 完成，发现的可立即修复项已修复

---

## Stage 17: 最终验证 + 收尾

**结论**: 🔲 进行中

---

## 偏差总表

| 编号 | 严重度 | 描述 | 涉及 Stage |
|------|--------|------|-----------|
| DEV-A | 🟡中 | 每群独立配置 UI（BossLady Console 下拉+滑块+删除表格） | 9, 14 |
| DEV-B | 🟡中 | Chart.js 图表（折线/饼/柱/面积）降级为 CSS 条形图 | 13 |
| DEV-C | 🟢小 | 自动刷新（30s 轮询）未实现 | 13 |
| DEV-D | 🟡中 | 图表维度切换控件未实现（依赖 DEV-B） | 13 |
| DEV-E | 🔴严重 | `_current_window_key` 全局状态并发串窗导致成本错误归因 | 11 |
| DEV-F | 🟢小 | `_conf_schema.json` 未覆盖 dynamic_sampling/group_overrides 等新参数 | 14 |
| DEV-G | 🟢小 | 私聊窗口命名双标准 PrivateMessage vs FriendMessage | 11 |

---

# 修复计划

> 以下修复 Stage 按优先级和依赖顺序排列

---

## 修复 Stage R1: `_current_window_key` 并发串窗重构 [DEV-E] ✅ 已完成

**优先级**: 🔴 P0 — 并发环境下会导致成本数据严重错误归因

### 问题详解

`self._current_window_key` 是一个实例级全局变量，在消息入口处赋值（L631群聊、L681私聊），在 API 调用**完成后**通过 `getattr(self, '_current_window_key', 'unknown')` 读取。异步环境中任何 `await` 都可能导致控制权切换，别的群消息进来覆盖此变量：

```
T1: 群A消息 → self._current_window_key = "GroupMessage:A"
T2: 群A FlashLite API 发起 await httpx.post(...)  ← 挂起
T3: 群B消息 → self._current_window_key = "GroupMessage:B"  ← 被覆盖
T4: 群A API 返回 → 读取 window_key = "GroupMessage:B"  ← 错了！
```

### 必读文件

- `main.py` L171（定义），L631/L681（赋值），L1633/L1862/L2251/L2387/L3271（读取）
- `cost_tracker.py` → `record()` 的 window_key 参数
- `Plan_3_4_面板与成本监控.md` §2.4（数据存储格式中的 window_key 字段）

### 修改方案

**核心思路**: 废除 `self._current_window_key` 全局状态，改为**参数显式传递**。

1. **`_call_flash_lite()`** (L1515): 新增 `window_key: str = "unknown"` 参数
   - 内部记账直接用参数 `window_key`，不再读 `self._current_window_key`
   - 所有调用点（L862/L1008/L1154/L4761/L4774/L4783/L4797）传入 `window_key`

2. **`_call_tool_model()`** (L1678): 新增 `window_key: str = "unknown"` 参数
   - 内部记账直接用参数
   - 所有调用点（L788/L3857/L4203/L4231/L5179）传入 `window_key`

3. **`track_main_model_cost()`** (L3237, on_llm_response 钩子):
   - 从 `event` 参数提取窗口信息（`event.session_id` + `event.get_sender_id()`）
   - 不再读 `self._current_window_key`

4. **清理**: 移除 L171 的 `self._current_window_key` 定义和 L631/L681 的赋值

### 测试

- 编写并发测试：模拟两个群同时发消息，验证记账窗口不串
- 验证所有已有测试脚本仍通过（test_stage11_cost_tracker.py）

### Codex

- 修改完成后启动 Codex gpt-5.3 xhigh 审核此次重构的正确性

---

## 修复 Stage R2: 私聊窗口命名统一 [DEV-G] ✅ 已完成（R1已顺带解决）

**优先级**: 🟢 P2 — 不影响功能但制造统计噪声

### 必读文件

- `main.py` L681（`PrivateMessage`）、L1129/L1136/L1993/L2127/L2188/L2728/L2852/L4034/L4184/L5169（`FriendMessage`）
- `cost_tracker.py` → `get_by_window()` 按 window_key 分组

### 修改方案

全局统一为 `FriendMessage`（因为绝大多数位置已经使用此命名）：
- L681: `self._current_window_key = f"PrivateMessage:{user_id}"` → 改为 `FriendMessage`
- 注：此行在 R1 中会被废除，如果 R1 先做则此处自动解决；如果 R2 先做则直接改 L681

### 测试

- grep 确认全代码库无残留 `PrivateMessage` 引用
- 检查历史 cost_logs JSON 文件中是否有 `PrivateMessage` 记录（如有则需做数据迁移脚本或兼容处理）

---

## 修复 Stage R3: `_conf_schema.json` 补齐 Plan_3 新参数 [DEV-F] ✅ 已完成

**优先级**: 🟢 P1 — AstrBot 原生面板看不到新参数

### 必读文件

- `_conf_schema.json`（当前 7 个字段）
- `main.py` L142-155（dynamic_sampling/group_overrides 初始化）
- `main.py` L248-257（cost_tracker 初始化）
- `Plan_3_1_FlashLite采样优化.md`（采样参数定义）

### 修改方案

在 `_conf_schema.json` 中新增以下字段：

```json
{
  "dynamic_sampling": {
    "description": "动态采样配置（JSON格式）",
    "type": "string",
    "hint": "包含 window_minutes、thresholds、intervals 三个字段",
    "default": "{\"window_minutes\":10,\"thresholds\":[5,15,30],\"intervals\":[3,5,10,15]}"
  },
  "group_overrides": {
    "description": "每群独立采样配置（JSON格式）",
    "type": "string",
    "hint": "格式: {\"群号\":{\"sync_interval\":10,\"enabled\":true}}",
    "default": "{}"
  },
  "review_interval_hours": {
    "description": "Sandbox 定期 Review 间隔（小时）",
    "type": "int",
    "hint": "建议 12-48",
    "default": 24,
    "minimum": 1
  }
}
```

> 注：`_conf_schema.json` 的 type 只支持 string/int/float/bool，复合类型（dict）需用 string 类型 + JSON 字符串

### 测试

- 启动 AstrBot → 打开插件配置面板 → 确认新字段可见且默认值正确
- 修改配置 → 保存 → 验证 config.json 写入正确

---

## 修复 Stage R4: 自动刷新 [DEV-C] ✅ 已完成

**优先级**: 🟢 P2

### 必读文件

- `BossLady_Console/frontend/app.js` → `loadCostPage()` 函数

### 修改方案

在 `loadCostPage()` 末尾添加自动刷新：

```javascript
// 自动刷新（30s）
if (window._costRefreshTimer) clearInterval(window._costRefreshTimer);
window._costRefreshTimer = setInterval(() => {
    if (document.getElementById('page-cost').style.display !== 'none') {
        loadCostPage();
    }
}, 30000);
```

页面切走时清除定时器（在路由切换函数中添加）。

### 测试

- MCP 截图验证页面正常 + 30s 后数据自动更新

---

## 修复 Stage R5: 每群独立配置 UI [DEV-A] ✅ 已完成

**优先级**: 🟡 P2 — 功能已有后端支持，仅缺前端

### 必读文件

- `Plan_3_4_面板与成本监控.md` §4.2（UI 设计稿）
- `BossLady_Console/frontend/index.html` → 采样配置区域
- `BossLady_Console/frontend/app.js` → saveSamplingConfig
- `BossLady_Console/backend/routers/models.py` → GET/POST flashlite 中 group_overrides 字段

### 修改方案

在采样策略配置区域下方新增"群聊独立配置"子区域：
- 表格（群号/间隔/启用/删除按钮）
- 添加行（群号输入框 + 间隔滑块 + 添加按钮）
- 保存时将表格数据序列化为 `group_overrides` JSON 写入后端

### 测试

- MCP 截图验证 UI 渲染
- 通过 UI 添加/删除群覆盖 → API 验证保存成功
- 启动 Codex high 审核前端 XSS 安全

---

## 修复 Stage R6: Chart.js 图表 + 维度切换 [DEV-B, DEV-D] ✅ 已完成

**优先级**: 🟡 P3 — 纯体验优化

### 必读文件

- `Plan_3_4_面板与成本监控.md` §3.1.4（可视化图表需求）
- `BossLady_Console/frontend/index.html` → 调用趋势区域
- `BossLady_Console/frontend/app.js` → loadCostPage 中的时间轴渲染

### 修改方案

1. 引入 Chart.js CDN（`<script src="https://cdn.jsdelivr.net/npm/chart.js">`）
2. 替换调用趋势区域为 Canvas + Chart.js 折线图
3. 新增模型分布饼图、窗口分布柱状图
4. 添加维度切换 Tab（按模型/按窗口/按时间）

### 测试

- 注入测试数据 → MCP 截图验证图表渲染
- 切换维度 → 截图验证切换生效

---

## 修复 Stage R7: 全量复验 + Codex 终审

**优先级**: 收尾

### 任务清单

- 运行所有测试脚本确认全部通过
- MCP 截图最终页面效果
- 启动 Codex gpt-5.3 xhigh 做最终全量审核
- 更新 Plan_3.md 总纲状态为"已完成"
- 写 Report_3.md 收尾报告
- 保存记忆到 memory-store

---

## 修复执行顺序建议

```
R1 (并发串窗重构, P0)
  → R2 (私聊命名统一, P2, R1可能顺带解决)
    → R3 (conf_schema补齐, P1)
      → R4 (自动刷新, P2)
        → R5 (每群UI, P2)
          → R6 (Chart.js图表, P3)
            → R7 (终审收尾)
```

每个修复 Stage 完成后进行自主 Review，R1 和 R7 完成时各启动一次 Codex 终审。
