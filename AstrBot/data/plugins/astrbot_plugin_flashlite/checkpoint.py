"""
CHECKPOINT 压缩模块 v2.0 — 三系统分立架构
==================================================

核心变更：引入 TFileManager，为每个对话窗口独立维护 T 文件（智能压缩上下文），
取代直接操作 messages.db 的旧方案。

系统分立：
  A: req.contexts  — AstrBot 框架管理（不触碰）
  B: messages.db   — FlashLite QQ 消息持久化（不触碰）
  C: T 文件        — 本模块管理，实际发送给 LLM 的请求体上下文

规划文档：QQBotPlan/Plan_2_CP*.md
"""

import asyncio
import copy
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiosqlite
from astrbot.api import logger

# S3 F1.3: 划轮状态机（唯一取号入口）。同目录纯逻辑模块，无 astrbot 依赖。
# 兼容两种导入：包内 `from .` 与测试/直跑时的顶层 `import`。
try:
    from . import round_tracker
except ImportError:
    import round_tracker

# S4 R4: record 机制（M1 RecordStore / compose_record 确定性聚合）。同目录纯逻辑模块，
# 无 astrbot 依赖；兼容包内 `from .` 与测试/直跑顶层 import。
try:
    from . import record
except ImportError:
    import record

# ========================
# Token 估算参数
# ========================
CHARS_PER_TOKEN_CN = 1.5    # 中文约 1.5 字/token
CHARS_PER_TOKEN_EN = 4.0    # 英文约 4 字符/token
IMAGE_TOKEN_ESTIMATE = 258  # 每张图片约 258 token（Gemini 低分辨率）

# ========================
# 路径常量
# ========================
DB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "QQ_data")
)
DB_PATH = os.path.join(DB_DIR, "messages.db")
CHECKPOINTS_DIR = os.path.join(DB_DIR, "checkpoints")

# T1 摘要注入时的固定标记
T1_SUMMARY_PREFIX = "[对话历史压缩摘要]"
T1_ACK_CONTENT = "好的，我已了解之前的对话历史。"

# record 概要块召回指针（D7 降档摘要不自知缺口修复）：
# 真机实测——D7 降档把老话题压成 summary/brief 注入后，主模型不知道这是压缩摘要、
# 误以为是完整逐字记忆，被要求逐字复述时凭摘要 + 固定人设幻觉编造冒充原文。
# 在 record 概要块开头统一加一句总说明，告知主模型：
#   ① 概要块各档（full 是 record 聚合记录、summary/brief 是进一步压缩摘要）均非
#      messages 逐字原文；② 需要某轮逐字原文/精确措辞/完整细节时调 QQ_data_original
#      召回真实消息；③ 严禁凭摘要自行编造。措辞精准区分「record 聚合/摘要 ↔ messages
#   逐字原文」。该串静态恒定，拼在 T1_SUMMARY_PREFIX 之后、各分级 block 之前，
#   不破坏 KVCache 前缀稳定性（每次注入完全一致）。
T1_RECORD_RECALL_HINT = (
    "（说明：以下为历史对话的分级记录。full 档为 record 聚合记录、summary/brief 档为"
    "进一步压缩的摘要，均非逐字原文。若需要某轮的逐字原文、精确措辞或完整细节，"
    "请调用 QQ_data_original 按消息序号/关键词召回真实消息原文后再回答，"
    "严禁凭本记录摘要自行编造或冒充原文。）"
)


# ========================
# Token 估算工具（保留自 v1）
# ========================

def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（中英文混合）"""
    if not text:
        return 0

    cn_chars = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', text))
    en_chars = len(text) - cn_chars

    cn_tokens = cn_chars / CHARS_PER_TOKEN_CN
    en_tokens = en_chars / CHARS_PER_TOKEN_EN

    return max(int(cn_tokens + en_tokens), 1)


def estimate_context_msg_tokens(msg: dict) -> int:
    """估算 OpenAI 格式消息的 token 数

    适配 v2 的 contexts 格式（{role, content, tool_calls, ...}）
    """
    tokens = 4  # 消息开头/结尾固定开销

    content = msg.get("content")
    if content:
        if isinstance(content, str):
            tokens += estimate_tokens(content)
        elif isinstance(content, list):
            # 多模态内容（text + image 混合）
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        tokens += estimate_tokens(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        tokens += IMAGE_TOKEN_ESTIMATE
                    elif "inline_data" in part:
                        tokens += IMAGE_TOKEN_ESTIMATE

    # tool_calls
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            tokens += estimate_tokens(fn.get("name", ""))
            tokens += estimate_tokens(fn.get("arguments", ""))

    return max(tokens, 1)


# ========================
# T 文件数据结构工厂
# ========================

# S4 R1: record_state 五锚默认值（_create_empty_t_file 初始化 + _ensure 嵌套补默认共用）。
#   - last_compressed_round_id : 压缩锚（S3 已有）——record 合成/压缩到哪一轮，
#                                由 S3 压缩链路单调推进。绝不与 last_grouped 混为一谈。
#   - last_grouped_rg_id       : 聚合锚（S4 新）——round-group 增量聚合到哪一组。
#   - round_groups             : 边界表（S4 新）——已封/在编的 round-group 列表。
#   - hit_table                : 命中表（S4/M4 新）——{rg_id: {hit_count,last_hit_ts,...}}。
#   - summary_watermark_rg_id  : summary 封板水位（S4/D8 新）——防「有 full 无 summary」空洞。
# 取号/格式约定全部沿用 round_tracker（rg_id 字符串、parse 数值比较）。
_RECORD_STATE_DEFAULTS = {
    "last_compressed_round_id": None,   # 压缩锚（S3 已有，勿动语义）
    "last_grouped_rg_id": None,         # 聚合锚（S4 新，与压缩锚解耦）
    "round_groups": [],                 # 边界表（S4 新）
    "hit_table": {},                    # 命中表（S4/M4 新）
    "summary_watermark_rg_id": None,    # summary 封板水位（S4/D8 新）
}


def _create_empty_t_file(window_key: str) -> dict:
    """创建空的 T 文件数据结构"""
    parts = window_key.split(":", 1)
    window_type = "group" if parts[0] == "GroupMessage" else "private"
    window_id = parts[1] if len(parts) > 1 else window_key

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return {
        # S3 F1.1: schema v2（引入 round_id/step_id 锚点体系 + 取号号源）
        "version": 2,
        "window_key": window_key,
        "window_type": window_type,
        "window_id": window_id,
        "T1": {
            "compressed_summary": "",
            "token_count": 0,
            "compression_ratio": 0.0,
            "original_msg_count": 0,
            "compression_count": 0,
            "last_compress_time": "",
            "compress_history": [],
        },
        "messages": [],
        "metadata": {
            "created_at": now_iso,
            "updated_at": now_iso,
            "total_messages_ever": 0,
            "total_compressions": 0,
            "avg_compression_ratio": 0.0,
            # S3 F1.1: 取号号源（唯一取号入口 round_tracker.assign_round 就地 +1）
            "next_round_id": 1,
            "next_step_id": 1,
            # S3 F2.2: 与 {window}.state.json 交叉校验的代号（save 时 ++，恢复取大者修正小者）
            "generation": 0,
            # S4 R1: record_state 五锚（占位状态字典，S4/S5/S6 消费）。
            # ⚠️ last_compressed(压缩锚, S3 已有)≠last_grouped(聚合锚, S4 新)是两个
            # 不同的锚：前者=「record 合成/压缩到哪一轮」(S3 压缩链路单调推进)；
            # 后者=「round-group 增量聚合到哪一组」(S4 record 生成器推进)。二者解耦。
            # deepcopy：round_groups(list)/hit_table(dict) 等可变默认值必须深拷贝，
            # 否则多个 T 文件共享同一引用、互相串改（单测 R1 已覆盖）。
            "record_state": copy.deepcopy(_RECORD_STATE_DEFAULTS),
            "bpc_state": {},
            "concurrency_state": {},
        },
    }


def _parse_step_id(sid: Any) -> int:
    """解析 step_id 字符串为整数（s00000123 → 123）；非法/None 返回 -1。"""
    if not sid or not isinstance(sid, str) or not sid.startswith("s"):
        return -1
    try:
        return int(sid[1:])
    except ValueError:
        return -1


def _recover_number_source_after_corruption(
    checkpoints_dir: str, window_key: str, fp: str
) -> Tuple[int, int]:
    """S3 批3.5-B(numbering-3)：T 文件损坏重建时，从 state.json + 现存 .corrupt 文件
    恢复 (next_round_id, next_step_id)，绝不复用历史号（取已见最大 +1）。

    缺陷：原 _create_empty_t_file 把 next_round_id/next_step_id 重置为 1，但 state.json
    独立存活（current_round_id=r000050）→ 重号；assign_round 又从 1 起，与历史轮号冲突，
    违反「round_id 全局单调绝不复用」铁律。

    号源来源（取各项 max + 1，下限 1）：
      (1) state.json 的 current_round_id（最近所在轮）+ last_user/assistant/first_reply
          step_id（state 不直接存 next_step_id，但这些字段是已分配步号的水位线索——
          T 文件完全损坏不可解析时，它们是 step 号源的唯一兜底）；
      (2) 现存 {fp}.corrupt.* 文件里 messages 的 max round_id/step_id（损坏前已落盘的
          最大轮/步）+ metadata 残存 next_round_id/next_step_id（已用号上界）；
      (3) 兜底下限 1。
    """
    max_round = 0
    max_step = 0

    # (1) state.json：current_round_id + 各 step_id 字段（step 号源兜底）
    try:
        st = round_tracker.load_state(checkpoints_dir, window_key)
        rn = round_tracker.parse_round_id(st.get("current_round_id"))
        if rn > max_round:
            max_round = rn
        for _sk in ("last_user_step_id", "last_assistant_step_id",
                    "first_reply_step_id"):
            sn = _parse_step_id(st.get(_sk))
            if sn > max_step:
                max_step = sn
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[T-FILE] 损坏重建读 state 号源失败 {window_key}: {e}")

    # (2) 现存 .corrupt 文件 messages 的 max round_id / step_id
    corrupt_prefix = os.path.basename(fp) + ".corrupt."
    try:
        for name in os.listdir(checkpoints_dir):
            if not name.startswith(corrupt_prefix):
                continue
            cp = os.path.join(checkpoints_dir, name)
            try:
                with open(cp, "r", encoding="utf-8") as cf:
                    cdata = json.load(cf)
            except Exception:
                continue  # 损坏文件本身也可能不可读，跳过
            cmsgs = cdata.get("messages") if isinstance(cdata, dict) else None
            if not isinstance(cmsgs, list):
                continue
            for m in cmsgs:
                if not isinstance(m, dict):
                    continue
                rn = round_tracker.parse_round_id(m.get("round_id"))
                if rn > max_round:
                    max_round = rn
                sn = _parse_step_id(m.get("step_id"))
                if sn > max_step:
                    max_step = sn
            # corrupt 文件 metadata 里若残存 next_* 号源，也纳入（已用号的上界）
            cmeta = cdata.get("metadata") if isinstance(cdata, dict) else None
            if isinstance(cmeta, dict):
                nr = int(cmeta.get("next_round_id", 0) or 0)
                if nr - 1 > max_round:
                    max_round = nr - 1
                ns = int(cmeta.get("next_step_id", 0) or 0)
                if ns - 1 > max_step:
                    max_step = ns - 1
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[T-FILE] 损坏重建扫 .corrupt 号源失败 {window_key}: {e}")

    next_round = max(max_round + 1, 1)
    next_step = max(max_step + 1, 1)
    return next_round, next_step


# ========================
# S3 F1.1: schema v1 → v2 迁移
# ========================

# message-level v2 字段终态默认值（S3 一次定到 v2 终态，含 S4-S7 占位）
# S3 实际写值的：round_id/step_id/first_reply/timestamp/receive_seq/message_id/
#               sender/has_multimodal；其余（compressed/recalled）留默认。
# S4 D4（批2a）：**砍掉 message.rg_id 字段**——组归属不回填到每条 message，改为按
#   round_id 区间从 record_state.round_groups / record.index sidecar 推断（mcp 本来也
#   不回填原始消息）。回填会与并发 merge-save 抢数组（重蹈批3.5 号源回退坑），聚合失败
#   时 rg_id 留 None 又让分级/召回悬空。grep 确认除占位与单测断言外无任何 message.rg_id
#   消费方，砍除安全。
_MESSAGE_V2_DEFAULTS = {
    "round_id": None,
    "step_id": None,
    "first_reply": False,
    "receive_seq": 0,
    "message_id": None,
    "sender": None,
    "has_multimodal": False,
    "compressed": False,
    "recalled": False,
}

# metadata-level v2 新增字段默认值（迁移 / 兜底补齐共用）
_METADATA_V2_DEFAULTS = {
    "next_round_id": 1,
    "next_step_id": 1,
    # S3 F2.2: generation 交叉校验代号（与 state.json 同步推进，恢复取大者修正小者）
    "generation": 0,
    # S4 R1: 五锚默认（见 _RECORD_STATE_DEFAULTS）。注意 _ensure_metadata_v2_fields
    # 对 record_state 做「嵌套 key 级」补默认——旧 T 文件已有 record_state（只含
    # last_compressed_round_id）时，顶层 key 存在不会被整体覆盖，须逐子键补缺。
    "record_state": copy.deepcopy(_RECORD_STATE_DEFAULTS),
    "bpc_state": {},
    "concurrency_state": {},
}


def _ensure_message_v2_fields(msg: dict, *, legacy: bool = False) -> dict:
    """为单条 message 补齐 v2 终态字段（已有字段不覆盖）。

    legacy=True 时（迁移旧消息）额外标 ``legacy=True``，且 round_id/step_id
    永久留 None（约束 5：不回填旧消息号，不重建历史）。
    """
    for k, default in _MESSAGE_V2_DEFAULTS.items():
        if k not in msg:
            msg[k] = default
    if legacy and "legacy" not in msg:
        msg["legacy"] = True
    return msg


def _ensure_metadata_v2_fields(metadata: dict) -> dict:
    """为 metadata 补齐 v2 新增字段（已有字段保留，如 dangling_repair_history）。

    S4 R1: record_state 改为「嵌套 key 级」补默认。旧 T 文件（S3 落盘）的
    record_state 顶层 key 已存在但只含 last_compressed_round_id，若仍按顶层
    `k not in metadata` 判断会整体跳过、四个新锚（last_grouped_rg_id /
    round_groups / hit_table / summary_watermark_rg_id）永远补不上。故对
    record_state 单独逐子键补缺，且**绝不覆盖**已有的 last_compressed_round_id。
    """
    for k, default in _METADATA_V2_DEFAULTS.items():
        if k not in metadata:
            metadata[k] = copy.deepcopy(default)
    # record_state 嵌套补默认（顶层已存在时也要补齐缺失子键）
    rec_state = metadata.get("record_state")
    if isinstance(rec_state, dict):
        for sk, sv in _RECORD_STATE_DEFAULTS.items():
            if sk not in rec_state:
                rec_state[sk] = copy.deepcopy(sv)
    return metadata


def _migrate_v1_to_v2(data: dict) -> dict:
    """v1 → v2 迁移：绝不重建，保留所有旧消息（约束 5：重建=蒸发历史）。

    - 旧 messages 每条补 v2 字段，round_id/step_id 永久 None，标 legacy=True。
      旧消息号不回填（next_round_id 从 1 起算，旧消息 round_id 永久 None）。
    - metadata 补 next_round_id=1/next_step_id=1/record_state/bpc_state/
      concurrency_state（已有的不覆盖，如 dangling_repair_history 保留）。
    - 设 version=2。
    """
    migrated = 0
    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        _ensure_message_v2_fields(msg, legacy=True)
        migrated += 1

    metadata = data.setdefault("metadata", {})
    _ensure_metadata_v2_fields(metadata)

    data["version"] = 2
    logger.warning(
        f"[T-FILE] schema v1→v2 迁移完成: {data.get('window_key', '?')} "
        f"保留旧消息 {migrated} 条（round_id 永久 None/legacy），next_round_id=1"
    )
    return data


# ========================
# 压缩 Prompt 构建
# ========================

def build_compress_prompt(
    messages_text: str,
    original_tokens: int,
    target_min_ratio: float,
    target_max_ratio: float,
    has_previous_summary: bool,
) -> str:
    """构建 v2 CHECKPOINT 压缩 Prompt

    v2 改进：不在 Prompt 中限制字数/token，由 API maxOutputTokens 硬保证上限。
    Prompt 鼓励「尽可能详细」，使模型自然趋向可用空间的上限，最大化信息保留。
    """
    summary_note = ""
    if has_previous_summary:
        summary_note = (
            "\n注意：输入内容开头有一段 [对话历史压缩摘要]，"
            "这是之前轮次的压缩结果。"
            "请将其与后续新消息融合为一份统一的新摘要，"
            "旧摘要中的信息可以适当精简但不要完全丢弃。\n"
        )

    msg_char_count = len(messages_text)

    return f"""你是一个对话压缩引擎。将以下对话内容压缩为结构化摘要。
{summary_note}
## 输出要求
- 尽可能详细地保留所有有价值的信息
- 越详细越好，不要省略重要细节
- 系统会自动控制输出长度上限，你无需担心过长
- 不要刻意缩减内容，宁可多写也不要遗漏

