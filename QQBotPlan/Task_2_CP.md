# CHECKPOINT 重构 Task

## 项目文档

- [Plan_2_CP.md](../Plan_2_CP.md) — 总纲领
- [Plan_2_CP_architecture.md](../Plan_2_CP_architecture.md) — 三系统分立架构
- [Plan_2_CP_T_file.md](../Plan_2_CP_T_file.md) — T 文件格式规范
- [Plan_2_CP_compression.md](../Plan_2_CP_compression.md) — 压缩策略
- [Plan_2_CP_integration.md](../Plan_2_CP_integration.md) — 集成修改清单
- [Report_2_11.md](../Report_2_11.md) — 原始分析报告

---

## Stage 1: 基础设施搭建

- [ ] 在 `checkpoint.py` 中新增 `TFileManager` 类
  - [ ] T 文件加载/保存/创建（原子写入）
  - [ ] Per-window asyncio.Lock
  - [ ] `build_llm_contexts()` 方法
  - [ ] `build_flashlite_context()` 方法
- [ ] 创建 `QQ_data/checkpoints/` 目录
- [ ] 在 `FlashLite.__init__` 中初始化 `_t_file_mgr`
- [ ] 面板新增参数
  - [ ] `checkpoint_compress_front_ratio`（默认 0.7）
  - [ ] `checkpoint_cooldown_seconds`（默认 300）
  - [ ] BossLady Console 前后端同步

## Stage 2: 核心压缩逻辑

- [ ] 实现 `compress_if_needed()` 方法
  - [ ] 三重触发守卫（token 超限 + 消息够多 + 冷却期过）
  - [ ] 压缩范围计算（前 N% + keep_recent 保护）
  - [ ] 新版压缩 Prompt（明确目标字数/token）
  - [ ] 压缩率验证 + warning 日志
  - [ ] T 文件更新（新 T1 + 剩余 messages）
- [ ] 保存压缩统计到 `checkpoint_history` 表（面板用）

## Stage 3: on_llm_request 集成

- [ ] 删除旧 CHECKPOINT system_prompt 注入（L2667-2687）
- [ ] 新增 T 文件驱动的 req.contexts 替换逻辑
  - [ ] 加载 T 文件
  - [ ] 从 req.contexts 增量提取新消息
  - [ ] 检查压缩
  - [ ] `req.contexts = build_llm_contexts(t_file)`
- [ ] 异常回退（T 文件出错时保持原始 req.contexts）
- [ ] LLM 回复后回写 T 文件（assistant + tool_call 记录）

## Stage 4: FlashLite 上下文迁移

- [ ] `_build_judgment_prompt()` 的上下文来源改为 T 文件
  - [ ] 群消息触发判断：从 T 文件读取
  - [ ] 私聊消息触发判断：从 T 文件读取
- [ ] Knowledge 系统兼容性验证
  - [ ] 确认 knowledge_update 字段正常输出
  - [ ] 确认 profile_update、active_users 字段正常

## Stage 5: 清理旧代码

- [ ] 删除 `check_and_compress()` 方法
- [ ] 删除 `build_context_for_main_model()` 方法
- [ ] 删除 main.py 中两处旧 CHECKPOINT 调用点
  - [ ] L866-881（群消息同步触发后）
  - [ ] L1169-1184（私聊同步触发后）
- [ ] 删除 `agent.py` 中 `_get_checkpoint_summary()` 方法

## Stage 6: 验证

- [ ] 启动 AstrBot，正常群聊对话
- [ ] 检查 T 文件是否正确创建和更新
- [ ] 检查日志：
  - [ ] 压缩率是否在 20-40% 目标范围
  - [ ] 不再频繁触发（冷却期生效）
  - [ ] 工具调用过程正确记录在 T 文件
- [ ] 检查面板 Knowledge：确认窗口摘要正常更新
- [ ] 检查主模型回复质量不受影响
- [ ] 重启 AstrBot，确认 T 文件持久化正常恢复
- [ ] Codex 独立 Review
