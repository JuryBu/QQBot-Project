# 审核报告：AstrBot Stage 7-10 四模块代码审查

**审核时间**: 2026-04-02
**审核范围**:
- `AstrBot/data/plugins/astrbot_plugin_flashlite/kv_cache.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/knowledge.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py`
- 集成链路补充核对：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
**范围假设**: 由于本轮对话未附具体模块列表，本报告按 `QQBotPlan/Task.md` 的 Stage 7-10 四个模块审查：`KV Cache`、`Memory + Knowledge`、`Sandbox`、`Agent`
**整体评价**: 代码已具备原型形态，但 Stage 7-10 与 `Task.md` 中“核心完成”的状态不符；当前最大风险不在语法，而在“未真正接线”和“安全边界失效”。

## 🔴 严重问题（必须修复）

### 问题 1：`SandboxSecurity.validate_path()` 的边界判断可被前缀字符串绕过，Sandbox 外路径会被误判为合法
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:62-73`
- **描述**：
  当前实现用 `real_path.startswith(self._root)` 判断路径是否仍在 Sandbox 内。这在 Windows 路径上是不安全的字符串前缀判断。
  例如根目录为 `...\Sandbox` 时，`..\Sandbox_evil\poc.txt` 解析后的真实路径是 `...\Sandbox_evil\poc.txt`，仍然会满足 `startswith("...\Sandbox") == True`。
  这会直接破坏 `QQBotPlan/Plan_1_sandbox.md:60-81` 所承诺的“Sandbox 外部绝对禁止 AI 触碰”和“路径逃逸检测”。
- **影响**：
  攻击者可以通过构造相邻目录名，绕过路径白名单，读取或写入 Sandbox 外部文件。
- **修复建议**：
  将所有边界判断改为真实路径层级判断，而不是字符串前缀判断。
  推荐方案：
  ```python
  root = Path(self._root).resolve()
  target = Path(full_path).resolve()
  if os.path.commonpath([str(root), str(target)]) != str(root):
      return False, "路径逃逸检测失败"
  ```
  同时对符号链接目标也复用同一套 `commonpath`/`is_relative_to` 逻辑。

### 问题 2：`sandbox_exec` 并没有真正实现“沙盒化执行”，执行脚本仍可直接访问宿主文件系统和网络
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:221-260`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:291-313`
- **设计对照**：`QQBotPlan/Plan_1_sandbox.md:60-81`, `QQBotPlan/Plan_1_sandbox.md:94-99`
- **描述**：
  `exec_code()` 只是把脚本写到 Sandbox 目录，再用宿主机上的 Python/Node/Bash 解释器启动子进程。它没有任何 OS 级隔离，也没有限制脚本本身对宿主文件系统、网络、进程的访问。
  具体问题包括：
  1. `_find_runtime()` 会回退到项目 `.venv` 或系统 `python/node`，不满足“只能使用 `base_tools/runtimes/` 中解释器”的设计。
  2. `env["PATH"]` 只是把 `runtimes/` 加到最前面，并没有移除宿主 PATH。
  3. `limits.json` 中的 `ram_limit_mb`、`concurrent_tasks_max`、`allow_direct_socket` 等配置没有被执行层使用。
- **影响**：
  任何能调用 `sandbox_exec` 的链路，实质上都获得了“在宿主机上执行任意脚本”的能力；这与设计文档定义的安全模型完全不一致。
- **修复建议**：
  至少补齐以下约束后再开放使用：
  1. 禁止回退到系统解释器，只允许 `base_tools/runtimes/` 内白名单运行时。
  2. 将工作目录强制锁定到 `workspace/` 子目录。
  3. 显式裁剪环境变量，只保留最小白名单环境。
  4. 在进程层增加资源限制和网络限制；如果当前环境做不到 OS 级隔离，就不要把它标记为“安全沙盒”。

### 问题 3：只读策略存在绕过，`system_report` 常态可写，`exec_code(cwd=...)` 还能把临时脚本写进只读目录
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:83-90`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:221-242`
- **设计对照**：`QQBotPlan/Plan_1_sandbox.md:69-73`, `QQBotPlan/Plan_1_sandbox.md:155-158`
- **描述**：
  1. `validate_path(..., allow_write=True)` 对 `base_tools/system_report/` 直接放行，没有任何“仅 Review 时开放”的运行时条件。
  2. `exec_code()` 对 `cwd` 只做了 `resolve_path()`，没有做写权限校验；随后会把 `_sandbox_exec.py/.js/.sh` 直接写入该目录。
  这意味着调用方可以把 `cwd` 指到 `config/` 或 `base_tools/system_report/`，从而在设计上应为只读的目录中落盘。
- **影响**：
  只读配置与维护报告区都可能被普通执行链路污染，破坏配置完整性与审计可信度。
- **修复建议**：
  1. `system_report` 写权限必须绑定显式的 `review_mode` 状态位，默认拒绝。
  2. `exec_code()` 在生成临时脚本前，必须对最终脚本路径执行 `validate_path(..., allow_write=True)`。
  3. `cwd` 只允许 `workspace/` 及其子目录。

### 问题 4：Stage 7 / Stage 10 的核心对象没有真正接入运行链路，`KV Cache` 与 `AgentRequestBuilder` 目前基本是“未使用代码”
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:30-40`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:109-118`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:648-682`
- **对照任务状态**：`QQBotPlan/Task.md:77-88`, `QQBotPlan/Task.md:122-130`
- **描述**：
  `main.py` 虽然导入了 `KVCacheManager` 和 `AgentRequestBuilder`，但初始化阶段只实例化了 `CheckpointManager`、`KnowledgeCache`、`MemoryStore`。
  实际运行链路里：
  1. 没有 `KVCacheManager` 实例，也没有任何 `cachedContent` 创建/复用逻辑。
  2. 没有 `AgentRequestBuilder` 实例，也没有地方调用 `build_system_instruction()` / `build_contents()`。
  3. `on_llm_request` 仅追加 `system_prompt` 文本，没有向 `ProviderRequest.func_tool` 注入真实工具集。
  4. `sandbox.py`、`memory.py`、`agent.py` 中定义的工具能力没有注册为 AstrBot 可执行工具。
