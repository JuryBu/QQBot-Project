# Task_5.md — Plan_5 执行清单

> 依据：`Plan_5.md`（总纲+8条critical条款）。每 Stage 独立可验证、补 smoke test。
> 8 条 critical 映射：C1/C2→S3，C3/C4→S7，C5/C6→S5，C7/C8→S6。
> 硬约束：S1 身份锚定修复**先于** T 文件清空 rebuild；复杂 Stage(S3/S4/S5/S6)动手前做定向找茬。

---

## 参数全覆盖表（解决 object-string / 删键 / 面板可调 三坑）

> 格式：参数 | schema键 | 类型 | 默认 | 范围 | 作用域 | 加载 | 控件。**所有键必须先进 `_conf_schema.json`**（否则框架 check_config_integrity 删键）。动态子键(群号/模型名)用 string 打包，固定结构用 object。代表性清单如下，S2 落地时按此格式补全 100%。

### record 组
| 参数 | 键 | 类型 | 默认 | 作用域 | 加载 |
|---|---|---|---|---|---|
| 轮 step 上限 | round_max_steps | int | 30 | per-group | 热 |
| 轮 token 上限 | round_max_tokens | int | 8000 | per-group | 热 |
| 轮次聚合目标轮数 | rg_target_rounds | int | 8(范围3-15) | 全局 | 热 |
| δ 连贯余量(token) | compress_delta_tokens | int | 800 | 全局 | 热 |
| 分级·full→summary 轮龄 | tier_summary_age | int | 20 | 全局 | 热 |
| 分级·summary→brief 轮龄 | tier_brief_age | int | 60 | 全局 | 热 |
| 分级滞回 gap | tier_hysteresis | int | 5 | 全局 | 热 |
| hit 衰减半衰期(轮) | hit_halflife | int | 30 | 全局 | 热 |
| 原文保留轮数 | keep_recent_rounds | int | 15 | per-group | 热 |

### BPC 组
| 硬压缩阈值(%窗口) | hard_compress_pct | float | 0.88 | per-group | 热 |
| BPC 预压缩阈值(%) | bpc_pct | float | 0.62 | per-group | 热 |
| BPC 替换 Δ 步 | bpc_delta_steps | int | 2 | 全局 | 热 |
| 降级①原文保留% | degrade_keep_pct | float | 0.75 | 全局 | 热 |
| 降级触发/恢复阈值 | degrade_trigger/recover | float | 0.65/0.50 | 全局 | 热 |
| model→窗口映射表 | model_window_map | string(JSON) | {}+默认32k | 全局 | 热 |

### 并发 / SO 组
| 窗口锁超时(秒) | lock_timeout_s | int | 120 | 全局 | 热 |
| 集体回复@上限 | collective_at_max | int | 5 | per-group | 热 |
| SO 插件自分段开关 | plugin_segment | bool | true | per-group | 热 |

### 多模态组
| 专用媒体模型 | media_model | string | gemini-2.5-flash | 全局 | 冷 |
| hit 原图预算(token) | hit_image_budget | int | 1500 | 全局 | 热 |
| hit 原图张数 | hit_image_count | int | 1 | 全局 | 热 |
| 各类型大小上限 | media_size_limits | object | 见媒体表 | 全局 | 热 |
| 分辨率(summary/full) | media_res | object | LOW/HIGH | 全局 | 热 |

### 身份 / 横切组
| bot QQ | bot_qq | string | <BOT_QQ> | 全局 | 冷 |
| 管理员 QQ | admin_qq | string | <ADMIN_QQ> | 全局 | 热 |
| 中断说明文案池 | interrupt_msgs | string(JSON,per群) | [...] | per-group | 热 |
| 阻塞话术池 | busy_msgs | string(JSON,per群) | [...] | per-group | 热 |
| 中断检测方式 | interrupt_detect | enum | astrbot_log | 全局 | 冷 |

## 媒体上限统一表（消除现 4 处写死不一）
| 类型 | 下载上限 | 分析上限 | 分辨率 | 备注 |
|---|---|---|---|---|
| 图片 | 10MB | 10MB | summary=LOW/full=HIGH | 下载≥分析 |
| 视频 | 50MB | 抽关键帧 | LOW | 非Gemini走专用模型抽帧 |
| PDF | 20MB | 20MB | — | 仅Gemini原生,他家转截图 |
| Office | 20MB | 按需(不自动) | — | sandbox工具读 |
| 语音 | 10MB | 转写 | — | 本期做 |
> 全部面板可调,保留现默认量级。

---

## Stage 执行清单

