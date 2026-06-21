# 📋 执行清单 (Task.md)

> 最终整合版 | 2026-04-02 | 对应 Plan_1 系列全部文件

---

## Stage 1-2：基础设施 ✅ 已完成

- [x] NapCat 升级到 v4.17.53
- [x] AstrBot 升级到 v4.22.2
- [x] QQ 登录恢复（扫码→快速登录）
- [x] AstrBot ↔ NapCat WebSocket 连通性验证
- [x] 分段正则修复（消除空行）
- [x] 回复方式改为引用回复
- [x] 添加唤醒词"老板娘"
- [x] context_enhancer 参数优化（30条/8条）

---

## Stage 3：多模态视觉处理
> 文档: Plan_1.md | 测试: Test_Stage3_multimodal.md

- [x] 修复 image_caption_provider_id（指向 gemini_pro）
- [x] 验证单图描述链路（配置已正确指向，待 AstrBot 重启后生效）
- [ ] 验证多图限制（max_images_in_context）— 待 AstrBot 运行时测试
- [ ] QQ 转发消息结构化提取 — 需 Agent 系统
- [ ] 表情包简述策略实现 — 需 Agent 系统

---

## Stage 4：消息持久化层 ✅ 已完成
> 文档: Plan_1_data.md GAP 2 | 测试: Test_Stage4_persistence.md

- [x] 设计 SQLite 表结构（qq_messages + checkpoint_history）
- [x] 编写 AstrBot 拦截插件（priority=9999，异步写入）
- [x] 实现撤回事件监听（OneBot notice.group_recall）
- [x] 查询接口：/qq_stats 命令已实现（完整 QQ_data_original API 在 Stage 10 Agent 集成时实现）
- [x] 多窗口并发写入验证（3窗口×2条测试通过）
- [x] 冷热数据策略：配置已支持（7天热/30天冷），清理逻辑在后续完善
- [x] 压力测试（900条批量写入 passed）
- [x] Review 修复：路径 BUG（3层→4层..）、config 读取、priority 方向确认

---

## Stage 5：Flash Lite 中断引擎 ✅ 核心完成
> 文档: Plan_1_models.md | 测试: Test_Stage5_flashlite.md

- [x] Flash Lite 调用框架（gemini-3.1-flash-lite-preview，Gemini REST API 直调）
- [x] 同步触发机制（每 N 条消息触发，消息计数器）
- [x] 异步触发机制（@/关键词检测立即触发，@强制唤醒主模型）
- [x] 语义判断逻辑（JSON prompt + 结果解析 + 降级处理）
  - [x] 明确@事件 → 强制触发
  - [x] 关键词匹配 → 语义分析后决策
  - [x] 引用/比喻中的关键词 → 不触发（场景4测试通过）
- [x] Knowledge 更新接口（内存缓存，每次调用后更新）
- [x] API 延迟基线验证：简单 1578ms，判断 2438ms（均在 5s 内）
- [ ] 工具反馈触发（依赖 Stage 10 Agent 集成）
- [ ] CHECKPOINT 超限触发（依赖 Stage 6）

---

## Stage 6：CHECKPOINT 压缩机制 ✅ 核心完成
> 文档: Plan_1_architecture.md | 测试: Test_Stage6_8_integration.md Part A

- [x] Token 估算器实现（中文 1.5字/token，英文 4字符/token，图片 258 token）
- [x] CHECKPOINT 上限配置（默认 50000 token，可配置）
- [x] 压缩 Prompt 设计（10-35% 压缩率指令，保留关键实体+时间线）
- [x] 压缩后写入 checkpoint_history 表
- [x] 集成到 Flash Lite 同步触发（每次同步后自动检查）
- [x] 最近 ~10 条消息保持不压缩
- [x] 主模型上下文 C' 构建（CHECKPOINT 摘要 + 最近消息）
- [ ] 压缩信息保留度验证（需真实数据，待 Stage 12 联调）
- [ ] RNN 式连续多次压缩的质量测试（需长时间运行数据）

---

## Stage 7：KV Cache 优化 ✅ 核心完成
> 文档: Plan_1_gaps.md GAP 3 + Plan_1_data.md | 测试: Test_Stage6_8_integration.md Part B