- **影响**：
  `Task.md` 中 Stage 7、Stage 10 标记为“核心完成”的内容，在当前代码里并未真正生效；运行时仍是 Flash Lite 基础唤醒 + 文本注入，而不是完整的 KV Cache / Agent 工具链。
- **修复建议**：
  1. 在 `FlashLiteEngine.on_loaded()` 中显式创建 `KVCacheManager`、`SandboxManager`、`AgentRequestBuilder`。
  2. 选定一条真正的主请求构建路径，把 `cachedContent`、CHECKPOINT、Knowledge、工具集统一接到同一处。
  3. 如果继续走 AstrBot 的 `ProviderRequest`，就必须通过 `req.func_tool` / `llm_tool` 注册真实工具，而不是只在 prompt 里写工具名字。

### 问题 5：`AgentRequestBuilder` 读取 CHECKPOINT 的 SQL 与真实表结构不兼容，接线后也会直接失效
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py:385-403`
- **对照表结构**：`AstrBot/data/plugins/astrbot_plugin_persistence/main.py:106-115`
- **对照写入逻辑**：`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:257-272`
- **描述**：
  `_get_checkpoint_summary()` 查询的是：
  - `compressed_summary`
  - `version`
  
  但真实表 `checkpoint_history` 中实际字段是：
  - `compressed_content`
  - 没有 `version` 字段，只有 `created_at`
  
  此外，`Knowledge` 使用的窗口键是 `GroupMessage:{group_id}`，而 `CheckpointManager.check_and_compress()` 写入数据库时存的 `window_id` 是裸 `group_id`。
- **影响**：
  即使未来把 `AgentRequestBuilder` 接入运行链路，CHECKPOINT 也会因为 SQL 报错或 key 不匹配而无法取回，等于 Stage 10 的核心历史压缩能力仍然不可用。
- **修复建议**：
  1. 改成查询 `compressed_content`。
  2. 用 `ORDER BY created_at DESC LIMIT 1` 或自增 `id DESC LIMIT 1`。
  3. 明确区分 `window_type` 与 `window_id`，不要混用 `GroupMessage:xxx` 和裸群号。

### 问题 6：`MemoryStore` 的读写接口没有强制工作区约束，破坏“每群/每号独立工作区”的隔离承诺
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py:173-197`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py:203-247`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py:253-261`
- **设计对照**：`QQBotPlan/Plan_1_memory.md:36-39`
- **任务对照**：`QQBotPlan/Task.md:95-100`
- **描述**：
  `query()` 支持按 `workspace` 过滤，但 `read()`、`update()`、`delete()` 全都只按 `mem_id` 操作，不要求调用方提供工作区。
  这意味着一旦某个窗口拿到另一工作区的 `mem_id`，就可以跨群读取、修改、删除长期记忆。
  同时，`agent.py` 给 `memory_read` / `memory_update` 的工具定义里也没有 `workspace` 参数，后续接入工具链时问题会被继续放大。
