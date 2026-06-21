# Plan_5 — 老板娘对话机制大升级（总纲 · 定稿 v1）

> 状态：🟢 **设计定稿 v1**（5 根决策已拍板 + 8 条 critical 已转验收条款）。1 项待实测（当前图）。
> 升级路线：step2（git兜底→清理→**本轮**→Plan_4功能→main.py拆分）。
> 依据：`Plan_5_上下文压缩升级_讨论记录.md`(决策清单) + `Plan_5_新机制设计_主人原文备份.md`(原始设计) + `_接班理解/Plan5找茬/`(对抗评审61洞) + `_接班理解/Plan5_调研/`(现状) + `_接班理解/体系bug审查/`(40bug)。
> 蓝本：`C:/Users/<user>/.gemini/antigravity/mcp-memory-store`(conversation-record 子系统)。
> 前提：老板娘当前**未运行**，改码无线上影响；unique_session=false（群共享窗口）。

---

## 项目概述

把现有 CHECKPOINT 压缩，升级为一套更成熟、无感、可跨 provider 的对话机制。七块：
1. **record 机制**：CP → conversation-record-memory（分级+增量+hit）
2. **BPC 背景预压缩**：低阈值后台无感压缩，几乎不触发硬阻塞
3. **并发集体回复**：多人同时找老板娘不漏不挤，一次集体回
4. **结构化输出 SO**：模型吐 JSON 自带分段+标@/引用，跨 gemini/openai/anthropic
5. **多模态升级**：历史图文字+指针召回、专用媒体模型、判定层能感知图
6. **用户身份锚定修复**：QQ 号唯一主键，消灭串号/(未知)
7. **配置断链修复**（零号任务）：面板改的能真生效

---

## 整体架构（数据流）

```
QQ → NapCat(OneBot) → AstrBot(aiocqhttp「老板娘」)
  → route_message(每条 buffer 进 T 文件, 打 round_id/step_id)
  → 5 路触发(FlashLite判断/@/唤醒词/私聊/Task完成) → 窗口锁(生成期缓冲)
  → on_llm_request: 组请求体 = record(分级brief/summary/full) + 原文(最近N轮) + 动态注入
  → 主模型(provider可切换) 吐 JSON 分段 → 集体回复(单条引用/多条@) → QQ
  后台并行: BPC 预压缩 / 媒体专用模型产 summary / Knowledge·Memory 维护
```
三系统分立不变：A=req.contexts(框架) / B=messages.db(全量存档,7热/30冷/90归档删) / C=T文件(record+原文,实际喂LLM)。

---

## 一、record 机制（CP → conversation-record）

- **T 文件 = record 部分 + 原文部分**；record 正文纯文字+多模态地址指针。**record 独立成文件**（仿 mcp `{window}.record.md`），与高频追加的 messages 原文 JSON 物理分离，避免整份 dump 拖垮 BPC。
- **三级粒度**：step（单条消息/单步工具；assistant.tool_calls+对应 tool 结果合成一 step）< 轮 round（两次「模型首条回复」之间；first_reply=第1条 assistant 含纯工具调用那条）< 轮次 round-group（模型按语义聚合，**3-15 轮/组**，借 mcp Phase；13-18 数字漂移已统一取小）。
- **轮上限 = step 数 / token 双重**（谁先到谁切轮，防单条巨文冲爆 1M）。**token 上限只在 step 边界生效**（step 原子优先；超大 step 单独成轮+内部 tool 结果截断）。
- **硬轮号（锚点先行，critical-2）**：`round_id`/`step_id` 持久化到**每条 message**，代码切分/快照/save/定位**一律用硬轮号、废弃数组下标**；record 文本里模型写的软轮号仅供阅读不参与定位。
- **增量压缩**：借 mcp `lastUpdatedRound` 增量恢复 + Local Compose 本地合成（模型只出变化的肚子+尾巴，代码拼装稳定区+重写区+重编号）+ **写入门禁/候选隔离**（失败绝不覆盖旧文件）。**校验/repair/隔离必须连同生成一起移植，且规则集为群聊重写**（开放信号词换「@未回复/话题未结/待确认」）。
- **分级读取** brief/summary/full 作用在**轮次级**：brief=标题(不单独生成)/summary=**预生成单独存**/full=完整 record 信息。按「过老 × 命中率」定档，**必须加滞回**（升/降档阈值留 gap 或台阶函数，防横跳）。
- **hit**：模型查到某轮 → 后台标记提优先级；**时间衰减**(LRU)+**升档封顶 summary**+**区分命中类型**(查原文>查record)。
- **分级↔压缩接力**：预算超→先分级降档读(廉价)→降到底仍超→增量压缩物理合并最老 round-group→仍超→BPC 三级降级。

