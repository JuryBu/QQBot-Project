# 🧪 Stage 6-8 测试：CHECKPOINT + KV Cache + Memory/Knowledge

> 对应: Plan_1_architecture.md / Plan_1_memory.md | 前置: Stage 4-5

---

# Part A: CHECKPOINT 压缩测试（Stage 6）

## 场景 A1：首次 CHECKPOINT 压缩

### 模拟输入
- 群聊中已累积 150 条消息（约 40000 token）
- CHECKPOINT 上限设为 50000 token
- 第 151 条消息到达，估算 token 超限

### 预期行为
1. Flash Lite 对前 140 条执行压缩（保留最近 10 条）
2. 压缩率 10-35% → 140 条 → 约 14-49 条等价文本
3. 关键信息保留：
   - 每个发言者的名字至少出现一次
   - 讨论的核心话题被提炼
   - 关键结论/决策被保留
   - 约定的时间/地点保留
4. 压缩后的请求体 C' 结构：
```
C' = [CHECKPOINT 摘要（~5000 token）] + [最近 10 条原文（~3000 token）]
     + knowledge + 系统prompt + 工具说明
```

### 验证方式
```python
# 压缩信息保留度验证
original_entities = extract_entities(original_140_messages)  # 人名、话题、时间等
compressed_entities = extract_entities(checkpoint_summary)
retention_rate = len(compressed_entities & original_entities) / len(original_entities)
assert retention_rate >= 0.8  # 至少保留80%的关键实体
```

## 场景 A2：连续多次 CHECKPOINT

### 模拟
```
第 1 次 CP: 消息 1-140 → 压缩为摘要 S1
  └→ C' = [S1] + [141-150]

第 2 次 CP: 消息 141-290 达到上限
  └→ 将 S1 + [141-280] → 压缩为摘要 S2
  └→ C' = [S2] + [281-290]

第 3 次 CP: ...
```

### 预期行为
1. 每次压缩都包含前次摘要，形成"RNN 式"滚动压缩
2. **信息衰减验证**：消息 1-50 的信息在 S3 中仍可追溯
3. 最近一次压缩的详细度 > 更早压缩的详细度

---

# Part B: KV Cache 测试（Stage 7）

## 场景 B1：首次缓存创建

### 操作
1. 构建固定区内容：knowledge + 系统说明 + 角色设定 + 工具 resource
2. 调用 `createCachedContent` API
3. 后续请求使用 `cachedContent` 引用

### 验证方式
```python
# 缓存创建成功
assert cache_response.status_code == 200
cached_content_name = cache_response.json()["name"]

# 后续请求使用缓存
request_body = {
    "cachedContent": cached_content_name,
    "contents": [incremental_messages],  # 只传增量
    "generationConfig": {...}
}
response = generate_content(request_body)
assert response.status_code == 200
```

### 注意
- [ ] 固定区内容必须 ≥ 32768 token（2.5 系列要求）
- [ ] 如果不足，将部分 CHECKPOINT 历史也放入缓存
- [ ] 验证 3.x 系列模型的最低 token 要求（可能不同）

## 场景 B2：Knowledge 更新后缓存重建

### 操作
1. Flash Lite 更新了 Knowledge
2. 固定区内容变化 → 旧缓存失效
3. 自动重建新缓存
4. 下一次请求使用新缓存

### 验证方式
- [ ] 旧缓存被删除（或 TTL 过期）
- [ ] 新缓存创建成功
- [ ] 请求使用新缓存后回复内容反映 Knowledge 更新

## 场景 B3：撤回消息后的缓存处理

### 模拟
```
1. 消息 A 在增量区内
2. A 被撤回
3. 缓存的增量区需要重建
```

### 预期行为
1. 撤回事件 → 检查消息是否在当前增量区
2. 在增量区内 → 从增量消息列表中移除 → 下次请求自动使用更新后的增量
3. 在 CHECKPOINT 内 → 不处理（压缩摘要不含已撤回的逐字内容）
4. **不需要整体重建缓存**（固定区未变化）

---

# Part C: Memory + Knowledge 集成测试（Stage 8）

## 场景 C1：Memory 写入和检索

### 模拟群聊（含值得记住的信息）
```
[群聊 <GROUP_B>]
17:00:00 Jury: 大家注意，下周三有数据库课的期中考试
17:00:05 张三: 收到，考试范围是到第几章？
17:00:10 Jury: 到第7章，SQL查询和范式都要考
17:00:15 李四: 需要带计算器吗
17:00:20 Jury: 不用，都是手写
17:00:25 张三: @老板娘 帮我记一下这个考试信息
```

### 预期行为
1. 主模型被触发 → 识别到"帮我记"的意图
2. 调用 memory_write：
```python
memory_write(
    title="数据库期中考试信息",
    content="下周三数据库课期中考试，范围到第7章，含SQL查询和范式，手写，不需要计算器",
    tags=["考试", "数据库", "<GROUP_B>"],
    workspace="group:<GROUP_B>"
)
```
3. 回复：「好的，我记下了~ 下周三数据库期中考哦 (ˊ˘ˋ*)」

