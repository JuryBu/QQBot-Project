# 📊 数据层真相与 API 参数参考 (Plan_1_data)

> 关联: [Plan_1_architecture.md](./Plan_1_architecture.md) | [Suggestion 系列](./Suggestion_Kaleidoscope_1.md)

---

## ⚠️ 关键假设验证结果

> [!CAUTION]
> 主人假设「QQ 消息像一本本书存在内存里，不会删除，可以时刻翻书看原内容」——**这个假设部分正确，但有重要缺口**。

### 当前实际情况（2026-04-02 验证）

| 存储位置 | 存什么 | 容量 | 持久性 |
|----------|--------|------|--------|
| `conversations` 表 (SQLite) | 仅 LLM 对话轮次（user/assistant role） | 群 <GROUP_B> 有 74 条/800KB | ✅ 持久，但**不含**未触发机器人的消息 |
| `context_enhancer` 内存缓冲 | 近期群聊消息（所有人的） | deque maxlen=60 条/群 | ❌ 运行时内存，重启后从缓存恢复 |
| `context_enhancer/data/context_cache.json` | 缓冲区的磁盘持久化 | 保留 recent+bot 数量的消息 | ⚠️ 持久但有限，7天不活跃清理 |
| `platform_message_history` 表 | 仅 WebChat 平台消息 | 19 条（不含 QQ） | ✅ 但不含 QQ 消息 |
| NapCat 端 | 不存储（只是 OneBot 适配器） | - | ❌ |

### 缺口总结

```
完整的 QQ 群聊消息流（所有人说的每一句话）
    ↓
    目前没人存这个！
    ↓
  ┌──────────────────────────────────────┐
  │ conversations 表：只存模型参与的对话    │ ← 群友闲聊不被@的消息丢失
  │ context_enhancer：只保留最近~60条      │ ← 更早的消息丢失
  │ NapCat：不存储                        │ ← 原始消息只在 QQ 客户端
  └──────────────────────────────────────┘
```

### 结论：需要自建消息持久化层

> [!IMPORTANT]
> **QQ_data_original 工具的数据源不存在**——必须新建一个 AstrBot 插件/模块来拦截并持久化**所有** QQ 消息。

实现方案：
1. 编写一个高优先级 AstrBot 插件（priority=1000），拦截所有群聊/私聊消息
2. 将原始消息以时间序写入 SQLite（或 JSON 文件，按群号/日期分目录）
3. 提供 QQ_data_original 查询接口（按群号+时间范围+关键词搜索）
4. 设计存储策略：
   - 热数据（近 7 天）：保留完整原始内容
   - 冷数据（7-30 天）：保留文本，图片 URL 保留但不下载
   - 归档数据（30天+）：可选压缩/清理

---

## 可移植性设计

> 主人要求：「方便在电脑之间打包导出」

### 需要打包的内容

```
AstrBotLauncher-0.1.5.6/
├── AstrBot/data/                    ← 核心数据（必须）
│   ├── data_v4.db                   ← 数据库（对话、统计）
│   ├── cmd_config.json              ← 全部配置
│   ├── plugins/                     ← 插件代码和数据
│   └── persona/                     ← 人格文件
├── Sandbox/                         ← 模型的虚拟空间（新建，必须）
│   ├── base_tools/                  ← 基础工具
│   └── workspace/                   ← 工作区
├── QQBotPlan/                       ← 规划文件（可选）
├── QQ_data/                         ← 🆕 原始消息持久化（必须）
│   ├── groups/
│   │   ├── <GROUP_B>/               ← 按群号分目录
│   │   │   ├── 2026-04-01.jsonl     ← 按日期分文件
│   │   │   └── 2026-04-02.jsonl
│   │   └── ...
│   └── private/
│       ├── <ADMIN_QQ>/
│       └── ...
├── Memory/                          ← 🆕 记忆系统持久化（必须）
│   ├── general/                     ← 全局记忆
│   ├── groups/                      ← 按群号
│   └── private/                     ← 按 QQ 号
└── Knowledge/                       ← 🆕 Knowledge 缓存（可重建，可选）
    └── knowledge_cache.json
```

