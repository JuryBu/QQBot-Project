# Plan_5 上下文压缩机制升级 — 讨论记录

> 状态：🟡 **讨论中（未定稿）**。讨论充分、方案定稿后再拆为 `Plan_5.md` + `Task_5.md`。
> 归属：升级路线 **step2**（顺序：git化 → 零风险清理 → **本轮压缩升级** → Plan_4功能 → main.py拆分）。
> 本轮目标：实现主人的新压缩/上下文机制设计，并顺带修复 CHECKPOINT 两个实锤 bug。
> 不挂 Plan_4（那是新功能体系），压缩属核心架构，独立成轮。
> 创建：2026-06（Opus 4.8 接班窗口，前辈为 4.6 Opus）

---

## 一、当前机制基线（接班核实代码所得，作为升级出发点）

### 1.1 三系统分立（checkpoint.py 文件头注释确认）
- **A = `req.contexts`**：AstrBot 框架原生对话历史，**不碰**
- **B = `messages.db`**：persistence 插件全量 QQ 流水持久化，永不压缩，**不碰**
- **C = T 文件**：flashlite 自管，**实际发给 LLM 的上下文**，会压缩滚动

### 1.2 T 文件结构（checkpoint.py 核实）
- 路径：`QQ_data/checkpoints/{window_key}.json`；`window_key` = `GroupMessage:群号` / `FriendMessage:QQ号`（文件名 `:`→`_`）
- 结构：`T1{compressed_summary, token_count, compression_ratio, original_msg_count, compression_count, last_compress_time, compress_history}` + `messages[原文数组]` + `metadata`
- ⚠️ **T2/T3 仅是设计概念，代码未物理区分**：实际只有 T1（摘要）+ messages（全部原文），靠 `keep_recent` 近似「最近 N 条不压」。主人将在新方案里重新处理 T1/T2/T3。

### 1.3 三模型对 T 文件 / 上下文的读取（核实，主人确认此差异化设计优秀、要策略保留、可改进）
| 模型 | 来源 | 格式 | 用量 | 对请求体影响 |
|---|---|---|---|---|
| 主模型 | T 文件 | OpenAI 结构化（`build_llm_contexts`：T1摘要+全部原文） | **全量** | **替换** `req.contexts`（main.py L2979） |
| FlashLite | T 文件 | 纯文本（`build_flashlite_context`） | 最近 8000 token | 只读 |
| 工具模型 | T 文件 | 纯文本 | 最近 8000 token | 只读，**默认不带**，需 `inject_context=true` |
- 同一份 T 文件，读法不同：主模型吃全量结构化大餐，另两个吃最近一截纯文本快照。

### 1.4 压缩触发（`compress_if_needed`，checkpoint.py L570）
- **三重守卫**：① 总 token > `token_limit`(config) ② messages 数 > `keep_recent` ③ 距上次压缩 > `cooldown` ＋ ④ 并发互斥
- 阈值 token 按**主模型完整上下文**（`build_llm_contexts` 全量）算 → ✅ 主人确认：阈值用最长的主模型，设计正确
- ⚠️ **唯一挂载点 = `inject_flashlite_context`（主模型请求时，main.py L2961）** → ✅ 主人确认：这是 bug，不该只由主模型触发
- 压缩**动作**调 FlashLite 模型生成摘要（`flash_lite_caller=self._call_flash_lite`）

### 1.5 消息流转
进消息 → `route_message` 每条 `buffer_message` 实时进 T 文件（群 L685 / 私聊 L745）→ FlashLite 触发时 `flush_buffer` 落盘 + 读快照判断 → 主模型请求时 append + compress + `build_llm_contexts` 替换 `req.contexts`
- ⚠️ T 文件与 messages.db **平行独立**，T 文件**从不从 db 补历史** → 机器人停机无回补，**不存在「db–T 文件 gap 导致瞬间注入」**

### 1.6 三模型协作 + 群聊/私聊区别（主人确认无问题）
- 群聊：攒够 N 条 / 隔段时间（同步触发，省钱）或 被@/唤醒词（异步触发，立即）才过 FlashLite
- 私聊：每条都过 FlashLite（不攒批），标准宽松；判断不回时 `stop_event` 拦截
- 主模型：被 FlashLite/被@强制/后台 Task 完成 唤醒才出场，吃全量+带工具+人格
- 工具模型：主模型派活（task_set/browser_agent）才上

---

## 二、已查实的两个 bug（修复目标）

