# Gemini Context Caching API 参考资料

> 来源：https://ai.google.dev/api/caching?hl=zh-cn + https://ai.google.dev/gemini-api/docs/caching?hl=zh-cn
> 获取时间：2026-04-13
> 最后更新时间 (UTC)：2026-03-24

---

## 一、概述

Gemini API 提供两种缓存机制：

| 类型 | 触发方式 | 保证省钱 | 存储费 | 我们使用 |
|------|---------|---------|--------|---------|
| **隐式缓存** | 自动（Gemini 2.5+） | ❌ 不保证 | ❌ 无 | ❌ |
| **显式缓存** | 手动 `cachedContents.create` | ✅ 保证 | ✅ 按TTL计费 | ✅ `kv_cache.py` |

### 我们的使用方式
- `kv_cache.py` → `createCachedContent` API = 显式缓存
- 缓存内容：Knowledge + 系统指令 + 角色设定 + 工具声明
- TTL：默认 3600s（1小时），提前60s重建避免竞态
- 存储费率：取决于模型（$1.00~$4.50/小时/百万token）

---

## 二、REST 方法

### 1. `cachedContents.create` — 创建缓存
```
POST https://generativelanguage.googleapis.com/v1beta/cachedContents
```

**请求体** (CachedContent):
```json
{
  "model": "models/{model}",
  "displayName": "bosslady-fixed-{timestamp}",
  "contents": [{ "role": "user", "parts": [{"text": "..."}] }],
  "systemInstruction": { "parts": [{"text": "..."}] },
  "tools": [...],
  "ttl": "3600s"
}
```

**响应** (CachedContent):
```json
{
  "name": "cachedContents/{id}",          // 缓存资源名（后续引用用）
  "model": "models/gemini-2.5-flash",
  "displayName": "bosslady-fixed-xxx",
  "createTime": "2026-04-13T14:00:00Z",
  "updateTime": "2026-04-13T14:00:00Z",
  "expireTime": "2026-04-13T15:00:00Z",  // 过期时间（=创建时间+TTL）
  "usageMetadata": {
    "totalTokenCount": 12345              // ⭐ 缓存消耗的 token 总数
  }
}
```

### 2. `cachedContents.list` — 列出缓存
```
GET https://generativelanguage.googleapis.com/v1beta/cachedContents?key={key}
```
- 查询参数：`pageSize`（最大1000）、`pageToken`
- 返回：缓存元数据列表（不含实际内容）

### 3. `cachedContents.get` — 获取单个缓存
```
GET https://generativelanguage.googleapis.com/v1beta/{name}?key={key}
```
- `name` 格式：`cachedContents/{id}`

### 4. `cachedContents.patch` — 更新缓存
```
PATCH https://generativelanguage.googleapis.com/v1beta/{name}?key={key}
```
- **只能更新过期时间**（`ttl` 或 `expireTime`）
- 不能更新内容、模型等（均为 Immutable）

### 5. `cachedContents.delete` — 删除缓存
```
DELETE https://generativelanguage.googleapis.com/v1beta/{name}?key={key}
```

---

## 三、CachedContent 资源结构

```json
{
  "name": "string",                    // 输出，资源ID
  "model": "string",                   // 必需，不可变
  "displayName": "string",             // 可选，不可变，最长128字符
  "contents": [Content],               // 可选，不可变，要缓存的内容
  "tools": [Tool],                     // 可选，不可变，工具声明
  "systemInstruction": Content,        // 可选，不可变，系统指令
  "toolConfig": ToolConfig,            // 可选，不可变
  "createTime": "string",              // 输出，RFC3339
  "updateTime": "string",              // 输出，RFC3339
  "usageMetadata": {
    "totalTokenCount": integer          // 输出，缓存token总数
  },
  // expiration (二选一)
  "expireTime": "string",              // 过期时间戳
  "ttl": "string"                      // 存活时长，如 "3600s"
}
```

---

## 四、存储费计费模型

### 计费公式
```
存储费 = 缓存token数 × 存储费率 × 存储时长(小时)
       = totalTokenCount / 1,000,000 × storage_rate_per_hour × TTL_hours
```

### 各模型存储费率

| 模型 | 存储费率 ($/小时/百万token) |
|------|--------------------------|
| gemini-3.1-pro-preview | $4.50 |
| gemini-2.5-pro | $4.50 |
| gemini-3.1-flash-lite-preview | $1.00 |
| gemini-3-flash-preview | $1.00 |
| gemini-2.5-flash | $1.00 |
| gemini-2.5-flash-lite | $1.00 |
| gemini-2.0-flash | $1.00 |
| gemini-2.0-flash-lite | 不可用 |

### 关键特性
1. **TTL 是承诺性的**：创建时指定 TTL="3600s"，即使提前 delete，也按完整 TTL 计费
2. **不按请求次数计费**：缓存被引用（hit）50次不多收存储费
3. **续期会产生新费用**：patch 修改 TTL 后按新的过期时间计费
4. **重建 = 删旧 + 建新**：两笔独立的存储费

### 示例计算
```
模型: gemini-2.5-flash
缓存token数: 50,000 tokens
TTL: 1小时
存储费率: $1.00/hr/M tokens

存储费 = 50,000 / 1,000,000 × $1.00 × 1 = $0.00005
```

---

## 五、与我们系统的关联

### kv_cache.py 生命周期事件

| 事件 | 触发条件 | 存储费 |
|------|---------|--------|
| `_create_cache()` | 首次创建 / TTL到期重建 / Knowledge更新重建 | ✅ 产生 |
| `_is_cache_valid() = True` | 内容未变 + TTL未到期 | ❌ 不产生 |
| `_delete_cache()` | 重建前清理 / cleanup关闭 | ❌ 不产生（费用已在create时确定） |
| `invalidate()` | Knowledge更新 → hash清空 | ❌ 本身不产生（下次ensure_cache时创建才产生） |

### 各场景费用分析

**场景A: 正常运行4小时**
- 创建1次 + TTL到期重建3次 = 4笔存储费
- 假设每次50K tokens: 4 × $0.00005 = $0.0002

**场景B: 开10分钟后关**
- 创建1次 = 1笔存储费（按完整1小时TTL计）
- $0.00005

**场景C: 关了1小时后再开**
- 上次创建的已过期（自然到期，不额外收费）
- 重新创建1次 = 新1笔
- $0.00005

**场景D: Knowledge频繁更新（1小时内更新5次）**
- 每次更新都触发 invalidate() → 下次请求时重建
- 5笔存储费 = 5 × $0.00005 = $0.00025

---

## 六、隐式缓存补充说明

- Gemini 2.5+ 自动启用隐式缓存
- 我们的系统同时享有隐式 + 显式两种缓存优惠
- 显式缓存部分（固定区）→ 按 cachedContents 的折扣费率
- 增量区的重复 token → 可能命中隐式缓存（不保证）
- API 响应的 `usage_metadata.cached_content_token_count` 包含两种缓存的命中数

### 最小 token 限制（隐式缓存）
| 模型 | 最小token |
|------|----------|
| 2.5 Flash / Flash-Lite | 1,024 |
| 2.5 Pro | 2,048 |

### 优化建议
- 把较大且常见的内容放在提示开头（提高隐式缓存命中）
- 短时间内发送相似前缀的请求（触发隐式缓存）
- 我们的 KV Cache 固定区设计天然满足这两个条件 ✅