### 打包方案
- 一键脚本：`pack_boss_lady.bat` → 排除 `.venv/`、`__pycache__/`、日志 → 压缩为 zip
- 恢复脚本：`unpack_boss_lady.bat` → 解压 → 重装 venv 依赖
- API Key 需要单独导出（安全考虑不放在 zip 里）

---

## Gemini API 参数参考（来自 Kaleidoscope 项目经验）

### thinkingBudget vs thinkingLevel 互斥规则

| 模型系列 | 思考参数 | 可用值 |
|----------|----------|--------|
| 2.5 系列 | `thinkingConfig.thinkingBudget` | 数值 0~65536 |
| 3.x 系列 | `thinkingConfig.thinkingLevel` | 枚举 MINIMAL/LOW/MEDIUM/HIGH |

> ⚠️ **两种参数不能混用**，必须根据模型系列选择。

### 我们的模型参数配置

| 模型 | 用途 | 思考参数 | 建议值 |
|------|------|----------|--------|
| `gemini-2.5-flash` | 主模型（当前） | `thinkingBudget` | 8192～16384 |
| `gemini-3.1-flash-lite-preview` | Flash Lite 中断 | `thinkingLevel` | `"MEDIUM"` |
| `gemini-3-flash-preview` | 工具模型 | `thinkingLevel` | `"MEDIUM"` 或 `"HIGH"`（可在task中指定） |

### REST API 字段名注意事项

| ❌ 错误 | ✅ 正确 |
|---------|---------|
| `imageGenerationConfig` | `imageConfig` |
| `thinking_budget` (snake_case) | `thinkingBudget` (camelCase) |
| `outputMimeType` | 不存在于 REST API |
| `response_modalities` | `responseModalities` |

### 安全设置（关闭所有限制）

```json
{
  "safetySettings": [
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"}
  ]
}
```

### 模型能力探测方案

来自 Kaleidoscope 的三层优先级架构可直接复用：
1. **探测缓存**：用最小请求（1 token 输入输出）真实测试每个模型支持哪些参数
2. **硬编码注册表**：验证过的已知模型 fallback
3. **启发式推理**：根据模型名称模式匹配

> 探测成本极低：约 20-30 次 API 调用 ≈ $0.00001

### createCachedContent 支持确认

| 模型 | 支持缓存 | 最低要求 |
|------|----------|----------|
| `gemini-3.1-flash-lite-preview` | ✅ | 需要验证最小 token 数 |
| `gemini-3-flash-preview` | ✅ | 需要验证最小 token 数 |
| `gemini-2.5-flash` | ✅ | 官方文档说 ≥32768 token |
| `gemini-2.5-pro` | ✅ | 官方文档说 ≥32768 token |

> [!NOTE]
> 2.5 系列要求缓存内容 ≥32768 token，但 3.x 系列可能有不同门槛，需要实测验证。我们的固定内容（knowledge+系统说明+工具resource+角色设定）合计可能不足 32768 token，可能需要将部分对话历史也放入缓存。

---

## 存储估算

### 原始消息存储（QQ_data）
- 活跃群每天约 200-500 条消息
- 每条消息约 200-500 字节（纯文本）
- 一个群一天 ≈ 100KB-250KB
- 一个群一个月 ≈ 3MB-7.5MB
- 10 个群一年 ≈ 360MB-900MB
- **结论**：本地 SQLite 完全够用

### Memory 记忆系统
- 每条记忆约 0.5-2KB
- 预计每群每月产生 50-100 条记忆
- 一年 ≈ 600-1200 条 ≈ 1-2MB
- **结论**：极低开销

### Knowledge 缓存
- 每个窗口 200-500 字 → 全局约 2000-3000 token
- 内存占用可忽略
