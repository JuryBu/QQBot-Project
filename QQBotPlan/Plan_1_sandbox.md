# 🔒 Sandbox 空间设计 (Plan_1_sandbox)

> 关联: [Plan_1_architecture.md](./Plan_1_architecture.md) | [Plan_1_models.md](./Plan_1_models.md)

---

## 核心理念

Sandbox **不只是一个文件夹**——它是模型的"虚拟本地系统"，提供：
- 受限但自由的文件/代码/工具操作空间
- 安全隔离（不影响宿主系统）
- **可扩展**的工具生态（模型可以自己给自己加工具！）

---

## 目录结构

```
Sandbox/
├── base_tools/             ← 基础工具区（ONLY READ，系统保护）
│   ├── runtimes/           ← 🆕 运行时环境（预配置，只读）
│   │   ├── python/         （Python 3.x 解释器 + pip + 常用库）
│   │   ├── node/           （Node.js + npm + 常用包）
│   │   └── c/              （GCC/MinGW 编译器 + 标准库）
│   ├── view_file.tool
│   ├── modify_file.tool    （允许并行调用）
│   ├── sandbox_exec.tool   （在 Sandbox 范围内执行命令，限定 runtimes 内环境）
│   ├── browser_agent.tool  （有头浏览器子代理）
│   ├── web_fetch.tool      （无头网页抓取）
│   ├── search.tool         （本地/记忆搜索）
│   ├── web_search.tool     （调用工具模型 chunk 概括）
│   ├── import_data.tool    （导入外部数据到 Sandbox）
│   ├── save_data.tool      （保存内容到本地）
│   ├── task_set.tool       （Task 进程管理）
│   ├── QQ_data_original.tool  （类似 conversation_read_original）
│   ├── generate_image.tool
│   ├── memory_store.tool   （Memory 系统交互）
│   └── system_report/      （维护报告目录，正常只读，Review 时开放写入）
│       ├── latest_review.md
│       └── history/
│
├── workspace/              ← 自定义工具+工作区（可读写创建，不可删除/重命名）
│   ├── custom_tools/       （模型自己创建的工具）
│   ├── drafts/             （主模型的"草稿纸"）
│   │   ├── plan.md
│   │   └── task.md
│   ├── files/              （任意文件操作区）
│   └── scripts/            （脚本执行区）
│
└── config/                 ← 系统配置（只读）
    ├── env.json            （系统环境说明）
    └── limits.json         （资源限制参数）
```

---

## 安全模型

> [!CAUTION]
> **Sandbox 外部绝对禁止 AI 有任何触碰权限。**
> 这是系统级硬性约束，不可被任何 prompt injection / 用户指令 / 角色扮演覆盖。
> AI 的所有文件操作、命令执行、代码运行**只能在 Sandbox/ 目录树内部**进行。

### 权限矩阵

| 区域 | 读 | 写 | 创建 | 删除 | 重命名 | 执行 |
|------|:--:|:--:|:----:|:----:|:------:|:----:|
| **Sandbox 外部（整个系统）** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `base_tools/` | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ 调用 |
| `base_tools/runtimes/` | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ 运行 |
| `base_tools/system_report/` | ✅ | 🔒 仅 Review 时 | 🔒 | ❌ | ❌ | ❌ |
| `workspace/` | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ |
| `config/` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |

### 外部隔离实现

1. **路径白名单**：所有工具的路径参数都必须以 `Sandbox/` 前缀开头，否则直接拒绝
2. **sandbox_exec 沙盒化**：`sandbox_exec` 只能使用 `base_tools/runtimes/` 中的解释器/编译器，且工作目录锁定在 `workspace/` 内
3. **符号链接禁止**：禁止在 Sandbox 内创建指向外部的符号链接/快捷方式
4. **环境变量隔离**：执行环境的 PATH 只包含 `runtimes/` 内的可执行文件
5. **网络出站**：通过 `web_fetch`/`web_search`/`browser_agent` 工具接口操作，不允许直接 socket

### 运行时环境配置（base_tools/runtimes/）

