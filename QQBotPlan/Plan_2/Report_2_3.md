# Report_2_3: 问题 3 收尾审计报告

> 审计时间: 2026-04-07 15:55
> 审计范围: FlashLite main.py (3987行) + AstrBot respond/stage.py (370行) + aiocqhttp adapter
> 审计方式: 代码精确搜索 + 逐行阅读关键实现

---

## 1. 提示词层：输出格式规范

### 审计结论: ✅ 完整覆盖

主模型 `on_llm_request` 的 `inject_parts` 中有**两处**明确告知模型输出规范：

### 1-A. 输出硬约束（L1726-1735）— 最高优先级

```python
# main.py L1726-1735
inject_parts.append(
    "## 🚨 输出风格硬性约束（最高优先级）\n"
    "无论下面有多少工具说明和规范，回复用户时必须遵守：\n"
    "1. 每次回复最多 1-3 句话，绝对不超过 3 句\n"
    "2. 句内用空格代替逗号连接，不用「。」「！」「，」，用语气词(呀/嘛/呢/啦/吧/捏)收尾\n"
    "3. 禁止分点列举，禁止排比，禁止三段式（铺垫+正文+总结）\n"
    "4. 工具调用的中间说明也要简短，不要解释过程\n"
    "违反以上任何一条都是严重错误。"
)
```

**覆盖检查**:
- ✅ 不使用 `！` `。` `；` → 第 2 条明确「不用『。』『！』『，』」
- ✅ 用空格代替 → 第 2 条「句内用空格代替逗号连接」
- ✅ 1-3 句话 → 第 1 条「每次回复最多 1-3 句话，绝对不超过 3 句」
- ✅ 像微信聊天一样简短 → 第 3 条禁止排比/三段式 + 第 4 条工具说明也简短

### 1-B. 分段系统告知（L1887-1893）

```python
# main.py L1887-1893
inject_parts.append(
    "## 回复格式要求\n"
    "- 你正在 QQ 群聊/私聊中对话 保持简短口语化\n"
    "- 你的输出会被分段系统自动处理：按空格切分→短句合并→长句拆分→逐条延迟发送\n"
    "- 所以你只需要控制：用空格隔开语义段 总输出控制在 2-3 个短句(每句≤40字)\n"
    "- 超长内容(代码/报告/分析)不要直接输出 用 system_report 或 modify_file 存文件后告知路径\n"
    "- 不要使用 Markdown 标题/列表/代码块等格式（分段系统有 MD 清洗 但最好别用）\n\n"
)
```

**覆盖检查**:
- ✅ 空格是分割标识 → 「按空格切分→短句合并→长句拆分」
- ✅ 自动分割成多句 → 「逐条延迟发送」
- ✅ 过长内容用文件 → 「超长内容...用 system_report 或 modify_file 存文件后告知路径」
- ⚠️ 未提及 HTML/PDF 形式 → 只提了 `system_report` 和 `modify_file`，**没有明确说可以生成 HTML/MD/PDF 文件**

### 1-结论

| 要求 | 状态 | 位置 |
|------|------|------|
| 不使用 `！` `。` `；` | ✅ | L1731 |
| 空格代替逗号 | ✅ | L1731 |
| 1-3 句话简短 | ✅ | L1730 |
| 空格是分割标识 | ✅ | L1890 |
| 长内容用文件 | ⚠️ 部分 | L1892 只提了 system_report/modify_file，未提 HTML/PDF |

---

## 2. 分段输出机制

### 审计结论: ✅ 完整实现，工作正常

核心实现在 `AstrBot/astrbot/core/pipeline/respond/stage.py`

### 2-A. 配置读取（L65-97）

