**审计结论**

这次“最新安全修复”只修掉了局部问题，没有封住攻击链。按攻击者视角看，`FlashLite` 目前仍可被稳定利用到宿主机级代码执行，`H-5` 也只是修了“路径逃逸绕过”，没有收紧“可读范围”本身。

## 🔴 严重问题（必须修复）

### 1. `C-1` 命令白名单可被白名单解释器直接绕过，`sandbox_exec` 仍是宿主机 RCE
- **位置**：[main.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3185)、[sandbox.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:154)、[sandbox.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:638)、[sandbox.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:683)、[sandbox.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:747)
- **问题**：白名单只检查“命令名”，但仍允许 `python`/`node`。而 `code` 模式本身就是任意脚本执行，且 Python 运行时会回退到项目 `.venv` 甚至系统 Python。
- **本地复现**：我直接跑了 `python -c "__import__('os').system('whoami')"` 和 `python -c "open('poc.txt','w').write('OWNED')"`，在白名单开启时都执行成功。
- **攻击意义**：只要攻击者能诱导模型调用 `sandbox_exec`，就不是“受限命令执行”，而是完整 RCE。
- **修复建议**：生产环境默认禁用 `code` 模式；`command` 模式移除 `python`/`node` 这类解释器；真正需要执行时放到容器/Job Object/受限账户里，不要回退到宿主 `.venv`。

### 2. `run_custom_tool` 是第二条任意代码执行链，能绕过你刚加的命令白名单
- **位置**：[tool_registry.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/tool_registry.py:243)、[tool_registry.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/tool_registry.py:288)、[main.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5422)
- **问题**：自定义工具的 handler `.py` 会被直接拼成 `exec_code(..., "python")` 执行。攻击者只要先用 `modify_file` 往 `workspace/` 写一个脚本，再 `run_custom_tool`，就能拿到同等级执行能力。
- **攻击意义**：即使你后面继续收紧 `command` 模式，只要 `run_custom_tool` 还在，这条链仍可直接落到 Python 执行。
- **修复建议**：禁用运行时自定义 Python 工具；改成受限 DSL/模板工具；至少要求显式管理员确认，不允许模型自行创建并执行脚本。

## 🟡 建议改进

### 3. `H-5` 修掉了前缀绕过，但白名单本身仍过宽，可直接复制敏感文件
- **位置**：[main.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5013)、[main.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5204)
- **问题**：`save_data(local_path=...)` 允许整个 `%TEMP%`、QQ/NapCat 缓存，以及整个 `AstrBot/data`。这不是“最小权限”。
- **实证**：`AstrBot/data/cmd_config.json`、`AstrBot/data/data_v4.db`、`AstrBot/data/plugins.json` 都在允许前缀内，理论上可被直接复制进 Sandbox。
- **攻击意义**：攻击者不需要再做路径穿越，只要知道常见路径，就能搬走配置、数据库、插件清单、缓存文件。
- **修复建议**：白名单缩到明确的附件缓存目录；去掉整个 `AstrBot/data` 和整个 `%TEMP%`；增加扩展名、MIME、文件大小、调用来源校验。

### 4. Review 写路径分支有属性错位，命中时会直接抛异常
- **位置**：[sandbox.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:85)、[sandbox.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:117)
- **问题**：`SandboxSecurity.validate_path(..., allow_write=True)` 里访问 `self._review_mode`，但这个属性定义在 `SandboxManager`，不在 `SandboxSecurity`。
- **实证**：我本地复现得到 `AttributeError`。
- **修复建议**：把 review 状态放进 `SandboxSecurity`，或由 `SandboxManager` 在调用前完成权限判定，不要跨对象偷读状态。

## ✅ 这次修复里真正有效的部分

- `H-5` 从 `startswith` 改成 `Path.resolve() + relative_to()`，这一步本身是对的，能挡住“同前缀兄弟目录”绕过。
- `C-2` 的 `search` 注入点目前看已经从字符串拼接改成 JSON 反序列化，我这轮没再看到原来的直接代码注入问题。
- 但这些都属于“点修复”，没改变核心事实：当前 `FlashLite` 仍把“和用户同权限的宿主执行能力”直接暴露给模型，[main.py](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2861) 甚至明确鼓励模型主动使用 `sandbox_exec`。

**审计假设**
- 本结论基于“攻击者可通过提示注入、恶意网页、恶意文档或对话诱导模型调用工具”这一现实威胁模型。
- 如果你的真实部署里完全不向不可信输入暴露 `sandbox_exec` / `run_custom_tool`，风险会下降；但从当前插件提示词和工具暴露方式看，并不是这样。