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
            # S3 占位状态字典（S4/S5/S6 消费，S3 留空）
            "record_state": {"last_compressed_round_id": None},
            "bpc_state": {},
            "concurrency_state": {},
        },
    }


# ========================
# S3 F1.1: schema v1 → v2 迁移
# ========================

# message-level v2 字段终态默认值（S3 一次定到 v2 终态，含 S4-S7 占位）
# S3 实际写值的：round_id/step_id/first_reply/timestamp/receive_seq/message_id/
#               sender/has_multimodal；其余（compressed/rg_id/recalled）留默认。
_MESSAGE_V2_DEFAULTS = {
    "round_id": None,
    "step_id": None,
    "first_reply": False,
    "receive_seq": 0,
    "message_id": None,
    "sender": None,
    "has_multimodal": False,
    "compressed": False,
    "rg_id": None,
    "recalled": False,
}

# metadata-level v2 新增字段默认值（迁移 / 兜底补齐共用）
_METADATA_V2_DEFAULTS = {
    "next_round_id": 1,
    "next_step_id": 1,
    # S3 F2.2: generation 交叉校验代号（与 state.json 同步推进，恢复取大者修正小者）
    "generation": 0,
    "record_state": {"last_compressed_round_id": None},
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
    """为 metadata 补齐 v2 新增字段（已有字段保留，如 dangling_repair_history）。"""
    import copy
    for k, default in _METADATA_V2_DEFAULTS.items():
        if k not in metadata:
            metadata[k] = copy.deepcopy(default)
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
        # Per-window 内存消息缓冲区（减少高频 I/O）
        self._msg_buffer: Dict[str, List[dict]] = {}
        # S3 F1.4: per-window WAL 临时键序号（message_id 缺失时兜底去重用，单调递增）
        self._wal_seq: Dict[str, int] = {}
        # S3 F2.2: 本进程「认领」的 in-flight WAL 窗口集合。
        # buffer_message 一旦往某窗口写 WAL，该窗口的 WAL 即本进程 in-flight 镜像
        # （等待 flush），**不是**上次崩溃残留 → load 触发的恢复对这些窗口跳过 replay
        # （否则会与内存 buffer 的 _merge_buffer 视图重复计数）。flush/clear 后移除。
        self._wal_owned: set = set()
        # S3 F2.2: 已做过崩溃恢复的窗口集合（按需恢复，每窗口仅在首次 load 触发一次）
        self._recovered: set = set()
        # S3 F2.2: 恢复专用锁（与业务 _get_lock 分离）。
        # load() 可能由已持有业务锁的调用方触发（append_messages/flush_buffer 持锁后
        # 调 load），若恢复复用业务锁会与之死锁（asyncio.Lock 不可重入）。恢复内部
        # 全程不抢业务锁（仅 _load_t_file_raw / _append_messages_inner / save，皆无锁），
        # 故用独立锁串行化「同窗口并发首次 load」即可。
        self._recover_locks: Dict[str, asyncio.Lock] = {}
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
        的残留**（buffer_message 同步、无法 async replay）：把残留消息吸收进内存
        buffer（随本批一起正常 flush 取号落盘，不丢），清掉旧 WAL，再从本条起重写
        本进程的 in-flight WAL。重复由 _append_messages_inner 的 message_id 去重兜底。
        """
        first_touch = (
            window_key not in self._msg_buffer
            and window_key not in self._wal_owned
        )
        if window_key not in self._msg_buffer:
            self._msg_buffer[window_key] = []

        if first_touch:
            # 吸收上次崩溃残留 WAL（若有），避免与本进程新 WAL 行混在同一文件
            residual = wal_read(CHECKPOINTS_DIR, window_key)
            if residual:
                for entry in residual:
                    rmsg = entry.get("msg")
                    if isinstance(rmsg, dict):
                        self._msg_buffer[window_key].append(rmsg)
                logger.warning(
                    f"[T-FILE] buffer 首次接触 {window_key}: 吸收残留 WAL "
                    f"{len(residual)} 条（随本批 flush 落盘）"
                )
                wal_clear(CHECKPOINTS_DIR, window_key)
            # 本进程认领该窗口 WAL（in-flight），并标记已恢复（无需 load 再 replay）
            self._wal_owned.add(window_key)
            self._recovered.add(window_key)
            self._wal_seq[window_key] = 0

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
        pending = self._msg_buffer.pop(window_key, [])
        if not pending:
            # buffer 空但可能残留空 WAL（极端情况）：顺手清掉，重置 seq / owned
            wal_clear(CHECKPOINTS_DIR, window_key)
            self._wal_seq.pop(window_key, None)
            self._wal_owned.discard(window_key)
            return
        async with self._get_lock(window_key):
            t_file = await self.load(window_key)
            # 路径 A（buffer flush）：真持久化 → 取号
            t_file = self._append_messages_inner(
                t_file, pending, window_key=window_key, assign_numbers=True
            )
            await self.save(window_key, t_file)
        # F1.4：T 文件落盘成功后才清 WAL（顺序关键：先 save 再清，崩在中间下次仍 replay）
        wal_clear(CHECKPOINTS_DIR, window_key)
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
            await self.save(window_key, t_file)
            return t_file

    async def load(self, window_key: str) -> dict:
        """加载 T 文件。不存在则创建空文件。

        如果文件损坏（JSON 解析失败），回退到空 T 并记录 error。
        返回数据已包含内存缓冲区中的未刷盘消息。

        S3 F2.2：首次访问某窗口时按需触发崩溃恢复（replay WAL / gen 校验 / 自愈），
        恢复每窗口仅做一次（_recovered 集合去重），后续 load 走快路径。
        """
        # F2.2 按需崩溃恢复（每窗口首次 load 触发一次，内部已防重入）
        await self._recover_window_if_needed(window_key)

        data = await self._load_t_file_raw(window_key)
        return self._merge_buffer(window_key, data)

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

        # 无 WAL 残留：仍需做 gen 校验 + 自愈（崩在 save 之后、清 WAL 之前不会到这；
        # 但崩在 T 文件 save 与 state save 之间会留 gen 不一致，需校验）
        t_file = await self._load_t_file_raw(window_key)
        state = round_tracker.load_state(CHECKPOINTS_DIR, window_key)
        metadata = t_file.setdefault("metadata", {})
        _ensure_metadata_v2_fields(metadata)

        dirty = False

        # ---- (2) replay WAL ----
        if wal_entries:
            # T 文件已落盘消息的 message_id 集合（去重依据）
            existing_ids = {
                m.get("message_id")
                for m in t_file.get("messages", [])
                if isinstance(m, dict) and m.get("message_id") is not None
            }
            to_replay: List[dict] = []
            for entry in wal_entries:
                msg = entry.get("msg")
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("message_id")
                # 有 message_id 且已在 T 文件 → 已落盘，跳过（去重）
                if mid is not None and mid in existing_ids:
                    continue
                to_replay.append(msg)
                if mid is not None:
                    existing_ids.add(mid)  # 防 WAL 内部同 message_id 重复行

            if to_replay:
                # 走唯一取号入口（与正常 append 合流），续 metadata.next_round_id
                # → round_id 跳号不重号
                t_file = self._append_messages_inner(
                    t_file, to_replay, window_key=window_key, assign_numbers=True
                )
                # _append_messages_inner 已推进 generation 并 save_state；重读 state
                state = round_tracker.load_state(CHECKPOINTS_DIR, window_key)
                metadata = t_file["metadata"]
                dirty = True
                logger.warning(
                    f"[T-FILE] 崩溃恢复 replay WAL {window_key}: "
                    f"{len(to_replay)}/{len(wal_entries)} 条补回（其余已落盘去重）"
                )
            else:
                logger.info(
                    f"[T-FILE] 崩溃恢复 {window_key}: WAL {len(wal_entries)} 条全部已落盘，仅清理"
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
        if has_wal:
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
        """
        now_iso_ms = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")

        # 取号准备：仅真持久化路径加载 state，确保 metadata v2 号源就位
        state = None
        if assign_numbers and window_key:
            state = round_tracker.load_state(CHECKPOINTS_DIR, window_key)
            metadata = t_file.setdefault("metadata", {})
            _ensure_metadata_v2_fields(metadata)

        for msg in new_messages:
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

        t_file["metadata"]["total_messages_ever"] += len(new_messages)

        # ---- F2.2 generation 同步推进：仅真持久化路径（取号成功）才 ++ ----
        # T 文件 metadata.generation 与 state.generation 始终保持相等并同步前进，
        # 二者写盘是两次独立 I/O，崩在中间会出现不一致 → 恢复时取大者修正小者。
        # 取号前先以「两边较大者」为基准，消除历史不一致，再 +1。
        if state is not None and window_key:
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

    def build_llm_contexts(self, t_file: dict) -> List[dict]:
        """从 T 文件构建 OpenAI 格式 contexts（发送给主模型）

        返回格式：[T1_user, T1_ack, msg1, msg2, ...]
        """
        contexts = []

        # 1. T1：压缩历史摘要
        t1 = t_file.get("T1", {})
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
            ctx_msg = {"role": msg["role"]}

            if msg.get("content") is not None:
                ctx_msg["content"] = msg["content"]

            if msg.get("tool_calls"):
                ctx_msg["tool_calls"] = msg["tool_calls"]

            if msg.get("tool_call_id"):
                ctx_msg["tool_call_id"] = msg["tool_call_id"]

            contexts.append(ctx_msg)

        # S3 F1.5: 读侧 last-mile dangling tool_calls 防御（C1 critical）
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
    # 压缩核心逻辑
    # ========================

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
    ) -> Tuple[dict, Optional[dict]]:
        """检查并执行压缩

        三重守卫：
          ① total_tokens > token_limit
          ② len(candidate) > keep_recent
          ③ 距上次压缩 > cooldown_seconds

        Returns:
            (更新后的 t_file, 压缩结果 dict 或 None)
        """
        # 构建候选上下文
        candidate = self.build_llm_contexts(t_file)

        if not candidate:
            return t_file, None

        # ① 估算总 token
        total_tokens = sum(estimate_context_msg_tokens(m) for m in candidate)

        if total_tokens <= token_limit:
            return t_file, None

        # ② 消息数量检查（基于 T 文件原始消息数，而非含 T1 的 candidate 数）
        raw_msg_count = len(t_file.get("messages", []))
        if raw_msg_count <= keep_recent:
            logger.debug(
                f"[CHECKPOINT] {window_key}: token {total_tokens} > {token_limit} "
                f"但原始消息数 {raw_msg_count} ≤ keep_recent {keep_recent}，跳过"
            )
            return t_file, None

        # ③ 冷却期
        last_time_str = t_file.get("T1", {}).get("last_compress_time", "")
        if last_time_str:
            try:
                last_time = datetime.fromisoformat(last_time_str).timestamp()
                if (time.time() - last_time) < cooldown_seconds:
                    logger.debug(
                        f"[CHECKPOINT] {window_key}: 冷却期内 "
                        f"(上次 {last_time_str})，跳过"
                    )
                    return t_file, None
            except ValueError:
                pass  # 时间解析失败，忽略冷却期

        # ④ 压缩互斥检查：同一窗口不允许并发压缩（S3 F1.8：per-window 锁取代全局 set）
        compress_lock = self._get_compress_lock(window_key)
        if compress_lock.locked():
            logger.info(
                f"[CHECKPOINT] {window_key}: 另一个压缩正在进行中，跳过"
            )
            return t_file, None

        # 三重守卫 + 互斥检查全部通过，取锁并执行压缩。
        # 用非阻塞 acquire 风格：locked() 已判过，此处必然立即获得（同窗口压缩串行；
        # 极端并发下若被抢先取走，acquire 会等待——但 locked() 检测已大幅收窄该窗口）。
        await compress_lock.acquire()
        try:  # M-1: finally 保证释放压缩锁
            logger.info(
                f"[CHECKPOINT] 触发压缩: {window_key}, "
                f"总 token {total_tokens} > {token_limit}, "
                f"消息 {len(candidate)} 条（原始 {raw_msg_count} 条）"
            )

            # 检测是否存在旧 T1 摘要（占 candidate 前 2 条）
            has_previous_summary = (
                len(candidate) > 0
                and candidate[0].get("role") == "user"
                and T1_SUMMARY_PREFIX in (candidate[0].get("content") or "")
            )
            t1_count_in_candidate = 2 if has_previous_summary else 0

            # 计算压缩范围（排除 T1 消息对后，对原始消息按比例压缩）
            available_for_compress = raw_msg_count - keep_recent
            if available_for_compress <= 0:
                logger.debug(
                    f"[CHECKPOINT] {window_key}: 排除保留后无可压缩消息，跳过"
                )
                return t_file, None

            original_compress_count = max(1, int(available_for_compress * compress_front_ratio))
            raw_messages = t_file.get("messages", [])

            # 语义完整性（legacy 兜底）：确保不切开 user-assistant 对话对。
            # round_id=None 的旧迁移消息无 round 信息，靠这条原 user/assistant 补丁防切。
            # 如果分割点正好在 user 消息后（下一条是 assistant 回复），多包含 1 条。
            # S3 批3.5-C2(C-3 candidate-raw 残留): 判定改用 raw_messages 下标，与最终切片
            # (to_compress/remaining 均 raw 系)一致——candidate 因 _repair 插占位/删 orphan
            # 长度漂移，用 candidate[_split_idx] 会读到错位消息致 ±1 误判。
            if original_compress_count < len(raw_messages):
                next_msg = raw_messages[original_compress_count]
                if next_msg.get("role") == "assistant" and original_compress_count < available_for_compress:
                    prev_msg = raw_messages[original_compress_count - 1] if original_compress_count > 0 else None
                    if prev_msg and prev_msg.get("role") == "user":
                        original_compress_count += 1

            # S3 F1.6：在 user-assistant 防切（legacy 兜底）之上叠加 round 边界对齐。
            # original_compress_count 是对 t_file["messages"] 的切分下标（见下方
            # remaining_messages = messages[original_compress_count:]）。对齐后切分点
            # 落在完整 round 边界——绝不把某轮 first_reply 切到该轮中间。
            # 同轮的 user+assistant+tool 共享 round_id，对齐天然也保证 tool 对完整。
            aligned_count = _align_compress_count_to_round_boundary(
                raw_messages, original_compress_count
            )
            # S3 批3.5-C(forward-align-overshoots-keep-recent): align 向后挪(退到 0 时)
            # 在「单个大 partial 轮覆盖到列表末尾」场景会返回 n(连续 user 刷屏 / 限流
            # 不回复时 round_tracker 把所有消息归同一 partial 轮)，越过
            # available_for_compress → remaining=[] → keep_recent 最近上下文被压光。
            # 夹回上限保住最近 keep_recent 条(此场景宁可切分点不在 round 边界)。
            _clamped = min(aligned_count, available_for_compress)
            clamp_hit = _clamped != aligned_count  # 是否真发生 overshoot 夹回
            aligned_count = _clamped
            # S3 批3.5-C2(C-2 orphan tool): clamp 夹回的切分点可能落在 step 中间——若
            # remaining[0] 是 tool 结果(其配对 assistant 已被压走)，读侧
            # _repair_tool_call_pairs 会把它当 orphan 静默删除致 tool 结果丢失。向前回退
            # 切分点到 step 边界(remaining[0] 非 tool 且其前一条非 assistant.tool_calls)；
            # 仅当找到有效边界(>0)才采用，否则保 clamp 值靠读侧 _repair 兜底。
            if clamp_hit:
                _safe = aligned_count
                while _safe > 0:
                    _rem0 = raw_messages[_safe] if _safe < len(raw_messages) else None
                    _prev = raw_messages[_safe - 1]
                    _rem0_tool = bool(_rem0) and _rem0.get("role") == "tool"
                    _prev_tc = _prev.get("role") == "assistant" and _prev.get("tool_calls")
                    if _rem0_tool or _prev_tc:
                        _safe -= 1
                    else:
                        break
                if _safe > 0:
                    aligned_count = _safe
            if aligned_count != original_compress_count:
                logger.info(
                    f"[CHECKPOINT] {window_key}: 压缩切分点调整 "
                    f"{original_compress_count} → {aligned_count} "
                    f"({'overshoot夹回+step对齐' if clamp_hit else 'F1.6 round边界对齐'})"
                )
                original_compress_count = aligned_count

            # S3 批3.5-C(candidate-raw-index-desync): to_compress 的原始消息部分从
            # raw(t_file messages)切，与 remaining_messages(下方 raw 系)同下标系。
            # candidate=build_llm_contexts 内 _repair_tool_call_pairs 插占位/删 orphan
            # 会改变长度，若用 candidate[compress_count] 切则与 raw 系错位 → 被压缩段与
            # 保留段错配 → 静默丢/重一条消息。旧 T1 摘要仍取 candidate 前缀(滚动压缩)。
            t1_prefix = candidate[:t1_count_in_candidate]
            to_compress = t1_prefix + raw_messages[:original_compress_count]

            # 序列化待压缩内容
            messages_text = serialize_messages_for_compress(to_compress)
            compress_tokens = estimate_tokens(messages_text)

            # 构建压缩 prompt
            prompt = build_compress_prompt(
                messages_text=messages_text,
                original_tokens=compress_tokens,
                target_min_ratio=target_min,
                target_max_ratio=target_max,
                has_previous_summary=has_previous_summary,
            )

            # 调用 Flash Lite（动态 max_output_tokens 硬保证压缩率上限）
            raw_max = max(100, int(compress_tokens * target_max))
            delta = max(50, int(raw_max * 0.15))  # 15% 余量，模型通常不会写满上限
            dynamic_max_tokens = raw_max + delta
            logger.info(
                f"[CHECKPOINT] 压缩 max_tokens: {dynamic_max_tokens} "
                f"(原文 {compress_tokens} × {target_max} + Δ{delta})"
            )

            t0 = time.monotonic()
            try:
                compressed_text = await flash_lite_caller(prompt, max_output_tokens=dynamic_max_tokens, window_key=window_key)
                latency = (time.monotonic() - t0) * 1000
            except Exception as e:
                logger.error(f"[CHECKPOINT] 压缩调用失败: {e}")
                return t_file, None

            if not compressed_text or not compressed_text.strip():
                logger.warning("[CHECKPOINT] Flash Lite 返回空结果，跳过")
                return t_file, None

            # 压缩率验证
            compressed_tokens = estimate_tokens(compressed_text)
            actual_ratio = compressed_tokens / max(compress_tokens, 1)

            if actual_ratio < target_min:
                logger.warning(
                    f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 低于目标 "
                    f"{target_min:.0%}，摘要可能过于简略 "
                    f"(原文 {compress_tokens} → {compressed_tokens} tokens)"
                )
            elif actual_ratio > target_max:
                logger.warning(
                    f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 高于目标 "
                    f"{target_max:.0%}，摘要可能保留过多细节"
                )
            else:
                logger.info(
                    f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} ✓ "
                    f"(目标 {target_min:.0%}~{target_max:.0%})"
                )

            # 计算实际被压缩的原始消息数（不含 T1 消息对）
            original_msgs_compressed_count = original_compress_count
            # 加上之前已经压缩的
            total_original_compressed = (
                t_file["T1"].get("original_msg_count", 0)
                + original_msgs_compressed_count
            )

            # 计算剩余的 messages（从原始消息数组中跳过被压缩的部分）
            remaining_messages = t_file["messages"][original_compress_count:]
            # 记录压缩前快照的消息总数（用于 Save 时合并中间到达的消息）
            pre_compress_msg_count = len(t_file["messages"])

            # S3 F1.6：算被压缩段最后一轮 round_id（用于更新 record_state）。
            # 倒扫被压缩段找最后一条非 None round_id（legacy 消息 round_id=None 跳过）。
            # S3 批3.5-C2(C-1): 若 clamp overshoot 把某轮 rN 切成两半(尾部留在
            # remaining)，不能把 rN 标为「已完整压缩」——否则 F2.x record 增量按
            # last_compressed_round_id 切片会漏写 rN 残留。跳过「尾部仍在 remaining」的
            # 被切轮，取被压缩段里最后一个【完整】压缩的轮。
            _remaining_first_rid = (
                remaining_messages[0].get("round_id") if remaining_messages else None
            )
            compressed_last_round_id = None
            for _m in reversed(t_file["messages"][:original_compress_count]):
                _rid = _m.get("round_id")
                if _rid is not None:
                    if _rid == _remaining_first_rid:
                        continue  # 该轮被切，尾部在 remaining，不算完整压缩
                    compressed_last_round_id = _rid
                    break

            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

            # 更新 T 文件
            old_history = t_file["T1"].get("compress_history", [])
            new_history_entry = {
                "time": now_iso,
                "before_tokens": compress_tokens,
                "after_tokens": compressed_tokens,
                "ratio": round(actual_ratio, 4),
                "msgs_compressed": original_msgs_compressed_count,
            }

            # 限制 compress_history 最多保留最近 20 条
            compress_history = (old_history + [new_history_entry])[-20:]

            t_file["T1"] = {
                "compressed_summary": compressed_text,
                "token_count": compressed_tokens,
                "compression_ratio": round(actual_ratio, 4),
                "original_msg_count": total_original_compressed,
                "compression_count": t_file["T1"].get("compression_count", 0) + 1,
                "last_compress_time": now_iso,
                "compress_history": compress_history,
            }
            t_file["metadata"]["total_compressions"] = (
                t_file["metadata"].get("total_compressions", 0) + 1
            )

            # S3 F1.6：更新 record_state.last_compressed_round_id = 已压缩的最后一轮。
            # 一致性合约（设计 §F2.1）：单调不减，绝不回退（崩溃恢复时 record 进度可信）。
            # round_id 形如 r000123，零填充字典序==数值序，可安全字符串比较取 max。
            if compressed_last_round_id is not None:
                rec_state = t_file["metadata"].setdefault("record_state", {})
                prev_lcr = rec_state.get("last_compressed_round_id")
                # S3 批3.5-D(lcr-string-compare): 数值比较而非字符串字典序，防号位超
                # 6 位(单窗口百万轮)后 'r1000000' < 'r999999' 致单调 max 永久冻结。
                if prev_lcr is None or round_tracker.parse_round_id(
                    compressed_last_round_id
                ) > round_tracker.parse_round_id(prev_lcr):
                    rec_state["last_compressed_round_id"] = compressed_last_round_id

            # 更新平均压缩率
            all_ratios = [h["ratio"] for h in compress_history if "ratio" in h]
            if all_ratios:
                t_file["metadata"]["avg_compression_ratio"] = round(
                    sum(all_ratios) / len(all_ratios), 4
                )

            # 保存 T 文件（合并式 Save：锁内 load-merge-save）
            # 压缩期间可能有新消息通过 append_messages 写入磁盘，
            # 需要重新 load 最新状态，提取中间到达的消息，追加到保留部分
            async with self._get_lock(window_key):
                current_t_file = await self.load(window_key)
                current_msgs = current_t_file.get("messages", [])

                # 冲突检测：如果磁盘消息数 < 压缩前快照消息数，
                # 说明另一个并发压缩已先保存（消息被裁剪），放弃本次保存
                if len(current_msgs) < pre_compress_msg_count:
                    logger.warning(
                        f"[CHECKPOINT] {window_key}: 检测到并发压缩冲突 "
                        f"(磁盘 {len(current_msgs)} < 快照 {pre_compress_msg_count})，"
                        f"放弃本次保存，使用磁盘最新状态"
                    )
                    return current_t_file, None

                # 提取压缩期间中间到达的消息
                mid_arrival_msgs = current_msgs[pre_compress_msg_count:]

                # 合并：压缩后保留部分 + 中间到达的消息
                t_file["messages"] = remaining_messages + mid_arrival_msgs

                # 同步 metadata：取 max 防止统计/号源回退
                cur_meta = current_t_file.get("metadata", {})
                t_file["metadata"]["total_messages_ever"] = max(
                    t_file["metadata"].get("total_messages_ever", 0),
                    cur_meta.get("total_messages_ever", 0),
                )
                # S3 批3.5-A(critical, compress-clobbers-numbersource): 号源/世代「只进不退」。
                # 压缩期间并发 append 经唯一取号入口推进了【磁盘】metadata 的
                # next_round_id/next_step_id/generation（mid_arrival 消息已带这些新号），
                # 但本次 save 的 t_file 是压缩前【快照】(旧号源)。若用快照整份覆盖磁盘 →
                # 号源回退 → 下次 append 重发已用过的 round_id/step_id（违反全局单调铁律，
                # 且号源只存 T 文件 metadata、无 state 备份、_do_recover 不校验，不可自愈）。
                # 故这三者与 total_messages_ever 一样从 cur_meta(磁盘最新)取 max。
                for _ns_key in ("next_round_id", "next_step_id", "generation"):
                    t_file["metadata"][_ns_key] = max(
                        int(t_file["metadata"].get(_ns_key, 0) or 0),
                        int(cur_meta.get(_ns_key, 0) or 0),
                    )

                if mid_arrival_msgs:
                    logger.info(
                        f"[CHECKPOINT] {window_key}: 压缩期间有 "
                        f"{len(mid_arrival_msgs)} 条新消息到达，已合并保留"
                    )

                await self.save(window_key, t_file)

        finally:
            # M-1: 无论成功/失败/异常，始终释放压缩锁（S3 F1.8）
            compress_lock.release()

        # 保存到 checkpoint_history 表（供面板统计）
        await self._save_to_db(
            window_key=window_key,
            compressed_content=compressed_text,
            original_count=original_msgs_compressed_count,
            compression_ratio=actual_ratio,
            token_estimate=compressed_tokens,
        )

        result = {
            "original_messages": original_msgs_compressed_count,
            "original_tokens": compress_tokens,
            "compressed_tokens": compressed_tokens,
            "compression_ratio": round(actual_ratio, 4),
            "kept_messages": len(remaining_messages),
            "latency_ms": round(latency),
        }

        logger.info(
            f"[CHECKPOINT] 完成: {window_key}, "
            f"{original_msgs_compressed_count} 条 → {actual_ratio:.1%}, "
            f"耗时 {latency:.0f}ms, 保留 {len(remaining_messages)} 条原文"
        )

        return t_file, result

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