## 压缩原则
1. 按话题/时间段分块，用简洁的标题标注每个话题段
2. 保留所有参与者名字和 QQ 号
3. 保留关键事实：人名、地名、数字、日期、结论、决定
4. 保留情感倾向和关系动态
5. 用「」包围重要原文引用
6. 去除：重复内容、纯表情、日常闲聊（你好/再见）、无信息量的应答
7. 如涉及图片/文件/工具调用，注明 [图片] [文件] [工具:名称→结果摘要]

## 输出格式
直接输出摘要，不要输出其他说明文字。格式参考：

【话题：xxx（时间段）】
参与者A 和 B 讨论了...关键信息:「原文引用」

## 原始内容（{msg_char_count} 字）
{messages_text}"""


# ========================
# S3 F1.5: dangling tool_calls 防御（C1 critical）
# ========================

# 占位 tool result 的默认文案（Q4 决策：人话 + system prompt 引导）
DANGLING_TOOL_PLACEHOLDER = "[工具结果丢失：系统重启/中断]"


def _repair_tool_call_pairs(
    contexts: List[dict],
    placeholder: str = DANGLING_TOOL_PLACEHOLDER,
) -> Tuple[List[dict], List[dict]]:
    """读侧 last-mile 防御：修复未配对的 tool_calls / tool 结果。

    组请求体（OpenAI messages）末尾若有未配对的 assistant.tool_calls（缺对应
    role=tool 结果），provider 会 400。崩溃重启 / 压缩切分会产生这种 dangling。
    本函数无条件修复，保证输出永远满足「每个 assistant.tool_calls[*].id 都有
    紧随其后的 role=tool 结果，且每个 role=tool 都有前序 assistant.tool_calls」。

    算法（O(N) 一遍扫）：
      - pending: tool_call_id -> 在 contexts 的 assistant 索引
      - 遇 assistant 且有 tool_calls → 每个 tc.id 加入 pending
      - 遇 role=tool → tool_call_id 在 pending 则弹出（配对成功）；
        不在 pending → orphan（缺头，标记待删）
      - 遍历完 pending 剩余 = dangling（assistant 发了 tool_calls 但无 tool 结果）

    修复：
      - dangling → 在该 assistant 消息之后（紧跟它原有的配对 tool 结果之后、
        在下一条非该 assistant 衍生的消息之前）插入占位 role=tool
      - orphan → 从输出删除

    Args:
        contexts: OpenAI 格式消息列表（不修改原 list，返回新 list）
        placeholder: 占位 tool result 的 content 文案

    Returns:
        (修复后 contexts, 修复记录 list)
        修复记录每条形如 {"type": "dangling_placeholder"|"orphan_dropped",
                          "tool_call_id": ..., "position": ...}
    """
    if not contexts:
        return contexts, []

    # 第一遍扫描：识别 dangling 与 orphan
    # pending[tcid] = assistant 在 contexts 中的索引
    pending: Dict[str, int] = {}
    # orphan_indices: role=tool 但无前序配对的索引集合（待删）
    orphan_indices = set()

    for idx, msg in enumerate(contexts):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                tcid = tc.get("id")
                if tcid:
                    pending[tcid] = idx
        elif role == "tool":
            tcid = msg.get("tool_call_id")
            if tcid in pending:
                # 配对成功，弹出
                pending.pop(tcid, None)
            else:
                # orphan：缺头（前无 assistant.tool_calls 发起，或重复结果）
                orphan_indices.add(idx)

    # 无任何修复需求 → 原样返回（不复制，零开销）
    if not pending and not orphan_indices:
        return contexts, []

    repairs: List[dict] = []

    # dangling: assistant_idx -> [缺失 tcid, ...]（保持发起顺序）
    dangling_by_assistant: Dict[int, List[str]] = {}
    for tcid, asst_idx in pending.items():
        dangling_by_assistant.setdefault(asst_idx, []).append(tcid)

    # 第二遍：重建输出 list
    # - 跳过 orphan
    # - 在每个 dangling assistant「衍生段」末尾插入占位
    #   衍生段 = 该 assistant 消息本身 + 紧随其后的连续 role=tool 消息
    repaired: List[dict] = []
    n = len(contexts)
    i = 0
    while i < n:
        if i in orphan_indices:
            msg = contexts[i]
            repairs.append({
                "type": "orphan_dropped",
                "tool_call_id": msg.get("tool_call_id") if isinstance(msg, dict) else None,
                "position": i,
            })
            i += 1
            continue

        msg = contexts[i]
        repaired.append(msg)

        # 该位置是 dangling assistant → 把它的衍生 tool 段也搬过来，再补占位
        if i in dangling_by_assistant:
            missing_ids = dangling_by_assistant[i]
            # 找出已配对的 tcid（该 assistant 的 tool_calls 里不在 missing 的）
            j = i + 1
            # 先把紧随其后的、属于该 assistant 的配对 tool 结果原样搬入
            while j < n:
                nxt = contexts[j]
                if (
                    isinstance(nxt, dict)
                    and nxt.get("role") == "tool"
                    and j not in orphan_indices
                ):
                    repaired.append(nxt)
                    j += 1
                else:
                    break
            # 在衍生段末尾补占位（每个缺失 tcid 一条）
            for tcid in missing_ids:
                placeholder_msg = {
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": placeholder,
                }
                repaired.append(placeholder_msg)
                repairs.append({
                    "type": "dangling_placeholder",
                    "tool_call_id": tcid,
                    "position": i,
                })
            i = j
            continue

        i += 1

    return repaired, repairs


# ========================
# S3 F1.4: buffer WAL（崩溃可恢复，约束 6）
# ========================
#
# buffer_message 是高频热路径（群活跃时每秒多条），纯内存 buffer 崩溃可丢几十条。
# WAL（Write-Ahead Log）方案：buffer_message 入口同步 append 一行 JSON 到
# ``{window安全文件名}.buffer.wal.jsonl``（append-only，绝不重写整文件），
# flush_buffer 落盘成功后删除该 WAL 文件。崩溃重启时 replay WAL（F2.2）。
#
# 每行带 message_id 用于 replay 去重；buffer 阶段消息常无 message_id（QQ 上报
# 的 user 消息有，但 assistant 补录 / synthetic 可能没有），此时用
# ``{window_key}#seq{N}`` 临时键兜底（仅用于 WAL 内部去重，不污染 message 字段）。

WAL_SUFFIX = ".buffer.wal.jsonl"


def wal_file_path(checkpoints_dir: str, window_key: str) -> str:
    """计算 {window}.buffer.wal.jsonl 路径（与 T 文件 / state.json 并列，命名一致）。"""
    safe_name = window_key.replace(":", "_")
    return os.path.join(checkpoints_dir, f"{safe_name}{WAL_SUFFIX}")


def wal_dedup_key(msg: dict, window_key: str, fallback_seq: int) -> str:
    """计算一条 WAL 消息的去重键。

    优先用 message_id（QQ 上报的 user 消息天然带）；缺失时用
    ``{window_key}#seq{N}`` 临时键（N=该窗口 WAL 内单调递增序号）。
    临时键只进 WAL 行的 ``_wal_key`` 字段，不写回 message 本体。
    """
    mid = msg.get("message_id")
    if mid is not None and mid != "":
        return f"mid:{mid}"
    return f"{window_key}#seq{fallback_seq}"


def wal_append(checkpoints_dir: str, window_key: str, msg: dict, dedup_key: str) -> None:
    """同步追加一行 JSON 到 WAL（append-only，绝不重写整文件）。

    一行 = {"_wal_key": 去重键, "msg": 原始消息 dict}。
    高频群每条 append < 1ms（仅一次 open(a)+write+close）。
    写失败仅记日志，绝不抛出阻断主链路（WAL 是兜底，丢一行不致命）。
    """
    fp = wal_file_path(checkpoints_dir, window_key)
    line = json.dumps({"_wal_key": dedup_key, "msg": msg}, ensure_ascii=False)
    try:
        os.makedirs(checkpoints_dir, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 — WAL 失败不能拖垮主链路
        logger.error(f"[T-FILE] WAL append 失败 {window_key}: {e}")


def wal_read(checkpoints_dir: str, window_key: str) -> List[dict]:
    """读取 WAL 全部行（每行解析为 {"_wal_key", "msg"}）。

    文件不存在 → []。坏行（JSON 解析失败）跳过并记日志，不阻断 replay。
    """
    fp = wal_file_path(checkpoints_dir, window_key)
    if not os.path.exists(fp):
        return []
    entries: List[dict] = []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning(f"[T-FILE] WAL 坏行跳过 {window_key}:{lineno}: {e}")
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("msg"), dict):
                    entries.append(obj)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[T-FILE] WAL 读取失败 {window_key}: {e}")
    return entries


def wal_clear(checkpoints_dir: str, window_key: str) -> None:
    """删除 WAL 文件（flush 落盘成功 / replay 完成后调用）。文件不存在则静默。"""
    fp = wal_file_path(checkpoints_dir, window_key)
    try:
        if os.path.exists(fp):
            os.remove(fp)
    except OSError as e:
        logger.error(f"[T-FILE] WAL 清理失败 {window_key}: {e}")


def gc_orphan_tmp_files(checkpoints_dir: str) -> int:
    """GC：扫 checkpoints 目录下 ``.t_file_*.tmp`` / ``.state_*.tmp`` 残留并删除。

    这些是 save / save_state 原子写崩在「写临时文件」与「os.replace」之间留下的
    半成品。启动期全扫一次（F2.2 步骤 1）。返回删除数量。
    """
    if not os.path.isdir(checkpoints_dir):
        return 0
    removed = 0
    try:
        for name in os.listdir(checkpoints_dir):
            if (name.startswith(".t_file_") or name.startswith(".state_")) \
                    and name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(checkpoints_dir, name))
                    removed += 1
                except OSError as e:
                    logger.warning(f"[T-FILE] tmp GC 删除失败 {name}: {e}")
    except OSError as e:
        logger.error(f"[T-FILE] tmp GC 扫描失败 {checkpoints_dir}: {e}")
    if removed:
        logger.info(f"[T-FILE] 启动 GC 清理半写临时文件 {removed} 个")
    return removed


def _is_round_boundary(messages: List[dict], idx: int) -> bool:
    """下标 idx 处是否为 round 边界（idx 之前/之后切开不切碎任何一轮）。

    边界定义：idx==0 或 idx==len（两端天然是边界）；否则 messages[idx] 与
    messages[idx-1] 的 round_id 不同（含「一个 None 一个非 None」的切换）即为边界。
    同一轮内部（前后 round_id 相等且非 None）则非边界。
    """
    n = len(messages)
    if idx <= 0 or idx >= n:
        return True
    prev_rid = messages[idx - 1].get("round_id")
    cur_rid = messages[idx].get("round_id")
    return prev_rid != cur_rid


def _max_grouped_round_end(round_groups: List[dict]) -> Optional[int]:
    """S4 R4：从 round_groups 边界表取「已聚合到的最大 round 终点」（数值）。

    供 compose_record_if_needed 两阶段提交里单调推进 last_compressed_round_id。
    跳过 legacy 组的占位 [0,0]（end=0 仍计入但通常被真轮号覆盖）。无有效组返回 None。
    """
    mx: Optional[int] = None
    for g in round_groups or []:
        if not isinstance(g, dict):
            continue
        rr = g.get("round_range")
        if not isinstance(rr, (list, tuple)) or len(rr) < 2:
            continue
        e = rr[1]
        if isinstance(e, bool):
            continue
        if isinstance(e, str):
            e = round_tracker.parse_round_id(e)
            if e < 0:
                continue
        if not isinstance(e, int):
            continue
        if mx is None or e > mx:
            mx = e
    return mx


def _align_compress_count_to_round_boundary(
    messages: List[dict], idx: int
) -> int:
    """S3 F1.6：把压缩切分下标 idx 对齐到最近的完整 round 边界（绝不切碎一轮）。

    切分语义：messages[:idx] 压缩、messages[idx:] 保留。要求切分点落在 round
    边界，使被压缩段与保留段各自的轮都完整（不会把某轮 first_reply 切到该轮中间）。

    对齐策略：**向前挪**（缩小压缩段）——若 idx 落在某轮中间，退到该轮起始下标，
    把整轮留给保留段。宁可少压一轮，绝不把半轮压进摘要。

    legacy / 边界安全（始终落在 round 边界，绝不切碎一轮）：
      - idx 已在边界 → 原样返回。
      - idx 前一条 round_id 为 None（legacy 旧迁移消息，无 round 信息）→ 不挪，
        返回原 idx，交由调用方的旧 user-assistant 防切补丁兜底（不报错）。
      - 向前挪会退到 0（idx 落在第一轮内部，前方无完整轮可压）→ 改为**向后挪**到
        本轮结束边界：压缩段 = 完整第一轮（仍不切碎、且非空），避免「压缩段为空
        导致 token 永降不下来」。
    """
    n = len(messages)
    if idx <= 0 or idx >= n:
        return idx
    # 已在 round 边界，无需对齐
    if _is_round_boundary(messages, idx):
        return idx
    # idx 落在某轮内部：若该位置前一条无 round 信息（legacy），不挪交给旧补丁
    if messages[idx - 1].get("round_id") is None:
        return idx
    # 向前退到本轮起始（第一个边界），整轮归入保留段
    aligned = idx
    while aligned > 0 and not _is_round_boundary(messages, aligned):
        aligned -= 1
    if aligned > 0:
        return aligned
    # 退到 0（落在第一轮内部）：向后挪到本轮结束边界，压缩完整第一轮（不切碎、非空）
    fwd = idx
    while fwd < n and not _is_round_boundary(messages, fwd):
        fwd += 1
    return fwd


def serialize_messages_for_compress(messages: List[dict]) -> str:
    """将 OpenAI 格式消息列表序列化为压缩 prompt 用的文本"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        ts = msg.get("timestamp", "")

        # 多模态分段 list → 拼接 text part, 图片/媒体段丢弃占位; None → ""
        # 不归一化的话 L205 `list + " "` 会抛 TypeError，整个 T-FILE 压缩静默熄火
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif content is None:
            content = ""

        # 处理 tool_calls
        if msg.get("tool_calls"):
            tool_descs = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                tool_descs.append(f"[工具调用: {fn.get('name', '?')}]")
            content = (content or "") + " " + " ".join(tool_descs)

        # 处理 tool 结果
        if role == "tool":
            tool_name = msg.get("tool_call_id", "tool")
            content = f"[工具结果 {tool_name}]: {content}"

        # 时间戳简化
        time_str = ""
        if ts:
            if "T" in ts:
                time_str = ts.split("T")[1][:8]
            else:
                time_str = ts

        # 角色映射
        if role == "assistant":
            sender = "老板娘 [BOT]"
        elif role == "user":
            meta = msg.get("meta", {})
            sender = meta.get("sender_name", "用户")
            qq = meta.get("sender_qq", "")
            if qq:
                sender = f"{sender}({qq})"
        else:
            sender = role

        prefix = f"[{time_str}] " if time_str else ""
        lines.append(f"{prefix}{sender}: {content}")

    return "\n".join(lines)


