# Report_2_14 — Codex 终审整合报告（CHECKPOINT + FlashLite 体系）

> 来源：两份 Codex GPT-5.3 终审报告的完整整合
> - `报告_CHECKPOINT机制终审_Codex.md`（xhigh reasoning）
> - `报告_FlashLite体系全面审核_Codex.md`（high reasoning）
> 审核时间：2026-04-11
> 整合时间：2026-04-11 03:40

---

## 一、整体评价

### CHECKPOINT 机制
**核心重构已落地（T 文件三系统分立、压缩主链路、面板参数链路完成），但"同窗口并发数据完整性"和"严格压缩率"仍有关键缺口，当前为"可用但未达到终审关闭标准"。**

### FlashLite 体系
**架构方向清晰，但安全边界与成本控制存在高风险点；建议先修复安全与递归调用问题，再做缓存与声明一致性优化。**

---

## 二、维度评级总览

### CHECKPOINT 机制逐项核对

| 审核维度 | 结论 | 说明 |
|---|---|---|
| 设计-实现一致性 | **部分通过** | 主架构已落地，细节收口不足（并发增量与严格压缩率） |
| 并发安全 | **部分通过** | `_compressing`、合并式 Save 已有，但同窗口并发追加仍可重复 |
| 数据完整性 | **未通过** | 存在"重复追加"和"截断后漏记"两类风险 |
| 压缩质量 | **部分通过** | Prompt + 动态 max tokens + 比率校验已实现，但区间非硬保证 |
| 面板参数联动 | **通过** | 前端 app.js、后端 models.py、config.json 与 main.py 已打通（含兼容旧 key） |
| 边界条件 | **部分通过** | 空 T/首次压缩/压缩中新消息合并已覆盖；同窗口并发一致性未闭环 |
| 回归风险 | **中等** | Knowledge/触发主链路未见回退，但自动化测试未跟进当前策略 |

### CHECKPOINT 设计文档逐项核对

| 设计文档 | 核对结论 | 说明 |
|---|---|---|
| CHECKPOINT机制讨论记录.md | **部分实现** | 三系统分立与 T 文件主链路已实现；"消息不丢失/不重复"的并发场景仍有缺口 |
| Plan_2_CP.md | **部分实现** | 决策 1~8、10 基本落地；决策 9"压缩率严格保证"仍为软校验 |
| Plan_2_CP_architecture.md | **已实现** | FlashLite/主模型上下文均切到 T 文件；req.contexts 在 on_llm_request 被替换 |
| Plan_2_CP_T_file.md | **部分实现** | T 文件结构、原子写入、per-window lock 已实现；增量提取未做内容对齐/指纹兜底 |
| Plan_2_CP_compression.md | **部分实现** | 三重守卫、前比例压缩、动态 maxOutputTokens 已实现；下限/区间仍非硬约束 |
| Plan_2_CP_integration.md | **部分实现** | on_llm_request 替换上下文、触发判断改读 T 文件已实现；"回复后回写 T"仍是启发式补录 |
| Plan_2_CP_缺漏_P0P1.md | **部分实现** | P0 关键项已基本处理；P1 数据完整性项未彻底收口 |
| Plan_2_CP_缺漏_P2优化.md | **部分实现** | 并发合并式 Save、参数校验已实现；增量提取健壮化与部分清理项未完成 |
| Plan_2_CP_P2_3_并发安全.md | **部分实现** | "压缩期间中间消息合并"已实现；"同窗口并发 load→extract→append"仍可重复追加 |

### FlashLite 体系维度评估

| 维度 | 评级 | 结论 |
|---|---|---|
| 架构完整性 | 需改进 | 三模型主链路可追踪，但子代理允许自递归委托，存在链路失控风险 |
| 代码质量 | 需改进 | 关键路径可读性尚可，但存在字符串拼接执行、异常兜底不充分 |
| 提示词一致性 | 良好 | 三套 prompt 主体与审计文档基本一致；存在工具声明与提示词能力漂移 |
| 性能与成本 | 有风险 | KV Cache 固定前缀不稳定，导致命中率显著受损；FlashLite 调用频率偏高 |
| 安全性 | 有风险 | "Sandbox"执行路径缺少进程级隔离，且存在可注入执行点 |