- **影响**：
  长期记忆存在跨群篡改和跨用户泄漏风险，与“独立工作区”设计目标相冲突。
- **修复建议**：
  1. 为 `read/update/delete` 增加必填 `workspace`。
  2. 对 `general` 工作区单独定义显式共享规则，避免隐式放大权限。
  3. 如果确实需要跨工作区能力，必须用单独的 `scope="global"` 或管理员开关显式开启。

## 🟡 建议改进

### 问题 7：`KnowledgeCache.update_window()` 无法清空过期字段，会把旧参与者/话题永久残留在摘要里
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/knowledge.py:116-123`
- **描述**：
  `active_users or existing.get(...)`、`mood or existing.get(...)`、`recent_topics or existing.get(...)` 会把空列表和空字符串都视为“没有新值”，导致模型即使想清空这些字段，也会被旧值覆盖。
- **修复建议**：
  用 `if xxx is None` 区分“未提供”和“明确设为空”，例如：
  ```python
  "active_users": existing.get("active_users", []) if active_users is None else active_users
  ```

### 问题 8：Knowledge 只限制了“单窗口摘要长度”，没有限制“全局注入总量”，很容易超过设计中的 token 预算
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/knowledge.py:28-31`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/knowledge.py:146-181`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:660-663`
- **设计对照**：`QQBotPlan/Plan_1_memory.md:97-101`
- **描述**：
  当前代码允许最多 20 个窗口、每个窗口 500 字，再加参与者和操作记录；但没有任何总 token 上限控制。按中文场景估算，很容易超过文档设计的 `2000-3000 token` 预算。
- **修复建议**：
  在格式化输出阶段增加全局裁剪策略，例如“仅保留最近 N 个活跃窗口 + 总 token 上限”。

### 问题 9：`MemoryStore.write()` 的 ID 生成策略存在碰撞风险，突发写入时可能直接触发主键冲突
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py:87-105`
- **描述**：
  当前 ID 由 `毫秒时间戳 + hash(title) % 10000` 构成；同毫秒内写入同标题或哈希碰撞时，会直接命中相同主键，且没有重试逻辑。
- **修复建议**：
  改用 `uuid4()` / `secrets.token_hex()`，或者在 SQLite 层引入自增主键并单独维护可读 ID。

### 问题 10：`AgentRequestBuilder.build_contents()` 的消息字段定义和持久化层不一致，后续接线后会丢文本/图片
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py:323-343`
- **对照位置**：`AstrBot/data/plugins/astrbot_plugin_persistence/main.py:87-100`
- **描述**：
  `build_contents()` 读取的是 `content`、`image_url`，而持久化层实际字段是 `content_text`、`image_urls`。
  即便未来把该构建器接上真实消息源，也会因为字段名不一致而生成空文本或错误的多模态块。
- **修复建议**：
  统一“主模型上下文消息 DTO”，避免由不同模块各自猜字段名。

## 🟢 微调建议

### 问题 11：当前四模块缺少可执行自动化测试，现有验证主要停留在 Markdown 测试方案
- **位置**：`QQBotPlan/Test_Stage6_8_integration.md:60-180`
- **描述**：
  仓库内未发现这四个模块对应的单元测试/集成测试代码；目前更多是“测试计划文档”，缺少 CI 可执行断言。
- **修复建议**：
  最少补三组自动化测试：
  1. `sandbox.py` 的逃逸/权限用例。
  2. `agent.py` 与 `checkpoint.py` 的 schema 对齐用例。
  3. `memory.py` 的 workspace 隔离与并发写入用例。

## ✅ 做得好的地方

- `MemoryStore` 全部 SQL 都使用参数化语句，基本避免了直接 SQL 注入风险。
- `CheckpointManager` 与 `PersistencePlugin` 的 `checkpoint_history` 字段命名在“写入路径”上是一致的，说明 Stage 6 内部自洽度比 Stage 7-10 集成链路更高。
- `KnowledgeCache` 至少实现了过期清理和窗口数量裁剪，说明作者已经考虑了长期运行下的基本收敛问题。
- 本次静态语法检查通过：`python -m py_compile` 未发现语法错误。

## 补充结论

- 如果只看“文件存在且能 import”，Stage 7-10 看起来已经完成。
- 如果按“真实运行链路是否闭环”和“安全承诺是否兑现”来审查，当前状态更接近“原型就绪，但未可上线”。
- 修复优先级建议：
  1. 先修 `sandbox.py` 的边界与执行安全问题。
  2. 再把 `KV Cache` / `AgentRequestBuilder` 真正接入主链路。
  3. 最后补 `Memory` 工作区隔离和自动化测试。