- 🔴 **Bug B — 私聊堆积不压缩**：`FriendMessage_<ADMIN_QQ>` T 文件 584 条、compression_count=0。根因 = 压缩检查只挂主模型请求；该私聊（管理员窗）FlashLite 大量判定不回复 → inject 不触发 → compress 永不跑。**【确认可修：压缩触发独立化】**
- 🔴 **Bug A — 群窗过度压缩 0.22%**：`GroupMessage_<GROUP_A>` 444 条 → T1 仅 356 字符。根因 = `max_output_tokens` 只压上限、不保下限；`actual_ratio < target_min` 时仅 `logger.warning` 无补救（checkpoint.py L715）；疑 14.6MB 图片 base64 致 token 估算失真。**【主人：和升级一起处理，之后说】**

---

## 三、本轮已确认的讨论共识（主人逐条拍板）

1. **压缩触发挂载是 bug** → 改为独立触发（消息追加后 / FlashLite 触发后检查），阈值仍用主模型（最长）
2. **三模型上下文读取差异化**（全量 vs 快照、结构化 vs 文本、只读 vs 替换）→ 优秀设计，**策略保留**，可改进
3. **T1/T2/T3 分层** → 主人将在新方案处理，改法他会说清楚
4. **请求体 / 本地文件分离** → 优秀，**后续要复用**，详情待主人说
5. **「瞬间注入」风险**（堆积不压缩 → 某次全量端上桌）→ 主人讲升级时自己注意；当前堆积 bug 可修
6. **真·瞬间注入（db–T gap）不存在**（代码无回补逻辑）→ 放心

---

## 四、新机制设计原文

主人口述的完整新机制（十大点 ①-⑩）见 `Plan_5_新机制设计_主人原文备份.md`（原文保真备份）。
三大支柱：CP→record（conversation-record-memory，蓝本 = `C:/Users/<user>/.gemini/antigravity/mcp-memory-store`）+ BPC 背景预压缩 + 多人并发集体回复。
调研报告见 `_接班理解/Plan5_调研/`（00 综合与 54 条追问清单 + 01~06 分维度）。

## 五、已确认设计决策（讨论中持续更新，截至 2026-06 接班窗口）

> 标 ✓ 为主人已拍板；标 ⏳ 为待定/待找茬。

### record 结构与粒度
- ✓ T 文件升级为 **record 部分 + 原文部分**；record 正文=**纯文字描述**，多模态存**地址指针**（Sandbox 路径/URL/按需临时文件），hit 命中时按指针召回原件（类比 MCP 读 conversation 原文）
- ✓ 三级粒度：**step**（单条消息/单步工具；assistant 工具调用 + 对应 tool 结果**合成一个 step**，防 tool_call_id 配对断裂）< **轮 round**（两次「模型首条回复」之间）< **轮次 round-group**（模型按语义聚合）
- ✓ 轮边界 `first_reply` = **第 1 条 assistant**（含只有 tool_calls、无文本那条）
- ✓ 轮上限 = **step 数 / token 双重机制**（谁先到谁切轮，防 step 少但单条巨文冲爆 1M），均面板可调；token 口径与 BPC/硬压缩**统一一套算法**
- ✓ 末轮缺尾（assistant 补录滞后）→ 用并发机制⑩「生成期间实时记录状态」解决
- ✓ round-group 聚合 = **模型自己按语义聚合**（用 mcp 机制，规范 3-15 轮/组）；增量场景靠 mcp 的 **Local Compose 边界**（`selectLocalComposeBoundary`：模型决定新轮接旧 Phase 还是开新 Phase，末尾开放则回滚重写）
- ✓ δ（压缩处理范围余量）= 按 **token/字**，对齐到完整轮不切半轮（对应 mcp `createRecordChunks` 字符预算切批）

### 分级读取与 hit
- ✓ 分级（brief/summary/full）作用在**轮次级**（不是 step）：**brief = 轮次标题**（不单独生成）/ **summary = 单独存一份** / **full = 完整 record 信息**；多模态对应 brief=占位、summary=文字描述、full=指针召回原件
- ✓ **hit round**：模型查到需要的轮 → 后台打标记 → 提优先级。**带时间衰减**（LRU，半衰期面板可调）+ **升档封顶到 full** + **区分命中类型**（查原文命中权重 > 查 record 命中）
- ⏳ **分级阈值**（过老 × 命中率 → 三档的映射：打分公式 vs 硬分段）= 主人原创、无 mcp 参考 → **重点找茬项**