### FlashLite 提示词一致性逐项核对

| 核对项 | 结果 | 备注 |
|---|---|---|
| `_build_flash_lite_system()` vs `Prompt_FlashLite_判断.md` | 一致 | 身份、触发条件、双模式输出与字段约束均可对齐 |
| FlashLite 压缩模式 vs `Prompt_FlashLite_压缩.md` | 一致 | 压缩走同入口，user prompt 切换；无格式冲突 |
| `_build_tool_model_system()` vs `Prompt_工具模型.md` | 基本一致 | 系统职责与三模式描述一致；工具声明能力存在 schema 漂移 |
| `inject_flashlite_context()` 各 Section vs `Prompt_主模型.md` | 基本一致 | 注入段落顺序/内容总体一致 |
| 工具声明能力与提示词描述一致性 | **不一致** | web_fetch/browser_agent 参数与可用模式漂移 |

---

## 三、问题分级清单（按优先级排序）

### 🔴 Critical 级

#### C-1: Sandbox 执行未实现进程级隔离
- **来源**: FlashLite 审核
- **位置**: `sandbox.py:565`, `sandbox.py:587`, `sandbox.py:617`
- **描述**: `exec_code` 的 `command`/`code` 模式直接调用系统 shell/解释器进程执行；仅做路径层校验，无法阻止脚本访问 Sandbox 外部文件、环境变量和网络资源
- **影响**: 模型被提示词注入或工具误用时可直接触达宿主系统敏感数据，安全边界与"Sandbox"命名不一致
- **修复建议**:
  1. 引入真正隔离运行时（容器/受限用户/作业对象+最小权限）
  2. 默认关闭 `command` 模式，仅允许白名单命令
  3. 对外部网络访问做可配置 deny-by-default

#### C-2: `search` 工具存在代码注入面
- **来源**: FlashLite 审核
- **位置**: `main.py:3470`, `main.py:3477`
- **描述**: `tool_search` 在 files 模式下将 `query` 直接插入 Python 源码字符串，未转义
- **影响**: 恶意 query 可突破"文件搜索"语义，转为任意脚本执行
- **修复建议**:
  1. 禁止字符串模板拼代码，改为固定脚本 + 参数传递（json.dumps/临时文件参数）
  2. 优先复用已有 `grep` 工具，避免二次实现执行链

---

### 🟠 High 级

#### H-1: 同窗口并发请求导致 T 文件消息重复追加（破坏"不重复"）
- **来源**: CHECKPOINT 终审
- **位置**: `main.py:2667`, `main.py:2670`, `checkpoint.py:351`
- **描述**: `on_llm_request` 先 `load` 再 `_extract_new_messages`，提取结果基于旧快照；并发请求拿到同一增量，`append_messages` 在锁内只做 append 不做幂等检查，导致重复写入
- **复现实验**: 并发两次 append 相同增量后 `final_len=14`（预期 12），尾部出现 `m10,m11,m10,m11` 重复
- **修复建议**:
  1. 用 window 级总锁包裹 `load → extract → append → compress → build_contexts` 整个事务链
  2. 或在 `append_messages` 增加尾部指纹去重（`role+content+tool_call_id+timestamp`）

#### H-2: 增量提取仅靠长度差，history 截断后可永久漏记新消息（破坏"不丢失"）
- **来源**: CHECKPOINT 终审
- **位置**: `main.py:3026`
- **描述**: `_extract_new_messages()` 仅在 `len(contexts) > processed_count` 时返回增量；外部截断导致 `len(contexts) < processed_count` 时函数持续返回空，后续新消息无法写入 T 文件
- **修复建议**:
  1. 增加"降级对齐"分支：当 `len(contexts) < processed_count` 时基于尾部内容/指纹重同步
  2. 为消息写入指纹并维护最近 N 条索引

