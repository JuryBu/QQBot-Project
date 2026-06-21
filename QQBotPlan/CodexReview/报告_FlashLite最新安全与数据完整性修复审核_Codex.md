# 审核报告：AstrBot FlashLite 最新安全与数据完整性修复

**审核时间**: 2026-04-12  
**审核范围**: `AstrBot/data/plugins/astrbot_plugin_flashlite/{sandbox.py,main.py,web_engine.py,checkpoint.py,test_checkpoint_v2.py,agent.py}`  
**整体评价**: 最新修复覆盖了部分路径校验与 T 文件并发保存问题，但仍存在多条可直接绕过 Sandbox 的高危旁路，且下载/损坏恢复链路仍有明显数据丢失风险。

## 🔴 严重问题（必须修复）

### 问题 1：`sandbox_exec` 仍可直接读取宿主机任意文件，Sandbox 隔离承诺失效
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:553-599`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:690-763`
- **调用入口**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3187-3203`
- **对外承诺**：`AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py:179-187`
- **描述**：`exec_code()` 仅限制 `cwd` 位于 Sandbox，但执行的 Python/Node 代码没有任何 OS 级文件系统隔离。`_find_runtime()` 还会回退到项目 `.venv` 或系统 Python，导致工具模型执行的代码可直接读取 `C:/Windows/win.ini` 等 Sandbox 外文件。插件 system prompt 明确宣称“Sandbox 外无文件操作权限”，当前实现与承诺不符。
- **复现**：已本地执行 `sm.exec_code(code="import pathlib; print(pathlib.Path(r'C:/Windows/win.ini').read_text(...)[:40])", language='python')`，返回成功并输出宿主机文件内容。
- **修复建议**：不要把“受限 cwd”当成沙盒。至少应做到：
  1. 禁用 `code` 模式对宿主解释器的直接执行，改为容器/低权限子进程/作业对象隔离。
  2. 若短期无法真正隔离，必须下调能力模型：移除“Sandbox 外无权限”的表述，并默认禁用 `python/node/bash` 任意代码执行。
  3. 为执行进程增加文件系统白名单或虚拟化挂载，而不是只限制工作目录。

### 问题 2：`web_fetch(file://...)` 可读取 Sandbox 外绝对路径，形成第二条本地文件读取旁路
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3611-3625`
- **核心实现**：`AstrBot/data/plugins/astrbot_plugin_flashlite/web_engine.py:257-279`
- **补充实现**：`AstrBot/data/plugins/astrbot_plugin_flashlite/web_engine.py:625-639`
- **描述**：`tool_web_fetch` 明确允许 `url=file://`，而 `WebFetchEngine._fetch_local_file()`/`fetch_html()` 只在“相对路径”时拼接 Sandbox 根目录；对于绝对路径会直接读取本地文件，没有任何 `SandboxSecurity` 校验。
- **复现**：已本地执行 `fetch_page('file:///C:/Windows/win.ini', mode='text')`，成功返回 `win.ini` 内容。
- **修复建议**：
  1. `file://` 必须统一走 `SandboxSecurity.resolve_path()`。
  2. 显式拒绝绝对 `file://`，只允许 `file://workspace/...` 这类 Sandbox 相对路径。
  3. `fetch_html()`、截图、下载、交互等所有本地文件入口要共用同一套路径校验，避免再次分叉。

### 问题 3：`save_data(local_path=...)` 白名单过宽，可复制 `%TEMP%` 与整个 `AstrBot/data`
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5200-5241`
- **描述**：工具说明写的是“仅限 QQ 消息附件缓存目录”，但实现实际放行了 `%TEMP%`、`%LOCALAPPDATA%\\Temp` 和整个 `AstrBot/data`。这意味着工具模型不仅能搬运 QQ 附件，还能读取临时目录中的任意缓存文件、以及 AstrBot 其他插件/数据文件，再通过 `send_file` 发回对话。
- **影响**：可造成本地缓存、日志、配置、数据库等被间接外传，和“只允许复制附件”的安全边界明显不一致。
- **修复建议**：
  1. 白名单收缩到明确的 QQ/NapCat 附件目录，不要包含整个 `%TEMP%`。
  2. `AstrBot/data` 不应整体放行，若确有业务需要，应精确到单个缓存子目录。
  3. 对 `local_path` 增加来源证明，例如只允许引用本轮消息解析出的附件路径句柄，而不是任意字符串路径。

## 🟡 建议改进

### 问题 4：`system_report` 的 Review 写入链路实际上是坏的，定期维护日志无法可靠落盘
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:84-96`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:113-118`
- **调用链**：`AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:486-505`
- **触发点**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:676-699`
- **写入入口**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5381-5413`
- **描述**：写权限检查读取的是 `SandboxSecurity._review_mode`，但该字段从未初始化，也没有从 `SandboxManager._review_mode` 同步过去。实际调用 `modify_file('base_tools/system_report/...')` 会抛出 `AttributeError`。
- **复现**：已本地复现 `AttributeError: 'SandboxSecurity' object has no attribute '_review_mode'`。
- **修复建议**：
  1. 把 Review 状态统一放到一个对象上，避免 `SandboxManager`/`SandboxSecurity` 双份状态。
  2. 为 `system_report` 增加最小集成测试，覆盖 Review 模式开启、关闭、索引追加三条路径。

### 问题 5：`save_data(url=...)` 会先截断目标文件，再做完整性判断；下载失败时会破坏旧文件
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5073-5094`
- **描述**：下载逻辑直接 `open(real_path, 'wb')` 写入目标路径。若同名文件已存在，文件会先被截断；后续一旦超出 50MB、网络异常、进程中断，旧文件已经丢失。代码在超限分支还会 `os.remove(real_path)`，进一步放大覆盖损失。
- **影响**：这与“数据完整性修复”的目标冲突，属于典型的非原子覆盖写入。
- **修复建议**：
  1. 始终下载到同目录临时文件，完成大小/类型/魔数校验后再 `os.replace()`。
  2. 对已存在文件保留回滚副本或至少在失败时保持旧文件不变。
  3. 为“覆盖旧文件 + 下载失败”的场景补一个回归测试。

### 问题 6：T 文件损坏后的“恢复”策略是直接清空，属于破坏性恢复
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:299-303`
- **测试锁定行为**：`AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py:141-146`
- **描述**：`load()` 遇到 JSON 损坏会直接创建空 T 文件覆盖原文件，之前的压缩摘要和消息队列全部丢失。测试也明确把“回退到空 T”当作正确行为。
- **影响**：一旦发生半写入、磁盘故障或手工编辑失误，会把原本可人工恢复的数据永久抹掉。
- **修复建议**：
  1. 损坏文件先改名为 `*.corrupt.<timestamp>`，保留现场。
  2. 引入 `.bak` 或双写版本文件，优先回滚到最近一次成功保存的快照。
  3. 测试应校验“保留损坏现场 + 可回退”，而不是校验“直接清空”。

## 🟢 微调建议

### 问题 7：发送文件/图片的降级分支重新使用了脆弱的 `startswith` 前缀校验
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4225-4228`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4263-4266`
- **描述**：当 `self._sandbox` 不可用时，代码退回到 `normpath + startswith(sandbox_base)` 判断。此前同类问题已经在 `sandbox.py` 中修过，这里又把旧模式带回来了。
- **修复建议**：降级分支也应统一用 `Path.resolve()` + `is_relative_to()` 或复用 `SandboxSecurity`，不要复制另一套简化校验。

### 问题 8：安全修复缺少可执行回归测试，现有测试脚本也无法直接跑通
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/test_memory.py`
- **补充位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/test_web_engine.py`
- **描述**：
  1. `test_checkpoint_v2.py` 在 Windows 默认 GBK 控制台下会因 emoji 输出触发 `UnicodeEncodeError`。
  2. `test_memory.py`、`test_web_engine.py` 直接依赖 `astrbot` 运行环境，没有最小 stub，无法独立执行。
  3. 当前没有任何自动化测试覆盖 `sandbox_exec`、`web_fetch(file://)`、`system_report`、`save_data` 的最新安全修复路径。
- **修复建议**：把安全相关用例升级为可在 CI 直接执行的最小化单测/集成测试；至少覆盖“禁止读取 Sandbox 外文件”“禁止绝对 file://”“Review 写入成功”“下载失败不破坏旧文件”。

## ✅ 做得好的地方

- `checkpoint.py` 已引入按窗口锁和压缩后 `load-merge-save` 合并逻辑，方向上优于直接操作 `messages.db`。
- `main.py:5228-5235` 对本地复制路径改用了 `Path.resolve() + relative_to()`，比单纯 `startswith` 更稳健。
- `main.py:5098-5117` 增加了 Content-Type 与魔数检查，至少能识别一部分“下载到的是错误页”问题。

## 验证记录

- 已运行：`python AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py`
  结果：因 GBK 控制台打印 emoji 触发 `UnicodeEncodeError`，脚本未完成。
- 已运行：`python AstrBot/data/plugins/astrbot_plugin_flashlite/test_memory.py`
  结果：缺少 `astrbot` 运行时 stub，`ModuleNotFoundError`。
- 已运行：`python AstrBot/data/plugins/astrbot_plugin_flashlite/test_web_engine.py`
  结果：缺少 `astrbot` 运行时 stub，`ModuleNotFoundError`。
- 已完成本地最小复现：
  1. `SandboxManager.exec_code()` 成功读取 `C:/Windows/win.ini`。
  2. `WebFetchEngine.fetch_page('file:///C:/Windows/win.ini')` 成功读取宿主机文件。
  3. `modify_file('base_tools/system_report/...')` 在 Review 模式下抛出 `AttributeError`。
