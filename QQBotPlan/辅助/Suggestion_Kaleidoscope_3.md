# Kaleidoscope 经验（续）：踩坑清单与 AstrBotLauncher 适配建议

---

## 七、完整踩坑清单

### 坑 1：imageGenerationConfig vs imageConfig

- **现象**：发送 `generationConfig.imageGenerationConfig.aspectRatio` 时 API 返回 400 `Cannot find field 'imageGenerationConfig'`
- **原因**：REST API 的字段名是 `imageConfig`，不是 `imageGenerationConfig`
- **修复**：全局搜索替换为 `imageConfig`
- **验证**：替换后 Nano Banana / Nano Banana Pro / Nano Banana 2 的 aspectRatio 和 imageSize 全部生效

### 坑 2：thinkingBudget vs thinkingLevel 互斥

- **现象**：对 Gemini 2.5 系列发送 `thinkingLevel` 被拒绝；对 3.x 系列发送 `thinkingBudget` 被拒绝
- **原因**：2.5 系列用数值型 `thinkingBudget`（0~65536），3.x 系列用枚举型 `thinkingLevel`（MINIMAL/LOW/MEDIUM/HIGH）
- **教训**：**两种参数不能混用，必须根据模型系列选择**
- **怎么适配**：models.get 返回的元数据中没有直接标记用哪种，但 2.5 系列模型名含 `"2.5"`，3.x 含 `"3"` 或 `"3.1"`，可以用名称匹配

### 坑 3：thinkingLevel 的部分支持

- **现象**：Nano Banana 2 (`gemini-3.1-flash-image-preview`) 发送 `thinkingLevel: "LOW"` 被拒绝，但 `"MINIMAL"` 和 `"HIGH"` 通过
- **原因**：不同模型支持的 thinkingLevel 枚举值不完全相同
- **教训**：**必须逐个探测每个 level**,  不能假设支持 thinkingLevel 就支持所有 levels

### 坑 4：Gemini API Key 发到了 Vertex AI 端点

- **现象**：HTTP 404 或 401 Unauthorized
- **原因**：`AIza...` 开头的 API Key 只能用于 `generativelanguage.googleapis.com`，不能用于 `aiplatform.googleapis.com`
- **规则**：API Key → generativelanguage；Service Account → aiplatform

### 坑 5：ChatRequest 没有传递新参数

- **现象**：前端设置了 aspectRatio、imageSize、thinkingLevel 等参数，但 API 调用中没有生效
- **原因**：后端的 `ChatRequest` Pydantic schema 只定义了 `temperature` 和 `max_tokens`，新增参数没有加到 schema 中
- **教训**：**每次新增模型参数，需要同时更新：schema → config 构建 → 前端传递**，形成完整链路

### 坑 6：流式响应只处理文本

- **现象**：图像生成模型返回了图片，但 SSE 流只处理了 `text` part，`inlineData`（图片 base64）被丢弃
- **原因**：流式处理逻辑只 check `part.get("text")`，没有 check `part.get("inlineData")`
- **修复**：增加对 `inlineData` 的处理，将图片数据以 `data:image/png;base64,...` 格式返回

### 坑 7：outputMimeType 不存在

- **现象**：REST API 发送 `imageConfig.outputMimeType` 被 400 拒绝
- **原因**：这个字段只在 Python SDK 中存在，REST API 的 imageConfig 下没有这个字段
- **规则**：**永远以 REST API 实测为准**，不要盲目相信 SDK 文档的字段在 REST 中也存在

---

## 八、给 AstrBotLauncher 的具体适配建议

### 8.1 如果你也用 Gemini API Key + REST

1. **端点必须是** `generativelanguage.googleapis.com/v1beta`
2. **字段名以 REST 实测结果为准**，不要直接把 SDK 文档的 snake_case 转 camelCase 就完事
3. **实现三层优先级**是性价比最高的方案

### 8.2 构建 generationConfig 的参考代码

```python
def build_generation_config(model_id: str, user_params: dict) -> dict:
    """根据模型能力构建 generationConfig"""
    caps = get_model_capabilities(model_id)
    config = {}

    # 基础参数（所有模型都支持）
    if "temperature" in user_params:
        config["temperature"] = user_params["temperature"]
    if "topP" in user_params:
        config["topP"] = user_params["topP"]
    if "maxOutputTokens" in user_params:
        config["maxOutputTokens"] = user_params["maxOutputTokens"]

    # 图像生成参数（仅图像模型）
    if caps.get("responseModalities"):
        config["responseModalities"] = ["TEXT", "IMAGE"]
        if caps.get("aspectRatio") or caps.get("imageSize"):
            image_config = {}
            if user_params.get("aspectRatio"):
                image_config["aspectRatio"] = user_params["aspectRatio"]
            if user_params.get("imageSize"):
                image_config["imageSize"] = user_params["imageSize"]
            if image_config:
                config["imageConfig"] = image_config

    # Thinking 参数（互斥，不能同时发）
    if caps.get("thinkingBudget") and user_params.get("thinkingBudget") is not None:
        config["thinkingConfig"] = {
            "thinkingBudget": user_params["thinkingBudget"]
        }
    elif caps.get("thinkingLevel") and user_params.get("thinkingLevel"):
        # 确保这个 level 在该模型支持的 levels 列表中
        supported = caps.get("supported_levels", [])
        level = user_params["thinkingLevel"]
        if not supported or level in supported:
            config["thinkingConfig"] = {
                "thinkingLevel": level
            }

    return config
```