#### H-3: 子代理可调用 `browser_agent` 导致自递归委托风险
- **来源**: FlashLite 审核
- **位置**: `main.py:1588`, `main.py:1767`, `main.py:4867`
- **描述**: `_call_tool_model` 动态加载 base_tools 时未排除 `browser_agent`；而 `tool_browser_agent` 内部又调用 `_call_tool_model`
- **影响**: 可能出现"子代理再委托子代理"的递归链，token/时延/费用放大，极端情况任务卡死
- **修复建议**:
  1. 在 `excluded_tools` 中加入 `browser_agent`
  2. 增加 delegation depth 上限（如 `max_delegate_depth=1`）

#### H-4: KV Cache 固定前缀不稳定，命中率被动态内容拉低
- **来源**: FlashLite 审核 + 我们的深度分析
- **位置**: `main.py:1288-1289`, `main.py:1421`, `main.py:1391-1392`
- **描述**: 用于缓存的 system 文本包含当前时间和 Knowledge 快照，`ensure_cache` 每次哈希都变化
- **影响**: 缓存重建频繁，FlashLite 15.7%/主模型 19.2% 命中率，成本浪费严重
- **修复建议**:
  1. 拆分 system 为 `static_system`（缓存）+ `dynamic_context`（contents 动态注入）
  2. 将时间与 Knowledge 移出缓存段
  → 详见 Plan_3_2

#### H-5: 本地复制白名单基于 `startswith` 路径边界校验不足
- **来源**: FlashLite 审核
- **位置**: `main.py:5175`
- **描述**: `save_data(local_path=...)` 通过字符串前缀判断允许路径，未使用 `commonpath/resolve` 边界判断
- **影响**: 可构造同前缀路径误判；白名单覆盖 `%TEMP%` 等广域目录
- **修复建议**:
  1. 使用 `Path.resolve()` + `os.path.commonpath` 严格校验目录归属
  2. 附件复制改为"消息上下文签名令牌"校验

---

### 🟡 Medium 级

#### M-1: `_compressing` 缺少全链路 `finally` 释放
- **来源**: CHECKPOINT 终审 + FlashLite 审核（两报告均提及）
- **位置**: `checkpoint.py:574`, `checkpoint.py:757`, `checkpoint.py:760`
- **描述**: 窗口压缩标记在多处分支手动 `discard`，缺少"从 add 到 return 全流程 finally"；异常路径可能让窗口长期处于"压缩中"
- **修复建议**: 将 `self._compressing.add(window_key)` 到函数结束包进 `try/finally`

#### M-2: 压缩率区间仍是软约束
- **来源**: CHECKPOINT 终审
- **位置**: `checkpoint.py:629`, `checkpoint.py:655`
- **描述**: 上限采用 `target_max + Δ`，越界仅 warning 不阻断；下限同样仅告警
- **修复建议**:
  1. 明确"硬约束定义"（如允许 `target_max + ε`）
  2. 对越界结果做一次自适应重试或拒绝覆盖 T1

#### M-3: 回归测试已失效
- **来源**: CHECKPOINT 终审
- **位置**: `test_checkpoint_v2.py:279`, `test_checkpoint_v2.py:286`
- **描述**: 测试仍断言旧版 prompt 中的字数区间文本（1500/3000），与当前实现不一致
- **修复建议**: 更新测试断言到现行策略