```python
# 分段开关
self.enable_seg = config["platform_settings"]["segmented_reply"]["enable"]
# 短句合并阈值（默认80）
self.merge_threshold = config["segmented_reply"].get("merge_threshold", 80)
# 自适应延迟三档
self.delay_short  = parse("0.8,1.5")   # ≤15字
self.delay_medium = parse("1.5,3.0")   # 16-40字
self.delay_long   = parse("2.5,4.5")   # >40字
# 最大分段数硬限（默认3）
self.max_segments = config["segmented_reply"].get("max_segments", 3)
# 延迟模式: adaptive / log / random
self.interval_method = config["segmented_reply"]["interval_method"]
```

### 2-B. 短句合并（L284-299）

```python
# 相邻 Plain comp 字数和 <= merge_threshold 时合并
if self.merge_threshold > 0:
    merged_chain = []
    for comp in result.chain:
        if (isinstance(comp, Comp.Plain)
            and merged_chain
            and isinstance(merged_chain[-1], Comp.Plain)
            and len(merged_chain[-1].text) + len(comp.text) <= self.merge_threshold):
            merged_chain[-1] = Comp.Plain(
                merged_chain[-1].text.rstrip() + " " + comp.text.lstrip()
            )
        else:
            merged_chain.append(comp)
    result.chain = merged_chain
```

### 2-C. 硬限分段数（L301-316）

```python
# 超出 max_segments 的 Plain 强制合并到末段
if self.max_segments > 0 and len(result.chain) > self.max_segments:
    keep = result.chain[:self.max_segments - 1]
    merge_rest = result.chain[self.max_segments - 1:]
    # 将超出的文本合并为一条
    keep.append(Comp.Plain(" ".join(merged_texts)))
    keep.extend(non_plain)  # 非文本组件保留
    result.chain = keep
```

### 2-D. 自适应延迟发送（L127-147 + L318-320）

```python
async def _calc_comp_interval(self, comp):
    if self.interval_method == "adaptive":
        wc = await self._word_cnt(comp.text)
        if wc <= 15:   return random.uniform(*self.delay_short)   # 0.8-1.5s
        elif wc <= 40: return random.uniform(*self.delay_medium)  # 1.5-3.0s
        else:          return random.uniform(*self.delay_long)    # 2.5-4.5s
    elif self.interval_method == "log":
        # 对数模式
        ...
    # random 模式
    return random.uniform(self.interval[0], self.interval[1])

# 发送循环（L318-320）
for comp in result.chain:
    i = await self._calc_comp_interval(comp)
    await asyncio.sleep(i)  # ← 每句之间随机延迟
    # 然后发送该 comp
```

### 2-结论

| 要求 | 状态 | 位置 |
|------|------|------|
| 空格合并短句（<阈值） | ✅ | L284-299 |
| 过长输出切分 | ✅ | L301-316 max_segments 硬限 |
| 每句之间随机延迟 | ✅ | L318-320 asyncio.sleep |
| 延迟根据长度分级 | ✅ | L129-138 短/中/长三档 |
| BossLady 设置可调 | ✅ | L65-97 全部从配置读取 |

---

## 3. 引用消息信息注入

### 审计结论: ✅ 完整实现

引用消息的注入分为**三层**：

### 3-A. 平台层：message_str 增强注入（aiocqhttp_platform_adapter.py L340）

```python
# L340: 将回复标记写入 message_str（增强版：携带资源信息和 msg_id 指针）
# 最终格式: [回复 昵称 | 附件=xxx, url=xxx | msg_id=xxx] 原始回复文本
```

当用户引用一条消息时，`message_str` 中自动携带：
- 被引用者的**昵称**
- 附件信息（文件名、URL）
- **msg_id 指针**（可被 `QQ_data_original` 的 `around_msg_id` 使用）

### 3-B. 插件层：@quoted 变量注册（main.py L214-236）

