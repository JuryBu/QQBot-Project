# Kaleidoscope 项目：Gemini API 模型参数适配经验

> 本文档来自 Kaleidoscope（万花筒 AI 绘图工作台）项目在对接 Gemini API（generativelanguage.googleapis.com）时的完整参数适配经验。
> 该项目使用 **REST API + API Key** 方式调用，与 Python SDK 调用方式的字段名存在映射差异，这是最大的坑点之一。

---

## 一、核心问题：Google 没有提供完整的模型参数兼容性文档

我们在实践中调研了所有可能的参数信息来源：

| 来源 | 提供的信息 | 能否直接用 |
|------|-----------|:---:|
| `models.get` API | 返回 `thinking: boolean` 等元数据，但 imageConfig 没有对应字段 | ⭐ 部分可用 |
| Discovery Document | 定义了 `GenerationConfig` 的完整 schema（imageConfig/thinkingConfig 等），但是**全局的**，不区分模型 | ⚠️ 仅 schema 参考 |
| Vertex AI Studio UI | 控制台内部知道每个模型支持什么，但不对外暴露 API | ❌ 无法程序访问 |
| 官方文档页面 | 零散说明，不成体系 | ❌ 需要人工整理 |
| **试错法（我们的方案）** | 发请求检测报错信息 | ✅ **最可靠** |

**结论**：目前唯一可靠的方式是「发真实 API 请求 → 看是否报错」来探测每个模型支持哪些参数。

---

## 二、最大的坑：REST API 字段名 ≠ SDK 字段名

### 2.1 字段名映射关系

Python SDK 使用 `snake_case`，REST API 使用 `camelCase`，但**不是所有字段都是简单的下划线转驼峰**：

| Python SDK 字段 | REST API 字段（generationConfig 内） | 说明 |
|----------------|--------------------------------------|------|
| `response_modalities` | `responseModalities` | ✅ 标准转换 |
| `image_config` | `imageConfig` | ✅ 标准转换 |
| `image_config.aspect_ratio` | `imageConfig.aspectRatio` | ✅ 标准转换 |
| `image_config.image_size` | `imageConfig.imageSize` | ✅ 标准转换 |
| `image_config.output_mime_type` | ❌ **不存在** | SDK 内部处理，REST 不支持 |
| `thinking_config.thinking_budget` | `thinkingConfig.thinkingBudget` | ✅ 标准转换 |
| `thinking_config.thinking_level` | `thinkingConfig.thinkingLevel` | ✅ 标准转换 |

### 2.2 我们踩过的坑

**错误字段名 `imageGenerationConfig`**：

我们最初参考了某些文档/示例，使用了 `imageGenerationConfig` 作为字段名，结果 API 返回 400 错误 `Cannot find field`。

**正确字段名是 `imageConfig`**，放在 `generationConfig` 内部：

```json
{
  "generationConfig": {
    "responseModalities": ["TEXT", "IMAGE"],
    "imageConfig": {
      "aspectRatio": "16:9",
      "imageSize": "1K"
    },
    "thinkingConfig": {
      "thinkingBudget": 4096
    }
  }
}
```

**`outputMimeType` 不存在于 REST API**：

虽然 Vertex AI Studio 的示例代码中 SDK 有 `output_mime_type="image/png"`，但 REST API 中 `imageConfig` 下没有这个字段。发送会被拒绝。SDK 可能在内部做了转换或忽略。

---

## 三、REST API 端点选择

### 3.1 Gemini API Key vs Vertex AI

这是另一个重大坑点。根据你使用的认证方式，端点完全不同：

| 认证方式 | 端点 | 认证方法 |
|---------|------|---------|
| **Gemini API Key**（以 `AIza...` 开头） | `https://generativelanguage.googleapis.com/v1beta` | URL 参数 `?key={API_KEY}` |
| **Vertex AI**（Service Account / ADC） | `https://{REGION}-aiplatform.googleapis.com/v1/publishers/google/models/` | Bearer Token |

> ⚠️ 我们最初错误地把 Gemini API Key 发到了 `aiplatform.googleapis.com` 端点，导致 404。
> **Gemini API Key 只能用 `generativelanguage.googleapis.com`**。

### 3.2 关键 API 路径

```
# 模型列表
GET  /v1beta/models?key={API_KEY}

# 非流式生成
POST /v1beta/models/{MODEL_ID}:generateContent?key={API_KEY}

# 流式生成（SSE）
POST /v1beta/models/{MODEL_ID}:streamGenerateContent?alt=sse&key={API_KEY}
```

### 3.3 模型过滤

`/v1beta/models` 返回的模型列表包含很多非生成模型（embedding、TTS 等），需要过滤：
- 只保留 `supportedGenerationMethods` 包含 `"generateContent"` 的模型
- 只保留模型名含 `"gemini"` 的（过滤掉 embedding 和旧模型）