### 8.3 探测请求的模板

```python
async def probe_param(model_id: str, gen_config_extra: dict) -> bool:
    """探测某个模型是否支持某组参数"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": "1"}]}],
        "generationConfig": {
            "maxOutputTokens": 1,  # 最小输出，省钱
            **gen_config_extra
        }
    }
    resp = await httpx.AsyncClient().post(url, json=body, timeout=15)
    if resp.status_code == 200:
        return True
    if resp.status_code == 400:
        error = resp.json().get("error", {}).get("message", "")
        if "Cannot find field" in error or "not supported" in error:
            return False
    return None  # 其他错误（429、500等）
```

### 8.4 分组探测策略

```python
# 探测顺序和分组
PROBE_GROUPS = [
    # 1. 先探测 responseModalities（决定是否是图像模型）
    {
        "name": "image_output",
        "config": {"responseModalities": ["TEXT", "IMAGE"]},
        "filter": lambda m: "image" in m.lower() or "banana" in m.lower(),
    },
    # 2. 图像参数（仅对上一步成功的模型）
    {
        "name": "image_config",
        "config": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"}
        },
        "filter": lambda m: probed_caps[m].get("responseModalities"),
    },
    # 3. thinkingBudget（2.5 系列）
    {
        "name": "thinking_budget",
        "config": {"thinkingConfig": {"thinkingBudget": 1}},
        "filter": lambda m: "2.5" in m,
    },
    # 4. thinkingLevel（3.x 系列，逐个 level 测试）
    {
        "name": "thinking_level",
        "levels": ["MINIMAL", "LOW", "MEDIUM", "HIGH"],
        "filter": lambda m: "3" in m and "2.5" not in m,
    },
]
```

---

## 九、我们 model_prober.py 的核心架构参考

```
C:\Users\<user>\Desktop\VC工具包\Kaleidoscope\backend\app\services\model_prober.py
```

关键设计点：

1. **`ModelProber` 类**：接收 API Key，管理探测生命周期
2. **`probe_all(models)`**：全量探测，对模型列表中每个模型执行探测序列
3. **`probe_incremental(models)`**：增量探测，只探测新模型、移除已删除模型
4. **`_probe_param(model_id, gen_config)`**：单次探测的原子操作
5. **`asyncio.Semaphore(3)`**：并发上限 3，防止 429 风控
6. **指数退避**：429 时等待 2^n 秒（2s→4s→放弃）
7. **请求间隔**：每次请求后 `asyncio.sleep(0.3)` 冷却

```
C:\Users\<user>\Desktop\VC工具包\Kaleidoscope\backend\app\services\vertex_ai.py
```

关键设计点：

1. **`_MODEL_CAPS_REGISTRY`**：硬编码的已验证模型能力注册表
2. **`_heuristic_caps(model_id)`**：基于模型名的启发式推理
3. **`get_model_capabilities(model_id)`**：三层优先级查询
4. **`build_gen_config(model_id, params)`**：用能力查询结果构建合法的 generationConfig

---

## 十、总结

| 经验教训 | 原则 |
|---------|------|
| 不要相信文档 100% | **以 REST 实测结果为唯一真相** |
| 字段名不要凭猜测 | **SDK snake_case → REST camelCase 不是 1:1，有例外** |
| 模型能力有差异 | **同系列不同模型的参数支持可以不同** |
| thinkingBudget vs thinkingLevel | **互斥，按模型系列选择** |
| 参数链路要完整 | **前端设置 → schema → config 构建 → API 调用** |
| 探测很便宜 | **maxOutputTokens: 1 + 最短 prompt → 几乎零成本** |
| 增量探测 | **只探新模型，不要每次全量** |
| 三层优先级 | **探测缓存 > 硬编码注册表 > 启发式推理** |

---

> **文件来源**：Kaleidoscope AI 绘图工作台项目  
> **适配日期**：2026-03-29  
> **API 版本**：generativelanguage.googleapis.com/v1beta  
> **注意**：Google 可能随时更新 API 参数结构，以上信息需要定期验证更新