## 二、BPC 背景预压缩

- **双阈值（context window 百分比）**：硬压缩 ~85-90%（阻塞保底）+ BPC ~60-65%（后台无感）。**分母 context window 来自 model_id→window_size 映射表**（面板可填可覆盖，缺失回退保守 32k；内部以绝对 token 算）。
- **流程**：触发→快照(只记边界 round_id)→后台压缩→出结果后 **1+Δ(默认2) steps 内乐观锁替换**（替换瞬间对比快照边界，期间有新消息则合并后再换；Δ 面板可调）。
- **BPC 与硬压缩独立互斥标记 + 独立 cooldown**（critical-5：绝不共用 `_compressing`，否则 BPC 堵死保底硬压缩）；硬压缩对 BPC **抢占式 cancel**。
- **撞硬压缩 → 整个抛弃 BPC 候选**，走同步硬压缩。
- **三级降级 + 滞回/恢复条件**：①降原文保留量(对齐 step 边界绝不切半 step，避免 provider 400)②全局降 record 读取粗略度一档③BPC 暂停（仍有硬压缩兜底）；连硬压缩也循环失败→中止该窗服务+通知管理员。**单条超限消息(巨图/巨转发)落保留区压不掉时不得误判「循环失败」中止整窗**。
- **前端话术**：说话中撞压缩→「大脑过载待会回你」+进集体回复队列；idle→静默。

## 三、并发集体回复 + 结构化输出 SO

- **窗口锁**：window_key=`GroupMessage:群号`(unique_session=false 已确认)/`FriendMessage:QQ`；主模型生成期**阻塞-记录**（插件状态机，不与框架 session_lock 死锁）。锁覆盖**到分段全部发完**（RespondStage 逐段 sleep 期间也算忙碌）。
- **5 路触发生成期缓冲**：FlashLite判断/@/唤醒词/私聊/Task完成 都汇到 `_notify_main_model`；忙碌期**全缓冲不逐条调 FlashLite**，解锁后**一次性批量判**哪些该回。
- **集体回复触发（critical-7/8）**：**插件自建直调主模型**（仿 `_wake_main_for_task`，放弃借框架 follow_up——框架无重入入口、follow_up 仅 per-sender 禁不掉）；端到端时序在 Task_5 给出；验证插件接管后同 sender 连发的去重。
- **提示词引导回谁**：集体回复时上下文**明确标「本次需回复：X、Y、Z 的哪几条」**（否则模型不知要回谁）。
- **SO 结构化输出**：主模型吐 **JSON 数组** `[{text, at:[qq] 或 reply_to:msgid}]`，自带分段+标@/引用。**必须绕开框架 RespondStage 二次加工**（merge_threshold合并/max_segments砍段/header 只挂首段）——倾向 per-group 关闭框架 segmented_reply、插件自行逐段发，让 per-段 @/引用真正生效。跨 provider 用「prompt 约束 JSON + 健壮容错解析」；msgid/qq 来源可靠性需保证(buffer 补 meta)。

## 四、多模态升级