#### M-4: 工具声明与提示词能力不一致（web_fetch / browser_agent 参数漂移）
- **来源**: FlashLite 审核
- **位置**: `base_tools/web_fetch.tool.json:15,20`, `base_tools/browser_agent.tool.json:8`, `main.py:2817`, `main.py:4760`
- **描述**: 提示词宣称 `web_fetch` 支持多模式（rich/tables/download/pipeline 等），但工具声明 enum 仅 5 种；`browser_agent` 声明缺 `inject_context`
- **影响**: 工具模型被提示词鼓励使用但 schema 不允许，导致调用失败率上升
- **修复建议**:
  1. 以实现签名自动生成 `.tool.json`（单一事实源）
  2. 增加校验机制确保 schema 与函数签名一致

#### M-5: Memory 迷你索引截断逻辑在 pinned 过多时可能反向扩容
- **来源**: FlashLite 审核
- **位置**: `main.py:1188`, `main.py:1189`
- **描述**: `MAX_INDEX - len(pinned)` 为负时切片会保留大量 non-pinned，导致索引超出预期上限
- **修复建议**: 先 `pinned = pinned[:MAX_INDEX]`，再按剩余额度追加 non-pinned

#### M-6: 子代理调用 `upload_data` 默认参数与 `event=None` 语义冲突
- **来源**: FlashLite 审核
- **位置**: `main.py:1776`, `main.py:4922`
- **描述**: 子代理路由传 `event=None`，而 `upload_data(send_to_qq=true)` 默认尝试 QQ 发送，易触发异常降级
- **修复建议**: 子代理路由对 `agent_upload_data` 强制注入 `send_to_qq=False`

---

### 🟢 Low 级

#### L-1: 遗留注释/兼容桩与现实现状态不一致
- **来源**: CHECKPOINT 终审
- **位置**: `agent.py:6`, `main.py:2259`, `main.py:1239`
- **描述**: `agent.py` 顶部公式仍写"CHECKPOINT 摘要 + 最近消息"，`_get_recent_context()` 已废弃但保留
- **修复建议**: 统一文案与死代码清理

#### L-2: 重复装饰器
- **来源**: FlashLite 审核
- **位置**: `main.py:4745`, `main.py:4746`
- **描述**: `@filter.llm_tool(name="browser_agent")` 重复声明
- **修复建议**: 保留一条

---

## 四、做得好的地方（两份报告汇总）

- ✅ T 文件压缩后采用"重载-合并-保存"策略，较好处理了压缩期间的新消息并发
- ✅ 工具模型 key 池有基础冷却和轮转，具备生产可用雏形
- ✅ 提示词体系分层完整（FlashLite/主模型/工具模型），整体可维护性较高
- ✅ 面板参数联动已打通（前端 → 后端 → config → main.py）
- ✅ 三系统分立架构清晰（Knowledge + CHECKPOINT + Memory）
- ✅ `py_compile` 全部通过

---

## 五、性能与成本评估

1. **FlashLite 调用频率偏高**
   - `sync_trigger_interval` 默认 5；私聊每条消息都触发
   - 粗估：`calls/min ≈ group_msgs/5 + private_msgs + @/关键词触发次数`
   → 详见 Plan_3_1 采样优化

2. **KV Cache 命中率被动态 system 影响明显**
   - 缓存 system 含实时 Knowledge + 系统时间
   - 缓存高频重建，抵消 KV 优势
   - FlashLite 15.7% / 主模型 19.2% / 工具模型估计类似
   → 详见 Plan_3_2 KV Cache 优化

3. **T 文件并发模型总体可用，但压缩互斥释放需补强**
   - 窗口锁 + merge-save 做得较好；`_compressing` 缺 finally

4. **API Key 池轮转机制设计合理**
   - 429 冷却与轮转机制已有
   - 建议：`max_retries=min(len(keys),3)` 可按配置放宽

---

## 六、验证记录

- `py_compile checkpoint.py main.py models.py`：**通过**
- `test_checkpoint_v2.py`：**失败**（旧断言与新 prompt 策略不一致）

---

## 七、建议修复顺序（综合两份报告）