### 原文保留 / 增量压缩 / 迁移
- ✓ 原文保留量 = 现有 **keep_recent 升级版**（保留最近 **N 轮** + token 双重，替代「最近 N 条」）
- ✓ 增量压缩借 mcp 三大件：`lastUpdatedRound` 增量恢复锚点 + Local Compose 本地合成（模型只出「变化的肚子+尾巴」，代码拼装稳定区+重写区+重编号）+ 写入门禁/候选隔离（失败绝不覆盖旧文件，存 temp）
- ✓ 旧 T 文件（v1）：**直接清空** + 保留 `messages.db` 按需 rebuild；并**兼作新 record 生成质量的测试集**

### BPC 背景预压缩
- ✓ 双阈值：硬压缩（~90%，阻塞保底）+ BPC 预压缩（低阈值，后台无感）
- ✓ 流程：触发→快照（只记边界 step 号）→后台压缩→出结果后 **1+Δ steps 内无感替换**
- ✓ 无感替换用**乐观锁**（不全程上锁；替换瞬间对比快照边界，期间有新消息就把新消息接到 BPC 结果后再换；复用现有 checkpoint.py L789 合并式 save 模式）
- ✓ 撞硬压缩 → **整个抛弃 BPC 候选**，走同步硬压缩
- ✓ 循环压缩三级降级：①降原文保留量（不足一轮降到 75%，留真空但能发话）②降 record 读取粗略度 ③BPC 暂停 / 硬阈值则中止该窗对话并通知管理员 <ADMIN_QQ>
- ✓ 前端话术：说话中撞压缩→「大脑过载了待会回你」+ 进集体回复队列；idle→静默压缩

### 并发集体回复（机制⑩）
- ✓ 主模型生成期间加**窗口级请求锁 + 记录状态**（不与框架 session_lock 死锁，插件层只做记录+决策）
- ✓ 结束后若又被触发 → 对期间所有「提到自己/需回复」消息**集体回复**；放弃「一次必回一条」：单条仍引用回复，多条集体回复可 @

### 多模态（⑦）
- ✓ 当前消息的图靠 AstrBot 框架喂主模型（已有效，非 bug）；历史/转发的图靠 record（文字描述 + 指针召回）
- ✓ **FlashLite 判定层要能感知图**（纠正现状：纯图消息判定层失明、易判「不回」）
- ⏳ 视频降帧/Office 解析/语音转写/媒体保留策略等范围 → 待细化（追问清单多模态组 11 条）

### 配置体系（零号任务）
- ✓ 🔴 配置断链已石锤：面板 FlashLite 参数写 `plugins/.../config.json`，但 main.py 读 `data/config/astrbot_plugin_flashlite_config.json` → 断链无效；`cmd_config.json` 部分（API Key/主模型/获取模型列表/capabilities）**有效**
- ✓ 修复 = 面板指向 main.py 真读的文件（验证框架格式兼容 + 重启是否覆盖）
- ✓ 本次升级**所有新参数必须面板可调**（⑤）

### 数据字段补全
- ✓ T 文件要补**结构化字段**：`sender{qq,name,is_bot}` / `message_id` / **毫秒时间戳**（现状：时间戳只到秒、QQ 号没进 T 文件且 meta.sender_qq 是死字段、message_id 没存；messages.db 均有，从 event/db 补）

### 中断/瞬间注入（⑧）
- ✓ 注入前去重（相同内容不重复注入）+ 触发阈值进压缩态 + 中断后加「系统睡了一会断片了」说明助模型理解

## 六、bug 审查关键修正（2026-06，详见 `_接班理解/体系bug审查/` 40 个 bug）

- 🔴 **配置零号任务升级为「系统工程」**：配置断链是体系性三处同构——① FlashLite 整块（面板写 config.json vs 运行时读 schema 注入的 *_config.json，键集合仅 2 重合）② 主模型选型（面板写 model_config.model vs 适配器读 provider 顶层 model → 实跑 gemini-2.5-flash 非面板显示的 gemini-3-flash-preview）③ Sandbox 工具路径分裂（子代理从不存在目录加载工具，静默跳过→search/web_fetch/generate_image/读QQ原文全失）。修复需：扩 _conf_schema.json 声明所有键 + 面板写入改 data/config 文件 + main.py 补 json.loads(dynamic_sampling/group_overrides) + 修 FlashLite model 硬编码 + 主模型同步写顶层 model。放大器：框架 check_config_integrity 会删 schema 未声明的键。
- 🔴 **私聊不压缩真相修正**：配置断链导致 checkpoint 实跑硬编码默认 **50000**（非面板显示的 10000）→ 私聊 32565 token 没到 50000 线，不压缩是**双因素**（阈值未生效 + 主模型不常被唤醒导致 compress 入口不触发），**非单纯触发逻辑 bug**。群窗 0.22% 仍是 maxOutputTokens 只压上限的质量 bug（与配置无关）。
- 🔴 **现状对账以「注入文件实际值 + cmd_config 顶层 model」为准**，所有 FlashLite 面板显示值与主模型选型不可信。
- ✅ **版本一致性确认**：start_bosslady.bat 加载 AstrBot/data/plugins/astrbot_plugin_flashlite/ 这份代码；老板娘当前**未运行**（改代码无线上影响）；cost_logs 全 flashlite 是历史日志/主模型钩子问题，非版本不一致。
- **一行级必崩修复（放 step1 顺手做）**：①存储费记账 cost_tracker.py:226 `_get_pricing`→`_resolve_pricing` + main.py self._model 未定义 ②子代理工具路径上溯 4 层 ③FlashLite model 改读 self._cfg。
- ⏳ **用户身份唯一锚定问题（主人新提）**：knowledge 用户卡片/memory 工作区/sync_nicknames 对同一 QQ 用户跨群昵称(nickname/card)不同、QQ 号锚定可能不准（分裂/串号）→ 子代理调研中，结论并入 record `sender{qq,name,is_bot}` 设计。

