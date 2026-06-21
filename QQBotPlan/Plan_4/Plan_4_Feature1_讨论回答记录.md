# 📋 Feature 1 讨论回答记录 + 决策汇总

> 基于 2026-04-14 主人回答整理

---

## 已确定的决策

| 问题 | 决策 | 备注 |
|------|------|------|
| Q1 向量模型 | **Gemini text-embedding-004**（主），m3e-base 作 fallback | AI_Chart_Generator 已删除，m3e 需重新下载 |
| Q2 维度 | **768** | Gemini embedding 默认值 |
| Q3 存储引擎 | **ChromaDB** | 需要 metadata 过滤（按群/用户/时间） |
| Q4 数据源 | Memory + QQ 群聊原文（含 bot 回复）| 以 QQ_data_original 查询内容为准 |
| Q5 索引策略 | **增量实时**，参考 FlashLite 采样策略（计数+时间窗口）减压 |  |
| Q6 切分策略 | **分层策略已确定** — Memory 逐条；QQ 原文用"话题摘要"向量化 | 见下方详述 |

---

## Q6 话题切分 — 已确定方案

### 分层处理

| 数据类型 | 切分策略 | 理由 |
|---------|---------|------|
| **Memory 条目** | 逐条向量化，不需任何 tag | 每条本身就是单主题、无混合内容 |
| **QQ 群聊原文** | **"话题摘要"向量化** | FlashLite 触发时顺便输出群"最近话题摘要"，向量化的是摘要而非逐条原文 |

### QQ 原文"话题摘要"方案详解

```
FlashLite 采样触发时（计数+时间窗口）
     ↓
FlashLite 额外输出字段：recent_topics_summary
     ↓ 示例输出：
     "最近群里在讨论：
      1. AI绘画的标签写法和模型选择
      2. 期末考试复习安排
      3. Steam夏促推荐游戏"
     ↓
将摘要文本 → Gemini text-embedding-004 向量化 → 存入 ChromaDB
     ↓ metadata:
     {group_id, timestamp, source: "topic_summary"}
```

**优势**：
- 不需要逐条消息打 tag → 无零散分类膨胀
- 摘要天然过滤了无意义消息（表情包、图片等）
- 向量化的是高质量主题描述 → embedding 精度高
- 完全复用 FlashLite 现有的采样触发机制 → 零额外成本
- 联想命中后 → 主模型通过 QQ_data_original 按时间段取原文

### 需要后续确认的细节
- [ ] FlashLite 输出 `recent_topics_summary` 的 prompt 设计
- [ ] 摘要粒度：每次输出几个话题？覆盖多长时间段？
- [ ] 是否需要保留历史摘要（累积）还是只保留最新的

---

## Q17 图片搜索功能调研

> 详细调研报告见 `Plan_4_Feature4_图像搜索调研.md`

### 关键发现

**已有插件**：
- `astrbot_plugin_setu` — Lolicon API，代码可参考复用
- `astrbot_plugin_pixiv_search` — pixivpy3，代码可参考复用
- `astrbot_plugin_img_rev_searcher` — **多引擎反向搜图** (SauceNAO/AnimeTrace/Google Lens/Bing/Baidu/Yandex/ExHentai)，可直接安装

**参考项目**：
- `cq-picsearcher-bot`（1.6k⭐）— SauceNAO + ascii2d + trace.moe，交互模式设计值得参考
- kasuie.cc 部署教程 — 该项目的实际使用经验

**Google Cloud Vision Web Detection**：
- ❌ ≠ Google Lens，效果明显弱于后者
- 开发者普遍反馈"不如 SauceNAO"
- 定位：fallback 补充，不作为主力

