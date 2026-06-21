# Kaleidoscope 经验（续）：模型参数真相表与自动探测方案

---

## 四、完整模型参数真相表（2026-03-29 实测）

以下是我们通过真实 API 调用验证的每个模型参数支持情况：

### 4.1 图像生成模型

| 模型 ID | responseModalities | imageConfig.aspectRatio | imageConfig.imageSize | thinkingBudget | thinkingLevel | 支持的 Levels |
|---------|:---:|:---:|:---:|:---:|:---:|:---:|
| `gemini-2.5-flash-image` (Nano Banana) | ✅ | ✅ | ✅ | ❌ | ❌ | — |
| `gemini-3-pro-image-preview` (Nano Banana Pro) | ✅ | ✅ | ✅ | ❌ | ❌ | — |
| `gemini-3.1-flash-image-preview` (Nano Banana 2) | ✅ | ✅ | ✅ | ❌ | ✅ | MINIMAL, HIGH |

> **关键发现**：Nano Banana 2 的 thinkingLevel 只支持 MINIMAL 和 HIGH，LOW 和 MEDIUM 被拒绝。这种「部分支持」的情况无法从文档中得知，只能通过逐个探测发现。

### 4.2 思考模型（2.5 系列用 thinkingBudget，3.x 系列用 thinkingLevel）

| 模型 ID | thinkingBudget | thinkingLevel | 支持的 Levels |
|---------|:---:|:---:|:---:|
| `gemini-2.5-pro` | ✅ | ❌ | — |
| `gemini-2.5-flash` | ✅ | ❌ | — |
| `gemini-3-pro-preview` | ❌ | ✅ | LOW, MEDIUM, HIGH |
| `gemini-3-flash-preview` | ❌ | ✅ | MINIMAL, LOW, MEDIUM, HIGH |
| `gemini-3.1-pro-preview` | ❌ | ✅ | LOW, MEDIUM, HIGH（MINIMAL 被拒绝）|
| `gemini-3.1-flash-lite-preview` | ❌ | ✅ | MINIMAL, LOW, MEDIUM, HIGH |

### 4.3 通用参数（所有 generateContent 模型都支持）

```json
{
  "temperature": 1.0,        // 0.0 ~ 2.0
  "topP": 0.95,              // 0.0 ~ 1.0
  "maxOutputTokens": 65536,  // 各模型上限不同，从 models.get 获取
  "candidateCount": 1,       // 1~8，同时出多组结果（我们用客户端并行替代）
  "seed": 0                  // 随机种子，同参数+同种子可复现结果
}
```

### 4.4 安全设置

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

### 4.5 imageConfig 可选值

| 参数 | 可选值 | 说明 |
|------|--------|------|
| `aspectRatio` | `"1:1"`, `"4:3"`, `"3:4"`, `"16:9"`, `"9:16"`, `"auto"` | 图像比例 |
| `imageSize` | `"0.5K"`, `"1K"`, `"2K"`, `"4K"` | 图像分辨率 |

> `outputMimeType` 在 REST API 中**不存在**（SDK 特有），发送会被 400 拒绝。

---

## 五、我们的三层优先级参数适配架构

### 总体设计

```
第 1 层：自动探测结果（Probed results）— 最高优先级
  ├── API Key 验证成功后自动触发（后台异步）
  ├── 用真实 API 调用探测每个模型的参数支持
  └── 结果缓存到内存 + DB，下次启动直接恢复

第 2 层：硬编码注册表（_MODEL_CAPS_REGISTRY）— 验证过的 fallback
  ├── 手动测试验证过的已知模型
  └── 万一探测失败时使用

第 3 层：启发式推理（Heuristic inference）— 保底 fallback
  ├── 根据模型名称模式匹配推断能力
  ├── 含 "image" → 推断支持 responseModalities + imageConfig
  ├── 含 "2.5" → 推断支持 thinkingBudget
  └── 含 "3." → 推断支持 thinkingLevel
```

### 查询逻辑（简洁版伪代码）

```python
def get_model_capabilities(model_id: str) -> dict:
    # 1) 探测结果 — 最高优先级
    probed = get_probed_caps(model_id)
    if probed:
        return probed

    # 2) 硬编码注册表 — 验证过的 fallback
    if model_id in _MODEL_CAPS_REGISTRY:
        return _MODEL_CAPS_REGISTRY[model_id]

    # 3) 启发式推理 — 保底
    return _heuristic_caps(model_id)
```

---

## 六、自动探测方案（ModelProber）

### 6.1 核心思路

对每个模型发送带特定参数的最小请求，根据 API 返回区分：
- ✅ HTTP 200 → 该参数被支持
- ❌ HTTP 400 + `Cannot find field` / `not supported` → 该参数被拒绝
- ⏳ HTTP 429 → 限流，指数退避重试
- 其他错误 → 模型本身不可用

### 6.2 成本优化

| 优化项 | 方法 | 效果 |
|--------|------|------|
| **最小 prompt** | `contents: [{"role":"user","parts":[{"text":"1"}]}]` | 输入 ~1 token |
| **最小输出** | `maxOutputTokens: 1` | 输出 ~1 token |
| **预筛选** | 只探测需要的模型（image 模型测 imageConfig，thinking 模型测 thinkingConfig） | 跳过 ~60% 模型 |
| **组合探测** | 先一次性测组合参数（imageConfig + aspectRatio + imageSize），成功则全部标记支持 | 减少请求数 |
| **总成本** | 约 20-30 次 API 调用 | ≈ $0.00001 |

### 6.3 并发风控

```python
class ModelProber:
    def __init__(self, api_key, max_concurrent=3, delay=0.3):
        self._semaphore = asyncio.Semaphore(max_concurrent)  # 最多 3 个并发
        self._delay = delay                                   # 请求间隔 0.3s

    async def _probe_param(self, model_id, gen_config, retries=2):
        for attempt in range(retries + 1):
            async with self._semaphore:
                resp = await self._client.post(url, json=body)
                if resp.status_code == 429:  # 限流
                    wait = 2 ** (attempt + 1)  # 指数退避 2s → 4s
                    await asyncio.sleep(wait)
                    continue
                return resp.status_code == 200
```

### 6.4 增量探测（关键优化）

**不要每次都全量探测**，而是只探测新增模型，移除已删除模型：

```python
async def probe_incremental(self, models):
    current_ids = {m["id"] for m in models}
    cached_ids = set(_probed_caps_cache.keys())

    # 1. 移除已删除模型
    for mid in (cached_ids - current_ids):
        del _probed_caps_cache[mid]

    # 2. 只探测新增模型
    new_models = [m for m in probeable if m not in cached_ids]
    for model_id, meta in new_models:
        caps = await self.probe_model(model_id, meta)
        _probed_caps_cache[model_id] = caps
```

### 6.5 触发时机

| 时机 | 探测类型 | 说明 |
|------|---------|------|
| API Key 验证成功 | 全量探测 | 首次获取完整能力图谱 |
| 刷新模型列表 | 增量探测 | 只探测新增模型 |
| 手动触发 `/models/probe` | 全量探测 | 用户主动刷新 |
| 正常使用 | 不探测 | 使用缓存结果 |