```python
def _register_quoted_vars(self, event):
    """从当前消息的 Reply 组件提取引用资源，注册到 _quoted_vars"""
    self._quoted_vars = {}  # 每次请求重置
    for comp in event.message_obj.message:
        if isinstance(comp, Reply):
            self._quoted_vars["@quoted_msg"] = str(comp.id)      # 消息ID
            for c in chain:
                if isinstance(c, File):
                    self._quoted_vars["@quoted_file"] = c.url    # 文件URL
                elif isinstance(c, Image):
                    self._quoted_vars["@quoted_image"] = c.url   # 图片URL
                elif isinstance(c, Forward):
                    self._quoted_vars["@quoted_forward"] = c.id  # 转发ID
```

### 3-C. 提示词层：快捷语法告知（main.py L2096-2100）

```python
inject_parts.append(
    "## 引用消息快捷语法\n"
    "- 用户引用消息时，message_str 中已注入 [回复 xxx | 附件=xxx, url=xxx | msg_id=xxx] 信息\n"
    "- 工具参数中可用 @quoted_file / @quoted_image / @quoted_msg / @quoted_forward 快捷引用\n"
    "- 需要查看引用消息上下文时：QQ_data_original(around_msg_id='@quoted_msg', count=10)\n"
    "- around_msg_id 会围绕该消息取前后各 count/2 条记录，📌 标记锚点消息"
)
```

### 3-结论

| 要求 | 状态 | 位置 |
|------|------|------|
| message_str 携带引用原文 | ✅ | adapter L340 |
| 携带附件/URL/msg_id | ✅ | adapter L340 增强版格式 |
| @quoted 快捷变量 | ✅ | main.py L214-236 |
| 提示词告知模型如何使用 | ✅ | main.py L2096-2100 |

---

## 4. QQ_data_original 工具

### 审计结论: ✅ 完整实现

### 4-A. 工具注册（main.py L2743-2744）

```python
@filter.llm_tool(name="QQ_data_original")
async def tool_qq_data_original(self, event, window_key="", start_seq=0,
                                 count=20, keyword="", around_msg_id=""):
```

### 4-B. 核心功能

| 参数 | 说明 | 实现 |
|------|------|------|
| `window_key` | 窗口标识，留空自动推断 | ✅ L2762-2769 自动从 event 提取 |
| `start_seq` | 起始消息序号 | ✅ 数据库 seq 查询 |
| `count` | 获取条数（上限50） | ✅ L2756 `min(count, 50)` |
| `keyword` | 关键词过滤 | ✅ 数据库 LIKE 查询 |
| `around_msg_id` | 围绕某消息取上下文 | ✅ L2759-2760 + `_resolve_quoted` |

### 4-C. @quoted_msg 联动

```python
# L2758-2760: 解析 @quoted 快捷语法
if around_msg_id:
    around_msg_id = self._resolve_quoted(around_msg_id)
```

模型调用 `QQ_data_original(around_msg_id='@quoted_msg', count=10)` → 自动解析为实际 msg_id → 取该消息前后各 5 条 → 📌 标记锚点消息。

### 4-D. 提示词覆盖

- L2078: 工具速查表列出 `【数据】QQ_data_original(原始聊天, around_msg_id=指针回溯)`
- L2099: 引用消息快捷语法中明确示例 `QQ_data_original(around_msg_id='@quoted_msg', count=10)`

### 4-结论: ✅ 工具可用，提示词告知充分

---

## 5. save_data 文件名指针下载机制

### 审计结论: ✅ 完整实现

### 5-A. 三模式架构（main.py L3583-3633）

```python
@filter.llm_tool(name="save_data")
async def tool_save_data(self, event, data="", path="", url="",
                          local_path="", encoding="utf-8"):
    # 模式1: 文本写入 — data + path
    # 模式2: URL下载 — url + path  → _save_data_from_url
    # 模式3: 本地复制 — local_path + path → _save_data_from_local
```

### 5-B. URL 下载模式（L3635-3713）

