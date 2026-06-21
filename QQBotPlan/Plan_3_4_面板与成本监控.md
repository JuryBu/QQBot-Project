# Plan_3_4_面板与成本监控.md — 面板可控性增强 + 成本监控系统

> 优先级：P1 | 预估收益：可视化运营、精细化成本管理
> 前置依赖：Plan_3_1/3_2/3_3 完成后做面板整合 | 影响范围：前端面板 + main.py 统计模块
> 最后更新：2026-04-13 | 状态：方案定稿

---

## 一、概述

Plan_3_4 整合所有面板改动需求，包括：
1. **成本监控面板**：基于 API 响应的 `usageMetadata` 实时跟踪 token 用量和费用
2. **FlashLite 采样参数**（与 Plan_3_1 配合）：智能动态采样参数面板化
3. **每群独立配置**：覆盖全局设置
4. **可视化图表**：按模型/窗口/时间切换的统计图

---

## 二、成本监控系统

### 2.1 数据源方案

**方案：本地 `usageMetadata` 记账 + 官方 `countTokens` API 精确计算**

不使用 GCP 控制台 API，原因：
- GCP 数据混入其他项目/用户的开支
- AI Studio 的开支和 API 调用的开支混入同一账单
- 延迟大（几小时~1天）

替代方案：
- 每次 Gemini API 调用后，从响应的 `usageMetadata` 字段提取：
  - `prompt_token_count`：输入 token 数（精确）
  - `cached_content_token_count`：缓存命中 token 数（精确）
  - `candidates_token_count`：输出 token 数（精确）
- 结合官方定价表计算费用
- 用官方 `countTokens` API（`POST models/{model}:countTokens`）做预估和校验

### 2.2 AstrBot 框架已有支持

`gemini_source.py` L525-531 已经在提取这些数据：

```python
def _extract_usage(self, usage_metadata):
    return Usage(
        input_other=usage_metadata.prompt_token_count or 0,
        input_cached=usage_metadata.cached_content_token_count or 0,
        output=usage_metadata.candidates_token_count or 0,
    )
```

需要在 FlashLite 插件侧收集这些数据并持久化记录。

### 2.3 定价表管理

从 Gemini API 定价页获取并内置定价表，支持面板手动更新：

```python
PRICING = {
    "gemini-3.1-flash-lite-preview": {
        "input": 0.25,          # $/M tokens
        "input_cached": 0.025,  # $/M tokens（缓存命中折扣）
        "output": 1.50,         # $/M tokens（含思考 token）
        "storage": 1.00,        # $/M tokens/h
    },
    "gemini-3-flash-preview": {
        "input": 0.30,
        "input_cached": 0.03,
        "output": 2.50,
        "storage": 1.00,
    },
    # ... 更多模型
}
```

### 2.4 数据存储

```python
# 每次 API 调用记录一条
{
    "timestamp": "2026-04-13T12:00:00",
    "model": "gemini-3-flash-preview",
    "call_type": "flashlite_judge|flashlite_compress|main_model|tool_model",
    "window_key": "GroupMessage:123456",
    "prompt_tokens": 5200,
    "cached_tokens": 4800,
    "output_tokens": 150,
    "cost_usd": 0.00052,
}
```

按天归档，保留 90 天历史。

---

## 三、面板展示

### 3.1 成本仪表盘（新增面板页/Tab）

#### 3.1.1 概览卡片区

| 卡片 | 数据 |
|------|------|
| 今日总成本 | $X.XXX (¥Y.Y) |
| 本周总成本 | $X.XXX (¥Y.Y) |
| 本月总成本 | $X.XXX (¥Y.Y) |
| 缓存命中率 | XX.X% (cached_tokens / total_input_tokens) |
| 今日 API 调用次数 | N 次 |
| FlashLite 采样效率 | N 触发 / M 消息 |

#### 3.1.2 按模型分类统计

| 模型 | 调用次数 | 输入 tokens | 缓存命中 | 输出 tokens | 成本 |
|------|---------|-----------|---------|-----------|------|
| FlashLite | 368 | 1.8M | 1.6M (89%) | 55K | $0.022 |
| 主模型 | 50 | 500K | 350K (70%) | 25K | $0.130 |
| 工具模型 | 5 | 100K | 80K (80%) | 20K | $0.064 |

#### 3.1.3 按窗口分类统计

