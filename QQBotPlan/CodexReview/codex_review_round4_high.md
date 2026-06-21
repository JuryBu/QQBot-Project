# 审核报告：Plan_3 系列 vs 实际实现（Review 4/6, High）

**审核时间**: 2026-04-13
**审核范围**:
- `QQBotPlan/Plan_3/Plan_3*.md`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py`
- `BossLady_Console/frontend/index.html`
- `BossLady_Console/frontend/app.js`
- `BossLady_Console/backend/routers/models.py`

**整体评价**: 文档中“已完成”的部分存在多处实现偏差，且有 3 项会直接导致功能失效或数据失真。

## 🔴 严重问题（必须修复）

### 问题 1：`media_summary` 中 `_wk` 作用域错误，导致中/大型内容摘要分支直接异常
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4803`、`main.py:4835`
- **描述**: `_wk` 只在小型分支内赋值（`main.py:4795`），但中型和大型分支继续使用 `_wk`（`main.py:4812`, `main.py:4822`, `main.py:4838`）。当内容不走小型分支时会触发 `UnboundLocalError`，函数最终返回 `摘要错误: ...`。
- **修复建议**: 将 ` _wk = self._extract_window_key(event)` 提前到三级分支前统一定义，或在每个分支内独立定义。

### 问题 2：BossLady 的 `group_overrides` 前后端未打通，UI 保存是“假成功”
- **位置**:
  - 前端读取/写入：`BossLady_Console/frontend/app.js:1952`、`app.js:2050`
  - 后端 GET 未返回：`BossLady_Console/backend/routers/models.py:160-177`
  - 后端 POST 模型缺字段：`models.py:183-199`
  - 后端 POST 未处理：`models.py:239-249`
- **描述**: 前端已按 `group_overrides` 读写，但后端接口既不返回也不接收该字段。用户在 UI 中添加/启用/禁用群覆盖后，看似成功，实际不会持久化到插件配置。
- **修复建议**:
  1. `UpdateFlashLiteRequest` 增加 `group_overrides: Optional[Dict[str, Any]]`。
  2. `GET /models/flashlite` 返回 `group_overrides`。
  3. `POST /models/flashlite` 写入并做结构校验（群号键、`sync_interval`、`enabled`）。

### 问题 3：CHECKPOINT 压缩调用未传 `window_key`，成本按窗口归因失真
- **位置**:
  - 压缩调用：`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:669`
  - 回调绑定：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2907`
  - 记账默认值来源：`main.py:1545`（`window_key: str = "unknown"`）
- **描述**: `compress_if_needed()` 调用 `flash_lite_caller(prompt, max_output_tokens=...)` 未传窗口标识，导致该路径成本记录落入 `unknown`，与 Plan_3_4 的“按窗口统计”目标冲突。
- **修复建议**: 在 `main.py` 传入带窗口参数的闭包（或修改 `compress_if_needed` 签名），确保压缩调用也携带当前 `window_key`。

## 🟡 建议改进

### 问题 4：`group_overrides.enabled` 在运行时未生效
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:766-773`
- **描述**: `_get_effective_interval()` 只读取 `sync_interval`，忽略 `enabled=false`。即使 UI 提供了“禁用群覆盖/关闭 FlashLite”语义，主流程不会执行该开关。
- **修复建议**: 在群聊入口先判断 override 的 `enabled`，`false` 时直接跳过 FlashLite 触发链路。

### 问题 5：采样“时间兜底间隔(sync_time_interval)”未进入 BossLady 配置链路
- **位置**:
  - 前端采样区无对应输入：`BossLady_Console/frontend/index.html:480-487`
  - 后端请求模型无字段：`BossLady_Console/backend/routers/models.py:183-199`
- **描述**: Plan_3_1 要求面板参数化 `sync_time_interval` 与 `sync_time_min_msgs`，当前仅后者可配置。
- **修复建议**: 前后端同步新增 `sync_time_interval` 字段，并写入 `config.json`。

## 🟢 微调建议

### 问题 6：Review 间隔“0=关闭”约定与实现不一致
- **位置**:
  - schema 描述允许 0：`AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json`
  - BossLady 后端强制最小 1：`BossLady_Console/backend/routers/models.py:237`
- **描述**: 配置语义不一致，运维侧容易误判。
- **修复建议**: 统一约定（推荐支持 0=关闭），并在 `_sync_trigger` 中显式处理关闭分支。

## ✅ 做得好的地方
- FlashLite / 工具模型的静态-动态拆分主干已落地，`_build_flash_lite_system`、`_build_judgment_prompt`、`_call_tool_model` 的结构方向与 Plan_3_2/3_3 基本一致。
- 成本页已接入 Chart.js、维度切换与自动刷新，基础可视化能力可用。