- [x] 固定区/增量区内容分区实现（kv_cache.py build_fixed_contents）
- [x] createCachedContent API 调用（封装为 KVCacheManager）
- [x] 缓存生命周期管理（TTL + 内容指纹 MD5 变化检测）
- [x] 撤回消息缓存不影响（固定区不含增量消息）
- [x] 缓存命中率验证：cached_tokens=1344 vs prompt_tokens=1371（仅 27 增量）
- [x] 最低门槛实测：1024 token（非文档 32768，重大发现）
- [x] API 延迟基线：创建 2344ms，生成 2438ms
- [ ] 与 Flash Lite 的 Knowledge 更新联动（invalidate 触发重建）
- [ ] 长期运行的缓存命中率监控（待 Stage 13 联调）

---

## Stage 8：Memory + Knowledge 双系统 ✅ 核心完成
> 文档: Plan_1_memory.md | 测试: Test_Stage6_8_integration.md Part C

- [x] Memory 存储实现（SQLite WAL，Memory/memory.db）
- [x] Memory 工具集（write/query/read/update/delete，5 个操作 13 项测试通过）
- [x] Knowledge 缓存实现（JSON，Knowledge/knowledge_cache.json）
- [x] Knowledge 窗口更新 + 格式化输出 + 过期清理
- [x] 按群号/QQ号的工作区隔离
- [x] 集成到 Flash Lite 同步触发（每次 knowledge_update 自动更新 KnowledgeCache）
- [ ] 指针系统实现（需对接 Stage 10 Agent 系统工具链）
- [ ] 用户画像积累机制（需长期运行数据）
- [ ] 跨窗口搜索能力（query 支持，待真实数据验证）
- [ ] Memory 可见性（需 Agent 工具支持"你记得我什么"查询）

---

## Stage 9：Sandbox 空间集成 ✅ 核心完成
> 文档: Plan_1_sandbox.md

- [x] Sandbox 目录结构创建（base_tools/workspace/config 三层）
- [x] env.json + limits.json 配置文件
- [x] sandbox.py 安全管理器（路径白名单+逃逸检测+权限矩阵+沙盒化执行）
- [x] 8 项安全测试全通过（读写权限、逃逸拒绝、路径解析、文件操作）
- [ ] base_tools 工具定义文件实现（Stage 10 工具链接入）
- [ ] runtimes 运行时环境（Python/Node.js/GCC 解释器部署）
- [ ] 自定义工具自动发现机制
- [ ] 定期 Review 机制（工具模型 Launch）

---

## Stage 10：主模型 Agent 集成 ✅ 核心完成
> 文档: Plan_1_architecture.md + Plan_1_models.md

- [x] agent.py 请求体构建器（C' 公式: Knowledge+SystemEnv+Persona+Tools+CHECKPOINT+最近消息）
- [x] 13 个工具定义（渐进式披露 brief 785chars / full 1881chars）
- [x] Gemini API 工具 Schema 导出（function_declarations）
- [x] on_llm_request 钩子注入 Knowledge + CHECKPOINT + Flash Lite 摘要到主模型请求
- [x] _notify_main_model 修复：set_extra 替代私有属性（修复 Codex 问题9）
- [x] CHECKPOINT 压缩结果接入主模型请求链路（修复 Codex 问题2）
- [ ] 渐进式工具披露运行时切换（brief→full 自动展开）
- [ ] Task 进程管理器实现（create/check/kill）
- [ ] 子代理模型调用框架
- [ ] 草稿纸工作流（workspace/drafts/）
- [ ] thinkingBudget/thinkingLevel 参数注入

---

## Stage 11：Web 控制台 MVP ✅ 核心完成
> 文档: Plan_1_webui.md

- [x] FastAPI 后端框架搭建（main.py + 路由注册 + SPA回退）
- [x] 一键启动脚本（start_bosslady.bat）
- [x] 仪表盘首页（状态卡片+统计数据）
- [x] Bot 管理（NapCat 状态/QR码/WebUI iframe/账号切换 + AstrBot 状态）
- [x] 模型配置（API Key 脱敏管理 + 主模型配置 + Flash Lite 配置 + Gemini API 模型列表）
- [x] 前端 SPA（紫色系 Glassmorphism + 侧边栏导航 + 动画）
- [x] 前端视觉测试通过 + 后端导入验证通过
- [ ] AstrBot 配置代理完善（读写更多 cmd_config.json 字段）
- [ ] 进程管理（启停 NapCat/AstrBot）

---

## Stage 12：Web 控制台完整版
> 文档: Plan_1_webui.md

