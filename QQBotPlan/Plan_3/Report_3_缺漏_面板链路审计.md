# Report_3_缺漏：老板娘面板全面链路审计

> 审计时间：2026-04-13 19:05  
> 审计范围：BossLady Console 前端 index.html + app.js ↔ 后端 routers/*.py ↔ FlashLite 插件 main.py + cost_tracker.py  
> 审计目标：逐页排查界面上所有内容的前后端链路完整性、数据一致性、逻辑正确性

---

## 一、UI 冗余：模型选择页遗留参数（可移除）

### 1.1 「同步间隔(条)」— 与采样策略面板重复

| 对比项 | 模型选择页 | 采样策略面板 |
|--------|-----------|------------|
| HTML id | `flashliteSyncInterval` (index.html L219) | `samplingFixedInterval` (index.html L481) |
| 加载来源 | `data.sync_interval` (app.js L412) | `data.sync_interval` (app.js L1936) |
| 保存 API | `POST /models/flashlite` body: `sync_interval` (app.js L616) | `POST /models/flashlite` body: `sync_interval` (app.js L1966) |
| 后端字段 | `config["sync_interval"]` (models.py L214) | 同一处 |

**结论**：两个 UI 写同一个 `config.json["sync_interval"]`。采样策略面板更完整（有模式选择、动态参数、群覆盖等上下文），模型选择页的是历史遗留。  
**操作**：从模型选择页 Flash Lite 卡片移除「同步间隔(条)」输入框及 label。

### 1.2 「CP上限(tokens)」— 与系统设置 CHECKPOINT 策略卡片重复

| 对比项 | 模型选择页 | 系统设置 CHECKPOINT 策略 |
|--------|-----------|-------------------------|
| HTML id | `flashliteCpLimit` (index.html L219) | `cpTokenLimit` (index.html L593) |
| 加载来源 | `data.checkpoint_limit` (app.js L413) | `flConfig.checkpoint_limit` (app.js L1370) |
| 保存 API | `POST /models/flashlite` body: `checkpoint_limit` (app.js L616) | `POST /models/flashlite` body: `checkpoint_limit` (app.js L1383) |
| 后端字段 | `config["checkpoint_limit"]` (models.py L216) | 同一处 |

**结论**：完全重复。系统设置 CHECKPOINT 策略卡片有 6 个完整参数（Token上限、保留消息数、压缩前比例、冷却期、压缩率下/上限）+ 三重守卫说明。模型选择页只有一个孤零零的 "CP上限"。  
**操作**：从模型选择页 Flash Lite 卡片移除「CP上限(tokens)」输入框及 label。

---

## 二、逻辑 Bug：群聊「禁用」不能完全阻断 FlashLite

### 2.1 问题描述

当前群聊独立配置的 `enabled: false` 只会让 `_get_effective_interval()` 返回 `999999`（main.py L771-772），这只影响**消息计数同步触发**。但有两条触发路径**完全不检查 enabled**：

**路径 1：异步触发（@/唤醒词）不检查 enabled**
```python
# main.py L674-684 — 在 enabled 检查之前！
if is_at or has_keyword:
    await self._async_trigger(...)  # ← 不检查 group_overrides
    return
```
→ 即使群被"禁用"，@老板娘 或关键词仍然会触发 FlashLite

**路径 2：时间兜底同步触发不受 effective_interval 控制**
```python
# main.py L695
time_trigger = time_elapsed >= self._sync_time_interval and self._msg_counters[group_id] >= self._sync_time_min_msgs
```
→ 时间兜底的判断用的是 `_sync_time_interval`（60秒）和 `_sync_time_min_msgs`（3条），和 `effective_interval` （返回 999999）完全无关。只要群里 60 秒内攒够 3 条消息，仍然会触发 `_sync_trigger()`

### 2.2 修复方案

在 `route_message()` 的群聊路径最前面（L661 之后、L664 之前）插入 enabled 检查：

```python
# 群聊禁用检查（在 is_at 和同步计数之前）
group_overrides = self._cfg("group_overrides", {})
_override = group_overrides.get(group_id, {}) if isinstance(group_overrides, dict) else {}
if isinstance(_override, dict) and not _override.get("enabled", True):
    return  # 该群已禁用 FlashLite 所有触发
```

这样异步触发（@/唤醒词）、消息计数、时间兜底**全部跳过**。

### 2.3 影响范围

- 涉及文件：`main.py` L661-700
- 不影响私聊路径（L706-723）
- 不影响未配置 group_overrides 的群（默认 `enabled: True`）

---

## 三、缺失参数：`sync_time_interval` 面板不可配置

### 3.1 问题描述

FlashLite 时间兜底触发有两个参数：
- `sync_time_interval`：时间阈值（秒），默认 60 → **面板无控件，后端无 GET/POST 支持**
- `sync_time_min_msgs`：最低消息数，默认 3 → **面板有控件** (`samplingMinMsgs`, index.html L485)，后端有支持 (models.py L201, L246)

`sync_time_interval` 在 main.py L142 通过 `self._cfg("sync_time_interval", 60)` 从 config 读取，但：
- 后端 `GET /models/flashlite` 不返回此字段 (models.py L153-182)
- 后端 `UpdateFlashLiteRequest` 不接受此字段 (models.py L185-203)  
- 前端采样策略面板没有对应的输入框

### 3.2 修复方案

- 后端 models.py GET flashlite: 加入 `"sync_time_interval": config.get("sync_time_interval", 60)`
- 后端 models.py UpdateFlashLiteRequest: 加入 `sync_time_interval: Optional[int] = None`
- 后端 POST handler: 加入 `if req.sync_time_interval is not None: config["sync_time_interval"] = max(30, min(600, req.sync_time_interval))`
- 前端 index.html 采样策略面板: 加入「时间兜底(秒)」输入框 `id="samplingTimeInterval"`
- 前端 app.js loadSamplingConfig: 加入读取回填
- 前端 app.js saveSamplingConfig: 加入此字段到 body

---

## 四、UI 差距：群聊独立配置未达原始设计

### 4.1 对比原始设计 (Plan_3_4 L165-186)

| 原始设计要求 | 当前实现 | 状态 |
|-------------|---------|------|
| 下拉选择已知群号 | 手动输入文本框 `<input type="text">` | ❌ 未达 |
| 自动填充已知群号列表（从 Knowledge 或 T 文件） | 无自动填充 | ❌ 缺失 |
| 滑块设置间隔 | 数字输入框 `<input type="number">` | ⚠️ 功能等价但形态不同 |
| enabled 开关（完全关闭 FlashLite） | 有开关但逻辑有 Bug（见第二项） | ⚠️ 逻辑不完整 |

### 4.2 修复方案

**已知群号的数据来源**（最可靠）：

从 `cost_logs` 的 JSON 日志中提取所有 `window_key` 为 `"GroupMessage:xxx"` 的去重群号。这些是**实际产生过 API 调用的群**，数据一定真实。

具体改动：
1. **后端**：新增 `GET /api/cost/known-groups` 端点
   ```python
   @router.get("/known-groups")
   async def get_known_groups():
       # 扫描所有 cost_logs JSON，提取 GroupMessage:* 的去重群号
       groups = set()
       for f in COST_LOGS_DIR.glob("*.json"):
           try:
               with open(f, "r", encoding="utf-8") as fp:
                   for r in json.load(fp):
                       wk = r.get("window_key", "")
                       if wk.startswith("GroupMessage:"):
                           groups.add(wk.split(":", 1)[1])
           except: pass
       return {"groups": sorted(groups)}
   ```

2. **前端**：群号输入改为 `<input>` + `<datalist>` 自动补全
   ```html
   <input type="text" id="groupOverrideId" list="knownGroupsList" ...>
   <datalist id="knownGroupsList"><!-- JS 填充 --></datalist>
   ```

3. **前端 JS**：在 `loadSamplingConfig()` 或 `loadCostPage()` 时拉取 `/cost/known-groups` 填充 datalist

---

## 五、数据不一致：cost.py 与 cost_tracker.py 的时间范围算法不同

### 5.1 问题描述

面板后端 `cost.py` 和插件内 `cost_tracker.py` 各自有独立的 `_get_period_range()` 实现，逻辑不一致：

| 时间段 | cost.py (面板后端) | cost_tracker.py (插件内) |
|--------|-------------------|-------------------------|
| `"week"` | `today - 6天` (固定滑窗) | `today - today.weekday()天` (本周一起) |
| `"month"` | `today - 29天` (固定滑窗) | `today.replace(day=1)` (本月1号起) |
| `"today"` | 相同 | 相同 |

**影响**：面板调用的是 `cost.py` 的路由。如果日后有其他地方调用 `cost_tracker.py` 的 `get_summary()` 等方法（如 Bot 命令 `!cost`），同样的 `period="week"` 会返回不同的数据范围。

但**当前面板实际只走 cost.py**，所以目前面板数据自身是一致的。

### 5.2 修复建议

统一两处逻辑为其中一种语义。建议统一为 `cost.py` 的滑窗语义（"近7日"/"近30日"更直觉），同时更新 `cost_tracker.py` 匹配。或者让面板后端 `cost.py` 直接调用 `CostTracker` 实例来避免重复代码。

---

## 六、缺失功能：调用趋势图粒度不可选

### 6.1 问题描述

前端 app.js L1741：
```javascript
const gran = period === 'today' ? 'hour' : 'day';
```

粒度**完全由 period 硬编码决定**：
- "今日" → 固定小时粒度
- "近7日" / "近30日" → 固定天粒度

用户**无法手动选择**粒度（如"近7日按小时查看"或"今日按5分钟查看"）。

后端已支持 `granularity` 查询参数（`hour` / `day`），前端没有暴露控件。

### 6.2 修复方案

在趋势图标题旁添加粒度选择下拉：
```html
<select id="costGranularity" class="input-field" style="width:80px" onchange="loadCostPage()">
    <option value="auto">自动</option>
    <option value="hour">小时</option>
    <option value="day">天</option>
</select>
```

JS 端：
```javascript
const granSel = document.getElementById('costGranularity')?.value || 'auto';
const gran = granSel === 'auto' ? (period === 'today' ? 'hour' : 'day') : granSel;
```

---

## 七、潜在问题：定价表维护风险

### 7.1 问题描述

成本计算数据链路：
1. **Token 数**：来自 Gemini API 响应的 `usageMetadata`，是 Google 服务端精确统计 → **精确**
2. **费用计算**：`cost_tracker.py` L136-148 用 `PRICING` 字典乘以精确 token 数 → **基于硬编码定价**
3. **面板显示**：`cost.py` 有**第二份独立的 `PRICING` 字典** (L22-39) → **两份需要手动同步**

存在两个风险：
- **风险 A**：Google 调价后，`PRICING` 未更新 → 面板显示的费用与 GCP 账单不一致
- **风险 B**：`cost.py` 和 `cost_tracker.py` 的 PRICING 不同步 → 面板"按模型"表和成本明细的数字自洽但与插件记录的 `cost_usd` 不一致

### 7.2 当前同步状态

对比两份 PRICING：

| 模型 | cost_tracker.py | cost.py | 一致？ |
|------|----------------|---------|--------|
| gemini-3.1-flash-lite-preview | ✅ 0.25/0.025/1.50/1.00 | ✅ 0.25/0.025/1.50/1.00 | ✅ |
| gemini-3-flash-preview | ✅ 0.30/0.03/2.50/1.00 | ✅ 0.30/0.03/2.50/1.00 | ✅ |
| gemini-2.5-flash-preview-04-17 | ✅ 0.15/0.0375/3.50/1.00 | ✅ 0.15/0.0375/3.50/1.00 | ✅ |
| gemini-2.5-pro-preview-05-06 | ✅ 1.25/0.3125/10.00/4.50 | ✅ 1.25/0.3125/10.00/4.50 | ✅ |

**当前两份一致**，但维护在两个文件中有失同步风险。

### 7.3 修复建议

- `cost.py` 的定价表改为从 `cost_tracker.py` 导入，消除重复源
- 或 `cost.py` 不自行计算费用，直接读取 JSON 日志里已经算好的 `cost_usd`（当前已是如此：L79 `sum(r.get("cost_usd", 0))`），则删除 cost.py 中多余的 PRICING 定义
- 面板提供定价表编辑功能（已有 `GET /cost/pricing` 端点但前端未使用）

### 7.4 存储费未计入

`cost_tracker.py` 的 `_calc_cost()` 只计算了：
- `uncached_input × input_price`
- `cached_tokens × input_cached_price`
- `output_tokens × output_price`

**没有计算 `storage` 费用**（隐式缓存的存储费用 $/M tokens/hour）。PRICING 定义了 `storage` 字段但从未使用。

这意味着使用 Context Caching 显式创建的高频率调用，存储费可能成为显著成本组成部分，但面板不会反映。

---

## 八、采样策略面板 `sampling_mode` 加载后的 UI 同步

### 8.1 问题描述

`loadSamplingConfig()` (app.js L1928-1958) 正确读取了 `sampling_mode` 和动态参数，并在最后调用 `toggleSamplingOptions()` 控制动态参数区域的显隐。

但**模型选择页 `saveFlashLite()`** (app.js L613-625) 保存时**不包含 `sampling_mode`、`dynamic_sampling`、`group_overrides` 字段**：

```javascript
// app.js L616 — saveFlashLite() 的 body
const flBody = {
    model: ...,
    sync_interval: ...,      // ← 包含
    checkpoint_limit: ...,   // ← 包含
    // 但没有 sampling_mode、dynamic_sampling、group_overrides！
};
```

这意味着：如果用户在采样策略面板保存了 `sampling_mode: "dynamic"`，然后在模型选择页只改了模型并点"保存 Flash Lite"，`POST /models/flashlite` 只会发送 `model` + `sync_interval` + `checkpoint_limit`。

**后端行为**：由于 `req.sampling_mode is None` → 不会覆盖（不更新），所以**目前不会破坏数据**。后端 POST 用的是"仅更新非 None 字段"的 merge 策略。

**但一旦移除模型选择页的 sync_interval 和 checkpoint_limit 后，saveFlashLite() 也应清理不再发送这些字段。**

---

## 九、成本监控面板 `cost.py` 中 `_load_records` 和 FlashLite 后端 `_get_period_range` 关系

### 9.1 当前链路结构

面板的成本数据**100% 来自 cost.py**，cost.py 自己读取 JSON 日志文件。**cost_tracker.py 的 get_summary() 等方法在面板链路中完全未使用**。

```
面板前端 → GET /api/cost/* → cost.py → 读 Sandbox/cost_logs/*.json
FlashLite 插件 → CostTracker.record() → 写 Sandbox/cost_logs/*.json
```

数据写入用 `cost_tracker.py`，读取用 `cost.py`，中间通过 JSON 文件解耦。这个架构是可行的，但 `_load_records` / `_get_period_range` 两处实现需要统一。

---

## 十、其他审计发现

### 10.1 面板前端 `_costTimelineData` 时间标签截断

```javascript
// app.js L1807
const labels = timeline.map(t => gran === 'hour' ? t.time.slice(11,13) + ':00' : t.time.slice(5));
```

后端 cost.py L192 用的时间格式是 `ts[:13]` = `"2026-04-13T15"`，所以 `t.time.slice(11,13)` 得到 `"15"`。

但 cost_tracker.py L428 用的格式是 `"%Y-%m-%d %H:00"`，中间是空格不是 `T`。

由于面板实际走 cost.py 不走 cost_tracker.py，所以 `slice(11,13)` 需要正确匹配 cost.py 的输出格式 `"YYYY-MM-DDTHH"` → `slice(11,13)` = `"HH"` ✅ 正确。

### 10.2 成本概览卡片的「FlashLite 调用」含义

summary API (cost.py L88) 统计的 `flashlite_calls` 包含所有 `call_type.startswith("flashlite")` 的记录，这包含了 `"flashlite"` 和可能的 `"flashlite_compress"` 等子类型。这是正确的行为。

但**第四个卡片同时显示的 `FlashLite 调用` 和 `API 调用次数` 数值相同**（图四：24 = 24），说明选定时间段内只有 FlashLite 类型的调用——这是正常的（主模型调用不通过 CostTracker 记录？）。

**需要确认**：主模型和工具模型的 API 调用是否也经过 `CostTracker.record()`？

### 10.3 主模型 / 工具模型的成本追踪覆盖检查 ✅ 已确认完整

经逐一确认，main.py 中 5 处 `cost_tracker.record()` 调用覆盖了所有 API 调用路径：

| 行号 | call_type | 触发场景 | 数据来源 |
|------|-----------|---------|---------|
| L1665 | `"flashlite"` | FlashLite 判断调用 | Gemini REST API `usageMetadata` |
| L1894 | `"tool_model"` | 工具模型（Sandbox 工具代理） | Gemini REST API `usageMetadata` |
| L2283 | `"main_model_task_wake"` | 主模型任务唤醒（Sandbox Task 完成回调） | Gemini REST API `usageMetadata` |
| L2419 | `"main_model_checkpoint"` | 主模型 CHECKPOINT Review | Gemini REST API `usageMetadata` |
| L3309 | `"main_model"` | 主模型常规回复（AstrBot 框架回调） | AstrBot `response.raw_completion` |

**结论**：成本追踪覆盖完整，所有模型的 API 调用都会经过 CostTracker 记账。

**但有一个注意点**：L3309 的主模型常规回复用的是 AstrBot 框架的 `raw_completion` 对象提取 token 数据（不是直接从 REST API），其可靠性取决于框架是否正确透传 `usageMetadata`。如果框架更新导致此字段缺失，主模型的部分调用可能不会被记账。

另外，面板按窗口分类的「主模型」列当前显示为 0（图四），并非追踪缺失，而是测试期间**老板娘判断了群消息但没有觉得需要回复**——FlashLite 看了但主模型没被触发。

---

## 十一、汇率实时化：成本监控页改为 USD + CNY 双显

### 11.1 问题描述

当前 `cost.py` 硬编码 `USD_TO_CNY = 7.2`，面板所有费用显示 CNY 都基于此固定汇率。实际美元兑人民币汇率持续波动（当前约 6.84）。

### 11.2 方案设计

**汇率 API 选型**（经测试验证均可达）：
- **主选**：ExchangeRate-API 免费版 `https://open.er-api.com/v6/latest/USD` — 无需 Key，每日更新
- **备选**：Frankfurter API `https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY` — 开源免费

**后端架构**：
1. 新建 `BossLady_Console/backend/services/exchange_rate.py`
   - 每日请求一次汇率 → 缓存到本地 JSON 文件
   - 请求失败时回退到上次缓存值 → 最后回退到硬编码 7.2
2. `cost.py` 移除 `USD_TO_CNY = 7.2`，改用 ExchangeRateService 实例获取实时汇率
3. 所有返回 `cost_cny` 的端点都用实时汇率计算
4. 新增 `GET /cost/exchange-rate` 端点返回当前汇率 + 来源 + 更新时间

**前端改动**：
- 总费用卡片：从 `¥xxx` 改为 `$xxx / ¥xxx`
- 按模型/按窗口表格：费用列双显
- 页面底部显示汇率来源和更新时间

---

## 十二、群聊独立配置参数扩展

### 12.1 需求描述

当前群聊独立配置只有 `sync_interval` + `enabled` 两个参数，缺少对群级行为精细控制的能力。需新增 3 个参数：

| 参数 | 字段名 | 类型 | 默认值 | 含义 |
|------|--------|------|--------|------|
| 回复长度限制 | `reply_length_limit` | int (tokens) | null=用全局 | 限制主模型对该群回复的 maxOutputTokens |
| 工具调用权限 | `tool_permission` | enum | `"full"` | `"full"` 全部工具 / `"search_only"` 仅搜索 / `"none"` 禁止工具 |
| 主模型思考预算 | `main_thinking_budget` | int | null=用全局 | 覆盖该群的 thinkingBudget |

### 12.2 后端改动

**models.py**：
- `group_overrides` 校验 schema 扩展为 5 个字段
- 新增 `reply_length_limit`/`tool_permission`/`main_thinking_budget` 的范围校验

**main.py (FlashLite 插件)**：
- 新增 `_get_group_config(group_id) -> dict` 方法统一获取群配置
- `route_message()` 群聊路径最前面加 enabled 总拦截（同时修复第二项 Bug）
- `_async_trigger` / `_sync_trigger` 获取群配置后：
  - `reply_length_limit` → 通过 `event.set_extra("per_group_reply_limit", N)` 传递到主模型
  - `tool_permission` → 在判断是否调用 `_call_tool_agent()` 时检查权限
  - `main_thinking_budget` → 通过 `event.set_extra("per_group_thinking_budget", N)` 传递
- `_wake_main_for_task()` 直调 Gemini API 的路径可直接使用群级参数

### 12.3 前端改动

群聊独立配置表格扩展：
- 表头增加「回复限制」「工具权限」「思考预算」列
- 添加群号表单增加对应输入控件
- 编辑已有群配置时弹出完整参数面板（或内联编辑）

---

## 十三、🔴 定价系统全面审计：价格错误 + 结构性缺陷

> **参考材料**：`QQBotPlan/辅助/参考材料_Gemini_API_定价表.md`（2026-04-13 从官方获取）
> **涉及文件**：`cost_tracker.py` L27-64

### 13.1 代码 vs 官方 逐条对比

| 代码中模型名 | 字段 | 代码值 | 官方实际 | 偏差 |
|---|---|---|---|---|
| `gemini-3.1-flash-lite-preview` | input | 0.25 | 0.25 | ✅ |
| | cached | 0.025 | 0.025 | ✅ |
| | output | 1.50 | 1.50 | ✅ |
| `gemini-3-flash-preview` | input | **0.30** | **0.50** | ❌ 低估 40% |
| | cached | **0.03** | **0.05** | ❌ 低估 40% |
| | output | **2.50** | **3.00** | ❌ 低估 17% |
| `gemini-2.5-flash-preview-04-17` | - | 全部 | N/A | ⚠️ 该模型名已不在官方列表（旧预览版） |
| `gemini-2.5-pro-preview-05-06` | input | 1.25 | 1.25（≤20万） | ✅（仅低档） |
| | cached | **0.3125** | **0.125**（≤20万） | ❌ 偏差 2.5 倍！ |
| | output | 10.00 | 10.00（≤20万） | ✅（仅低档） |
| | 分级 | 无 | **有**（>20万时翻倍） | ❌ 缺失 |

### 13.2 结构性缺陷

#### A. 分级定价未实现（P0）
Gemini 3.1 Pro 和 2.5 Pro 对 prompt ≤20万 / >20万 token 有不同价格档（最大差距：输入翻倍，输出+50%）。当前 `_calc_cost()` 只有一个固定价格，无法根据 prompt_tokens 选档。

#### B. 模型名硬编码（P1）
PRICING 字典只有 4 个模型名。面板下拉列表里有 20+ 个模型可选（参见参考材料 §八.D），用户切换到如 `gemini-2.5-flash-lite`、`gemini-2.0-flash`、`gemini-3.1-pro-preview` 的主模型/工具模型都会落入 DEFAULT_PRICING 兜底，而 DEFAULT 价格也不一定对。

#### C. 图片模型未覆盖（P1）
`gemini-3.1-flash-image-preview`、`gemini-3-pro-image-preview`、`gemini-2.5-flash-image` 的图片输出按 token 数换算为张数计价（$30~$120/百万token），与文本输出完全不同。CostTracker 没有区分 "text output" 和 "image output" 的能力。

#### D. 过期模型名仍在字典中
`gemini-2.5-flash-preview-04-17` 和 `gemini-2.5-pro-preview-05-06` 是旧的 preview 版本名，已不在官方定价页面中。应更新为当前稳定版或 latest 别名。

### 13.3 修复方案

1. **PRICING 结构重构**：支持分级定价
```python
# 示例：分级定价结构
"gemini-2.5-pro": {
    "tiers": [
        {"threshold": 200_000, "input": 1.25, "input_cached": 0.125, "output": 10.00},
        {"threshold": float('inf'), "input": 2.50, "input_cached": 0.25, "output": 15.00},
    ],
    "storage": 4.50,
},
# 无分级的保持原结构
"gemini-3.1-flash-lite-preview": {
    "input": 0.25, "input_cached": 0.025, "output": 1.50, "storage": 1.00,
},
```

2. **`_calc_cost()` 改造**：接收 prompt_tokens 判断分级
3. **模型名模糊匹配**：`gemini-3-flash` 前缀 → 匹配 gemini-3-flash-preview 定价
4. **覆盖面板所有可选模型**：全面更新定价表，覆盖参考材料 §八.D 中所有模型
5. **DEFAULT_PRICING 更新**：当前默认值 (0.30/0.03/2.50) 实际上是旧 2.5 Flash 的价格，面板应在使用兜底时显示警告

### 13.4 待定：图片输出计价

图片生成模型的输出 token 单价与文本输出不同（如 Flash Image 图片输出 $60/百万token vs 文本输出 $3/百万token）。需要 CostTracker 能区分调用类型，或在 record() 时传入 `output_type="image"` 来选择对应价格。这是一个较大的结构变更，建议独立 Stage 处理。

---

## 修复优先级排序

| 优先级 | 编号 | 问题 | 理由 |
|--------|------|------|------|
| 🔴 P0 | 二 | 禁用逻辑 Bug | 功能缺陷，禁用群仍能触发 FlashLite |
| 🔴 P0 | 十三 | 定价系统错误 + 缺分级 | 成本计算结果不准确，直接影响费用数据可信度 |
| 🟡 P1 | 一 | 模型页遗留参数移除 | UI 混乱，两处修改互相冲突的风险 |
| 🟡 P1 | 三 | sync_time_interval 面板缺控件 | 时间兜底秒数无法通过面板调整 |
| 🟡 P1 | 四 | 群聊独立配置 UI 升级 | 设计差距 |
| 🟡 P1 | 十一 | 汇率实时化 + USD/CNY 双显 | 费用精度和可读性 |
| 🟡 P1 | 十二 | 群聊独立配置参数扩展 | 精细控制需求 |
| 🟢 P2 | 五 | period_range 不一致 | 当前面板数据自洽，仅影响未来扩展 |
| 🟢 P2 | 六 | 趋势图粒度选择 | 功能增强 |
| 🟢 P2 | 七 | 定价表维护 / 存储费 | ⬆️ 已被十三覆盖，并入十三处理 |
| 🔵 P3 | 八 | saveFlashLite() 移除遗留字段发送 | 跟随 P1 移除操作 |
| 🔵 P3 | 十 | 主模型成本追踪覆盖检查 | ✅ 已确认完整，无需修复 |