### 阶段 A：安全与数据完整性（先修，否则后续优化意义有限）
1. **C-1**: Sandbox 进程隔离（可分阶段，先关 command 模式白名单化）
2. **C-2**: search 工具代码注入修复（改参数传递）
3. **H-1**: 同窗口并发 T 文件重复追加（事务化锁 或 尾部指纹去重）
4. **H-2**: 增量提取截断后漏记（降级对齐分支）
5. **M-1**: `_compressing` finally 化

### 阶段 B：成本优化（Plan 3 系列）
6. **H-4**: KV Cache 前缀稳定化（Plan_3_2）→ 预估月省 ¥30-45
7. **Plan_3_1**: FlashLite 采样优化 → 预估月省 ¥10-15

### 阶段 C：功能完善与一致性
8. **H-3**: 子代理递归委托风险（excluded_tools + depth 上限）
9. **H-5**: 路径白名单校验修复
10. **M-4**: 工具声明与提示词对齐
11. **M-5**: Memory 迷你索引截断修复
12. **M-6**: 子代理 upload_data 默认参数修复

### 阶段 D：质量收口
13. **M-3**: 回归测试更新
14. **L-1/L-2**: 死代码清理、重复装饰器

> [!NOTE]
> M-2（压缩率软约束）经用户确认**不需要处理**，当前的 warning + maxOutputTokens 硬上限策略已足够。

---

## 八、已确认修复方案详细设计

> [!IMPORTANT]
> 以下方案均已获得用户确认，按优先级排序。H-4（KV Cache）和 M-2（压缩率）不在此列。

### Fix C-1: Sandbox 安全加固

**目标**: 限制 `exec_code` 的执行能力，防止模型误用/注入时触达宿主系统

**改动点**: `sandbox.py`

**方案**:
1. `command` 模式增加白名单校验，仅允许以下命令前缀：
   - `pip install`、`pip list`（包管理）
   - `python`（限定在 Sandbox 目录内的脚本）
   - `grep`/`find`/`dir`/`type`（文件查看类）
2. 非白名单命令拒绝执行并返回错误提示
3. 增加面板开关 `sandbox_command_whitelist_enabled`（default: true）
4. 日志记录所有被拦截的命令

**验证**: 构造恶意命令（如 `rm -rf /`、`curl http://xxx`）确认被拦截

---

### Fix C-2: search 工具代码注入修复

**目标**: 消除 `tool_search` 中 query 直接拼接 Python 代码的注入风险

**改动点**: `main.py:3470-3477`

**方案**:
1. 不再将 query 拼入 Python 源码字符串
2. 改为将 query 通过 `json.dumps()` 转义后写入临时参数文件
3. 固定脚本读取参数文件中的 query 执行搜索
4. 或者直接使用 subprocess 调用 grep/ripgrep 工具，query 作为命令行参数传入

```python
# 修复前（危险）
code = f'import glob; results = [f for f in glob.glob("**/*", recursive=True) if "{query}" in open(f).read()]'

# 修复后（安全）
import json, tempfile
param_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
json.dump({"query": query, "path": search_path}, param_file)
# 固定脚本读取 param_file 执行搜索
```

**验证**: 传入含 `"; import os; os.system("whoami")` 的 query 确认不执行

---

### Fix H-1: 同窗口并发 T 文件重复追加

**目标**: 防止同一窗口的并发请求重复追加消息到 T 文件

**改动点**: `main.py:2665-2696` + `checkpoint.py:341-385`

**方案**: Window 级事务锁（两阶段）

> [!WARNING]
> `compress_if_needed` 内部有自己的 `_get_lock(window_key)` (L723)，`asyncio.Lock` 不可重入。
> 若把压缩放在外层锁内 → 死锁。压缩期间阻塞同窗口请求 5-10s → 与 P2-3 merge-save 设计矛盾。
> **因此锁只覆盖 Phase 1（快速的原子操作），压缩在锁外运行。**

1. `append_messages` 拆分为：
   - `append_messages()`：公开 API，自带锁（保持向后兼容）
   - `_append_messages_unlocked()`：内部版本，不加锁（供事务链内部调用）
