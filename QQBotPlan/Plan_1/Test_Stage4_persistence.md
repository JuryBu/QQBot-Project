# 🧪 Stage 4 测试：消息持久化层

> 对应: Plan_1_gaps.md GAP 2 | 前置: AstrBot 插件框架可用

---

## 测试目标
验证自建的全量消息持久化插件能正确拦截、存储、查询所有 QQ 消息。

---

## 场景 1：基础消息写入

### 模拟输入（60 秒内的群聊）
```
[群聊 <GROUP_B>]
15:00:00 张三: 有人在吗
15:00:05 李四: 在的
15:00:10 王五: 刚下课
15:00:15 张三: 今天的作业谁抄了
15:00:20 李四: 我抄了一部分
15:00:25 [图片: 作业照片]
15:00:30 王五: 6
15:00:35 张三: 🐂
15:00:40 赵六: @张三 给我也抄一下
15:00:45 张三: 好
15:00:50 [老板娘被@]: @老板娘 你觉得抄作业对不对
15:00:55 老板娘: 我觉得......
```

### 预期行为
1. **全部 12 条消息**（含老板娘回复）都被写入 SQLite
2. 每条消息包含完整字段：sender_id, sender_name, content_text, created_at
3. 含图片的消息 has_image=True，image_urls 存有 URL
4. 表情（🐂）被正确存储
5. @消息的 content_raw 包含 At 组件信息
6. 异步写入不阻塞消息处理（延迟 < 10ms）

### 验证方式
```sql
-- 验证消息总数
SELECT COUNT(*) FROM qq_messages WHERE window_id='<GROUP_B>' 
  AND created_at BETWEEN '15:00:00' AND '15:01:00';
-- 预期: 12

-- 验证图片消息
SELECT * FROM qq_messages WHERE has_image=1 AND window_id='<GROUP_B>';
-- 预期: 1 条，image_urls 非空

-- 验证顺序
SELECT sender_name, content_text, created_at FROM qq_messages 
  WHERE window_id='<GROUP_B>' ORDER BY created_at ASC LIMIT 5;
-- 预期: 张三→李四→王五→张三→李四 按时间排序
```

---

## 场景 2：消息撤回处理

### 模拟输入
```
[群聊 <GROUP_B>]
15:05:00 张三: 我喜欢隔壁班的小红
15:05:10 [系统: 张三 撤回了一条消息]
15:05:15 李四: 来不及了我截图了hhhh
```

### 预期行为
1. "我喜欢隔壁班的小红" 被写入数据库
2. 收到撤回通知 → 标记 `is_recalled=True`, `recalled_at=15:05:10`
3. **不物理删除**（保留完整历史）
4. QQ_data_original 查询时标注「[已撤回]」

### 验证方式
```sql
SELECT content_text, is_recalled, recalled_at 
FROM qq_messages WHERE window_id='<GROUP_B>' AND sender_name='张三'
  AND content_text LIKE '%小红%';
-- 预期: is_recalled=1, recalled_at='15:05:10'
```

---

## 场景 3：QQ_data_original 查询接口

### 测试 3.1：按时间范围查询
```python
# 查询最近 1 小时的消息
result = qq_data_original.query(
    window_type="group",
    window_id="<GROUP_B>",
    time_range="1h",
    limit=50
)
# 预期: 返回最多 50 条消息，按时间排序
```

### 测试 3.2：关键词搜索
```python
result = qq_data_original.search(
    window_id="<GROUP_B>",
    keyword="作业",
    limit=10
)
# 预期: 返回包含"作业"的消息列表
```

### 测试 3.3：按发送者过滤
```python
result = qq_data_original.query(
    window_id="<GROUP_B>",
    sender_id="123456789",
    time_range="24h"
)
# 预期: 只返回该用户的消息
```

---

## 场景 4：多窗口并发写入

### 模拟输入
```
[同时在 3 个群中有消息]
15:10:00 [群 <GROUP_B>] 张三: 消息A-1
15:10:00 [群 <GROUP_A>] Alice: 消息B-1
15:10:00 [私聊 <ADMIN_QQ>] Jury: 消息C-1
15:10:01 [群 <GROUP_B>] 李四: 消息A-2
15:10:01 [群 <GROUP_A>] Bob: 消息B-2
15:10:01 [私聊 <ADMIN_QQ>] Jury: 消息C-2
```

### 预期行为
1. 6 条消息全部正确写入，不丢失
2. 每条消息的 window_type 和 window_id 正确
3. 不发生数据库锁冲突（异步写入+队列/WAL模式）

### 验证方式
```sql
SELECT window_id, COUNT(*) FROM qq_messages 
  WHERE created_at >= '15:10:00' GROUP BY window_id;
-- 预期: <GROUP_B>→2, <GROUP_A>→2, <ADMIN_QQ>→2
```

---

## 压力测试

### 高频消息写入
- 模拟活跃群：每秒 3 条消息，持续 5 分钟
- 总计约 900 条消息
- 验证：无丢失，写入延迟 < 50ms/条
- 数据库大小增长合理

### 冷热数据迁移测试
- 写入 30 天的模拟数据（约 15000 条/群）
- 触发冷数据策略（热 7 天保留全量，冷 7-30 天保留文本）
- 验证：冷数据中 image_urls 为空或标记为「[图片已清理]」
- 查询冷数据的响应时间 ≤ 500ms

### 数据库大小测试
- 模拟 10 个群 × 30 天 × 300 条/天 = 90000 条
- 预期数据库大小 ≤ 50MB（纯文本）
- 验证 SQLite 的 WAL 模式不会导致文件膨胀