- **自动注入只给文字**（summary/指针，不塞 base64，省 token）；**召回靠模型主动 function call 翻**（conversation_read_original 类）messages.db(round_id 捞原文)/images(捞原图)，**受老化约束**（7热/30冷/90归档删 + 图片 500MB LRU）；翻不到（已老化/LRU清）→明确降级「该图/原文已过期清理」，**禁止编造**。
- **FlashLite 判定层吃 summary 级文字描述**（纠正纯图失明）；**summary 由专用媒体模型异步产出**回填 record（判定与 summary 生成解耦，critical-3：不破坏 FlashLite 的 KVCache）。
- **专用媒体模型（默认 Gemini）**：非 Gemini 主模型不能原生吃 PDF/视频 → 专用模型把视频(抽关键帧)/PDF 转 summary+截图，主模型(任意 provider)只消费文字+截图。
- **hit 给原图（critical-4）**：给多模态**单独 token 预算**，hit 且预算够时注入 **1 张 LOW 原图**（张数/预算挂 MM-7 面板可调）；预算不够仍只给 summary。
- **媒体大小上限统一**：现 4 处写死不一(下载5/分析10/视频下载20/摘要50MB)→统一成每类型一个值（下载上限≥分析上限）+ 面板可调（保留现默认量级，非动态按模型）。
- Office 不自动解析（按需 sandbox 工具看，补「读 Office」能力）；语音本期转写进 record；集体回复多模态降 summary。

## 五、用户身份锚定

- **`sender = {qq(唯一主键,协议结构化字段取,不从文本解析), name(per-window显示名:群card/私聊nickname,绝不提升全局), is_bot(替代name判断,bot QQ配置默认<BOT_QQ>)}`**。
- **最小修复（critical 优先）**：`route_message` 补取 `sender.user_id` + `buffer_message` 补 meta（覆盖群/私聊；转发内嵌/quoted/notice 的 sender 缺失另行补）。
- knowledge 画像：**全局按 QQ 合并事实** + 新增 `display_names{window_key:名}`（单值覆盖）；顶层 nickname 降级；`sync_nicknames/get_user_cards` 同步改读 display_names。
- **删昵称反查合并分支**（knowledge.py L279-297，自动串号根源）；**删旧格式 profile_update 兼容分支**（L1023 等，脏卡 ξ(2680872177) 复发源）。
- **person_id 同人归并**：只接受**人工/管理员指定**，系统绝不自动靠昵称谐音猜（柚子820001643≠紬子<ADMIN_QQ>）。
- 拿不到 QQ → 宁漏不串（qq 空跳过建画像，不占位）。

## 六、配置断链修复（零号任务）

- **三处断链**：FlashLite 整块(面板写 config.json vs 运行时读 schema 注入 *_config.json)、主模型选型(model_config.model vs 顶层 model)、Sandbox 工具路径分裂。
- **修复**：扩 `_conf_schema.json` 声明所有键（含 Plan_5 全部新参数，否则框架 check_config_integrity 删键）+ 面板写入改 `data/config` 文件 + main.py 对动态键补 `json.loads` + 修硬编码（FlashLite model / `self._model` 未定义 / cost_tracker `_get_pricing` 笔误 / 子代理工具路径）+ 主模型同步写顶层 model。
- **动态子键(群号/自定义模型名) string 打包；固定结构(dynamic_sampling/thinking_config) object 展开**。
- **热加载**：文案/阈值走 /reload 端点热生效；模型名/API Key/Sandbox/KVCache 走重启。

---

## 8 条 Critical 验收条款（定稿核心，每条须可验收）

