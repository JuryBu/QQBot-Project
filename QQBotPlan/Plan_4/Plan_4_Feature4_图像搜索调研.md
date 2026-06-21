# 🔍 Feature 4 图像搜索功能 — 深度调研报告

> 2026-04-14 调研整理

---

## 一、现有生态盘点

### 1.1 我们已安装的 AstrBot 插件

| 插件 | 功能 | 接口 | 状态 |
|------|------|------|------|
| `astrbot_plugin_setu` | 色图获取（随机/标签） | `api.lolicon.app/setu/v2` | ✅ 已安装 |
| `astrbot_plugin_pixiv_search` | Pixiv 标签搜索 + 订阅推送 | `pixivpy3` (AppPixivAPI) | ✅ 已安装，需 Refresh Token |

### 1.2 可安装的 AstrBot 插件

| 插件 | 仓库 | 功能 |
|------|------|------|
| `astrbot_plugin_img_rev_searcher` | [drdon1234/...](https://github.com/drdon1234/astrbot_plugin_img_rev_searcher) | 多引擎反向搜图 |

**`img_rev_searcher` 详解**（85 commits，活跃维护至 2026-01）：

支持引擎：
- **SauceNAO** — 插画/同人画溯源（Pixiv/Danbooru 等）
- **AnimeTrace** — 动漫角色识别（多模型支持：anime/lovelive/game 等）
- **Google Lens** — 通用反向搜图（视觉匹配/精确匹配/产品搜索）
- **Bing** — 微软反向搜图
- **Baidu** — 百度识图
- **Yandex** — 俄系搜索引擎识图
- **EhSubs (ExHentai)** — 本子搜索（需 Cookie 认证，地区限制）

依赖：`httpx>=0.23.0`, `Pillow>=9.0`, `selenium>=4.0`, `pyquery`

搜索方式：图片 URL / 上传文件 / Base64 编码，支持 GIF（截取第一帧）

### 1.3 参考项目：cq-picsearcher-bot

| 属性 | 值 |
|------|-----|
| 仓库 | [Tsuk1ko/cq-picsearcher-bot](https://github.com/Tsuk1ko/cq-picsearcher-bot) |
| Stars | 1.6k ⭐ |
| 语言 | JavaScript (Node.js) |
| 协议 | OneBot 11（支持 NapCatQQ） |
| 版本 | v3.22.1 (2025-11) |
| 搜索引擎 | **SauceNAO + ascii2d + trace.moe (whatanime)** |

关键设计参考：
- 私聊**直接发图**即搜，群聊需要关键词触发或 @机器人
- 同一消息多图**批量搜索**
- SauceNAO API Key 集成，有搜索配额管理
- 附加功能：复读、色图、OCR、B站解析/推送、ChatGPT 等

**价值**：虽然不能直接用（Node.js 项目），但它的**交互模式设计**（私聊自动搜、群聊触发搜、批量搜）非常值得参考。

---

## 二、可用 API 接口全景

### 2.1 二次元/ACG 专用

| 接口 | 用途 | 费用 | 精度 | API Key |
|------|------|------|------|---------|
| **SauceNAO** | 插画/同人画溯源 | 免费 200次/天（注册） | ⭐⭐⭐⭐⭐ | 需要 |
| **trace.moe** | 动漫截图 → 番名+集数+时间戳 | 免费（有频率限制） | ⭐⭐⭐⭐⭐ | 不需要 |
| **ascii2d** | 二次元反向搜图补充 | 免费 | ⭐⭐⭐ | 不需要 |
| **AnimeTrace** | 动漫角色识别 | 免费 | ⭐⭐⭐⭐ | 不需要 |
| **Lolicon API** | 色图随机获取 | 免费 | N/A | 不需要 |
| **pixivpy3** | Pixiv 标签搜/详情/下载 | 免费 | ⭐⭐⭐⭐ | Refresh Token |

### 2.2 通用搜图

| 接口 | 用途 | 费用 | 精度 | 备注 |
|------|------|------|------|------|
| **Google Cloud Vision** | Web Detection（类 Google Lens） | 1000次/月免费 | ⭐⭐⭐ | 非二次元效果中等 |
| **Google Lens (via img_rev_searcher)** | 视觉匹配/产品搜索 | 免费（爬取） | ⭐⭐⭐⭐ | selenium 爬取方式 |
| **Bing 识图** | 微软反向搜图 | 免费（爬取） | ⭐⭐⭐ | |
| **Baidu 识图** | 百度反向搜图 | 免费（爬取） | ⭐⭐⭐ | 中文生态好 |
| **Yandex** | 俄系反向搜图 | 免费（爬取） | ⭐⭐⭐⭐ | 对二次元较友好 |
| **TinEye** | 版权追踪式精确脸搜 | 有限免费 | ⭐⭐⭐ | 非 ACG 向 |

### 2.3 效果对比总结

| 场景 | 最佳接口 | 次选 |
|------|---------|------|
| 找插画作者/出处 | **SauceNAO** | ascii2d |
| 找动漫截图来源 | **trace.moe** | SauceNAO |
| 识别动漫角色 | **AnimeTrace** | Gemini 多模态 |
| 通用反向搜图 | **Google Lens** | Yandex |
| 本子搜索 | **ExHentai** (需 Cookie) | SauceNAO |

### 2.4 Google Cloud Vision 效果评估

调研结论：**不建议作为主力搜图引擎，但可作为补充**

- Cloud Vision 的 Web Detection ≠ Google Lens，效果明显不如后者
- 开发者普遍反馈"结果比 Google Lens 弱很多"
- 适合识别 Web 上已有的图片出处（重复传播追踪）
- 对 ACG 插画的识别精度显著不如 SauceNAO
- **建议定位**：作为 fallback 在 SauceNAO/trace.moe 都没结果时尝试

---

## 三、集成方案建议

### 3.1 架构设计

```
用户发送图片 + "搜图"指令（或 @老板娘 + 图片）
         ↓
老板娘通过 tool calling 调用搜图工具
         ↓
┌────────────────────────────────────────┐
│         Sandbox 搜图工具包              │
│                                        │
│  ACG 层:                               │
│  ├── SauceNAO (插画来源)               │
│  ├── trace.moe (动画截图)              │
│  ├── ascii2d (补充)                    │
│  └── AnimeTrace (角色识别)             │
│                                        │
│  通用层（fallback）:                    │
│  ├── Google Lens (via selenium/爬取)    │
│  ├── Yandex                            │
│  └── Google Cloud Vision (API)         │
│                                        │
│  Pixiv 层:                             │
│  ├── pixivpy3 (标签搜索+详情)          │
│  └── Lolicon API (随机色图)            │
└────────────────────────────────────────┘
         ↓
结果整合 → 图文卡片回复
```

### 3.2 复用策略

| 来源 | 可复用内容 | 方式 |
|------|----------|------|
| `astrbot_plugin_setu` | Lolicon API 调用逻辑 + 图片混淆 | 直接参考代码 |
| `astrbot_plugin_pixiv_search` | pixivpy3 认证/搜索/下载逻辑 | 直接参考代码 |
| `astrbot_plugin_img_rev_searcher` | **核心搜索引擎封装（整个 ImgRevSearcher 目录）** | 可直接安装复用 |
| `cq-picsearcher-bot` | 交互模式设计（私聊自动/群聊触发/批量搜） | 设计参考 |

### 3.3 与 FlashLite 集成方式

不走传统命令式，而是融入老板娘的自然交互：

- **主动搜图**：群友发图+"这是谁画的" → 老板娘自然回应 + 搜图结果
- **tool calling**：主模型收到图片后，可通过工具调用主动搜图
- **Gemini 多模态**：先用 Gemini 识图描述内容 → 再调搜索工具精确查找
- **批量搜**：多图消息自动并发搜索，合并结果回复

---

## 四、需主人决策的事项

### D1. img_rev_searcher 安装方式？
- **A**：直接通过 AstrBot 插件市场安装，保持独立运行
- **B**：参考其代码，提取搜索引擎封装部分到 Sandbox 工具
- **C**：先安装试用效果，后续再决定是否整合

### D2. SauceNAO API Key
- 需要注册获取：https://saucenao.com/user.php
- 免费额度：200次/天（注册用户），未注册 6次/30s
- 主人需要去注册一个吗？

### D3. Google Cloud Vision 是否纳入？
- 1000次/月免费，需要 Google Cloud 账户
- 效果中等，作为 fallback 可以，但不是必须
- 主人决定是否开通

### D4. ExHentai 支持？
- img_rev_searcher 支持但需要 Cookie + 优质欧美 IP
- 是否需要这个功能？

### D5. Pixiv Refresh Token
- pixiv_search 插件已安装但需要 Token 才能工作
- 获取方式参考：https://gist.github.com/karakoo/5e7e0b1f3cc74cbcb7fce1c778d3709e
- 主人是否已配置？


————————————————————————————————————————————————————————————————————————————————————————————

好的主人，先来处理你的几个问题，然后看看你打开的文件喵~

Used MCP tool: web-fetcher/web_fetch_page
Searched web: "ExHentai cookie 获取方法 ipb_member_id ipb_pass_hash E-Hentai 登录 2025"
Viewed stage.py:1-383
Viewed output.txt:1-4
Viewed astr_message_event.py:1-505

好的，现在看完了所有你打开的文件。让我来逐一回答喵~

---

### 📌 各项决策回复

**D1 ✅ B — 提取代码到 Sandbox 工具**，记录

**D2 — SauceNAO 免注册也够用**
你说得对！6次/30s 对于机器人场景完全够了。搜图不是高频操作，一个搜图请求处理完发回用户远超 30s。先不注册，如果以后用量上来再考虑。

**D3 — Google Cloud Vision 暂不纳入**
对的，目前 SauceNAO + trace.moe + Google Lens (via selenium) 已经够强了。Cloud Vision 效果本身就弱于 Google Lens，1000次/月额度没必要浪费。

**D4 — ExHentai Cookie**
ExHentai 需要的是 **E-Hentai 账户登录后的 Cookie**，具体三个值：
- `ipb_member_id` — 用户 ID
- `ipb_pass_hash` — 登录哈希
- `igneous` — 里站权限验证（**最关键**，新号需养 10-20 天才有权限）

获取方法：浏览器登录 E-Hentai → F12 → Application → Cookies → 复制这三个值。你挂优质欧美代理的话 IP 没问题。**需要的是你的 E-Hentai 账号**。

**D5 — Pixiv Refresh Token**
你没理解错！那个 gist 是一个 **Playwright 自动化脚本**，流程是：
1. 脚本打开浏览器 → 自动访问 Pixiv OAuth 页面
2. 自动填入你的 **Pixiv 用户名和密码**
3. 如果开了两步验证 → 需要你输入验证码
4. 自动获取 `access_token` 和 `refresh_token`

所以**你需要做的只是**：提供 Pixiv 用户名+密码，或者在弹出的浏览器里登录。其他都是脚本自动完成的。我可以帮你跑这个脚本，就是需要你的 Pixiv 凭据。

---

### 🔥 重点：setu 插件的反审查机制

分析了 `main.py`，它用了**两个关键手段**绕过 QQ 图片审查：

#### 1. `image_obfus()` — 图片哈希混淆
```python
# 随机选 3 个像素点，每个通道 ±1
# 人眼完全看不出变化，但图片哈希值完全改变
# QQ 的 NSFW 检测有一套基于图片哈希的黑名单机制
# 乱改像素后哈希就不在黑名单里了
```

#### 2. `send_forward` — 合并转发发送
```python
# 用 Node（合并转发节点）包装图片发送
# 合并转发消息在 QQ 端的审查宽松得多
# 因为转发消息只有接收者点开才能看到内容
# QQ 对直接发送的图片审核更严，但对转发节点内的图片审核较弱
```

从 `stage.py` 来看，AstrBot 自身消息发送管线也有：
- `Node`、`Nodes` 类型支持 → 可以用合并转发
- `Image.fromBytes()` → 支持字节流发图
- 分段回复机制 → 搜图结果可以分段发送

### 📝 搜图工具设计建议

未来搜图工具的图片发送应该参考 setu 的模式：
1. **搜图结果图片通过 `image_obfus` 预处理** — 防止被 QQ 哈希黑名单拦截
2. **可选使用合并转发发送** — 对 NSFW 内容用转发包装
3. **面板控制开关** — 是否启用混淆 / 是否用转发模式

Used MCP tool: memory-store/memory_update

所有决策都记录好了~ 还有什么想讨论的吗主人？ (=^•ω•^=)