- [x] 对话内存浏览器（消息查看/搜索/统计/清理）
- [x] Memory 管理界面（查看/编辑/搜索/导入导出）
- [x] Knowledge 实时查看（JSON 美化+更新日志）
- [x] Sandbox 空间浏览器（文件树+资源统计+手动 Launch Review）
- [x] 插件管理（AstrBot 插件开关/配置）
- [x] 人格设定编辑器（Markdown 编辑+预览）
- [x] 系统设置（导出/导入/日志查看/安全配置）

---

## Stage 13：全链路联调
> 测试: Test_Stage6_8_integration.md Part D

- [ ] 完整流程端到端验证（消息→持久化→Flash Lite→主模型→回复）
- [ ] 3 群并发压力测试
- [ ] 跨窗口 Context 隔离验证
- [ ] 24 小时稳定性运行测试
- [ ] 控制台 ↔ Agent 系统联动验证
- [ ] 打包导出 + 新电脑恢复验证
- [ ] 最终性能基线记录

---

## 🔴 差距修复（2026-04-02 全面对比后新增）

> 来源：全面对比 Plan_1 系列 8 文件 + 初始讨论记录 vs 当前实现
> 分析文档：brain/implementation_plan.md

### Stage 15：消息链路连通 + 真实健康检查（P0）✅
> [!IMPORTANT] 没有消息流入 → Memory/Knowledge/CHECKPOINT 全无数据源 → Agent 系统不工作

- [x] 后端健康检查：5路并发 HTTP/WS 深度探测
- [x] 仪表盘状态：QQ BOT 三层判断 + OneBot WS 告警条
- [x] 新增今日消息 + CHECKPOINT 次数统计卡片
- [x] 截图验证通过

### Stage 16：Sandbox 基础工具实装（P0）✅
> [!IMPORTANT] 工具模型没有基础工具 → Sandbox 全部功能不可用

- [x] 14 个 `.tool.json` 定义文件（base_tools/）
- [x] 后端 `/api/sandbox/tools` 接口
- [x] WebUI Sandbox 页「基础工具列表」卡片（grid + 类别标签 + badge）
- [x] 截图验证通过

### Stage 17：WebUI 功能补全（P1-P2）部分完成
- [x] 插件管理：卡片式展示（复用 tools-grid）+ 启用 badge
- [ ] 仪表盘：模型调用统计 + API开销
- [ ] 对话内存：CHECKPOINT 历史 + 撤回记录 + 按窗口分组
- [ ] Memory：用户画像 + 批量操作 + 趋势图
- [ ] Knowledge：窗口展开 + 时间线 + 手动刷新
- [x] Bot管理：NapCat/AstrBot 一键重启按钮（POST /restart）
- [x] 人格设定：来源信息 + 唤醒词配置提示
- [ ] 系统设置：CHECKPOINT/FlashLite/KVCache 参数 + 密码

---

## Plan 文件清单

| 文件 | 内容 |
|------|------|
| [Plan_1.md](./Plan_1.md) | 总纲 |
| [Plan_1_architecture.md](./Plan_1_architecture.md) | 两层对话管理 + CHECKPOINT |
| [Plan_1_models.md](./Plan_1_models.md) | 三模型分工 |
| [Plan_1_sandbox.md](./Plan_1_sandbox.md) | Sandbox + 安全 + 运行时环境 |
| [Plan_1_memory.md](./Plan_1_memory.md) | Memory + Knowledge |
| [Plan_1_data.md](./Plan_1_data.md) | 数据层 + API 参数 + 可移植性 |
| [Plan_1_gaps.md](./Plan_1_gaps.md) | GAP 补充 |
| [Plan_1_webui.md](./Plan_1_webui.md) | Web 控制台 |
| [Test_Stage3_multimodal.md](./Test_Stage3_multimodal.md) | 多模态测试 |
| [Test_Stage4_persistence.md](./Test_Stage4_persistence.md) | 持久化测试 |
| [Test_Stage5_flashlite.md](./Test_Stage5_flashlite.md) | Flash Lite 测试 |
| [Test_Stage6_8_integration.md](./Test_Stage6_8_integration.md) | 集成测试 |
| [初始讨论记录副本.md](./初始讨论记录副本.md) | 原始讨论存档 |
| [Suggestion_Kaleidoscope_1-3.md](./Suggestion_Kaleidoscope_1.md) | API 适配参考 |