1. **dangling tool_calls 防御**：组请求体前扫 messages，末尾未配对的 assistant.tool_calls → 补占位 tool result 或回滚整条；独立于机制⑩，覆盖崩溃重启路径。验收：构造缺尾 T 文件→组请求体不被 provider 400。
2. **锚点先行**：round_id/step_id + 划轮状态持久化每条 message，崩溃重启可恢复；所有切分/快照/save 基于硬轮号。验收：重启后轮号连续、增量不漏压/重复压。
3. **FlashLite 判定与 summary 解耦**：判定层只喂文字/占位，summary 交专用媒体模型异步产出；FlashLite payload 结构不被图破坏。验收：发图判定不破坏 KVCache 命中、不暴涨 token。
4. **hit 原图/原文档位与预算**：多模态单独预算、hit 注入 1 张 LOW 原图；文字 hit 从 messages.db 按 round_id 召回；翻不到降级不编造。验收：hit 历史图能看到原图、过期图给明确提示。
5. **BPC 与硬压缩独立互斥**：独立标记+cooldown，硬压缩抢占 BPC。验收：BPC 频繁触发时硬压缩仍能触发、token 不失控。
6. **context window 数据来源**：model_id→window 映射表+缺失默认 32k；阈值内部绝对 token。验收：切换 1M↔128k 模型不爆窗。
7. **集体回复端到端时序**：插件自建直调主模型的完整时序（缓冲→批量判→构造上下文→直调→分段发→回写 record）。验收：A 回复期间 B/C @，结束后一次集体回 B/C。
8. **集体回复纯插件通道 + 去重**：不依赖框架 follow_up；验证同 sender 连发不重复回复。验收：A 连发多条不触发多次重复回复。

---

## Stage 落地顺序

| Stage | 内容 | 性质 |
|---|---|---|
| **S0** | stage0 整理 + git 兜底 + GitHub push（见 `_接班理解/stage0_整理准备.md`） | 卫生工程 |
| **S1** | 一行级必崩修复(self._model/cost_tracker 笔误/FlashLite model 硬编码/子代理路径) + 身份锚定最小修复 + 删昵称反查 | 独立可验证、低风险高收益 |
| **S2** | 配置断链修复（扩 schema + 路径 + json.loads + 热加载） | 可单测面板↔运行时往返 |
| **S3** | 锚点先行（round_id/step_id 持久化 + 划轮状态机 + 崩溃恢复 + dangling 防御） | record 硬地基 |
| **S4** | record 机制（独立文件 + 增量压缩移植 + 分级读取 + hit） | 改动最大 |
| **S5** | BPC 背景预压缩（双阈值 + 乐观锁替换 + 三级降级） | — |
| **S6** | 并发集体回复 + SO（窗口锁 + 批量判 + 插件直调 + JSON 输出） | — |
| **S7** | 多模态升级（_extract_multimodal + 专用媒体模型 + 召回对接老化） | — |
| **S8** | Plan_4 新功能（F3群管→F2 MCP→SKILL→F4搜图→F1向量） | 后续轮 |

> 迁移顺序硬约束：**先 S1 身份锚定修复，再清空 T 文件 rebuild**（否则 rebuild 出的 record 身份照样脏）。main.py 拆分（原 step3）建议提前到改动大的 S4 前，对触碰的核心路径先抽模块。

---

## 待实测 / 遗留

- ✅ **当前图实测已确认**（2026-06）：主模型能准确描述当前图真实内容（分两条消息发也能）→ 当前图**没丢**、框架喂图正常 → 多模态改造 = **纯补「历史图召回」能力**（非修 bug）
- token 单一来源：写死 `estimate_tokens()`（含图片计法，顺带修 Bug A 图片 token 失真）+ `get_context_window(model)`
- main.py 5997 行：每 Stage 补 smoke test；配置读取/并发锁/工具加载先抽模块再改

## 关联文档
- 执行清单：`Task_5.md`（+ 参数全覆盖表、媒体上限统一表）
- 决策全程：`Plan_5_上下文压缩升级_讨论记录.md`
- 对抗评审：`_接班理解/Plan5找茬/00_综合风险清单.md`
- stage0：`_接班理解/stage0_整理准备.md`