### S0 — 文件整理 + git 兜底 + GitHub push
- 目标：干净地基 + 版本控制 + 远程备份
- 依据：`_接班理解/stage0_整理准备.md`
- 执行：[ ].gitignore [ ]脱敏(隐私脱/人格留) [ ]git init+首提交 [ ]顶层归档清理(QQ.exe/zip/旧NapCat删,AstrBot_old/根data/老脚本/venv归档) [ ]孤儿配置删(context_enhancer/group_chat/heartflow) [ ]QQBotPlan分层(Plan_1~5/+INDEX+辅助/+对话快照/)+引用更新 [ ]push 到 JuryBu/QQBot-Project
- 验收：clone 后能跑;无明文密钥入库;人格 prompt 在库供 RP 参考;Plan 文档无死链

### S1 — 一行级必崩修复 + 身份锚定最小修复 + 删昵称反查
- 目标：低风险高收益的独立修复，先把现成 bug 清掉
- 执行：[ ]cost_tracker `_get_pricing`→`_resolve_pricing` [ ]新增 `self._model=_cfg('model',FLASH_LITE_MODEL)`+URL改用 [ ]子代理工具路径上溯4层 [ ]route_message 补 `sender.user_id`+buffer_message 补 meta(qq/name/is_bot) [ ]删 knowledge.py 昵称反查合并(L279-297) [ ]删旧格式 profile_update 兼容分支
- 验收：存储费记得上;FlashLite model 面板可切;子代理工具可用;新消息 T 文件 meta 有真 QQ 号;不再产生 (未知)/串号脏卡
- smoke：发几条群消息看 T 文件 meta.sender_qq 有值

### S2 — 配置断链修复（详见 `S2_实现方案.md`）
- 目标：面板改的能真生效（零号任务）
- 范围切分：S2 只做「现存断链键(model/tool_model/image_model/checkpoint_*/cost_tracker) + 基础设施(`_cfg_json`/`SANDBOX_ROOT`/BOM/reload)」；**Plan_5 新机制参数随 S3-S7 各自实现时声明**(不在 S2 堆死键，声明跟随消费方)
- 执行：[ ]主模型面板写顶层 model+custom_extra_body+回显读顶层+cmd_config 对齐 `gemini-3-flash-preview`(主人定) [ ]扩 schema 批1(现存断链键，cost_tracker 拆 cost_usd_to_cny+cost_custom_pricing) [ ]面板 `FLASHLITE_CONFIG` 指向 A 文件 + 编码 utf-8-sig(BOM 对齐) [ ]封装 `_cfg_json` 助手修 dynamic_sampling(L149)/group_overrides(L668/812) 静默失效 [ ]`SANDBOX_ROOT` 常量统一6处+cost_logs 迁移搬历史 json [ ]record() model 硬编码 L1722→self._model [ ]`/reload` 端点(触发通道待验证 AstrBot 机制)
- storage_policy：保留走 B 文件特例，不动 persistence
- 验收：面板改 checkpoint/采样/群覆盖/主模型 → 重启后 diff A 文件值生效且无键被框架删(log 无「将从当前配置中删除」)
- smoke：面板改一参数→读 A 文件确认写入→重启确认未被删 + `_cfg` 读到新值
- 进度(2026-06)：组1-5 **代码完成 + adversarial 复查 pass + py_compile 全过**；cmd_config 顶层 model 已切 `gemini-3-flash-preview`；cost_logs 已迁 `SANDBOX_ROOT/cost_logs`。**待**：运行验证(重启+面板往返)、组6 reload(调研 AstrBot 机制)、sp 脏数据(gemini_proxy)清理

### S3 — 锚点先行（C1 + C2）⚠️动手前定向找茬
- 目标：record/BPC 的硬地基——每条消息有真轮号、崩溃可恢复
- 执行：[ ]message 加 round_id/step_id 字段+毫秒时间戳 [ ]划轮状态机(first_reply锚点,step/token双上限,token只在step边界切) [ ]划轮状态持久化落盘+崩溃恢复 [ ]**dangling tool_calls 防御**(组请求体前扫描,缺尾补占位/回滚) [ ]所有切分/快照/save 改基于硬轮号废弃数组下标
- 验收(C1)：构造缺尾T文件→组请求体不被provider 400;(C2)重启后轮号连续、增量不漏压/重复压
- smoke：模拟崩溃重启,校验轮号与 lastUpdatedRound 交叉一致

### S4 — record 机制 ⚠️动手前定向找茬
- 目标：CP→conversation-record（独立文件+增量+分级+hit）
- 执行：[ ]record 独立文件(与messages原文JSON分离) [ ]增量压缩移植 mcp(lastUpdatedRound+Local Compose+写入门禁,校验规则群聊化重写) [ ]round-group 模型语义聚合(3-15轮) [ ]分级读取 brief/summary/full(过老×命中率+滞回,summary预生成单独存) [ ]hit 标记(衰减+封顶summary+区分命中类型) [ ]分级↔压缩接力链 [ ]旧T文件清空+从messages.db rebuild(S1身份修复后)
- 验收：长对话压缩率达标(20-40%)、分级不横跳、hit 升降正常、record 文件不随消息暴涨
- smoke：拿 messages.db 真实历史 rebuild record,人工核对质量