## BPC 组本轮确认（完整）
- ✅ 双阈值用 **context window 百分比**（硬 ~85-90% / BPC ~60-65%，面板可调）
- ✅ Δ 默认 **2**（即 1+2 steps），面板可调，step = QQ 消息粒度
- ✅ 撞硬压缩时主模型正在生成 → 等当前这条吐完 → 发「大脑过载待会回你」→ 压缩 → 剩余进集体回复队列；已发短句不撤
- ✅ 降级①：降原文保留量（75% 按 token 或条数皆可），留真空但能发话；真空期靠 record+缩水原文+当前消息应答 + 「系统睡了一会断片了」说明圆场
- ✅ 降级②：全局降 record 读取粗略度一档（summary→brief）
- ✅ 降级③ 分级递进（关键时序）：BPC 阈值低先循环触发 → **先暂停 BPC**（仍有硬压缩兜底）；连硬压缩也循环失败、彻底救不了 → **中止该窗服务**（暂停响应等人工介入）+ 通知管理员
- ✅ 管理员通知：面板可填（默认 <ADMIN_QQ>），私聊管理员 + 日志双保险
- ✅ 快照：只记边界 step 号（喂乐观锁对比）

## record 用户身份锚定（确认）
- 🔴 现状致命断链：QQ 号在 route_message 入口(main.py:674-675)就丢、buffer_message 不传 meta → T 文件 meta.sender_qq 永空 → FlashLite「看昵称猜号」。铁证：脏卡片 ξ(2680872177)、大量「(未知)」、串号(抽子内容错挂柚子卡)、card 跨群覆盖全局昵称。干净源头=persistence qq_messages 的 sender_id/sender_name
- ✅ record `sender={qq(唯一主键,协议结构化字段取,不从文本解析), name(per-window显示名:群用card/私聊用nickname,绝不提升全局), is_bot(替代name判断,bot QQ从配置默认<BOT_QQ>)}`
- ✅ knowledge 画像：全局按 QQ 合并事实 + 新增 display_names{window_key:显示名}(单值覆盖,不存历史)；顶层 nickname 降级
- ✅ 拿不到 QQ 号 → 宁漏不串(qq空跳过建画像,不占位)
- ✅ 最小修复(优先)：route_message 补取 sender.user_id + buffer_message 补 meta
- ✅ **同人多号归并(person_id)要做但极谨慎**：柚子(820001643,群友)≠紬子(<ADMIN_QQ>,主人自己某群昵称)谐音不同人 → 合并**只接受人工/管理员指定,系统绝不自动靠昵称谐音猜**
- ✅ record 不混进 persistence 表(sender 从 event 实时取,可对账 qq_messages)
- 清理：三处 profile_update 解析统一+删旧格式兜底；update_user_profile 非数字 qq 拒绝建卡；现有 cache 脏 key 迁移清洗