**待主人决策**：见 `Plan_4_Feature4_图像搜索调研.md` 末尾 D1-D5
| Q7 融合策略 | **互补模式**（向量只推 FlashLite 没推的），默认 5 条，面板可调 |  |
| Q8a 注入位置 | **选项 C：user 消息上方** | 离用户消息近，注意力强 |
| Q8b 注入量 | **默认 1-2 条**，面板可调区间 | 做成滑动条 |
| Q8c 触发条件 | **每次都搜索 + 阈值判断是否注入** | 阈值面板可调 |
| Q8d 干扰风险 | 通过 prompt 说明联想是"参考性质"+ 引导用 QQ_data_original 进一步确认 |  |
| Q9 与 Memory 关系 | 联想辅助 Memory 选择，配合 FlashLite 互补机制 | 不是独立系统 |
| Q10 KV Cache | 联想走 conversation 通道，不影响 KV Cache |  |
| Q11 面板粒度 | **全部专家模式 + 主人提到的所有参数** | 顶级可控精度 |
| Q12 成本预算 | **不在意 ¥0.5-1/月** | 内存开销也可接受 |
| Q13 数据量级 | 4-5 活跃群，200-1000 条/天，**ChromaDB** | 有分级维护机制 |
| Q14 启动顺序 | **三个场景都要** | 不分先后 |
| Q15 验证方式 | **直接上线**，因为可以关 |  |
| Q16 多模态 | **✅ 要支持** — 图片等多模态内容也建向量索引 |  |

---

## 🔥 核心未解决问题：Q6 话题切分

### 主人的深度分析摘要

**问题本质**：群聊中话题混合、穿插、渐进切换是常态，不存在泾渭分明的边界。

**方案 A（逐条消息）的问题**：精度虽高但对向量检索来说"精度"意义不大——
联想需要的是"片段"而非"单条"，后续具体上下文由模型通过 QQ_data_original 自行获取。

**方案 B（滑动窗口）的问题**：
- 一个 7 条消息的窗口中 4 条聊 A、3 条聊 B → embedding 被稀释
- 无法准确匹配 A 话题或 B 话题的联想请求
- 群聊中这种混合话题是常态，可预见效果不好

**方案 C（话题 tag 分类）的理想设计**：
- 对每条消息打话题 tag → 同 tag 消息归入同一"话题片段"
- 允许消息同时属于多个 tag（重叠）
- 解决了混合话题的 embedding 稀释问题

**方案 C 的问题**：
- 每条消息都需要维护 tag 列表 → 额外存储和计算
- 零散消息产生大量微小分类 → 信息量膨胀
- tag 的生成本身需要 LLM 或复杂规则 → 成本和延迟

### 需要进一步讨论

> 如果采用 tag 切分，能否设计更高效的体系？
> 如果实在不行，有没有折中方案？

---

## Q17 图片搜索功能调研结果

### 现有 AstrBot 插件

| 插件 | 接口 | 状态 |
|------|------|------|
| `astrbot_plugin_setu` | `api.lolicon.app/setu/v2` | 已安装 |
| `astrbot_plugin_pixiv_search` | `pixivpy3`（AppPixivAPI, 需 Refresh Token）| 已安装 |
| `astrbot_plugin_img_rev_searcher` | SauceNAO + Bing + Google Lens 等 | 插件市场有，未安装 |

### 可用搜索 API 调研

| 接口 | 用途 | API Key | 免费额度 | 精度 |
|------|------|---------|---------|------|
| **SauceNAO** | 插画溯源（Pixiv/Danbooru 等） | 需要 | 200次/天（未登录6次/30s） | ⭐⭐⭐⭐ |
| **trace.moe** | 动漫截图识别（定位集数+时间戳） | 不需要 | 无限制（有频率限制） | ⭐⭐⭐⭐⭐ |
| **Lolicon API** | 色图随机/自定义标签获取 | 不需要 | 较大 | ⭐⭐⭐ |
| **pixivpy3** | Pixiv 标签搜索/插画详情 | Refresh Token | 无限 | ⭐⭐⭐⭐ |
| **Google Cloud Vision** | Web Detection（类似 Google Lens） | 需要 | 1000次/月免费 | ⭐⭐⭐⭐ |
| **SerpApi (Google Lens)** | 第三方爬取 Google Lens 结果 | 需要 | 100次/月免费 | ⭐⭐⭐⭐⭐ |
| **TinEye** | 版权/使用追踪式反向搜图 | 需要 | 有限免费额度 | ⭐⭐⭐ |

### 图三中群友提到的 soutubot
- 本质是 SauceNAO + trace.moe 的 Telegram bot 封装
- 不提供公开 API，但其模式可以直接借鉴
- 我们自己整合这些 API 就能做出同等甚至更强的搜图能力

### Google Lens 接口情况
- **没有官方公开 API**
- Google Cloud Vision API 的 Web Detection 功能最接近，但不完全等同
- 第三方 SerpApi 可以爬取 Google Lens 结果，但有 ToS 风险
- 建议：优先用 SauceNAO + trace.moe + Cloud Vision，够用就不引入灰色地带的爬虫