### 后续验证
```
[3天后]
17:00:00 张三: @老板娘 下周有什么考试来着
```
→ 主模型调用 memory_query → 找到记忆 → 回复考试信息

## 场景 C2：Knowledge 自动更新

### 模拟
```
[群聊 <GROUP_B> - 5条消息后 Flash Lite 同步触发]
17:10:00 A: 你们觉得AI专业怎么样
17:10:05 B: 卷死了
17:10:10 C: 但是工资高
17:10:15 A: 也是，好好学还是有前途的
17:10:20 D: 同意
→ Flash Lite 触发
```

### 验证 Knowledge 更新
```json
{
  "windows": {
    "GroupMessage:<GROUP_B>": {
      "summary": "最近在讨论AI专业的前景，大家认为虽然卷但薪资不错，有前途",
      "active_users": ["A", "B", "C", "D"],
      "mood": "讨论性",
      "last_active": "2 分钟前"
    }
  }
}
```
- [ ] summary 准确反映话题
- [ ] active_users 列表正确
- [ ] mood 合理

## 场景 C3：用户画像积累

### 模拟长期交互记录
```
[Day 1] Jury: @老板娘 帮我查一下PyTorch的文档
[Day 3] Jury: @老板娘 这个LSTM的loss不降是什么原因
[Day 5] Jury: @老板娘 今天好累不想写代码了
[Day 7] Jury: @老板娘 你觉得深度学习有意思吗
[Day 10] Jury: @老板娘 推荐个好听的歌
```

### 预期用户画像积累
```
memory://user_profile/<ADMIN_QQ>
- 常聊话题: AI/深度学习, 编程, 音乐
- 技术关注: PyTorch, LSTM, 深度学习
- 特征: AI专业学生, 有时会累但对技术有热情
- 好感度: 高（频繁互动、信任感）
```

### 验证方式
- [ ] Memory 中存在用户画像记忆条目
- [ ] 画像内容与互动历史一致
- [ ] 主模型回复风格因画像而有差异化

---

# Part D: 全链路集成测试

## 场景 D1：完整流程——从群聊消息到回复

### 模拟完整链路
```
[群聊 <GROUP_B>]
18:00:00 张三: 今天好热啊
18:00:05 李四: 是啊，37度
18:00:10 王五: 我买了个冰西瓜
18:00:15 赵六: 羡慕
18:00:20 钱七: @老板娘 你怕热吗
```

### 完整处理链路验证
```
① 消息持久化 → 5条消息全部写入 SQLite ✓
② Flash Lite 同步触发（第5条到达时） ✓
③ @事件异步触发（第5条有@） ✓
④ 实际是同一事件，异步优先 ✓
⑤ Flash Lite 判断：明确@事件 → 触发主模型 ✓
⑥ Knowledge 更新：「群里在聊天气热，有人买了西瓜」✓
⑦ 主模型收到：
   - Knowledge（含本群最新摘要）
   - 系统说明 + 角色设定
   - CHECKPOINT 历史（如有）
   - 最近 5 条消息原文
   - 工具列表
⑧ 主模型生成回复 ✓
⑨ 回复通过 AstrBot → NapCat → QQ 发出 ✓
⑩ 老板娘的回复也被消息持久化记录 ✓
```

## 场景 D2：群聊风暴压力测试

### 参数
- 3 个群同时活跃
- 每群每秒 2-3 条消息
- 持续 10 分钟
- 其中穿插 5 次 @老板娘
- 其中触发 2 次 CHECKPOINT

### 验证指标
| 指标 | 预期 | 上限 |
|------|------|------|
| 消息持久化丢失率 | 0% | 0.1% |
| Flash Lite 平均延迟 | ≤ 1s | 3s |
| 主模型回复延迟 | ≤ 3s | 8s |
| 内存使用增长 | ≤ 50MB | 200MB |
| SQLite 数据库大小 | ≤ 5MB | 20MB |
| Knowledge 更新频率 | 每 30s | - |
| CHECKPOINT 压缩时间 | ≤ 3s | 6s |

## 场景 D3：跨窗口 Context 隔离

### 模拟
```
[群A <GROUP_B>] 在讨论游戏
[群B <GROUP_A>] 在讨论考试
[私聊 <ADMIN_QQ>] Jury 在请教代码

三个窗口的 @老板娘 几乎同时到达
```

### 验证
- [ ] 三个回复的内容完全独立，不串窗口
- [ ] Knowledge 中三个窗口的 summary 各自准确
- [ ] Memory 工作区隔离正确
- [ ] 并发处理无死锁/竞态

## 场景 D4：长时间运行稳定性

### 参数
- 模拟 24 小时运行
- 白天活跃（300 条/小时/群）→ 晚上安静（20 条/小时/群）
- 中间触发约 100 次 CHECKPOINT

### 验证
- [ ] 24 小时无内存泄漏
- [ ] SQLite 文件不异常膨胀
- [ ] Knowledge 持续准确
- [ ] Flash Lite API 调用无 429 限流（或正确处理退避）
- [ ] 系统可在期间安全重启并恢复