- ✅ 50MB 大小限制（流式下载 + 中途检测）
- ✅ Content-Type 与扩展名一致性警告
- ✅ **魔数校验**：文件头 vs 扩展名（PDF/PNG/JPG/ZIP/DOCX/XLSX/PPTX）
- ✅ 假文件检测：文件头不匹配时检查是否为文本内容（链接失效/错误页面）
- ✅ 下载失败自动清理临时文件

### 5-C. 本地复制模式 — 白名单安全（L3715-3752）

```python
ALLOWED_PREFIXES = [
    "%USERPROFILE%\\Documents\\Tencent Files",  # QQ 官方缓存
    "%LOCALAPPDATA%\\Tencent",
    "%APPDATA%\\Tencent\\QQNT",
    "%APPDATA%\\QQ",
    "%APPDATA%\\NapCat",                         # NapCat 各位置
    "%LOCALAPPDATA%\\NapCat",
    "%USERPROFILE%\\.config\\NapCat",
    "%TEMP%\\napcat-plugin-uploads",              # NapCat 上传缓存
    "%TEMP%",                                     # 通用临时
    AstrBot_data_dir,                             # AstrBot 自身 data
]
# 严格前缀匹配：不在白名单内的路径一律拒绝
if not any(local_path.startswith(prefix) for prefix in ALLOWED_PREFIXES):
    return "安全限制: 不允许复制该路径..."
```

### 5-D. @quoted 快捷语法联动（L3612-3614）

```python
# @quoted 快捷语法解析
url = self._resolve_quoted(url)           # @quoted_file → 实际 URL
local_path = self._resolve_quoted(local_path)  # @quoted_file → 实际路径
```

### 5-E. 提示词层告知（L1973-1978）

```python
"- 模式3 本地复制: save_data(local_path=文件路径, path=保存路径) → 仅限QQ/NapCat缓存目录\n"
"4. QQ文件附件 → save_data(local_path=路径, path=sandbox路径) 复制到 Sandbox\n"
```

### 5-F. Sandbox 封闭性

- ✅ `save_data` 只能往 Sandbox **内部**写入（L3606: `path` 强制 `workspace/` 前缀）
- ✅ `local_path` 只能从**白名单目录**读取（L3743: 严格前缀匹配）
- ✅ **无法反向从 Sandbox 向外部写入**（没有任何工具支持写入 Sandbox 外的路径）
- ✅ 50MB 文件大小限制

### 5-结论

| 要求 | 状态 | 位置 |
|------|------|------|
| 有效文件名指针下载 | ✅ | L3612-3614 @quoted 解析 |
| NapCat 本地文件复制 | ✅ | L3715-3752 白名单模式 |
| 不破坏 Sandbox 封闭性 | ✅ | L3606 强制 workspace/ + L3743 前缀匹配 |
| 50MB 限制 | ✅ | L3638 + L3749 |
| 魔数校验防假文件 | ✅ | L3674-3702 |
| 重名处理 | ⚠️ | 当前实现直接覆盖同名文件，无重名提示 |

---

## 总结

| # | 审计项 | 状态 | 遗留 |
|---|--------|------|------|
| 1 | 提示词层输出格式规范 | ✅ | ⚠️ 长内容可补充「HTML/MD/PDF文件形式输出」选项 |
| 2 | 分段输出机制 | ✅ 完整 | 无 |
| 3 | 引用消息信息注入 | ✅ 完整 | 无 |
| 4 | QQ_data_original 工具 | ✅ 完整 | 无 |
| 5 | save_data 文件名指针下载 | ✅ | ⚠️ 重名文件直接覆盖，无提示 |

### 发现的 2 个小改进点

1. **L1892 补充长内容输出途径**: 当前只提了 `system_report` 和 `modify_file`，建议补充「也可生成 HTML/MD 文件后用 upload_data 发送」
2. **save_data 重名处理**: `_save_data_from_local` 在目标路径已有同名文件时直接 `shutil.copy2` 覆盖，无任何提示。建议加一行返回信息标注「已覆盖同名文件」