### S5 — BPC 背景预压缩（C5 + C6）⚠️动手前定向找茬
- 执行：[ ]双阈值(context window%,model_window_map映射表+缺失默认32k) [ ]快照记边界round_id [ ]后台压缩+1+Δ乐观锁替换 [ ]**BPC与硬压缩独立互斥标记+cooldown**(硬压缩抢占BPC) [ ]三级降级+滞回恢复 [ ]单条超限消息不误判中止整窗 [ ]前端话术
- 验收(C5)：BPC频繁触发时硬压缩仍能触发、token不失控;(C6)切换1M↔128k模型不爆窗
- smoke：模拟接近阈值,观察BPC后台压缩+无感替换不撕裂

### S6 — 并发集体回复 + SO（C7 + C8）⚠️动手前定向找茬
- 执行：[ ]窗口锁(group_id,unique_session=false)阻塞-记录,覆盖到分段发完 [ ]5路触发生成期缓冲 [ ]解锁后批量判+提示词引导回谁 [ ]**插件自建直调主模型**(仿_wake_main_for_task,端到端时序) [ ]SO JSON分段(at/reply_to)+跨provider容错解析 [ ]绕RespondStage二次加工(per-group关segmented_reply,插件自发) [ ]单条引用/多条@(上限5)
- 验收(C7)：A回复期B/C@→结束后一次集体回B/C;(C8)同sender连发不重复回复
- smoke：3人并发@,观察不漏不重不挤

### S7 — 多模态升级（C3 + C4）
- 执行：[ ]_extract_multimodal(替_extract_text,保全段类型+落盘指针) [ ]自动注入只文字+指针 [ ]召回 function call 翻messages.db/images(对接老化7/30/90+500MB,翻不到降级不编) [ ]**FlashLite判定吃summary文字**(判定与summary生成解耦) [ ]专用媒体模型(非Gemini转summary+截图,视频抽关键帧) [ ]**hit给1张LOW原图**(单独预算) [ ]媒体上限统一+面板可调 [ ]语音转写
- 验收(C3)：发图判定不破坏KVCache;(C4)hit历史图能看原图、过期图给提示
- smoke：发图→判定触发→历史hit召回原图

### S8 — Plan_4 新功能（后续轮，不在本 Plan_5 范围）
- F3群管理→F2 MCP/SKILL→2.5 SKILL导入→F4搜图→F1向量检索

---

## 待复核 / 小本本
- [ ] 13-18 vs 3-15 轮：定稿取 3-15（群聊话题切换频繁），主人若有偏好可调
- [ ] token 单一来源：实现 `estimate_tokens()`(含图片计法,顺带修Bug A图片token失真)+`get_context_window(model)`
- [ ] main.py 5997行：S3/S4 动大手术前，先把配置读取/并发锁/工具加载抽模块（对应原 step3 拆分提前）
- [ ] 转发内嵌/quoted/notice 的 sender 缺失补全（S1 最小修复之外的深度锚定）
- [ ] person_id 人工合并入口（命令 or 面板按钮）+ 存储位置，S4 时定
- [ ] 现存脏卡(ξ(2680872177)等)迁移清洗脚本
- [ ] 当前图实测 ✅ 已确认（多模态=补历史图能力）
- [ ] **S3-S7 各 Stage 执行隐含追加**（S2 范围切分的承接）：声明本 Stage 新参数进 `_conf_schema.json`（动态子键 string(JSON) 打包、固定结构 object 展开）+ 面板控件 + string 键走 `_cfg_json` 助手；归类见「参数全覆盖表」。否则面板可调但重启被框架 check_config_integrity 删键
- [ ] reload 触发通道：S2 实现时验证 AstrBot 插件配置 reload 机制，若机制重则 S2 先实现「重读配置方法」，完整热加载体验后续完善
- [ ] **send_image/send_file fallback else 路径瑕疵**（main.py 约 L4589/4627）：剥离前缀得 clean_path/clean_file 但 join 仍用原始 image_path/file_path——S1 前既有 bug，非 S2 范围，待修
- [ ] A 文件缺 model/tool_model 等新键 + B 文件残留旧 sync_interval：历史双写，S2 运行验证(重启框架 check_config_integrity 按新 schema 注入 A 文件)时确认消解
- [ ] sp 脏数据 `provider_perf_chat_completion='gemini_proxy'`（data_v4.db preferences）：指向不存在的 provider，每次会话打 warning，回落 gemini_pro 仍能用；运行验证前顺手清理消噪
