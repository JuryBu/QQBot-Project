# Task_2_14.md — Plan 2 系列收尾修复任务
> 基于 Report_2_14.md 第八节已确认修复方案
> 排除：H-4（移至 Plan 3）、M-2（不处理）

---

## Stage 1: 安全加固（Critical 级） `[完成 ✅]`

### C-1: Sandbox 命令白名单 ✅
- [x] `sandbox.py`: 在 `exec_code` 的 command 模式增加白名单校验
- [x] 定义白名单列表 + 黑名单列表（支持管道/链式命令检查）
- [x] 非白名单命令返回错误提示 + 日志记录
- [x] `_command_whitelist_enabled` 属性开关（默认开启）
- [x] 验证：rm/curl/wget/net/reg/powershell 等危险命令全部拦截 ✅
- [x] 验证：pip/python/grep/find/dir/echo/node 等正常命令放行 ✅
- [x] 验证：链式命令 `pip install && curl evil` 第二段被拦截 ✅

### C-2: search 工具注入修复 ✅
- [x] `main.py:3468-3492`: 将 query 从 f-string 拼接改为 json.dumps 参数化
- [x] 双层 json 保护：`json.dumps(json.dumps(query))` 确保安全
- [x] 验证：`"; import os; os.system("whoami")` 注入无效 ✅
- [x] 验证：含换行符的 query 安全重建 ✅

---

## Stage 2: 数据完整性（High 级） `[完成 ✅]`

### H-1: 同窗口并发重复追加（两阶段锁） ✅
- [x] `checkpoint.py`: 三方法拆分 `append_messages` / `_append_messages_unlocked` / `_append_messages_inner`
- [x] `checkpoint.py`: 公开 API 保持自带锁（向后兼容）
- [x] `main.py:2665-2700`: Phase 1（锁内）: `load → extract → append → save`
- [x] Phase 2（锁外）: `compress_if_needed`（避免死锁 + 不阻塞新消息）
- [x] 编译通过确认无语法错误 ✅

### H-2: 增量提取截断对齐 ✅
- [x] `main.py:3033-3090`: 增加 `len(contexts) < processed_count` 降级分支
- [x] 新增 `_msg_fingerprint()` 静态方法（role+content[:50]+tool_call_id）
- [x] 反向查找对齐点提取新消息
- [x] 验证：6 个场景（正常增量/截断对齐/无法对齐/空/相等/tool_call_id）全部 PASS ✅

---

## Stage 3: 功能修复（High + Medium 级） `[完成 ✅]`

### H-3: 子代理递归封禁 ✅
- [x] `main.py:1588`: `excluded_tools` 加入 `browser_agent` 和 `run_custom_tool`

### H-5: 路径白名单加固 ✅
- [x] `main.py:5228`: 改用 `Path.resolve()` + `relative_to()` 严格校验
- [x] 防止 `/tmp2` 匹配 `/tmp` 前缀等路径穿越

### M-1: _compressing finally 化 ✅
- [x] `checkpoint.py:595`: `_compressing.add` 后包裹 try/finally
- [x] 移除 4 处手动 discard（L664, L669, L756, L781）
- [x] L603-L776 的 166 行代码正确缩进到 try 块内
- [x] 编译通过确认缩进正确 ✅

### M-4: 工具声明 schema 对齐 → 运行时检查
- [ ] base_tools/ 目录为运行时生成，需在实际运行环境中验证
- [ ] `browser_agent.tool.json`: 确认 `inject_context` 参数存在

### M-5: Memory 迷你索引截断 ✅
- [x] `main.py:1187-1190`: 先 `[:MAX_INDEX]` 限制 pinned 再计算剩余额度

### M-6: 子代理 upload_data 修复 → N/A
- [x] 代码中已无 `upload_data` 调用，无需处理

### P2-2: 后端参数校验（新增）→ 运行时检查
- [ ] `models.py`: 参数校验逻辑需在实际运行环境中确认位置

---

## Stage 4: 质量收口（Medium + Low 级） `[部分完成]`

### M-3: 回归测试更新 → 运行时验证
- [ ] `test_checkpoint_v2.py`: 需在完整环境中运行测试确认兼容性

### L-1: 死代码清理 + 系统认知更新 ✅
- [x] `agent.py`: 注释已在之前迭代中更新（L48-49 标注迁移）
- [x] `_get_recent_context()` 已在之前迭代中移除
- [x] `_get_checkpoint_summary()` 已标注废弃并返回 None

### L-2: 重复装饰器 ✅
- [x] `main.py:4755`: 删除重复的 `@filter.llm_tool(name="browser_agent")`

---

## Stage 5: 综合验证 `[完成 ✅]`
- [x] `py_compile` 全部 12 个模块通过 ✅
- [x] C-1 白名单测试 21 case 全部 PASS ✅
- [x] C-2 注入测试 4 case 全部 PASS ✅
- [x] H-2 指纹对齐测试 6 case 全部 PASS ✅
- [ ] 启动系统确认功能正常（需用户验证）
- [ ] 请 Codex 进行修复后复审