# ========================
# TFileManager —— 核心 T 文件管理器
# ========================

class TFileManager:
    """Per-window T 文件管理器

    为每个对话窗口维护一份独立的 T 文件（JSON），
    包含 T1（压缩历史摘要）和 messages（未压缩的原文消息）。

    T 文件存放路径：QQ_data/checkpoints/{window_key}.json
    """

    def __init__(self):
        # Per-window 互斥锁
        self._locks: Dict[str, asyncio.Lock] = {}
        # S3 F1.8: per-window 压缩锁 dict（防止同一窗口并发压缩；不同窗口压缩并行）。
        # 替代旧 self._compressing: set（全窗口共享语义 → S5 多人并发瓶颈）。
        # 完全参照 self._recover_locks 模式（惰性建锁取锁）。同窗口压缩仍串行
        # （locked() 检测命中即跳过，保持旧「跳过不阻塞」语义），不同窗口各持各锁并行。
        self._compress_locks: Dict[str, asyncio.Lock] = {}
        # S4 R4(D6): per-window「round_segmenting」锁——record 单一压缩执行器的写入权
        # 边界。compose_record 的两阶段提交（temp 候选 → 进锁校验 generation → 原子写
        # record_state/record.md/锚点）全程持此锁。与 _compress_locks 分离：旧 T1 覆盖式
        # 压缩已退役，record 增量聚合是不同的写入器；S4 同步链路与 S5 BPC 后台压缩都只是
        # 这把锁的调用者（D6 单一写入器），共用同一把锁保证「压最老组/推锚点/写 record.md」
        # 这一底层动作的串行性，根治批3.5-A 号源/generation 回退坑。
        self._round_segment_locks: Dict[str, asyncio.Lock] = {}
        # Per-window 内存消息缓冲区（减少高频 I/O）
        self._msg_buffer: Dict[str, List[dict]] = {}
        # S3 F1.4: per-window WAL 临时键序号（message_id 缺失时兜底去重用，单调递增）
        self._wal_seq: Dict[str, int] = {}
        # S3 F2.2 / 批3.5-B: 本进程「认领」的 in-flight WAL 窗口集合。
        # buffer_message 一旦往某窗口写 WAL，该窗口的 WAL 即本进程 in-flight 镜像
        # （等待 flush），**不是**上次崩溃残留 → _do_recover 对这些窗口跳过 replay
        # 且不清 WAL（否则会与内存 buffer 的待 flush 消息重复落盘 / 丢失 WAL 兜底）。
        # flush/clear 后移除。批3.5-B 起 _do_recover replay 段实际据此守卫。
        self._wal_owned: set = set()
        # S3 F2.2: 已做过崩溃恢复的窗口集合（按需恢复，每窗口仅在首次 load 触发一次）
        self._recovered: set = set()
        # S3 F2.2: 恢复专用锁（与业务 _get_lock 分离）。
        # load() 可能由已持有业务锁的调用方触发（append_messages/flush_buffer 持锁后
        # 调 load），若恢复复用业务锁会与之死锁（asyncio.Lock 不可重入）。恢复内部
        # 全程不抢业务锁（仅 _load_t_file_raw / _append_messages_inner / save，皆无锁），
        # 故用独立锁串行化「同窗口并发首次 load」即可。
        self._recover_locks: Dict[str, asyncio.Lock] = {}
        # S4 批4 M4/D10: per-window hit 命中队列。命中**不在生成期实时写**（D10 防三方
        # 竞态：hit 写 vs compose 替换 round_groups vs S5 BPC）。record_hit 只把
        # (rg_id, hit_type, now_ts, now_round) 入此内存队列；生成结束 + 锁释放后由
        # compose_record_if_needed 锁内收尾（或独立 flush_hit_queue 收尾）统一落进
        # metadata.record_state.hit_table，根除并发覆盖。进程崩溃丢未 flush 的队列项
        # 可接受（hit 是软热度信号，丢失仅退化为纯 age 定档，符合 D9「崩溃缺失降级纯
        # age 不报错」）；已落盘的 hit_table 走 T 文件原子 save，纳入崩溃恢复。
        self._hit_queue: Dict[str, List[dict]] = {}
        # 确保 checkpoints 目录存在
        os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
        # S3 F2.2 步骤 1：启动期全扫 GC 半写临时文件（.t_file_*.tmp / .state_*.tmp）。
        # 仅这一项启动时全扫一次；窗口级 WAL replay 走按需恢复（见 load()）。
        try:
            gc_orphan_tmp_files(CHECKPOINTS_DIR)
        except Exception as e:  # noqa: BLE001 — GC 失败不阻断启动
            logger.error(f"[T-FILE] 启动 tmp GC 异常: {e}")

    def buffer_message(self, window_key: str, msg: dict) -> None:
        """纯内存追加消息到缓冲区 + 同步写 WAL（崩溃可恢复，约束 6 / F1.4）。

        消息会在下次 load() / flush_buffer() 时批量写入磁盘 T 文件。
        WAL 是兜底：进程在 flush 前崩溃时，重启 replay WAL（F2.2）补回这批消息。
        WAL append-only，flush_buffer 成功后清理（见 flush_buffer）。

        首次接触某窗口（本进程内）时，磁盘上若已有 WAL 文件，那是**上次进程崩溃
        的残留**（buffer_message 同步、无法 async replay）。

        S3 批3.5-B(first-touch-absorb / residual-absorbed-not-rewal / 抢标决策)：
          - 吸收残留消息进内存 buffer（随本批 flush 取号落盘，不丢）。
          - **重写残留到新 in-flight WAL**（按本进程新 seq）：吸收进内存后若不重写，
            二次崩溃（flush 前再挂）这批残留就永久丢（旧 WAL 已被吸收清掉）。重写后
            二次崩溃仍能 replay 补回。
          - **不抢标 _recovered**：让首次 load（flush_buffer 内部触发）仍走 _do_recover
            做 gen 校验 + dangling 自愈（抢标会永久屏蔽这两项）。该窗口随即被认领进
            _wal_owned，_do_recover 据此识别「本进程 in-flight WAL」跳过 replay（消息在
            内存 buffer，由 flush 唯一落盘），故 first-touch 路径不产生重复；万一 flush
            前二次崩溃，重写的 WAL 在新进程（窗口不在 _wal_owned）走 replay 补回。
        """
        first_touch = (
            window_key not in self._msg_buffer
            and window_key not in self._wal_owned
        )
        if window_key not in self._msg_buffer:
            self._msg_buffer[window_key] = []

        if first_touch:
            self._wal_seq[window_key] = 0
            # 吸收上次崩溃残留 WAL（若有），避免与本进程新 WAL 行混在同一文件
            residual = wal_read(CHECKPOINTS_DIR, window_key)
            if residual:
                residual_msgs = [
                    entry["msg"] for entry in residual
                    if isinstance(entry.get("msg"), dict)
                ]
                self._msg_buffer[window_key].extend(residual_msgs)
                # 清掉旧 WAL 文件，再按新 seq 把残留重写进 in-flight WAL（防二次崩溃丢）
                wal_clear(CHECKPOINTS_DIR, window_key)
                for rmsg in residual_msgs:
                    _seq = self._wal_seq[window_key]
                    self._wal_seq[window_key] = _seq + 1
                    wal_append(
                        CHECKPOINTS_DIR, window_key, rmsg,
                        wal_dedup_key(rmsg, window_key, _seq),
                    )
                logger.warning(
                    f"[T-FILE] buffer 首次接触 {window_key}: 吸收并重写残留 WAL "
                    f"{len(residual_msgs)} 条（随本批 flush 落盘，不抢标 _recovered）"
                )
            # 本进程认领该窗口 WAL（in-flight）。注意：**不**抢标 _recovered，
            # 让 _do_recover 仍做 gen 校验/自愈，重复靠落盘 receive_seq 去重挡。
            self._wal_owned.add(window_key)

        # 添加时间戳
        if "timestamp" not in msg:
            msg["timestamp"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._msg_buffer[window_key].append(msg)

        # ---- F1.4 WAL：同步追加一行（append-only，绝不重写整文件）----
        seq = self._wal_seq.get(window_key, 0)
        dedup_key = wal_dedup_key(msg, window_key, seq)
        self._wal_seq[window_key] = seq + 1
        wal_append(CHECKPOINTS_DIR, window_key, msg, dedup_key)

    async def flush_buffer(self, window_key: str) -> None:
        """将缓冲区中的消息批量写入 T 文件磁盘（带锁），落盘成功后清理 WAL（F1.4）。"""
        # S3 批3.5-B(flush-walclear-race)：pop pending 移入锁内。旧代码锁外 pop 后
        # await self.load 让出事件循环，并发 buffer_message 可往同窗口写新 WAL 行，
        # flush 结束 wal_clear 删整文件 → 新消息留内存却无 WAL 兜底（崩溃丢）。
        # pop 进锁内后，pop 与 wal_clear 之间不再让出，消除该并发窗口。
        async with self._get_lock(window_key):
            pending = self._msg_buffer.pop(window_key, [])
            if not pending:
                # S3 批3.5-B(flush-empty-wipes-residual-wal, critical)：buffer 空时
                # 绝不无条件 wal_clear。WAL 仍可能有【上次崩溃残留】（崩前未落盘消息）；
                # 旧代码直接删 = 永久丢。改为：若 WAL 文件存在，先触发崩溃恢复 replay
                # （_recover_window_if_needed 内部按 receive_seq 去重落盘；若是本进程
                # in-flight owned 则跳过 replay 且不删，留待真正有 buffer 时 flush）。
                wal_fp = wal_file_path(CHECKPOINTS_DIR, window_key)
                if os.path.exists(wal_fp):
                    # 恢复用独立 _recover_locks（不与本 _get_lock 死锁），replay 残留落盘
                    await self._recover_window_if_needed(window_key)
                else:
                    # 确无 WAL：清理本进程残留计数 / 认领标记即可
                    self._wal_seq.pop(window_key, None)
                    self._wal_owned.discard(window_key)
                return
            t_file = await self.load(window_key)
            # 路径 A（buffer flush）：真持久化 → 取号。
            # 注意：load 已触发 _do_recover，因本窗口在 _wal_owned（buffer_message
            # 已认领）故跳过 replay，pending 是唯一落盘来源，无需 dedup。
            t_file = self._append_messages_inner(
                t_file, pending, window_key=window_key, assign_numbers=True
            )
            await self.save(window_key, t_file)
            # F1.4：T 文件落盘成功后才清 WAL（顺序关键：先 save 再清，崩在中间下次仍
            # replay）。
            # S3 批3.5-B(flush-walclear-race)：pop 已进锁内消除 flush↔flush 重复落盘。
            # 但 buffer_message 是无锁同步热路径，可能在上面 await load/save 让出时插入，
            # 往同窗口 buffer 追加【pop 之后】的新消息并写新 WAL 行。直接 wal_clear 删
            # 整文件会丢这些新行的兜底（消息仍在 buffer 但二次崩溃前无 WAL）。
            # 处理：wal_clear 后，若 buffer 又积了新消息，按新 seq 重写它们的 WAL，
            # 保留兜底（这些消息归下次 flush 落盘）；否则彻底清理认领。
            wal_clear(CHECKPOINTS_DIR, window_key)
            leftover = self._msg_buffer.get(window_key) or []
            if leftover:
                self._wal_seq[window_key] = 0
                for lmsg in leftover:
                    _ls = self._wal_seq[window_key]
                    self._wal_seq[window_key] = _ls + 1
                    wal_append(
                        CHECKPOINTS_DIR, window_key, lmsg,
                        wal_dedup_key(lmsg, window_key, _ls),
                    )
                # 仍认领该窗口（in-flight），等下次 flush
            else:
                self._wal_seq.pop(window_key, None)
                self._wal_owned.discard(window_key)

    def _get_lock(self, window_key: str) -> asyncio.Lock:
        """获取 per-window 的 asyncio.Lock"""
        if window_key not in self._locks:
            self._locks[window_key] = asyncio.Lock()
        return self._locks[window_key]

    def _get_compress_lock(self, window_key: str) -> asyncio.Lock:
        """S3 F1.8: 获取 per-window 压缩锁（惰性建锁，参照 _recover_locks 模式）。

        与业务 _get_lock 分离：压缩是长耗时操作（含 flash_lite 网络调用），
        独立锁让「同窗口压缩互斥」与「同窗口读写 T 文件」解耦，且不同窗口压缩并行。
        """
        if window_key not in self._compress_locks:
            self._compress_locks[window_key] = asyncio.Lock()
        return self._compress_locks[window_key]

    def _get_round_segment_lock(self, window_key: str) -> asyncio.Lock:
        """S4 R4(D6): 获取 per-window「round_segmenting」锁（惰性建锁，参照
        _get_compress_lock 模式）。record 单一压缩执行器（compose_record 两阶段提交）
        持此锁。不同窗口各持各锁并行；同窗口 record 写入串行（S4 同步 + S5 BPC 共用）。
        """
        if window_key not in self._round_segment_locks:
            self._round_segment_locks[window_key] = asyncio.Lock()
        return self._round_segment_locks[window_key]

    def _file_path(self, window_key: str) -> str:
        """计算 T 文件路径"""
        safe_name = window_key.replace(":", "_")
        return os.path.join(CHECKPOINTS_DIR, f"{safe_name}.json")

    # ========================
    # 读/写
    # ========================

    async def _load_t_file_raw(self, window_key: str) -> dict:
        """读盘 + v1→v2 迁移 + 损坏兜底，返回纯 T 文件（**不 merge buffer、不触发恢复**）。

        是 load() 与崩溃恢复（_recover_window_if_needed）共用的底层读入口，
        本身不调用 self.load，避免恢复 ↔ load 递归。
        """
        fp = self._file_path(window_key)

        if not os.path.exists(fp):
            t_file = _create_empty_t_file(window_key)
            await self.save(window_key, t_file)
            logger.info(f"[T-FILE] 创建新 T 文件: {fp}")
            return t_file

        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)

            # S3 批3.5-D(nondict-json): 合法 JSON 但顶层非 dict(文件被截断成 [...]、
            # 裸数字/字符串)时，下方 data.get 会抛 AttributeError 穿透损坏兜底。
            # 显式判型并当损坏处理（参照 round_tracker.load_state 的同款防御）。
            if not isinstance(data, dict):
                raise TypeError(f"T 文件顶层非 dict: {type(data).__name__}")

            # S3 F1.1: 版本兼容检查 —— v1 自动迁移到 v2（绝不重建，约束 5）
            version = data.get("version")
            if version == 1:
                data = _migrate_v1_to_v2(data)
                await self.save(window_key, data)
            elif version != 2:
                # 未知版本（既非 1 也非 2）：补齐 metadata + message v2 字段，不删消息
                logger.warning(
                    f"[T-FILE] 未知版本({version})，补齐 v2 字段后沿用"
                )
                _ensure_metadata_v2_fields(data.setdefault("metadata", {}))
                # S3 批3.5-D(unknown-version-skips-message-legacy): 补 message-level
                # v2 字段 + legacy（version=None 丢版本的真 v1 文件落此分支时，旧消息
                # 也要补齐，否则字段缺失被 version=2 永久固化）。
                for _m in data.get("messages", []):
                    if isinstance(_m, dict):
                        _ensure_message_v2_fields(_m, legacy=True)
                data["version"] = 2
                await self.save(window_key, data)
            else:
                # version == 2: S3 批3.5-D(v2-fastpath-skips-metadata-backfill)
                # 批3a 前生成的 v2 文件 metadata 缺 generation 等后加字段，快路径补齐；
                # 仅当确有字段被补上才 save（避免每次 load 都写盘）。
                _md = data.setdefault("metadata", {})
                _before = len(_md)
                _ensure_metadata_v2_fields(_md)
                if len(_md) != _before:
                    await self.save(window_key, data)

            return data

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"[T-FILE] 文件损坏 {fp}: {e}")
            # 问题6修复: 保留损坏文件现场，而非直接覆盖
            try:
                import time as _time
                corrupt_path = f"{fp}.corrupt.{int(_time.time())}"
                os.rename(fp, corrupt_path)
                logger.warning(f"[T-FILE] 损坏文件已保留: {corrupt_path}")
            except OSError as rename_err:
                logger.warning(f"[T-FILE] 无法保留损坏文件: {rename_err}")
            t_file = _create_empty_t_file(window_key)
            # S3 批3.5-B(numbering-3)：损坏重建绝不让号源回退到 1（state.json 存活 +
            # .corrupt 残存历史号 → 否则重号）。从 state + .corrupt 取已见 max+1 续号。
            try:
                nr, ns = _recover_number_source_after_corruption(
                    CHECKPOINTS_DIR, window_key, fp
                )
                t_file["metadata"]["next_round_id"] = nr
                t_file["metadata"]["next_step_id"] = ns
                if nr > 1 or ns > 1:
                    logger.warning(
                        f"[T-FILE] 损坏重建号源续号 {window_key}: "
                        f"next_round_id={nr} next_step_id={ns}（绝不复用历史号）"
                    )
            except Exception as _nse:  # noqa: BLE001
                logger.error(f"[T-FILE] 损坏重建号源恢复失败 {window_key}: {_nse}")
            await self.save(window_key, t_file)
            return t_file

    async def load(self, window_key: str, *, merge_buffer: bool = True) -> dict:
        """加载 T 文件。不存在则创建空文件。

        如果文件损坏（JSON 解析失败），回退到空 T 并记录 error。
        merge_buffer=True（默认）：返回数据合并内存缓冲区中的未刷盘消息（**只读视图**用途，
        如 _extract / build_llm_contexts）。这些 buffer 消息以 assign_numbers=False merge
        进来，round_id 留 None——**绝不可被 save 回盘**（S4 批6 #2/#3/#7）。
        merge_buffer=False：跳过 _merge_buffer，返回纯磁盘视图（**落盘路径**专用，如 compose
        提交块 / 幂等迁移块 / flush_hit_queue）。这些路径 load→save 整份写盘，若 merge 了
        未取号 buffer 消息，会把 round_id=None 持久化进 T 文件（_rounds_from_messages 永远
        跳过 → 丢轮），且 buffer 未被 pop，下次 flush_buffer 重复落盘。故落盘前一律传 False。

        S3 F2.2：首次访问某窗口时按需触发崩溃恢复（replay WAL / gen 校验 / 自愈），
        恢复每窗口仅做一次（_recovered 集合去重），后续 load 走快路径。
        """
        # F2.2 按需崩溃恢复（每窗口首次 load 触发一次，内部已防重入）
        await self._recover_window_if_needed(window_key)

        data = await self._load_t_file_raw(window_key)
        if merge_buffer:
            return self._merge_buffer(window_key, data)
        return data

    async def _recover_window_if_needed(self, window_key: str) -> None:
        """S3 F2.2：某窗口首次访问时按需崩溃恢复（每窗口仅一次）。

        触发点决策 = (b) 按需恢复：老板娘多窗口，启动全扫慢；在窗口首次被
        load 访问时恢复该窗口最划算。唯一的全局动作（.tmp GC）已在 __init__
        启动期做过一次。

        恢复流程（设计 §3 F2.2）：
          (1) tmp GC —— 已在 __init__ 启动期全扫，此处不重复
          (2) replay {window}.buffer.wal.jsonl：读每行，按 message_id 与 T 文件已有
              消息去重，未落盘的经 _append_messages_inner 取号 append，成功后删 WAL
          (3) load T 文件 + state.json（由 _load_t_file_raw / load_state 完成）
          (4) generation 交叉校验：T 文件 metadata.generation vs state.generation，
              取大者修正小者（防两次独立写盘崩在中间导致的不一致）
          (5) 跑一次 _repair_tool_call_pairs 对 T 文件 messages 自愈（dangling 落盘修复）

        算法约束：不重算划轮（沿用 state）；round_id 全局单调绝不复用（崩后宁跳号
        不重号——号源 metadata.next_round_id 持久化在 T 文件，replay 续号即跳号不重号）；
        state.partial=open 且末尾已 assistant → 保持（待下次 user 闭合，不强制改写）。
        """
        if window_key in self._recovered:
            return
        # 用恢复专用锁（绝不用业务 _get_lock，否则与持业务锁的 load 调用方死锁）。
        rlock = self._recover_locks.get(window_key)
        if rlock is None:
            rlock = asyncio.Lock()
            self._recover_locks[window_key] = rlock
        async with rlock:
            # double-check：等锁期间可能已被另一并发首次 load 恢复完
            if window_key in self._recovered:
                return
            try:
                await self._do_recover(window_key)
            except Exception as e:  # noqa: BLE001 — 恢复失败不阻断主链路
                logger.error(f"[T-FILE] 崩溃恢复异常 {window_key}: {e}")
            finally:
                # 标记已恢复（即便异常也不反复重试拖垮主链路；
                # 残留问题留待下次进程启动 / 人工介入）
                self._recovered.add(window_key)

    async def _do_recover(self, window_key: str) -> None:
        """实际恢复逻辑（调用方已持有 window 锁）。"""
        wal_entries = wal_read(CHECKPOINTS_DIR, window_key)
        wal_file = wal_file_path(CHECKPOINTS_DIR, window_key)
        has_wal = os.path.exists(wal_file)

        # S3 批3.5-B：本进程 in-flight WAL 守卫。窗口在 _wal_owned 说明该 WAL 是本进程
        # buffer_message 写下的 in-flight 镜像（消息在内存 buffer，待 flush 落盘），
        # **不是**上次崩溃残留 → 绝不 replay（否则 replay 落盘 + flush 又落盘 = 重复，
        # 且末尾 wal_clear 会把内存里未落盘消息的 WAL 兜底删掉）。first-touch 吸收残留
        # 后认领 _wal_owned，故残留经此路径只由 flush 落盘一次。仅做 gen 校验 + 自愈。
        owned_inflight = window_key in self._wal_owned
        replay_entries = [] if owned_inflight else wal_entries

        # 无 WAL 残留：仍需做 gen 校验 + 自愈（崩在 save 之后、清 WAL 之前不会到这；
        # 但崩在 T 文件 save 与 state save 之间会留 gen 不一致，需校验）
        t_file = await self._load_t_file_raw(window_key)
        state = round_tracker.load_state(CHECKPOINTS_DIR, window_key)
        metadata = t_file.setdefault("metadata", {})
        _ensure_metadata_v2_fields(metadata)

        dirty = False

        # ---- (2) replay WAL ----
        if replay_entries:
            # S3 批3.5-B(replay-dedup-ignores-wal-key)：去重统一下沉到落盘入口
            # _append_messages_inner(dedup_against_existing=True)，按 receive_seq 主键
            # （覆盖 message_id=None 的 user/assistant 补录）+ message_id 兜底去重，
            # 不再在此用 message_id 预过滤（旧逻辑对 message_id=None 恒不去重 → 重复落盘）。
            to_replay: List[dict] = [
                entry["msg"] for entry in replay_entries
                if isinstance(entry.get("msg"), dict)
            ]
            before_n = len(t_file.get("messages", []))
            if to_replay:
                # 走唯一取号入口（与正常 append 合流），续 metadata.next_round_id
                # → round_id 跳号不重号；落盘前对 T 文件已有消息按 receive_seq 去重
                t_file = self._append_messages_inner(
                    t_file, to_replay, window_key=window_key,
                    assign_numbers=True, dedup_against_existing=True,
                )
                appended_n = len(t_file.get("messages", [])) - before_n
                if appended_n > 0:
                    # _append_messages_inner 已推进 generation 并 save_state；重读 state
                    state = round_tracker.load_state(CHECKPOINTS_DIR, window_key)
                    metadata = t_file["metadata"]
                    dirty = True
                    logger.warning(
                        f"[T-FILE] 崩溃恢复 replay WAL {window_key}: "
                        f"{appended_n}/{len(replay_entries)} 条补回（其余已落盘 receive_seq 去重）"
                    )
                else:
                    logger.info(
                        f"[T-FILE] 崩溃恢复 {window_key}: WAL {len(replay_entries)} 条全部已落盘，仅清理"
                    )

        # ---- (4) generation 交叉校验：取大者修正小者 ----
        t_gen = int(metadata.get("generation", 0) or 0)
        s_gen = int(state.get("generation", 0) or 0)
        if t_gen != s_gen:
            big = max(t_gen, s_gen)
            logger.warning(
                f"[T-FILE] generation 不一致 {window_key}: T={t_gen} state={s_gen} "
                f"→ 取大者 {big} 修正"
            )
            metadata["generation"] = big
            state["generation"] = big
            try:
                round_tracker.save_state(CHECKPOINTS_DIR, window_key, state)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[T-FILE] gen 校验 save_state 失败 {window_key}: {e}")
            dirty = True

        # ---- (5) 自愈：对落盘 messages 跑一次 dangling 修复 ----
        msgs = t_file.get("messages", [])
        if msgs:
            placeholder = (metadata.get("dangling_tool_placeholder_template")
                           or DANGLING_TOOL_PLACEHOLDER)
            repaired, repairs = _repair_tool_call_pairs(msgs, placeholder)
            if repairs:
                # 占位消息补齐 v2 字段（_repair 产物只有 role/tool_call_id/content）
                for m in repaired:
                    if isinstance(m, dict):
                        _ensure_message_v2_fields(m, legacy=False)
                t_file["messages"] = repaired
                metadata.setdefault("dangling_repair_history", []).extend(repairs)
                dirty = True
                logger.warning(
                    f"[T-FILE] 崩溃恢复自愈 {window_key}: dangling 修复 {len(repairs)} 处"
                )

        # ---- 落盘修复结果 + 清 WAL ----
        if dirty:
            await self.save(window_key, t_file)
        # S3 批3.5-B：owned_inflight（本进程 in-flight WAL）时绝不清 WAL / 不动 _wal_seq
        # ——那是 buffer_message 写的、待 flush 的镜像（消息尚在内存 buffer 未落盘），
        # 由 flush_buffer 负责清；此处清掉会让内存里未落盘消息丢失 WAL 兜底（二次崩溃丢）。
        if has_wal and not owned_inflight:
            # 顺序：先 save 再清 WAL（崩在中间下次仍可 replay，幂等去重）
            wal_clear(CHECKPOINTS_DIR, window_key)
            self._wal_seq.pop(window_key, None)

    def _merge_buffer(self, window_key: str, t_file: dict) -> dict:
        """将内存缓冲区中的消息合并到 T 文件数据中（纯内存，不写盘，只读视图）。

        S3 约束 1：本路径**绝不取号**（assign_numbers=False）。merge 只为构建
        load() 的临时视图（供 _extract_new_messages 比对 / build_llm_contexts），
        这批 buffer 消息的真号在 flush_buffer 时才唯一分配；若此处取号，同一条
        buffer 消息每次 load 都会重号。
        """
        pending = self._msg_buffer.get(window_key, [])
        if pending:
            t_file = self._append_messages_inner(
                t_file, pending, window_key=window_key, assign_numbers=False
            )
        return t_file

    async def save(self, window_key: str, t_file: dict) -> None:
        """原子保存 T 文件（先写临时文件再重命名）"""
        fp = self._file_path(window_key)
        dir_path = os.path.dirname(fp)
        os.makedirs(dir_path, exist_ok=True)

        t_file["metadata"]["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        try:
            # 写入临时文件
            fd, tmp_path = tempfile.mkstemp(
                dir=dir_path, prefix=".t_file_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(t_file, f, ensure_ascii=False, indent=2)
                    # S3 F1.7(a): flush + fsync 强制落盘，再 rename，
                    # 防「rename 已生效但内容仍在 OS page cache 未落盘」时断电丢数据。
                    f.flush()
                    os.fsync(f.fileno())

                # 原子重命名（Windows 上需要先删除目标）
                if os.path.exists(fp):
                    os.replace(tmp_path, fp)
                else:
                    os.rename(tmp_path, fp)
            except Exception:
                # 清理临时文件
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

        except Exception as e:
            logger.error(f"[T-FILE] 保存失败 {fp}: {e}")
            raise

    # ========================
    # 消息追加
    # ========================

    async def append_messages(
        self, window_key: str, new_messages: List[dict]
    ) -> dict:
        """追加新消息到 T 文件。返回更新后的 T 文件。

        会自动为消息添加 timestamp（如果没有）。
        公开 API，自带锁（向后兼容）。
        """
        if not new_messages:
            return await self.load(window_key)

        async with self._get_lock(window_key):
            t_file = await self.load(window_key)
            # 公开 API（真持久化）：取号
            t_file = self._append_messages_inner(
                t_file, new_messages, window_key=window_key, assign_numbers=True
            )
            await self.save(window_key, t_file)

        return t_file

    async def _append_messages_unlocked(
        self, window_key: str, t_file: dict, new_messages: List[dict]
    ) -> dict:
        """追加新消息到 T 文件（无锁版本）。

        供事务链内部调用（调用方已持有窗口锁，路径 B：on_llm_request extract）。
        注意：调用方必须自行调用 save() 持久化。
        返回更新后的 t_file（内存中）。
        S3 F1.3：真持久化路径 → 取号（与路径 A buffer flush 合流到唯一入口）。
        """
        if not new_messages:
            return t_file
        return self._append_messages_inner(
            t_file, new_messages, window_key=window_key, assign_numbers=True
        )

    def _append_messages_inner(
        self,
        t_file: dict,
        new_messages: List[dict],
        *,
        window_key: Optional[str] = None,
        assign_numbers: bool = False,
        dedup_against_existing: bool = False,
    ) -> dict:
        """追加消息的核心逻辑（纯内存操作 + 可选取号，无 asyncio 锁）。

        S3 F1.2/F1.3：本函数是「唯一取号入口」。
          - assign_numbers=True（真持久化路径：flush_buffer / _append_messages_unlocked
            / append_messages）：load_state → 逐条 round_tracker.assign_round →
            把 round_id/step_id/first_reply 写进 message v2 字段 → save_state；
            号源 metadata.next_round_id/next_step_id 由 assign_round 就地 +1，
            随 t_file save 持久化。
          - assign_numbers=False（只读合并视图：_merge_buffer）：绝不取号
            （否则同一条 buffer 消息每次 load 都重号），round_id/step_id 留 None。

        约束 1：buffer flush（路径 A）与 extract（路径 B）必须合流到本函数取号，
        防双号 / 跳号 / 倒序。

        S3 批3.5-B：``dedup_against_existing``（仅崩溃恢复落盘路径传 True）——
        这是「buffer/WAL 崩溃恢复去重」的【统一落点】。崩溃 replay / first-touch
        吸收残留 WAL 重写后再次 replay，都可能把同一条消息二次落盘；本函数在落盘
        前对 t_file 已有消息按 ``receive_seq``（F3.1 全局单调唯一纳秒戳，去重主键，
        覆盖 message_id=None 的 user/assistant 补录）+ ``message_id``（兜底，旧路径
        无 receive_seq 时）双键去重，已落盘的跳过不再 append/取号。
        正常 append（dedup_against_existing=False）不做去重：receive_seq 天然唯一，
        加去重纯属无谓开销。legacy 旧消息 receive_seq 缺省 0 / None，是不会被 replay
        的历史，不进入去重集合（避免把多条 receive_seq=0 的旧消息当成互相重复）。
        """
        now_iso_ms = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")

        # 取号准备：仅真持久化路径加载 state，确保 metadata v2 号源就位
        state = None
        if assign_numbers and window_key:
            state = round_tracker.load_state(CHECKPOINTS_DIR, window_key)
            metadata = t_file.setdefault("metadata", {})
            _ensure_metadata_v2_fields(metadata)

        # ---- S3 批3.5-B：崩溃恢复去重集合（receive_seq 主键 + message_id 兜底）----
        existing_rseq: set = set()
        existing_mid: set = set()
        if dedup_against_existing:
            for _m in t_file.get("messages", []):
                if not isinstance(_m, dict):
                    continue
                _rs = _m.get("receive_seq")
                # receive_seq=0/None 是 legacy 旧消息默认值，不是真去重键 → 不收集
                if isinstance(_rs, int) and _rs > 0:
                    existing_rseq.add(_rs)
                _mid = _m.get("message_id")
                if _mid is not None and _mid != "":
                    existing_mid.add(_mid)

        appended = 0
        for msg in new_messages:
            # ---- S3 批3.5-B：落盘前去重（仅崩溃恢复路径）----
            if dedup_against_existing:
                _rs = msg.get("receive_seq")
                _mid = msg.get("message_id")
                if isinstance(_rs, int) and _rs > 0 and _rs in existing_rseq:
                    continue  # receive_seq 已落盘（含 message_id=None 的补录）
                if _mid is not None and _mid != "" and _mid in existing_mid:
                    continue  # 兜底：message_id 已落盘（旧路径无 receive_seq）
                # 记入集合，防本批内部同键重复（同一条在 WAL 出现多行）
                if isinstance(_rs, int) and _rs > 0:
                    existing_rseq.add(_rs)
                if _mid is not None and _mid != "":
                    existing_mid.add(_mid)

            # ---- 构建 v2 存储格式 ----
            stored_msg = {
                "role": msg.get("role", "user"),
            }

            # content 可能是 str 或 None（tool_calls 消息）
            if msg.get("content") is not None:
                stored_msg["content"] = msg["content"]

            # 保存 tool_calls
            if msg.get("tool_calls"):
                stored_msg["tool_calls"] = msg["tool_calls"]

            # 保存 tool_call_id（tool role 消息）
            if msg.get("tool_call_id"):
                stored_msg["tool_call_id"] = msg["tool_call_id"]

            # 时间戳（v2 升毫秒；保留已带的 timestamp）
            stored_msg["timestamp"] = msg.get("timestamp", now_iso_ms)

            # 元数据（如果有，S1 sender_qq 兼容字段）
            if msg.get("meta"):
                stored_msg["meta"] = msg["meta"]

            # ---- v2 终态字段（F1.2）：先铺默认，再从传入 msg 取已知值 ----
            _ensure_message_v2_fields(stored_msg, legacy=False)
            # receive_seq / message_id / sender 由上游 buffer（F3.1）填，
            # 本批未铺路则留默认（0 / None / None）。
            if msg.get("receive_seq") is not None:
                stored_msg["receive_seq"] = msg["receive_seq"]
            if msg.get("message_id") is not None:
                stored_msg["message_id"] = msg["message_id"]
            if msg.get("sender") is not None:
                stored_msg["sender"] = msg["sender"]
            if msg.get("has_multimodal") is not None:
                stored_msg["has_multimodal"] = bool(msg.get("has_multimodal"))

            # ---- F1.3 取号：唯一入口，仅真持久化路径执行 ----
            if state is not None:
                assigned = round_tracker.assign_round(
                    stored_msg,
                    state,
                    metadata,
                    now_ts=time.time(),
                    msg_tokens=estimate_context_msg_tokens(stored_msg),
                    # round_max_steps/tokens：F6 配置键未建，暂用默认 30/8000
                    round_max_steps=round_tracker.DEFAULT_ROUND_MAX_STEPS,
                    round_max_tokens=round_tracker.DEFAULT_ROUND_MAX_TOKENS,
                )
                stored_msg["round_id"] = assigned["round_id"]
                stored_msg["step_id"] = assigned["step_id"]
                stored_msg["first_reply"] = assigned["first_reply"]

            t_file["messages"].append(stored_msg)
            appended += 1

        # total_messages_ever 按【实际落盘】条数推进（崩溃恢复去重跳过的不计）
        t_file["metadata"]["total_messages_ever"] += appended

        # ---- F2.2 generation 同步推进：仅真持久化路径（取号成功）才 ++ ----
        # T 文件 metadata.generation 与 state.generation 始终保持相等并同步前进，
        # 二者写盘是两次独立 I/O，崩在中间会出现不一致 → 恢复时取大者修正小者。
        # 取号前先以「两边较大者」为基准，消除历史不一致，再 +1。
        # S3 批3.5-B：崩溃恢复全部被去重（appended==0）时无实际落盘，不推进 gen / 不
        # 重写 state（避免无谓写盘；两边仍各自维持，下游 gen 校验另行兜底）。
        if state is not None and window_key and appended > 0:
            base_gen = max(
                int(t_file["metadata"].get("generation", 0) or 0),
                int(state.get("generation", 0) or 0),
            )
            new_gen = base_gen + 1
            t_file["metadata"]["generation"] = new_gen
            state["generation"] = new_gen
            # ---- 取号后持久化 state（metadata 随 t_file save 落盘）----
            try:
                round_tracker.save_state(CHECKPOINTS_DIR, window_key, state)
            except Exception as e:
                logger.error(f"[T-FILE] state.json 保存失败 {window_key}: {e}")

        return t_file

    # ========================
    # 构建 LLM Contexts
    # ========================

    def build_llm_contexts(
        self, t_file: dict, window_key: Optional[str] = None,
        record_cfg: Optional[dict] = None,
    ) -> List[dict]:
        """从 T 文件构建 OpenAI 格式 contexts（发送给主模型）。

        两种后端（S4 R3 / D7）：
          - **record 视图**（window_key 提供 + record 可用）：已聚合 round-group 按 D7
            定档公式 tier_for_group（轮龄为主 + 命中修正 + 滞回）逐组读对应档文本
            （full_text/summary_text/brief），末尾未聚合轮取 messages 原文。
            形如 [record 概要块(已聚合,分级) + 末尾原文(未聚合)]。
            record_cfg：D7 定档 / 接力配置（tier_*/hit_* ）；缺则 record 模块 DEFAULT 兜底。
          - **全量视图**（window_key=None 或 record 空/坏，fallback）：现状 T1 摘要
            + messages 全量原文，形如 [T1_user, T1_ack, msg1, msg2, ...]。

        ⚠️ **高危注入主路径**：context_mixin 注入端传 window_key 切 record 视图；
        checkpoint 内部触发判定 / 接力中止判定（compose_record_if_needed）**不传**
        window_key，维持全量视图旧行为（token 口径不变，不破坏接力判据）。

        **fallback 铁律（D1）**：record 路径任一环节异常 / round_groups 为空 / 文本读
        不出 → 静默回退全量视图，端到端绝不崩。record.md 是派生物，真理源是
        metadata.record_state（round_groups 直接带各组 tier 文本），故读取本身不依赖
        record.md 文件；window_key 提供时顺带 rebuild_index_if_stale 保 sidecar 同步
        （坏了重渲），但重渲失败也不影响主路径。

        dangling tool_calls 修复（S3 F1.5 读侧 last-mile）在两条路径汇合后无条件执行。
        """
        contexts = None

        # ---- record 视图（仅注入主路径传 window_key 时尝试）----
        if window_key:
            try:
                contexts = self._build_contexts_from_record(
                    t_file, window_key, record_cfg=record_cfg
                )
            except Exception as e:  # noqa: BLE001 — 注入主路径，任何异常都不许崩
                logger.warning(
                    f"[T-FILE] build_llm_contexts: record 视图构建异常 "
                    f"{window_key}: {e} → fallback 全量"
                )
                contexts = None

        # ---- fallback / 默认：全量视图（T1 + messages 全量）----
        if contexts is None:
            contexts = self._build_contexts_full(t_file)

        # ---- S3 F1.5: 读侧 last-mile dangling tool_calls 防御（C1 critical）----
        # 输出前无条件修复未配对的 tool_calls / tool 结果，防止 provider 400。
        placeholder = DANGLING_TOOL_PLACEHOLDER
        try:
            placeholder = (t_file.get("metadata", {}) or {}).get(
                "dangling_tool_placeholder_template"
            ) or DANGLING_TOOL_PLACEHOLDER
        except Exception:
            placeholder = DANGLING_TOOL_PLACEHOLDER

        contexts, repairs = _repair_tool_call_pairs(contexts, placeholder)
        if repairs:
            logger.warning(
                f"[T-FILE] build_llm_contexts: dangling tool_calls 修复 "
                f"{len(repairs)} 处 → {repairs}"
            )
            try:
                meta = t_file.setdefault("metadata", {})
                meta.setdefault("dangling_repair_history", []).extend(repairs)
            except Exception as _re:
                logger.debug(f"[T-FILE] dangling_repair_history 记录失败: {_re}")

        return contexts

    # --- 全量视图（fallback / 内部触发判定用，行为同 S3 旧 build_llm_contexts）---
    def _build_contexts_full(self, t_file: dict) -> List[dict]:
        """现状全量：T1 压缩摘要（user+assistant ACK 两条）+ messages 全量原文。

        record 不可用时的 fallback，也是 checkpoint 内部触发/接力判定的固定口径
        （token 不被 record 概要缩减 → 不破坏接力中止判据）。dangling 修复由调用方
        build_llm_contexts 统一收尾，此处只产原始序列。
        """
        contexts: List[dict] = []

        # 1. T1：压缩历史摘要
        t1 = t_file.get("T1", {}) or {}
        summary = t1.get("compressed_summary", "")
        if summary:
            contexts.append({
                "role": "user",
                "content": f"{T1_SUMMARY_PREFIX}\n{summary}",
            })
            contexts.append({
                "role": "assistant",
                "content": T1_ACK_CONTENT,
            })

        # 2. 原文消息
        for msg in t_file.get("messages", []):
            if not isinstance(msg, dict) or "role" not in msg:
                continue
            ctx_msg = {"role": msg["role"]}
            if msg.get("content") is not None:
                ctx_msg["content"] = msg["content"]
            if msg.get("tool_calls"):
                ctx_msg["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                ctx_msg["tool_call_id"] = msg["tool_call_id"]
            contexts.append(ctx_msg)

        return contexts

    # --- record 视图（S4 R3 / D7）：已聚合按分级定档读 + 末尾未聚合原文 ---
    def _build_contexts_from_record(
        self, t_file: dict, window_key: str,
        record_cfg: Optional[dict] = None,
    ) -> Optional[List[dict]]:
        """从 record 构建注入上下文：[已聚合 record 概要块(分级)] + [末尾未聚合原文]。

        返回 None 表示 record 不可用（round_groups 为空 / 无有效聚合组），由调用方
        fallback 全量。返回非 None（含空 list 上层会补 dangling 修复）表示走 record 视图。

        实现要点（D1：metadata 真理源，record.md 派生物 + D7 分级 + D8 防空洞）：
          - 已聚合组文本直接取自 record_state.round_groups 各组的 tier 文本
            （full_text/summary_text/...，render_record_md 同源），**不依赖 record.md
            文件 I/O**——record.md 坏了也读得出，根治「派生物坏 → 注入断」。
          - **分级定档（批3 D7）**：每组实际读哪一档由 record.tier_for_group 算
            （base=age 阶梯 + hit_score 升档 + 滞回），而非静态读组内 tier。now_round 取
            metadata.next_round_id-1（已分配最大轮）；hit_table 取 record_state.hit_table
            （批3 常空 → 纯 age 兜底，不报错）；record_cfg 提供 tier_*/hit_* 阈值（缺则 DEFAULT）。
          - **D8 防空洞**：组处于「已封板、水位之后、却无 summary」空洞态时（group_has_summary_gap）
            强制留 full，绝不降到尚未生成的 summary/brief（读空块）。
          - 末尾未聚合原文 = messages 里 round_id 数值 > 已聚合水位
            （max round_range[1]）的原始 message（保留 role/content/tool_calls/
            tool_call_id 结构，dangling 修复才有效）。legacy 占位 [0,0] 时水位=0，
            真轮 r1+ 全部作末尾原文（legacy 概要 + 全部真轮，不丢轮）。
          - 顺带 rebuild_index_if_stale 保 sidecar 与 record.md 同步（D1/D2，best-effort，
            失败仅 warning 不影响注入）。
        """
        meta = t_file.get("metadata", {}) or {}
        rec_state = meta.get("record_state", {}) or {}
        round_groups = rec_state.get("round_groups") or []

        # 有效聚合组：dict 且 round_range 可解析。无 → record 不可用，fallback。
        valid_groups = [
            g for g in round_groups
            if isinstance(g, dict) and self._rg_round_end(g) is not None
        ]
        if not valid_groups:
            return None

        # 已聚合水位：所有组 round_range 终点的最大值（含 legacy 占位 0）。
        watermark = _max_grouped_round_end(valid_groups)
        if watermark is None:
            return None

        # 分级定档所需上下文：now_round（已分配最大轮）/ hit_table / summary 水位。
        try:
            now_round = int(meta.get("next_round_id", 1) or 1) - 1
        except (TypeError, ValueError):
            now_round = None
        hit_table = rec_state.get("hit_table") or {}
        summary_wm = rec_state.get("summary_watermark_rg_id")
        import time as _time
        now_ts = _time.time()

        contexts: List[dict] = []

        # ---- (a) 已聚合 round-group：按 D7 分级定档逐组读对应档文本 ----
        # 定档 = record.tier_for_group（base age 阶梯 + hit 升档 + 滞回）；D8 空洞守护：
        # 「已封板/水位后/无 summary」的组强制 full 防读空块。概要块以 user/assistant ACK
        # 对注入（沿用 T1 注入契约，主模型理解为「历史记录摘要」）。
        block_parts: List[str] = []
        for g in valid_groups:
            tier = record.tier_for_group(
                g, now_round, hit_table, record_cfg,
                now_ts=now_ts, prev_tier=g.get("tier"),
            )
            # D8 防空洞：想降档到 summary/brief 但该组处于 summary 空洞 → 强制 full。
            if tier != record.TIER_FULL and record.group_has_summary_gap(g, summary_wm):
                tier = record.TIER_FULL
            # S4 批6 #9：回写算出的 tier 进内存视图组对象（方案 b，不持久化磁盘）。
            # 组 tier 字段原硬初始化为 'full' 且从不回写 → 滞回 prev_tier 恒为 full 失效。
            # 这里把本次真实定档写回内存 dict，使「同一 t_file 视图被同轮内再次 build」时
            # （如接力中止判据 build_llm_contexts(t_file) 二次读）滞回 prev 取到真实上次档。
            # tier 是读时时变量（依赖 now_round/hit），仅作滞回 prev 参考、每次仍重算，
            # 故不持久化到磁盘（避免锁外读路径回写 disk 快照与 compose 替换竞态，见 #9 验证）。
            g["tier"] = tier
            body = record._select_tier_body(g, tier)
            if not body:
                continue
            rr = g.get("round_range") or [None, None]
            title = g.get("title") or ""
            head = f"[{g.get('rg_id', '?')} 轮次{rr[0]}-{rr[1]} {tier}]"
            # 降档组（summary/brief）逐组标「(摘要)」：配合块首召回指针，让主模型逐组
            # 识别哪些是压缩档、避免把摘要当逐字原文复述（D7 不自知缺口修复）。
            if tier != record.TIER_FULL:
                head += "（摘要）"
            if title:
                head += f" {title}"
            block_parts.append(f"{head}\n{body}")

        if block_parts:
            record_text = "\n\n".join(block_parts)
            # 概要块开头统一加召回指针（T1_RECORD_RECALL_HINT）：告知主模型这是分级记录、
            # 非逐字原文，逐字原文须调 QQ_data_original 召回，严禁凭摘要编造（D7 缺口修复）。
            contexts.append({
                "role": "user",
                "content": f"{T1_SUMMARY_PREFIX}\n{T1_RECORD_RECALL_HINT}\n\n{record_text}",
            })
            contexts.append({
                "role": "assistant",
                "content": T1_ACK_CONTENT,
            })

        # ---- (b) 末尾未聚合原文：round_id 数值 > 水位的原始 message ----
        # 保留原 message 结构（含 tool_calls / tool_call_id），dangling 修复才生效。
        # legacy 无号消息（parse=-1）：水位 > -1 时不收（已被 legacy 概要覆盖）。
        for msg in t_file.get("messages", []):
            if not isinstance(msg, dict) or "role" not in msg:
                continue
            rn = round_tracker.parse_round_id(msg.get("round_id"))
            if rn <= watermark:
                continue  # 已聚合区间内（或无号），由概要块覆盖
            ctx_msg = {"role": msg["role"]}
            if msg.get("content") is not None:
                ctx_msg["content"] = msg["content"]
            if msg.get("tool_calls"):
                ctx_msg["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                ctx_msg["tool_call_id"] = msg["tool_call_id"]
            contexts.append(ctx_msg)

        # ---- (c) sidecar 同步（D1/D2 best-effort，失败不影响注入）----
        try:
            generation = int(meta.get("generation", 0) or 0)
            record.rebuild_index_if_stale(
                CHECKPOINTS_DIR, window_key, rec_state,
                generation=generation, logger=logger,
            )
        except Exception as _se:  # noqa: BLE001
            logger.debug(
                f"[T-FILE] {window_key}: rebuild_index_if_stale best-effort 失败 {_se}"
            )

        # ---- (d) 空视图 fallback（S4 批6 #6）：record 视图实质为空 → 触发全量 ----
        # valid_groups 非空但所有组 tier 文本读不出（block_parts 全空）且无尾部原文
        # （全部 message round_id <= watermark）时 contexts==[]。空 list != None 致
        # build_llm_contexts 不 fallback，直接把上下文清空（违反 D1：fallback 应降级全量
        # 而非清空）。这里把「文本读不出导致的空」并入 fallback 语义，返回 None 触发全量。
        # 注意：返回 None 时上层走 _build_contexts_full；若真是全新空窗口（valid_groups 空）
        # 已在前面 1671 行返回 None，不会走到这里；此处仅兜「有组但读空」的局部损坏态，
        # 全量 fallback 也为空时上层不再二次 fallback（已在全量分支），无死循环。
        if not contexts:
            logger.warning(
                f"[T-FILE] {window_key}: record 视图产出空"
                f"（tier 文本全空且无尾部原文）→ fallback 全量"
            )
            return None

        return contexts

    @staticmethod
    def _rg_round_end(group: dict) -> Optional[int]:
        """取单个 round-group 的 round_range 终点（数值）；不可解析返回 None。"""
        rr = group.get("round_range")
        if not isinstance(rr, (list, tuple)) or len(rr) < 2:
            return None
        e = rr[1]
        if isinstance(e, bool):
            return None
        if isinstance(e, int):
            return e
        if isinstance(e, str):
            n = round_tracker.parse_round_id(e)
            return n if n >= 0 else None
        return None

    # ========================
    # 构建 FlashLite 上下文
    # ========================

    def build_flashlite_context(
        self, t_file: dict, max_tokens: int = 8000
    ) -> str:
        """从 T 文件构建 FlashLite 触发判断用的文本上下文

        采用从尾部向前截断到 max_tokens 的策略。
        """
        parts = []

        # T1 摘要
        t1 = t_file.get("T1", {})
        summary = t1.get("compressed_summary", "")
        if summary:
            parts.append(f"[对话历史摘要]\n{summary}\n")

        # 原文消息（从最新向前构建，满 max_tokens 时停止）
        messages = t_file.get("messages", [])
        msg_lines = []
        running_tokens = estimate_tokens("\n".join(parts)) if parts else 0

        # 从尾部向前遍历
        for msg in reversed(messages):
            line = self._format_msg_for_flashlite(msg)
            line_tokens = estimate_tokens(line)

            if running_tokens + line_tokens > max_tokens:
                break

            msg_lines.insert(0, line)
            running_tokens += line_tokens

        if msg_lines:
            parts.append("\n".join(msg_lines))

        return "\n".join(parts)

    @staticmethod
    def _format_msg_for_flashlite(msg: dict) -> str:
        """将单条消息格式化为 FlashLite 可读的文本行"""
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        # 同 serialize_messages_for_compress 的归一化:
        # `or ""` 在 list (truthy) 上失效会导致 list 原样穿透到 f-string 渲染成
        # `[{'type':'text', 'text':...}]` 字面串污染 FlashLite 触发判定 (实测 210/341 条触发)
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif content is None:
            content = ""
        ts = msg.get("timestamp", "")

        # 时间简化
        time_str = ""
        if ts and "T" in str(ts):
            time_str = str(ts).split("T")[1][:8]

        # 角色映射
        if role == "assistant":
            sender = "老板娘 [BOT]"
        elif role == "user":
            meta = msg.get("meta", {})
            name = meta.get("sender_name", "用户")
            qq = meta.get("sender_qq", "")
            sender = f"{name}({qq})" if qq else name
        elif role == "tool":
            tool_id = msg.get("tool_call_id", "")
            return f"[{time_str}] [工具结果 {tool_id}]: {content[:200]}"
        else:
            sender = role

        # tool_calls 简化
        if msg.get("tool_calls"):
            tool_names = [
                tc.get("function", {}).get("name", "?")
                for tc in msg["tool_calls"]
            ]
            content = f"[调用工具: {', '.join(tool_names)}]"

        prefix = f"[{time_str}] " if time_str else ""
        return f"{prefix}{sender}: {content}"

    # ========================
    # 压缩核心逻辑（S4 R2 退役：旧 T1 覆盖式 blob 压缩已下线，转 record 增量聚合）
    # ========================

    # --- S4 R2(D5): legacy T1 迁移（幂等纯函数） ---
    @staticmethod
    def _migrate_legacy_t1_to_record_group(t_file: dict) -> Tuple[bool, dict]:
        """S4 R2(D5)：把现存旧 T1（compressed_summary 单 blob，覆盖式压缩产物）封档成
        「第 0 号 legacy round-group」（rg000000，legacy_rg=True，sealed=True，不再压）。

        旧覆盖式压缩与 record per-group 累加是两套不相容状态：同段历史会被压两次、
        last_compressed_round_id 语义撞车。R2 上线把现存 T1 一次性封进
        record_state.round_groups 当冷冻历史段，record 增量从其后真轮号开始累加。

        **幂等**（最高危约束）：
          - 已迁移（round_groups 里已存在 legacy_rg 组）→ 直接返回 (False, t_file)，
            绝不重复迁移、绝不重压。
          - T1 为空（无 compressed_summary）→ 无需迁移，返回 (False, t_file)。

        **不丢不重压**：迁移只把 T1.compressed_summary 文本搬进 legacy 组的 summary_text/
        full_text，**不动 T1 本体、不动 messages 原文数组**——build_llm_contexts 仍照旧从
        T1 渲染注入（R3 批3 才改注入端），保证迁移不破坏现有上下文注入与压缩率。

        legacy 组 round_range 推断（存量 T1 多无 round 覆盖信息）：
          - 起点固定 0（历史冷冻段从最初算起）。
          - 终点取 record_state.last_compressed_round_id 的数值（若有，S3 后压缩留下）；
            否则取 0（纯 blob 无轮号覆盖信息，占位区间 [0,0]）。
        legacy 组之后的真轮号（r1+）由 validate 规则 4「legacy 段后允许一次跳变」放行。

        返回 (changed, t_file)：changed=True 表示本次产生迁移（调用方据此落盘）。
        """
        t1 = t_file.get("T1") or {}
        summary = (t1.get("compressed_summary") or "").strip()
        meta = t_file.setdefault("metadata", {})
        rec_state = meta.setdefault("record_state", {})
        rg_list = rec_state.get("round_groups")
        if not isinstance(rg_list, list):
            rg_list = []
            rec_state["round_groups"] = rg_list

        # 幂等：已有 legacy_rg 组 → 不重复迁移
        for g in rg_list:
            if isinstance(g, dict) and g.get("legacy_rg"):
                return False, t_file

        # 无旧 T1 摘要 → 无需迁移（首次 compose 直接从 r1 起）
        if not summary:
            return False, t_file

        # 推断 legacy 区间终点
        lcr = rec_state.get("last_compressed_round_id")
        end_n = round_tracker.parse_round_id(lcr) if lcr else -1
        if end_n < 0:
            end_n = 0

        legacy_group = {
            "rg_id": record.format_rg_id(0),       # rg000000，legacy 专用号
            "round_range": [0, end_n],
            "tier": record.TIER_SUMMARY,           # 历史段以 summary 档存（已是压缩摘要）
            "sealed": True,                        # 封档，不再压
            "legacy_rg": True,                     # 历史冷冻标记
            "full_text": summary,                  # 无更细 full，full 回退用摘要
            "summary_text": summary,
            "title": "历史对话摘要（迁移自旧 T1）",
            "token_est": int(t1.get("token_count", 0) or 0),
        }
        # legacy 组放在 round_groups 最前（第 0 号）
        rg_list.insert(0, legacy_group)
        # 聚合锚指向 legacy 组（record 增量从其后开始）
        rec_state["last_grouped_rg_id"] = legacy_group["rg_id"]
        meta["record_legacy_migrated_at"] = datetime.now().strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        return True, t_file

    async def compress_if_needed(
        self,
        window_key: str,
        t_file: dict,
        flash_lite_caller: Callable,
        token_limit: int = 50000,
        keep_recent: int = 10,
        compress_front_ratio: float = 0.7,
        cooldown_seconds: int = 300,
        target_min: float = 0.20,
        target_max: float = 0.40,
        record_cfg: Optional[dict] = None,
    ) -> Tuple[dict, Optional[dict]]:
        """S4 R2(D5)：旧 T1 覆盖式 blob 压缩【已退役】。

        本入口保留以兼容 context_mixin Phase2 现有调用点（签名不变，只新增可选
        record_cfg），但内部不再做覆盖式前段 70% 压缩。改为：① 幂等迁移现存 T1 →
        legacy round-group；② 转调 record 单一压缩执行器 compose_record_if_needed
        （D6 两阶段提交）。

        旧参数 keep_recent / compress_front_ratio / target_min/max 在 record 路径下不再
        生效（record 用 rg_target_rounds / force_seal 阈值），保留仅为签名兼容。
        token_limit 作为 record 触发阈值的兜底默认（record_cfg.record_compose_token_limit 优先）。
        record_cfg：面板组装的 record 配置 dict（rg_*/compress_delta_floor 等）；None 时
        record 全走 DEFAULT 兜底（仍可跑，只是面板调参不生效）。

        返回 (t_file, result)：result 为本次 record 聚合摘要（dict）或 None（未触发/无产出）。
        """
        return await self.compose_record_if_needed(
            window_key=window_key,
            t_file=t_file,
            flash_lite_caller=flash_lite_caller,
            cfg=record_cfg,
            token_limit=token_limit,
        )

    # --- S4 R4(D6): record 单一压缩执行器 + round_segmenting 锁两阶段提交 ---
    def _make_segment_llm_caller(
        self, flash_lite_caller: Callable, window_key: str, main_loop
    ):
        """把 _call_flash_lite（async，返回纯文本）包成 compose_record 要的同步契约
        llm_caller(batch_rounds, cfg) -> List[GroupSpec]。

        关键（跨 loop 安全）：record.compose_record 是纯同步逻辑，会被 run_in_executor
        丢到【worker 线程】跑。但 _call_flash_lite 内部用 self._session（aiohttp
        ClientSession，绑定**主 loop**），在 worker 线程新建 loop 跑会因 session 跨 loop
        报错。故这里用 asyncio.run_coroutine_threadsafe 把 flash_lite 协程**提交回主
        loop** 执行，worker 线程阻塞 .result() 等结果——aiohttp 始终在主 loop，安全。

        prompt 构造 / JSON 解析全走 record 纯逻辑（build_segment_prompt /
        parse_group_specs），跨 provider 健壮容错。caller 抛异常 / 返回空 → compose 兜底。
        """
        import concurrent.futures as _cf

        def caller(batch_rounds, cfg):
            prompt = record.build_segment_prompt(batch_rounds, cfg)
            # 动态 max_output_tokens：按本批字符量给足分段输出空间（约 1/2 原文）。
            total_chars = sum(int(r.get("char_len", 0) or 0) for r in batch_rounds)
            max_out = max(1024, min(8192, total_chars // 3 + 512))
            coro = flash_lite_caller(
                prompt, max_output_tokens=max_out, window_key=window_key
            )
            # 提交回主 loop 执行（aiohttp session 在主 loop）；worker 线程阻塞等结果。
            fut = asyncio.run_coroutine_threadsafe(coro, main_loop)
            try:
                raw = fut.result(timeout=120)
            except _cf.TimeoutError:
                fut.cancel()
                raise RuntimeError("flash_lite 分段调用超时(120s)")
            specs = record.parse_group_specs(raw, batch_rounds, logger=logger)
            return specs
        return caller

    async def compose_record_if_needed(
        self,
        window_key: str,
        t_file: dict,
        flash_lite_caller: Callable,
        cfg: Optional[dict] = None,
        token_limit: int = 50000,
    ) -> Tuple[dict, Optional[dict]]:
        """S4 R4(D6)：record 增量聚合的单一压缩执行器，持 round_segmenting 锁两阶段提交。

        触发策略（本批最简，BPC 后台留 S5）：请求体 token 超阈值（cfg.record_compose_token_limit
        或 token_limit）→ 同步进入接力聚合。

        接力链（D6）：连续多次 compose 直到任一中止判据命中：
          (a) compose 无新增产出（wrote=False）；
          (b) 连续 compose 后请求体 token 降量 < compress_delta_floor（默认 200，防巨图死循环）——
              **判据是『降量不足』而非『仍超阈值』**；
          (c) 接力次数达 record_max_relay_rounds（默认 3，次数兜底）。

        两阶段提交（防批3.5-A 号源/generation 回退）：
          1. 锁外算候选：record.compose_record 产出新 round_groups + 已写 record.md.tmp→正式
             （候选隔离，validate 门禁不过绝不覆盖）。
          2. 进 round_segmenting 锁 → 重新 load 磁盘最新 t_file → 取 max(候选 generation,
             磁盘 generation) 校验：候选若基于陈旧 generation 仍可提交，但只把 record_state
             的边界表/锚点【单调】合并进磁盘最新 t_file（last_grouped/last_compressed 取数值大者），
             绝不用陈旧快照整份覆盖磁盘 metadata 号源。
          3. 锁内 save（原子 mkstemp→fsync→replace）落盘。
        区分可逆 / 不可逆回滚边界：record.md 已被 compose 原子写（不可逆侧，但派生物可重渲）；
        record_state 锚点更新在锁内事务（可逆：失败则磁盘维持原状）。

        compose 失败（LLM down / 门禁拒收）→ 维持现状不破坏（批2a 兜底已保证不写盘）。
        """
        cfg = cfg if isinstance(cfg, dict) else {}
        # 1) 触发判定：请求体 token 是否超阈值
        candidate_ctx = self.build_llm_contexts(t_file)
        if not candidate_ctx:
            return t_file, None
        trig_limit = int(
            cfg.get("record_compose_token_limit", token_limit) or token_limit
        )
        cur_tokens = sum(estimate_context_msg_tokens(m) for m in candidate_ctx)
        if cur_tokens <= trig_limit:
            return t_file, None

        # 2) 互斥：同窗口 record 写入串行（locked 即跳过，保持「不阻塞」语义）
        seg_lock = self._get_round_segment_lock(window_key)
        if seg_lock.locked():
            logger.info(f"[RECORD] {window_key}: 另一 record 聚合进行中，跳过")
            return t_file, None

        # 2.5) S4 R2(D5): 首次触发先幂等迁移现存旧 T1 → legacy round-group。
        # 在 round_segment 锁 + 业务锁内做，保证迁移落盘与后续 compose 不被并发撕裂。
        # 幂等：已有 legacy_rg 组 / 无旧 T1 → 不产生变更、不重复迁移、不重压。
        async with seg_lock:
            async with self._get_lock(window_key):
                # S4 批6 #7：落盘路径用 merge_buffer=False，避免把未取号 buffer 消息
                # （round_id=None）随 migrated save 持久化进 T 文件 + 下次 flush 重复落盘。
                disk_t = await self.load(window_key, merge_buffer=False)
                migrated, disk_t = self._migrate_legacy_t1_to_record_group(disk_t)
                if migrated:
                    await self.save(window_key, disk_t)
                    legacy_end = (
                        (disk_t.get("metadata", {}) or {})
                        .get("record_state", {})
                        .get("round_groups", [{}])[0]
                        .get("round_range", [0, 0])[1]
                    )
                    logger.warning(
                        f"[RECORD] {window_key}: 旧 T1 已迁移为 legacy round-group "
                        f"(rg000000, round_range=[0,{legacy_end}], sealed)"
                    )
                t_file = disk_t  # 接力基于迁移后磁盘最新

        delta_floor = int(cfg.get("compress_delta_floor", 200) or 200)
        max_relay = int(cfg.get("record_max_relay_rounds", 3) or 3)

        relay = 0
        last_tokens = cur_tokens
        total_groups_added = 0
        any_wrote = False

        while relay < max_relay:
            relay += 1
            # ---- 阶段一（锁外）：算候选 + 候选隔离写 record.md ----
            prev_state = (t_file.get("metadata", {}) or {}).get("record_state", {})
            messages = t_file.get("messages", [])
            loop = asyncio.get_running_loop()
            caller = self._make_segment_llm_caller(
                flash_lite_caller, window_key, loop
            )
            try:
                compose_res = await loop.run_in_executor(
                    None,
                    lambda: record.compose_record(
                        CHECKPOINTS_DIR, window_key, messages, prev_state,
                        caller, cfg,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"[RECORD] {window_key}: compose 执行异常 {e}")
                break

            if not compose_res.wrote:
                # 无产出（无新增 / LLM 失败 / 门禁拒收）→ 接力中止 (a)
                if compose_res.errors:
                    logger.info(
                        f"[RECORD] {window_key}: compose 未写盘 "
                        f"(fallback={compose_res.fallback}) {compose_res.errors}"
                    )
                break

            # ---- 阶段二（进锁）：两阶段提交 generation 校验 + 锚点单调合并 ----
            committed = False
            async with seg_lock:
                async with self._get_lock(window_key):
                    # S4 批6 #2：落盘路径用 merge_buffer=False。本提交块（含下方 disk-ahead
                    # 作废分支的 save）会整份写盘 disk_t，若 merge 了未取号 buffer 消息，
                    # round_id=None 被持久化（_rounds_from_messages 永远跳过 → 丢轮）且
                    # buffer 未 pop，下次 flush_buffer 重复落盘。
                    disk_t = await self.load(window_key, merge_buffer=False)
                    disk_meta = disk_t.setdefault("metadata", {})
                    disk_rec = disk_meta.setdefault("record_state", {})

                    # generation 取 max 单调回写：append 路径取号会推进磁盘 generation；
                    # compose 不取号、不应让 metadata.generation 回退（批3.5-A 铁律：号源
                    # 只进不退）。save() 不改 generation，故这里显式取 max 守住。
                    cand_gen = int(
                        (t_file.get("metadata", {}) or {}).get("generation", 0) or 0
                    )
                    disk_gen = int(disk_meta.get("generation", 0) or 0)
                    disk_meta["generation"] = max(cand_gen, disk_gen)

                    # ★A① 单调守卫（防 round_groups 整份替换吞并发结果）：候选是基于阶段一
                    # 快照算的「全量 round_groups」。若进锁后磁盘的已聚合最大轮终点已 >
                    # 候选的（说明并发 compose / 迁移在阶段一期间推进了磁盘边界表），整份替换
                    # 会回退/吞掉磁盘更新的组 → 作废本候选（可逆回滚：不写边界表，本次接力跳过，
                    # 下一轮接力基于磁盘最新重算）。绝不让陈旧候选覆盖更超前的磁盘边界表。
                    cand_end = _max_grouped_round_end(compose_res.round_groups)
                    disk_end = _max_grouped_round_end(disk_rec.get("round_groups") or [])
                    if (disk_end is not None and cand_end is not None
                            and disk_end > cand_end):
                        logger.warning(
                            f"[RECORD] {window_key}: 阶段二检测磁盘边界表更超前 "
                            f"(disk_end={disk_end} > cand_end={cand_end})，作废本候选不覆盖"
                        )
                        disk_meta["record_state"] = disk_rec
                        await self.save(window_key, disk_t)  # 仅落 generation 的 max
                        t_file = disk_t
                        # 不计入 any_wrote；中止接力（磁盘已被并发推进，让其接管）
                        break

                    # 锚点单调合并：last_grouped_rg_id（数值大者）
                    new_grouped = compose_res.last_grouped_rg_id
                    if new_grouped is not None:
                        old_grouped = disk_rec.get("last_grouped_rg_id")
                        if (old_grouped is None
                                or record.parse_rg_id(new_grouped)
                                > record.parse_rg_id(old_grouped)):
                            disk_rec["last_grouped_rg_id"] = new_grouped

                    # ★S4 批4 D9 重编号 key 迁移（必须在边界表替换【前】算）：compose 对
                    # 回滚重写窗口内的组用 _next_rg_num 重新编号（rg_id 软号变，硬 round_id
                    # 不变）。老 hit_table 的 key 可能在新 round_groups 里已不存在 → 直接替换
                    # 边界表会让老 hit 悬空。先按 round_range 重叠把老 hit 迁移到新 rg_id。
                    old_groups_for_hit = disk_rec.get("round_groups") or []
                    old_hit_table = disk_rec.get("hit_table") or {}
                    migrated_hit = record.migrate_hit_table_on_renumber(
                        old_hit_table, old_groups_for_hit, compose_res.round_groups
                    )

                    # 边界表替换：候选是「全量新 round_groups」（kept + 新组，已含 legacy +
                    # sealed 前缀）。上方单调守卫已确认磁盘未更超前，此替换不丢并发结果。
                    disk_rec["round_groups"] = compose_res.round_groups
                    disk_rec["hit_table"] = migrated_hit  # 迁移后的 hit_table 落定

                    # ★S4 批4 D10 收尾事务：边界表替换 + hit 迁移完成后，在【同一锁内同一
                    # save】把内存 hit 队列 flush 进 hit_table。这是「根除三方竞态」的关键——
                    # hit 写 / compose 替换 round_groups / BPC 都在这把 round_segment 锁内
                    # 串行，hit 落盘永远基于最新 round_groups 现算 rg_id（队列存硬 round_id）。
                    self._flush_hit_queue_into(disk_rec, window_key)

                    # last_compressed_round_id 单调推进 = 已聚合到的最大 round 终点。
                    if cand_end is not None:
                        new_lcr = round_tracker.format_round_id(cand_end)
                        old_lcr = disk_rec.get("last_compressed_round_id")
                        if (old_lcr is None
                                or cand_end
                                > round_tracker.parse_round_id(old_lcr)):
                            disk_rec["last_compressed_round_id"] = new_lcr

                    disk_meta["record_state"] = disk_rec
                    await self.save(window_key, disk_t)
                    t_file = disk_t  # 后续接力基于磁盘最新
                    committed = True

            if not committed:
                break

            any_wrote = True
            total_groups_added += 1

            # ---- 接力中止判据 ----
            new_ctx = self.build_llm_contexts(t_file)
            new_tokens = sum(estimate_context_msg_tokens(m) for m in new_ctx)
            delta = last_tokens - new_tokens
            logger.info(
                f"[RECORD] {window_key}: 接力#{relay} token {last_tokens}→{new_tokens} "
                f"(Δ={delta})"
            )
            # (b) 降量不足（防巨图死循环）——非「仍超阈值」
            if delta < delta_floor:
                logger.info(
                    f"[RECORD] {window_key}: 接力中止 token 降量 {delta} "
                    f"< floor {delta_floor}"
                )
                break
            last_tokens = new_tokens
            # 仍超阈值才继续接力；已降到阈值下则自然停
            if new_tokens <= trig_limit:
                break

        if not any_wrote:
            return t_file, None

        result = {
            "mode": "record_compose",
            "relay_rounds": relay,
            "groups_committed": total_groups_added,
            "token_before": cur_tokens,
            "token_after": last_tokens,
        }
        logger.info(
            f"[RECORD] {window_key}: record 聚合完成 relay={relay} "
            f"committed={total_groups_added} token {cur_tokens}→{last_tokens}"
        )
        return t_file, result

    # ========================
    # S4 批4 M4 / D9 / D10：hit 命中队列 + 收尾事务落盘
    # ========================
    def record_hit(
        self,
        window_key: str,
        *,
        round_int: Optional[int] = None,
        rg_id: Optional[str] = None,
        hit_type: str = "raw",
        now_ts: Optional[float] = None,
        now_round: Optional[int] = None,
    ) -> bool:
        """D10：登记一次命中——**只入内存队列，不实时写盘**（防三方竞态）。

        命中信号（S4 批4 原文命中线）：主模型调 QQ_data_original 查历史原文 → 命中的
        round 所属 round-group 打 hit。队列项**优先存硬 round_int**（compose 重编号后
        rg_id 会变，round_id 永不变）；落盘时（compose 收尾 / flush_hit_queue）按最新
        round_groups 现算 rg_id，根除「队列里 rg_id 失效」。也允许直接传 rg_id（record
        命中线 S7 用，本批占位）。round_int 与 rg_id 至少给一个，否则忽略。

        参数：
          round_int : 命中的硬 round_id 整数（首选，落盘现算 rg_id）。
          rg_id     : 直接指定 round-group（次选；round_int 缺时用）。
          hit_type  : 'raw'（原文召回，强）/ 'record'（被动读，弱）。
          now_ts    : 命中时刻（秒）；None → time.time()。
          now_round : 命中时的当前最大轮（D10 hit_keep 锁定窗口锚）。
        返回 True=已入队。
        """
        if round_int is None and not rg_id:
            return False
        if hit_type not in (record.HIT_TYPE_RAW, record.HIT_TYPE_RECORD):
            hit_type = record.HIT_TYPE_RAW
        ts = float(now_ts) if now_ts is not None else time.time()
        item = {
            "round_int": int(round_int) if round_int is not None else None,
            "rg_id": rg_id,
            "hit_type": hit_type,
            "ts": ts,
            "now_round": (
                int(now_round)
                if isinstance(now_round, int) and not isinstance(now_round, bool)
                else None
            ),
        }
        self._hit_queue.setdefault(window_key, []).append(item)
        logger.debug(
            f"[RECORD-HIT] {window_key}: 入队 round={round_int} rg={rg_id} "
            f"type={hit_type}（队列 {len(self._hit_queue[window_key])} 项，待收尾落盘）"
        )
        return True

    def _flush_hit_queue_into(self, record_state: dict, window_key: str) -> int:
        """把 window_key 的 hit 队列 flush 进 record_state.hit_table（**调用方持锁**）。

        必须在 round_segment 锁 + 业务锁内调用，且 record_state 已是磁盘最新（含本次
        compose 替换后的 round_groups）。队列项按最新 round_groups 现算 rg_id（round_int
        优先）→ apply_hit_to_table 累加。落盘由调用方的 save 统一完成（同一事务）。
        清空已 flush 的队列。返回成功落定的命中条数。
        """
        queue = self._hit_queue.get(window_key)
        if not queue:
            return 0
        hit_table = record_state.get("hit_table")
        if not isinstance(hit_table, dict):
            hit_table = {}
            record_state["hit_table"] = hit_table
        groups = record_state.get("round_groups") or []
        applied = 0
        deferred: List[dict] = []  # 命中早于聚合：round 还在末尾未聚合区 → 回填等将来
        for item in queue:
            rg_id = item.get("rg_id")
            round_int = item.get("round_int")
            # 优先按硬 round_int 现算 rg_id（compose 重编号无关，round_id 不变）。
            if round_int is not None:
                resolved = record.round_id_to_rg_id(round_int, groups)
                if resolved:
                    rg_id = resolved
            if not rg_id:
                # 命中的 round 尚未聚合进任何组（落末尾未聚合原文区）。**不丢弃**：回填队列，
                # 待该 round 将来被 compose 聚合进组后再落盘（防「命中早于聚合 → hit 永久丢」
                # 的软缺陷）。仅当 item 连 round_int 都没有（无锚的无效项）才真正丢弃。
                if round_int is not None:
                    deferred.append(item)
                continue
            record.apply_hit_to_table(
                hit_table, rg_id, item.get("hit_type", "raw"),
                item.get("ts", time.time()), item.get("now_round"),
            )
            applied += 1
        # 回填未聚合命中（带上限，防极端下队列无限堆积——超限丢最老的，保留最近热度）。
        _HIT_DEFER_CAP = 256
        if len(deferred) > _HIT_DEFER_CAP:
            deferred = deferred[-_HIT_DEFER_CAP:]
        self._hit_queue[window_key] = deferred
        if applied or deferred:
            logger.debug(
                f"[RECORD-HIT] {window_key}: 收尾落定 {applied} 命中进 hit_table"
                f"（{len(deferred)} 条命中早于聚合，回填待将来）"
            )
        return applied

    async def flush_hit_queue(self, window_key: str) -> int:
        """无 compose 触发时的【独立收尾路径】：持锁把 hit 队列 flush 进 hit_table 并落盘。

        compose_record_if_needed 已在其锁内事务顺带 flush（与边界表替换同一 save，无竞态）。
        但命中往往发生在「请求体未超阈值、本轮不触发 compose」的常态轮，那些命中会一直
        攒在队列里。本方法供 context_mixin 在每轮收尾（注入完成后）调用一次，确保命中及时
        落盘、不无限堆积。持 round_segment 锁 + 业务锁（与 compose 同锁串行，仍无竞态）。
        无队列项 → 不 load/不 save、直接返回 0。返回落定命中条数。
        """
        if not self._hit_queue.get(window_key):
            return 0
        seg_lock = self._get_round_segment_lock(window_key)
        async with seg_lock:
            async with self._get_lock(window_key):
                # 再检查（进锁前可能被 compose 收尾清空）
                if not self._hit_queue.get(window_key):
                    return 0
                # S4 批6 #3：落盘路径用 merge_buffer=False。本路径 applied>0 时 save 整份
                # 写盘 disk_t；若 merge 未取号 buffer 消息（round_id=None），会随之持久化
                # → 丢轮 + 下次 flush 重复落盘。hit 命中在活跃群常见，触发面比 compose 更广。
                disk_t = await self.load(window_key, merge_buffer=False)
                disk_meta = disk_t.setdefault("metadata", {})
                disk_rec = disk_meta.setdefault("record_state", {})
                applied = self._flush_hit_queue_into(disk_rec, window_key)
                if applied:
                    disk_meta["record_state"] = disk_rec
                    await self.save(window_key, disk_t)
                return applied

    # ========================
    # 数据库写入（面板统计）
    # ========================

    @staticmethod
    async def _save_to_db(
        window_key: str,
        compressed_content: str,
        original_count: int,
        compression_ratio: float,
        token_estimate: int,
    ):
        """将压缩结果写入 checkpoint_history 表（供面板统计）"""
        if not os.path.exists(DB_PATH):
            return

        parts = window_key.split(":", 1)
        window_type = "group" if parts[0] == "GroupMessage" else "private"
        window_id = parts[1] if len(parts) > 1 else window_key

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """INSERT INTO checkpoint_history
                       (window_type, window_id, compressed_content,
                        original_msg_range_start, original_msg_range_end,
                        compression_ratio, token_estimate, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        window_type,
                        window_id,
                        compressed_content,
                        0,  # 不再用 msg ID 追踪范围
                        original_count,
                        compression_ratio,
                        token_estimate,
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    ),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"[CHECKPOINT] 保存统计到 DB 失败: {e}")


# ========================
# 保留的 CheckpointManager（兼容旧接口，逐步废弃）
# ========================

class CheckpointManager:
    """CHECKPOINT 压缩管理器（v1 兼容层）

    保留 get_stats() 供面板查询，其余功能已迁移到 TFileManager。
    """

    def __init__(
        self,
        token_limit: int = 50000,
        keep_recent: int = 10,
        target_compression_min: float = 0.10,
        target_compression_max: float = 0.35,
    ):
        self.token_limit = token_limit
        self.keep_recent = keep_recent
        self.target_compression_min = target_compression_min
        self.target_compression_max = target_compression_max

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算 token 数（代理到模块级函数）"""
        return estimate_tokens(text)

    @staticmethod
    def estimate_message_tokens(msg: Dict[str, Any]) -> int:
        """估算单条消息 token 数（v1 格式：messages.db 行）"""
        tokens = 0
        content = msg.get("content_text", "")
        if content:
            tokens += estimate_tokens(content)
        sender = msg.get("sender_name", "")
        if sender:
            tokens += estimate_tokens(sender) + 2
        if msg.get("has_image"):
            image_urls = msg.get("image_urls")
            if image_urls:
                try:
                    urls = json.loads(image_urls) if isinstance(image_urls, str) else image_urls
                    tokens += len(urls) * IMAGE_TOKEN_ESTIMATE
                except (json.JSONDecodeError, TypeError):
                    tokens += IMAGE_TOKEN_ESTIMATE
        return max(tokens, 1)

    async def get_stats(self, window_id: Optional[str] = None) -> Dict:
        """获取 CHECKPOINT 统计信息"""
        if not os.path.exists(DB_PATH):
            return {"error": "数据库不存在"}

        stats = {}
        async with aiosqlite.connect(DB_PATH) as db:
            if window_id:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM checkpoint_history WHERE window_id = ?",
                    (window_id,),
                )
                stats["checkpoints"] = (await cursor.fetchone())[0]

                cursor = await db.execute(
                    "SELECT AVG(compression_ratio) FROM checkpoint_history WHERE window_id = ?",
                    (window_id,),
                )
                avg = (await cursor.fetchone())[0]
                stats["avg_compression"] = round(avg, 3) if avg else 0
            else:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM checkpoint_history"
                )
                stats["total_checkpoints"] = (await cursor.fetchone())[0]

        return stats