2. `on_llm_request` 分两阶段：
   - Phase 1（锁内）：`load → extract → append → save`（毫秒级，原子化）
   - Phase 2（锁外）：`compress_if_needed`（秒级，自带 merge-save 保护）

```python
# main.py on_llm_request 改动

# Phase 1: 原子化 load-extract-append（锁内，快速）
async with self._t_file_mgr._get_lock(window_key):
    t_file = await self._t_file_mgr.load(window_key)
    new_msgs = self._extract_new_messages(req.contexts, t_file)
    if new_msgs:
        t_file = await self._t_file_mgr._append_messages_unlocked(
            window_key, t_file, new_msgs
        )
        await self._t_file_mgr.save(window_key, t_file)

# Phase 2: 压缩运行在锁外（已有自己的 merge-save 保护）
t_file, compress_result = await self._t_file_mgr.compress_if_needed(...)

# Phase 3: 构建上下文
req.contexts = self._t_file_mgr.build_llm_contexts(t_file)
```

**验证**:
1. 模拟并发 append 相同增量，确认不出现重复消息
2. 模拟压缩期间新消息到达，确认 merge-save 正确合并
3. 确认无死锁（压缩内部锁和外层锁不冲突）

---

### Fix H-2: 增量提取截断后漏记修复

**目标**: 当 AstrBot 框架截断 contexts 后，增量提取仍能正确识别新消息

**改动点**: `main.py:3026-3043`

**方案**: 增加降级对齐分支

```python
def _extract_new_messages(self, contexts, t_file):
    existing_count = len(t_file.get("messages", []))
    compressed_count = t_file.get("T1", {}).get("original_msg_count", 0)
    processed_count = compressed_count + existing_count

    if not contexts:
        return []

    if len(contexts) > processed_count:
        # 正常情况：contexts 比 T 文件多 → 增量提取
        return contexts[processed_count:]
    
    elif len(contexts) < processed_count:
        # ⚠️ 异常情况：AstrBot 截断了 contexts
        # 降级策略：用 T 文件最后一条消息的内容指纹
        # 在 contexts 中反向查找对齐点
        t_msgs = t_file.get("messages", [])
        if t_msgs:
            last_t_fingerprint = self._msg_fingerprint(t_msgs[-1])
            # 从 contexts 末尾向前找匹配
            for i in range(len(contexts) - 1, -1, -1):
                if self._msg_fingerprint(contexts[i]) == last_t_fingerprint:
                    # 找到对齐点，i+1 之后是新消息
                    new_msgs = contexts[i+1:]
                    if new_msgs:
                        logger.info(f"[T-FILE] 降级对齐: contexts 被截断 "
                                   f"({len(contexts)} < {processed_count}), "
                                   f"找到 {len(new_msgs)} 条新消息")
                    return new_msgs
        
        # 完全无法对齐 → 记录 warning，不追加（宁可不追加也不追加错）
        logger.warning(f"[T-FILE] contexts 截断且无法对齐 "
                      f"({len(contexts)} < {processed_count})")
        return []
    
    return []  # 无新消息

@staticmethod
def _msg_fingerprint(msg: dict) -> str:
    """消息指纹：role + content 前50字 + tool_call_id"""
    role = msg.get("role", "")
    content = str(msg.get("content", ""))[:50]
    tcid = msg.get("tool_call_id", "")
    return f"{role}|{content}|{tcid}"
```

**验证**: 模拟 contexts 被截断到 25 条（T 文件已处理 50 条），然后新增消息，确认能正确追加

---

### Fix H-3: 子代理递归委托封禁

**目标**: 禁止子代理（browser_agent 内部的工具模型）再调用 browser_agent

**改动点**: `main.py:1588`（`_call_tool_model` 中 `excluded_tools` 列表）

