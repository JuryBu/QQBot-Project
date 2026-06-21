# Report_3_Plan3_对照审核.md

> 审核时间：2026-04-13 | Codex gpt-5.3 xhigh + high 双进程
> 审核基准：Plan_3.md + Plan_3_1~3_4 + Plan_3系列讨论原始内容.md
> 对照目标：实际代码实现是否完整满足 Plan 需求

---

## 审核结论

**整体评价**: Plan_3 核心功能（FlashLite 采样优化、KVCache、成本监控）大部分已实现。原审核发现的 3 个严重问题 + 1 个 enabled 开关均已修复。

---

## 🔴 严重问题

### 问题 1: ~~`group_overrides` 前后端契约断裂~~ → ✅ 已修复
- **两轮一致**: ✔ xhigh 和 high 均独立发现
- **修复内容**:
  - 后端 `models.py` GET 返回 `group_overrides` 字段
  - 后端 `UpdateFlashLiteRequest` 新增 `group_overrides` 字段
  - 后端 POST 增加字段校验（key 纯数字）并落盘
  - 前端 `addGroupOverride` 增加纯数字校验

### 问题 2: ~~`media_summary` 的 `_wk` 变量作用域错误~~ → ✅ 已修复
- **两轮一致**: ✔
- **位置**: `main.py:4793`
- **修复内容**: 将 `_wk = self._extract_window_key(event)` 提到 if/elif/else 分支前统一定义

### 问题 3: ~~CHECKPOINT 压缩调用未传 `window_key`~~ → ✅ 已修复
- **位置**: `checkpoint.py:669`
- **修复内容**: `flash_lite_caller(prompt, ..., window_key=window_key)` 显式传参

---

## 🟡 建议改进

### 1. ~~`enabled` 开关未接入触发链路~~ → ✅ 已修复
- **位置**: `main.py:766`
- **修复内容**: `_get_effective_interval` 中检查 `enabled` 字段，禁用时返回极大间隔等效跳过采样

### 2. `sync_time_interval` 未在 BossLady 面板暴露
- **位置**: `models.py:183`, `index.html:471`, `main.py:142`
- **描述**: Plan_3 参数化不完整，该参数只能通过 AstrBot 原生面板修改
- **建议**: 前后端同步补齐

### 3. `sync_trigger_interval` 与 `sync_interval` 双轨并存
- **位置**: `main.py:116`, `_conf_schema.json:2`, `models.py:159`
- **描述**: 新键和旧键共存，存在"显示值≠生效值"风险
- **建议**: 统一为一个键名

### 4. `_conf_schema` 类型不一致
- **描述**: schema 将 `dynamic_sampling/group_overrides` 定义为 `string`，但引擎按 `dict` 读取
- **建议**: AstrBot 插件系统 schema 仅支持 string/int/float/bool，需在 main.py 初始化时做 `json.loads` 反序列化

### 5. 成本路由缺 `POST /pricing`
- **位置**: `cost.py:214`
- **描述**: Plan_3_4 要求"面板可更新定价"，但 API 未实现
- **建议**: 补充 pricing 接口或降级记录为"已知限制"

---

## 🟢 微调建议

### 1. 成本面板为"降级版"
- **描述**: 目前 4 卡片 + 折线/环图，未达计划中的 6 指标 + 折线/饼/柱/面积完整矩阵
- **评估**: 当前版本功能可用，完整矩阵可作为后续迭代目标

### 2. `review_interval_hours` 的"0=关闭"语义不一致
- **位置**: `_conf_schema.json:70`, `models.py:237`
- **描述**: schema 写明 0 可关闭，但后端强制最小 1
- **建议**: 统一语义并在主逻辑中显式处理关闭态

---

## Plan_3 需求覆盖率总表

| Plan 文档 | 核心需求 | 实现状态 |
|-----------|---------|---------|
| Plan_3_1 FlashLite采样优化 | 固定/动态采样切换 | ✅ 完成 |
| Plan_3_1 | 每群独立配置 | ✅ 完成（前后端已打通） |
| Plan_3_2 KVCache优化 | system prompt 静动分离 | ✅ 完成 |
| Plan_3_2 | KVCache hash 稳定性 | ✅ 完成（hash缺model维度需修） |
| Plan_3_3 工具模型KVCache | 工具模型 KVCache | ✅ 完成 |
| Plan_3_4 成本监控 | CostTracker 全链路 | ✅ 完成 |
| Plan_3_4 | BossLady 成本面板 | ✅ 完成（降级版） |
| Plan_3_4 | Chart.js 可视化 | ✅ R6 完成 |
| Plan_3_4 | 面板可更新定价 | ❌ 未实现 |
