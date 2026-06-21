# 审核报告：FlashLite 体系全面审核

**审核时间**: 2026-04-11 02:06:45（本地）  
**审核范围**: 
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py`
- `Sandbox/base_tools/*.tool.json`（与工具模型声明一致性相关）
- `QQBotPlan/提示词审计/*.md`
- `QQBotPlan/Plan_2*.md`

**整体评价**: 架构方向清晰，但安全边界与成本控制存在高风险点；建议先修复安全与递归调用问题，再做缓存与声明一致性优化。

## 一、维度评估

| 维度 | 评级 | 结论 |
|---|---|---|
| 架构完整性 | 需改进 | 三模型主链路可追踪，但子代理工具声明允许自递归委托，存在链路失控风险 |
| 代码质量 | 需改进 | 关键路径可读性尚可，但存在字符串拼接执行、异常兜底不充分等问题 |
| 提示词一致性 | 良好 | 三套 prompt 主体与审计文档基本一致；存在工具声明与提示词能力漂移 |
| 性能与成本 | 有风险 | KV Cache 固定前缀不稳定，导致命中率显著受损；FlashLite 调用频率偏高 |
| 安全性 | 有风险 | “Sandbox”执行路径缺少进程级隔离，且存在可注入执行点 |

## 二、问题分级（Critical / High / Medium / Low）

### 🔴 Critical 1：Sandbox 执行并未实现进程级隔离，实际可直接执行宿主命令
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:565`, `AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:587`, `AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:617`
- **描述**：`exec_code` 的 `command`/`code` 模式直接调用系统 shell / 解释器进程执行；当前仅做路径层校验，无法阻止脚本主动访问 Sandbox 外部文件、环境变量和网络资源。
- **影响**：一旦模型被提示词注入或工具误用，可能直接触达宿主系统敏感数据，安全边界与“Sandbox”命名不一致。
- **修复建议**：
  1. 引入真正隔离运行时（容器/受限用户/作业对象+最小权限）。
  2. 默认关闭 `command` 模式，仅允许白名单命令。
  3. 对外部网络访问做可配置 deny-by-default，并按工具场景临时放行。

### 🔴 Critical 2：`search` 工具存在代码注入面（用户 query 直接拼接到 Python 代码）
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3470`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3477`
- **描述**：`tool_search` 在 files 模式下将 `query` 直接插入 Python 源码字符串，未转义。
- **影响**：恶意 query 可突破“文件搜索”语义，转为任意脚本执行。
- **修复建议**：
  1. 禁止字符串模板拼代码，改为固定脚本 + 参数传递（`json.dumps`/临时文件参数）。
  2. 优先复用已有 `grep` 工具，避免二次实现执行链。

### 🟠 High 1：子代理可调用 `browser_agent` 导致自递归委托风险
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1588`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1767`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4867`
- **描述**：`_call_tool_model` 动态加载 base_tools 时未排除 `browser_agent`；而 `tool_browser_agent` 内部又调用 `_call_tool_model`。
- **影响**：可能出现“子代理再委托子代理”的递归链，带来 token/时延/费用放大，极端情况下出现任务卡死。
- **修复建议**：
  1. 在 `excluded_tools` 中加入 `browser_agent`（以及 `run_custom_tool` 视策略决定）。
  2. 增加 delegation depth 上限（例如 `max_delegate_depth=1`）。

### 🟠 High 2：KV Cache 固定前缀不稳定，命中率会被系统时间/Knowledge 动态内容拉低
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1288`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1289`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1421`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1391`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1392`
- **描述**：用于缓存的 system 文本包含当前时间和 Knowledge 快照，`ensure_cache` 每次哈希都易变化。
- **影响**：缓存重建频繁，增加延迟与成本，且占用 Gemini cachedContent 管理额度。
- **修复建议**：
  1. 拆分 system 为 `static_system`（缓存）+ `dynamic_context`（contents 动态注入）。
  2. 将时间与 Knowledge 移出缓存段，改为 user parts 附加。

### 🟠 High 3：本地复制白名单基于 `startswith`，路径边界校验不足
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5175`
- **描述**：`save_data(local_path=...)` 通过字符串前缀判断允许路径，未使用 `commonpath/resolve` 边界判断。
- **影响**：可构造同前缀路径误判；同时白名单覆盖 `%TEMP%` 等广域目录，存在误读非附件文件风险。
- **修复建议**：
  1. 使用 `Path.resolve()` + `os.path.commonpath` 严格校验目录归属。
  2. 附件复制改为“消息上下文签名令牌”校验，避免通用本地路径透传。

### 🟡 Medium 1：工具声明与提示词能力不一致（web_fetch / browser_agent 参数漂移）
- **位置**：`Sandbox/base_tools/web_fetch.tool.json:15`, `Sandbox/base_tools/web_fetch.tool.json:20`, `Sandbox/base_tools/browser_agent.tool.json:8`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2817`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4760`
- **描述**：提示词宣称 `web_fetch` 支持多模式（rich/tables/download/pipeline 等），但工具模型声明 enum 仅 5 种；`browser_agent` 声明缺 `inject_context`。
- **影响**：工具模型被提示词鼓励使用但 schema 不允许，导致调用失败率和重试成本上升。
- **修复建议**：
  1. 以实现签名自动生成 `.tool.json`（单一事实源）。
  2. 增加 CI 校验：`tool schema` 与函数签名/文档差异即阻断。

### 🟡 Medium 2：压缩互斥标记缺少 `finally` 兜底
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:574`, `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:760`
- **描述**：`_compressing.add(window_key)` 后并非全路径都在 `finally` 释放；若中段抛未捕获异常，窗口可能长期处于“压缩中”。
- **影响**：该窗口后续压缩全部被跳过，历史膨胀。
- **修复建议**：压缩主流程包裹 `try/finally: self._compressing.discard(window_key)`。

### 🟡 Medium 3：Memory 迷你索引截断逻辑在 pinned 过多时可能反向扩容
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1188`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1189`
- **描述**：`MAX_INDEX - len(pinned)` 为负时切片会保留大量 non-pinned，导致索引超出预期上限。
- **影响**：FlashLite prompt 体积膨胀，增成本并降低稳定性。
- **修复建议**：先 `pinned = pinned[:MAX_INDEX]`，再按剩余额度追加 non-pinned。

### 🟡 Medium 4：子代理调用 `upload_data` 默认参数与 `event=None` 语义冲突
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1776`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4922`
- **描述**：子代理路由传 `event=None`，而 `upload_data(send_to_qq=true)` 默认尝试 QQ 发送，易触发异常降级。
- **影响**：子代理文件交付链不稳定，结果可读性下降。
- **修复建议**：子代理路由对 `agent_upload_data` 强制注入 `send_to_qq=False`，并在提示词写明“子代理只产出路径指针”。

### 🟢 Low 1：重复装饰器
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4745`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4746`
- **描述**：`@filter.llm_tool(name="browser_agent")` 重复声明。
- **修复建议**：保留一条，避免注册行为不确定性。

## 三、提示词一致性逐项核对

| 核对项 | 结果 | 备注 |
|---|---|---|
| `_build_flash_lite_system()` vs `Prompt_FlashLite_判断.md` | 一致 | 身份、触发条件、双模式输出与字段约束均可对齐 |
| FlashLite 压缩模式（双模式并存）vs `Prompt_FlashLite_压缩.md` | 一致 | 压缩走同入口，user prompt 切换；无格式冲突 |
| `_build_tool_model_system()` vs `Prompt_工具模型.md` | 基本一致 | 系统职责与三模式描述一致；工具声明能力存在 schema 漂移 |
| `inject_flashlite_context()` 各 Section vs `Prompt_主模型.md` | 基本一致 | 注入段落顺序/内容总体一致 |
| 工具声明能力与提示词描述一致性 | 不一致 | 主要是 `web_fetch`/`browser_agent` 参数与可用模式漂移（见 Medium 1） |

## 四、性能与成本评估（含数据依据）

1. **FlashLite 调用频率偏高**
- 依据：`sync_trigger_interval` 默认 5（`main.py:108`）；私聊每条消息都触发（`main.py:625-639`）。
- 粗估公式：`calls/min ≈ group_msgs/5 + private_msgs + @/关键词触发次数`。

2. **KV Cache 命中率受动态 system 影响明显**
- 依据：缓存 system 含实时 `Knowledge + 系统时间`（`main.py:1288-1289`, `1391-1392`, `1421`）。
- 结果：缓存高频重建，抵消 KV 优势。

3. **T 文件并发模型总体可用，但压缩互斥释放需补强**
- 依据：窗口锁 + merge-save（`checkpoint.py:723-757`）做得较好；但 `_compressing` 缺 finally（见 Medium 2）。

4. **API Key 池轮转机制设计合理**
- 依据：429 冷却与轮转（`main.py:1514-1524`, `1677-1682`）。
- 建议：`max_retries=min(len(keys),3)` 可按配置放宽，避免 key 多时利用不足。

## 五、优先修复建议（执行顺序）

1. 先修复两项 Critical（Sandbox 隔离 + search 注入），否则后续优化意义有限。  
2. 修复递归委托与 KV Cache 稳定性（High），可直接降低费用与异常重试。  
3. 对齐 tool schema 与提示词（Medium），减少模型“会说不会调”的调用失败。  
4. 补齐压缩互斥 `finally` 与 Memory 索引上限修正，提升长期稳定性。

## ✅ 做得好的地方

- T 文件压缩后采用“重载-合并-保存”策略，较好处理了压缩期间的新消息并发。  
- 工具模型 key 池有基础冷却和轮转，具备生产可用雏形。  
- 提示词体系分层完整（FlashLite/主模型/工具模型），整体可维护性较高。
