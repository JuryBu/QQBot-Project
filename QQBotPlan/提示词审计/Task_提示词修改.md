# Task: FlashLite 提示词审计后修改 — 全部完成 ✅

## 任务 A：FlashLite system prompt 合并双模式输出格式 ✅

- [x] L1262-1278: `_build_flash_lite_system()` 输出格式改为双模式
  - 模式一：消息判断 — 保持全部标记行
  - 模式二：对话压缩 — 简短说明直接输出摘要文本
- [x] L1288: 删除重复的"标记行之外"行
- [x] 确认 `build_compress_prompt()` (checkpoint.py) 不需要修改

## 任务 B：工具模型 inject_context 参数 ✅

- [x] B1. `_call_tool_model` 签名增加 `context_text: str = ""`
- [x] B1. messages 构建时 context_text 非空则拼接前缀
- [x] B2. `tool_task_set` 签名增加 `inject_context: str = ""`
- [x] B2. handler 中 inject_context=true 时从 event 推断 window_key 读取 T 文件上下文快照
- [x] B2. 上下文存入 meta["context_text"]，两处 _call_tool_model 调用传入
- [x] B3. `tool_browser_agent` 签名增加 `inject_context: str = ""`
- [x] B3. handler 中 inject_context=true 时获取上下文并传入 _call_tool_model
- [x] B4. 主模型 prompt: task_set create 参数描述追加 inject_context
- [x] B4. 主模型 prompt: 工具速查 browser_agent 追加 inject_context 说明

## 验证 ✅

- [x] `py_compile` 语法检查通过
- [x] AST 签名验证：三个函数签名正确
- [x] AST 逻辑验证：双模式格式正确、context_text 传递链完整

## 审计文档更新 ✅

- [x] Prompt_FlashLite_判断.md — 输出格式段改为双模式
- [x] Prompt_FlashLite_压缩.md — 冲突标注改为"已修复"，差异表更新
- [x] Prompt_主模型.md — Section 14/15 追加 inject_context 参数，第四节改为"已实现"
- [x] Prompt_工具模型.md — 第二节增加上下文注入机制描述
