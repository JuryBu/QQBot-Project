# Plan_1 缺漏-2 测试文件

> 对应文档：`Plan_1_缺漏_2.md`  
> 覆盖范围：工具执行后端 + 权限模型 + 配置一致性

---

## 测试 1：已修复项回归验证

### 1.1 agent.py 工具定义完整性
```python
# 预期：20 个工具定义，语法正确
import ast, re
code = open('agent.py', encoding='utf-8').read()
ast.parse(code)
tools = re.findall(r'^    "(\w+)": \{$', code, re.MULTILINE)
assert len(tools) == 20, f"期望 20，实际 {len(tools)}"
```

### 1.2 main.py @filter.llm_tool 注册数
```python
# 预期：9 个工具注册（后续补全到更多）
import re
code = open('main.py', encoding='utf-8').read()
llm_tools = re.findall(r'@filter\.llm_tool\(name="(\w+)"\)', code)
assert len(llm_tools) >= 9
```

### 1.3 env.json / limits.json 一致性
```python
import json
env = json.load(open('Sandbox/config/env.json'))
assert env['tool_count'] == 17
limits = json.load(open('Sandbox/config/limits.json'))
assert 'max_concurrent' in limits['execution']
assert 'max_code_size_kb' in limits['execution']
assert 'allow_outbound' in limits['network']
```

---

## 测试 2：QQ_data_original 查询功能

### 2.1 基础关键词查询
```
输入: query="老板娘", window_key="GroupMessage:123456", limit=10
预期: 返回该群中包含"老板娘"的消息列表
验证: 每条结果含 sender_name, content_text, created_at
```

### 2.2 时间范围查询
```
输入: window_key="GroupMessage:123456", time_start="2026-04-01", time_end="2026-04-02"
预期: 只返回此时间范围内的消息
```

### 2.3 CHECKPOINT 回溯场景
```
场景: AI 收到用户回复引用了已被 CHECKPOINT 压缩的内容
操作: 调用 QQ_data_original 查询原文
预期: 能找到被压缩前的原始消息内容
```

---

## 测试 3：task_set 任务管理

### 3.1 创建简单任务
```
输入: action="create", task_config={
  "description": "帮群友搜索Python教程",
  "source_pointer": "GroupMessage:123456:msg_789",
  "steps": [
    {"tool": "web_search", "args": {"query": "Python入门教程", "max_results": 3}},
    {"tool": "save_data", "args": {"format": "md", "target_path": "workspace/results/python_tutorial.md"}}
  ],
  "wake_condition": "all_steps_done"
}
预期: 返回 task_id，步骤开始执行
```

### 3.2 检查任务进度
```
输入: action="check", task_id="task_001"
预期: 返回各步骤状态（pending/running/done/failed）
```

### 3.3 终止任务
```
输入: action="kill", task_id="task_001"
预期: 子进程被清理，任务标记为 killed
```

---

## 测试 4：media_summary 多媒体摘要

### 4.1 转发消息摘要
```
输入: msg_type="forward", data=<合并转发JSON>
预期: 返回"收到N条转发消息，内容概要：XXX"
验证: 调用工具模型完成总结
```

### 4.2 视频消息摘要
```
输入: msg_type="video", data={"url": "xxx", "duration": 120, "size_mb": 15}
预期: 返回"收到视频（2分钟, 15MB）"
```

### 4.3 混合类型（转发内含视频）
```
输入: msg_type="forward", data=<含视频的转发JSON>
预期: 先提取转发内容，发现视频后自动调用视频处理，综合摘要
```

---

## 测试 5：权限模型验证

### 5.1 base_tools 只读保护
```
调用: modify_file("base_tools/test.json", "test")
预期: 抛出 PermissionError 或返回错误
```

### 5.2 config 只读保护
```
调用: modify_file("config/env.json", "{}")
预期: 抛出 PermissionError
```

### 5.3 system_report 默认只读
```
非 Review 模式下:
调用: modify_file("base_tools/system_report/test.md", "test")
预期: 拒绝写入

Review 模式下:
系统 launch Review → 开放 system_report 写入 → Review 结束恢复只读
```

### 5.4 workspace 自由操作
```
调用: modify_file("workspace/test/hello.txt", "hello")
预期: 成功写入
```

### 5.5 路径逃逸防护
```
调用: view_file("../../etc/passwd")
预期: 拒绝，SandboxSecurity.validate_path 报路径逃逸
```

### 5.6 删除/重命名禁止
```
调用: 任何删除或重命名操作
预期: 拒绝（workspace 内也不允许删除）
```

---

## 测试 6：web_search + web_fetch

### 6.1 web_search 基础搜索
```
输入: query="Python异步编程教程", max_results=5
预期: 返回工具模型总结的搜索结果摘要（非原始JSON）
验证: 结果含标题、摘要、来源URL
```

### 6.2 web_fetch 页面抓取
```
输入: url="https://docs.python.org/3/library/asyncio.html"
预期: 返回正文 Markdown 格式
验证: 去除导航栏/广告等噪音
```

---

## 测试 7：system_report 定期 Review

### 7.1 自动 Review Launch
```
触发: 系统定时器触发 Sandbox Review
预期: 
  1. 切换 system_report 为可写
  2. 工具模型执行 Review（文件统计 + 异常检测）
  3. 写入 report_YYYYMMDD_HHMMSS.md
  4. 恢复 system_report 为只读
```

### 7.2 Review 报告内容验证
```
读取最新 report 文件
预期含: workspace 文件数、总大小、custom_tools 数量、最近异常记录
```

---

## 测试 8：save_data + upload_data

### 8.1 save_data 保存 JSON
```
输入: data='{"key": "value"}', target_path="workspace/output/result.json", format="json"
预期: 文件创建成功，内容为格式化的 JSON
```

### 8.2 upload_data 发送文件
```
输入: source_path="workspace/output/result.json", target="GroupMessage:123456"
预期: 文件通过 QQ 发送到指定群聊
```

---

## 测试 9：generate_image 图片生成

### 9.1 控制台生图模型配置
```
操作: 在模型配置页选择生图模型并保存
预期: 配置保存到 config.json，只显示有 imageGeneration 能力的模型
```

### 9.2 基础生图
```
输入: prompt="一只坐在月亮上的猫", size="1024x1024"
预期: 调用定义的生图模型 API，返回图片
```

---

## 测试 10：渐进式工具披露

### 10.1 Brief 模式
```
调用: _build_tool_section("brief")
预期: 每个工具只显示一行简短描述
```

### 10.2 Full 模式
```
调用: _build_tool_section("full")
预期: 显示完整的参数 schema
```