**方案**:
1. 在工具模型加载 base_tools 时，将 `browser_agent` 加入 `excluded_tools`
2. 同时将 `run_custom_tool` 也加入排除（防止间接递归）
3. 可选：增加 `_delegate_depth` 计数器，超过 1 层直接拒绝

```python
# _call_tool_model 中
excluded_tools = ["browser_agent", "run_custom_tool"]  # 防递归
```

**验证**: 确认工具模型的可用工具列表中不包含 browser_agent

---

### Fix H-5: 路径白名单校验加固

**目标**: 防止路径绕过攻击

**改动点**: `main.py:5175`

**方案**:
```python
from pathlib import Path

def _is_path_allowed(self, path_str: str) -> bool:
    """严格校验路径是否在白名单目录内"""
    try:
        resolved = Path(path_str).resolve()
        for allowed_dir in self._allowed_dirs:
            allowed_resolved = Path(allowed_dir).resolve()
            try:
                resolved.relative_to(allowed_resolved)
                return True
            except ValueError:
                continue
        return False
    except Exception:
        return False
```

**验证**: 测试 `../` 路径穿越、符号链接、同前缀路径（如 `/tmp2` vs `/tmp`）

---

### Fix M-1: `_compressing` finally 化

**目标**: 确保压缩异常时互斥标记被正确释放

**改动点**: `checkpoint.py:574-760`

**方案**: 将 L574 的 `self._compressing.add(window_key)` 到函数末尾包进 try/finally

```python
self._compressing.add(window_key)
try:
    # ... 整个压缩流程 (L575-769) ...
    return t_file, result
finally:
    self._compressing.discard(window_key)
```

同时删除 L643、L648、L735、L760 处的手动 `discard` 调用（由 finally 统一处理）

**验证**: 在压缩过程中注入异常，确认 `_compressing` 集合不残留窗口标记

---

### Fix M-3: 回归测试更新

**目标**: 使 test_checkpoint_v2.py 反映当前实现

**改动点**: `test_checkpoint_v2.py:279-286`

**方案**: 更新断言条件，匹配现行"去字数硬约束 + API maxOutputTokens 控制"策略

---

### Fix M-4: 工具声明 schema 对齐

**目标**: 工具模型的 .tool.json 声明与实际 handler 签名一致

**改动点**: `Sandbox/base_tools/web_fetch.tool.json`, `browser_agent.tool.json`, `main.py`

**方案**:
1. `web_fetch.tool.json` 的 mode enum 补全所有实际支持的模式
2. `browser_agent.tool.json` 增加 `inject_context` 参数声明
3. 主模型提示词中工具描述与 schema 对齐

---

### Fix M-5: Memory 迷你索引截断修复

**目标**: 防止 pinned 过多时 non-pinned 反向扩容

**改动点**: `main.py:1188-1189`

**方案**:
```python
# 修复前
non_pinned = non_pinned[:MAX_INDEX - len(pinned)]

# 修复后  
pinned = pinned[:MAX_INDEX]  # 先限制 pinned 数量
remaining = max(0, MAX_INDEX - len(pinned))
non_pinned = non_pinned[:remaining]
```

---

### Fix M-6: 子代理 upload_data 默认参数修复

**目标**: 子代理路径下不触发 QQ 发送

**改动点**: `main.py:1776`, `main.py:4922`

**方案**: 子代理路由对 `agent_upload_data` 强制注入 `send_to_qq=False`

---

### Fix L-1: 死代码清理

**目标**: 清理过时注释和废弃方法

**改动点**: `agent.py:6`, `main.py:2259`, `main.py:1239`

**方案**: 
- 更新 `agent.py` 顶部注释
- 移除/标记 `_get_recent_context()` 
- 更新 system 文案中过时的 `CheckpointManager` 引用

---

### Fix L-2: 重复装饰器

**目标**: 移除重复的 `@filter.llm_tool` 声明

**改动点**: `main.py:4745-4746`

**方案**: 删除重复的一行