| 窗口 | 消息量 | FlashLite触发 | 主模型触发 | 总 tokens | 成本 |
|------|-------|-------------|----------|----------|------|
| 群A (123456) | 200 | 40 | 12 | 800K | $0.08 |
| 群B (789012) | 150 | 30 | 10 | 600K | $0.06 |
| 私聊 (345678) | 20 | 20 | 8 | 200K | $0.03 |

#### 3.1.4 可视化图表（可切换维度）

- **时间轴**：折线图 — 按小时/天/周的成本和调用趋势
- **模型分布**：饼图 — 各模型成本占比
- **窗口分布**：柱状图 — 各群聊/私聊的成本对比
- **缓存效率**：面积图 — 缓存命中 vs 未命中的 token 趋势

图表支持切换：按模型 / 按窗口 / 按时间

### 3.2 刷新机制

**自动刷新**：每次 API 调用产生新数据时更新统计。实现方式：
- 后端在每次 API 调用后异步更新统计缓存
- 面板打开时拉取最新统计数据
- 如果面板处于打开状态，通过轮询（30s 间隔）或 WebSocket 推送更新

---

## 四、FlashLite 采样参数面板（配合 Plan_3_1）

### 4.1 新增面板区域：FlashLite 采样策略

```
FlashLite 采样策略:
  ○ 固定模式（使用全局同步间隔）
  ● 动态模式（根据群活跃度自动调整）   ← 默认

固定模式参数:
  同步间隔(条):         [5]     ← 已有
  时间兜底间隔(秒):      [60]    ← 新增
  时间兜底最低消息数:    [3]     ← 新增

动态模式参数:
  非常活跃阈值(条/10min): [30]  → 间隔 [15]
  活跃阈值(条/10min):     [15]  → 间隔 [10]
  普通阈值(条/10min):     [5]   → 间隔 [5]
  不活跃(低于普通):             → 间隔 [3]
```

### 4.2 每群独立配置

UI 设计：**下拉选择已知群号 + 滑块设置间隔**

```
群聊独立配置:
  ┌──────────────────────────────────────────┐
  │ 群号/名称          │ 同步间隔 │ 操作     │
  │ ▼ 选择群聊...      │          │          │
  ├────────────────────┼──────────┼──────────┤
  │ 测试群 (123456789) │ ══●══ 10 │ 🗑 删除  │
  │ 闲聊群 (987654321) │ ═●═══  3 │ 🗑 删除  │
  └──────────────────────────────────────────┘
  [+ 添加群聊覆盖]
```

下拉列表自动填充已知的群号（从 Knowledge 或 T 文件记录中获取），用户也可手动输入。

覆盖范围（除了 sync_interval 外，还支持）：
- 是否启用 FlashLite（完全关闭某群的消息判断）

取值优先级：群独立配置 > 智能动态 > 全局默认

---

## 五、改动清单

### Stage 1：数据采集层
- [ ] main.py: 在 `_call_flash_lite` / `_call_tool_model` 返回后提取 usageMetadata
- [ ] main.py: 记录每次调用到本地存储（JSON/SQLite）
- [ ] main.py: 异步写入，不阻塞主流程
- [ ] 内置定价表（PRICING dict），支持面板修改

### Stage 2：统计计算层
- [ ] 新增 `CostTracker` 类：聚合统计（按模型/窗口/时间）
- [ ] 按天归档，保留 90 天
- [ ] 缓存命中率计算
- [ ] 汇率配置（USD→CNY，默认 7.2）

### Stage 3：前端面板
- [ ] 新增"成本监控"面板页/Tab
- [ ] 概览卡片区（6个指标）
- [ ] 按模型/按窗口的统计表格
- [ ] 可视化图表（折线/饼图/柱状图/面积图）
- [ ] 图表维度切换控件
- [ ] 自动刷新机制

### Stage 4：采样配置面板
- [ ] FlashLite 采样策略区域（固定/动态模式切换）
- [ ] 动态模式参数（4级阈值+间隔）
- [ ] 每群独立配置 UI（下拉+滑块+删除）
- [ ] 后端 config_schema 对应字段

### Stage 5：整合验证
- [ ] 面板前后端链路测试（子代理/MCP 启动验证）
- [ ] 数据准确性验证（对比 GCP 控制台数据）
- [ ] 图表渲染正确性
- [ ] 每群覆盖配置生效验证