## 并发集体回复（机制⑩）本轮确认
- ✅ 目标(主人定)：窗口级**阻塞-记录** + 解除「一次只回一条/单人」限制；单人→引用回复那条明确对谁，多人→@或称呼ID集体回复
- ✅ 实现路线**由助手判断**(主人不限定技术)：保留框架能力优先——先试「扩展 follow_up 不魔改」(加同窗口判定分支)，不行用伪造 event 重入 pipeline；不用丢工具循环/分段的纯直连
- ✅ 判定：忙碌期间**全缓冲、不逐条调 FlashLite**，解锁后**一次性批量**过 FlashLite 判哪些该回 → 一次集体回复；标准=@+唤醒词+FlashLite 语义
- ✅ 结束点 = **分段逐条全部发完**(用户视角真说完)，期间来的消息算下一轮
- 助手直接定的技术项：窗口键用插件自有 GroupMessage:gid/FriendMessage:qq(不依赖框架 unique_session)；接管期禁用框架 follow-up 防双触发；锁超时默认~120s+面板可调(主模型卡死保护)；引用用 NapCat reply 段(msg_id 从 adapter 取)
- ⏳ 待主人定的产品点：@规则(@所有 vs @最相关几个+上限)、背景消息(非需回复的普通消息是否进上下文做背景)

## 七b、SO / 多模态 / 横切 / 全局 本轮确认（2026-06）

### 结构化输出 SO
- ✅ 主模型输出改 **JSON 数组**，每段 `{text, at:[qq] 或 reply_to:msgid}`，模型自带分段+自标 @/引用（取代代码事后切分）。@ 克制：**昵称称呼为主，要强调才 @，别乱 @**
- ✅ 跨 provider：gemini/openai/anthropic 统一「prompt 约束 JSON + 健壮容错解析」，不绑某家 function-calling
- ✅ 主模型 provider **可切换**（按 API Key 实时获取可用模型，面板已如此），输出层 provider-agnostic

### 多模态
- ✅ record 历史图：存**文字指针+描述**，不重输原图；模型要看自己 function call 调，hit 才召回；**即便 hit 也不塞 base64，summary 封顶**
- ✅ summary 谁生成：**FlashLite 对「最近一轮新触发」的图直接吃原图（判定 + 顺手生成 summary 存 record），历史图用已存 summary**（不额外调模型）
- ✅ 原图留 Sandbox + LRU 容量/过期清理（面板可调）
- ✅ **视频/PDF 的 provider 差异（关键）**：仅 Gemini 原生支持 PDF/视频，非 Gemini 主模型不能原生喂 → 配**专用媒体模型（默认填 Gemini）**把视频/PDF 转成 summary+截图，主模型(任意 provider)只消费文字 summary+截图；视频降帧用**抽关键帧当多图**
- ✅ Office 文件：**不自动解析**，按需（模型下载到 sandbox 用工具看）。现状 PDF 可 view_file 看文本、Office 二进制看不了 → 实现时补「读 Office」工具或让模型 sandbox_exec 自读
- ✅ 媒体大小上限/数量：**保留现有默认上限（图≤10MB 等）+ 面板可调**（不动态按模型参数，默认值可在面板调整即可）
- ✅ 分辨率分级 summary=LOW / full=HIGH（配合专用媒体模型）
- ✅ 转发嵌套媒体只 brief 占位+指针不自动分析；语音本期转写进 record；集体回复多模态降 summary

### 横切
- ✅ **中断检测不靠消息间隔时间**（避免与"真没人说话"混淆），靠 **AstrBot 运行日志判断有无断点**（实现时确认日志可用性）；断片文案面板可配（固定随机池或可改）
- ✅ 阻塞话术真发 QQ 消息、随机文案池、防刷屏
- ✅ per-group 可调：record/BPC 阈值 + 采样 + 工具权限 + **中断/阻塞文案**；全局：BPC Δ / 三级降级 / 压缩模型 / 媒体处理参数 / 时间戳精度 等机制级
- 〔助手定·技术〕生效时机：尽量热加载（改 config 即时生效），做不到标"需重启"；配置存储=展开独立 schema 键（避 JSON 字符串没 json.loads 的坑）；前端本期继续原生 JS

### 全局
- ✅ record 压缩用 **FlashLite，不走 MCP 跨链路**
- ✅ 四系统不重叠：messages.db=全量存档 / T 文件 record=喂 LLM 上下文 / Knowledge=全局最近信息 / Memory=模型自写长期记忆（定位以既有 Plan 文件为准，定稿时核对）
- ✅ 轮定义统一：模型第一个 step 起=一轮，到下次第一个 step 前、未触发 step/token 上限的都算一轮；集体回复=正常一轮不特殊

## 八、待办

- ⏳ **分级阈值**（过老×命中率→brief/summary/full）留 adversarial 找茬
- ⏳ 实现时确认：CC-1 AstrBot 运行日志/断点可用性、MM-6 Office 读取工具
- ⏳ 开 **adversarial 找茬 workflow**（全部决策 + 配置断链修复方案 + 分级阈值）
- ⏳ 定稿 `Plan_5.md` + `Task_5.md`