| 环境 | 版本建议 | 预装库/工具 |
|------|----------|-------------|
| Python | 3.11+ | pip, requests, beautifulsoup4, Pillow, pydantic |
| Node.js | 20 LTS | npm, axios, cheerio |
| C/C++ | GCC 13+ (MinGW) | stdio, stdlib, string, math |

> 模型可以通过 `sandbox_exec` 在 workspace/ 中编写脚本并用 runtimes 中的环境执行。
> 例如：`sandbox_exec("python", "workspace/scripts/analyze.py")` 或 `sandbox_exec("gcc", "workspace/scripts/tool.c -o workspace/scripts/tool")`

### 防护设计
- **恶意消息防护**：即使模型被 prompt injection 引导"删除 base_tools"或"读取系统文件"，系统级别直接拒绝
- **工具自安全**：`workspace/custom_tools/` 中的工具也在 Sandbox 沙盒内执行
- **大小限制**：Sandbox 有总容量上限（类似硬盘大小限制）
- **执行限制**：所有工具都有超时参数和运存限制参数
- **路径逃逸检测**：对 `..`、绝对路径、符号链接等逃逸手法进行检测并拒绝

---

## 基础工具详表

| 工具名 | 功能 | 超时 | 备注 |
|--------|------|------|------|
| `view_file` | 查看文件内容 | 5s | 行号范围支持 |
| `modify_file` | 修改文件（允许并行） | 10s | 精确替换模式 |
| `sandbox_exec` | 在 Sandbox 内运行命令 | 可配，上限较高 | 支持 Python/Node/Shell |
| `browser_agent` | 有头浏览器子代理 | 60s | 可录制视频 |
| `web_fetch` | 无头网页抓取 | 30s | 和我们的 web-fetcher 类似 |
| `search` | 搜索记忆/文件 | 5s | 本地搜索 |
| `web_search` | 网络搜索 | 30s | 调用工具模型 chunk 概括 |
| `import_data` | 导入外部数据 | 30s | 支持 URL/文件路径 |
| `save_data` | 保存到本地 | 10s | 可选勾选发送到 QQ |
| `task_set` | Task 进程管理 | - | 创建/check/kill |
| `QQ_data_original` | 查看原始 QQ 聊天记录 | 10s | 类似 conversation_read_original |
| `generate_image` | 图片生成 | 60s | 调用图片生成模型 |
| `memory_store` | Memory 系统交互 | 5s | 读写查记忆 |
| `system_report` | 读取维护报告 | 5s | 正常只读 |

---

## 自定义工具机制

模型可以在 `workspace/custom_tools/` 中创建符合以下格式的工具：
- **MCP 工具**：标准 MCP server 定义
- **SKILL 文件**：SKILL.md + 辅助脚本
- **脚本工具**：可执行脚本 + 配套的 tool 定义文件

> 系统自动扫描 `workspace/custom_tools/` 和 `base_tools/` 中的工具定义，合并为可用工具列表。

---

## 定期 Review 机制

工具模型定期 launch Sandbox Review：

1. **安全检查**：扫描异常文件、权限问题
2. **文件整理**：清理临时文件、整理 workspace
3. **空间报告**：统计使用量、资源占用
4. **维护报告**：写入 `base_tools/system_report/`（此时该目录临时开放写入）
5. **报告保护**：Review 结束后 system_report 恢复只读

---

## 系统环境说明（env.json 示例）

```json
{
  "sandbox_version": "1.0",
  "total_storage_mb": 512,
  "used_storage_mb": 42,
  "ram_limit_mb": 256,
  "exec_timeout_default_ms": 30000,
  "exec_timeout_max_ms": 300000,
  "system_time": "2026-04-02T03:43:00+08:00",
  "available_languages": ["python", "node", "c", "bash"],
  "tool_count": 14,
  "custom_tool_count": 0,
  "last_review": "2026-04-02T00:00:00+08:00"
}
```

---

## 渐进式披露

工具系统采用**渐进式披露**提高效率：
- 初始只暴露工具名称和简短描述
- 模型请求使用时才展开完整参数和说明
- 减少每次请求的固定 token 开销
- 类似 AstrBot 的 `tool_schema_mode: full/brief` 配置
