"""
S3 F2.1 — 划轮状态机 round_tracker.py
======================================
为每条 message 分配真 round_id / step_id，实现「锚点先行」（C2 critical）。

设计依据：QQBotPlan/Plan_5/S3_实现方案.md §1.1 决策表 + §2 约束 1-3 + §3 F2.1。

核心语义
--------
- round_id 作用域：per-window 自增，全限定字符串 ``r{int:06d}``。
- step_id  作用域：per-window 自增，全限定字符串 ``s{int:08d}``，与 round 平行。
- 取号唯一入口：号源是 ``metadata['next_round_id']`` / ``metadata['next_step_id']``，
  由本模块 ``assign_round`` 一处取号 +1 回写；调用方（F1.3 _append_messages_inner）
  负责把 metadata 持久化进 T 文件。buffer 阶段不分真号（易失语义）。
- first_reply 定义：该轮第 1 条 ``role == "assistant"``（**含纯 tool_calls 那条**）；
  ReAct 段（assistant → tool → assistant ...）的后续 assistant/tool 全部吸入本轮，
  直至下条 user。
- 划轮状态独立持久化：``{window}.state.json``（< 1KB），与 T 文件分离，避免每条
  消息触发 MB 级主 JSON 整份 dump。

闭合规则（§1.1 + 找茬 B 边界）
-----------------------------
(a) 下条 user 闭合：新 msg 是 user 且上轮已有 first_reply → 闭合上轮 + 开新轮。
(b) idle 兜底：``now - last_user_ts > round_idle_close_s``（默认 600）且 partial=open
    → ``should_idle_close`` 返回 True，后台定时器调 ``close_round`` 闭合，
    标 ``closed_by="idle_timeout"``。
(c) 双上限：``round_step_count >= round_max_steps``（30）或
    ``round_token_count >= round_max_tokens``（8000）→ step 边界切轮
    （语义：轮内最多 N，第 N+1 条另起新轮。决策采纳 >= 而非方案原文的 >）。

不变量
------
- 连续 user（bot 长时不回）：多条 user 无 assistant → 都落在同 1 个 partial 轮，
  绝不每条 user 开新轮。
- 空轮 / 撤回：step 仍分配不跳号，round_id 连续单调递增。
- 跨窗口独立：round_id / step_id per-window 自增，不同窗口各自一份 state。
- round_id 全局单调，绝不复用；崩后宁可跳号也不重号（崩溃恢复见 F2.2）。

本模块为**纯逻辑**：不依赖 astrbot.api，state.json I/O 仅用标准库（便于单测）。
F1.3 集成时由调用方注入 metadata 并负责其持久化。
"""

import json
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, Optional


# ========================
# 默认阈值（与 _conf_schema.json F6.1 对齐；调用方应传入实际配置值）
# ========================
DEFAULT_ROUND_MAX_STEPS = 30
DEFAULT_ROUND_MAX_TOKENS = 8000
DEFAULT_ROUND_IDLE_CLOSE_S = 600

PARTIAL_OPEN = "open"
PARTIAL_CLOSED = "closed"


# ========================
# 号位格式化
# ========================
def format_round_id(n: int) -> str:
    """round_id 全限定字符串：r{int:06d}"""
    return f"r{n:06d}"


def format_step_id(n: int) -> str:
    """step_id 全限定字符串：s{int:08d}"""
    return f"s{n:08d}"


# ========================
# state.json 初始结构
# ========================
def new_state(generation: int = 0) -> Dict[str, Any]:
    """创建一份空的 per-window 划轮状态（对应 {window}.state.json）。

    generation：与 T 文件交叉校验的代号（F2.2 用，T 文件 save 时同步 ++）。
    """
    return {
        "generation": generation,
        "current_round_id": None,            # str | None，当前所在轮
        "current_round_started_at": "",      # ISO 字符串，本轮第一条消息时间
        "first_reply_step_id": None,         # str | None，本轮第 1 条 assistant 的 step_id
        "partial_round_status": PARTIAL_CLOSED,  # "open" | "closed"
        "last_user_step_id": None,           # str | None
        "last_assistant_step_id": None,      # str | None
        "last_user_ts": 0.0,                 # float，最近一条 user 的时间戳（idle 判定用）
        "last_role": None,                   # str | None，最近一条消息的 role
        "round_step_count": 0,               # int，当前轮已分配的 step 数
        "round_token_count": 0,              # int，当前轮累计 token（调用方传入累加）
        "closed_by": None,                   # str | None，本轮（或上一轮）闭合原因
    }


# ========================
# 号源辅助（唯一取号入口语义）
# ========================
def _take_round_id(metadata: Dict[str, Any]) -> str:
    """从 metadata['next_round_id'] 取号 +1 回写。号源缺失时从 1 起算。"""
    n = int(metadata.get("next_round_id", 1) or 1)
    metadata["next_round_id"] = n + 1
    return format_round_id(n)


def _take_step_id(metadata: Dict[str, Any]) -> str:
    """从 metadata['next_step_id'] 取号 +1 回写。号源缺失时从 1 起算。"""
    n = int(metadata.get("next_step_id", 1) or 1)
    metadata["next_step_id"] = n + 1
    return format_step_id(n)


# ========================
# 轮闭合
# ========================
def close_round(state: Dict[str, Any], reason: str) -> None:
    """闭合当前轮：partial_round_status=closed，记 closed_by=reason。

    幂等：已 closed 再调只更新 closed_by（不破坏号位）。
    """
    state["partial_round_status"] = PARTIAL_CLOSED
    state["closed_by"] = reason


def should_idle_close(
    state: Dict[str, Any],
    now_ts: float,
    round_idle_close_s: int = DEFAULT_ROUND_IDLE_CLOSE_S,
) -> bool:
    """idle 兜底判定（规则 b）。后台定时器（默认 30s）周期调用。

    返回 True 的条件：当前轮 partial=open，且距最近一条 user 已超过
    round_idle_close_s 秒（bot 长时不回话 / 群聊低活时段）。

    注意：只做判定，不修改 state。调用方拿到 True 后应自行 close_round
    并标 closed_by="idle_timeout"，再持久化 state。
    """
    if state.get("partial_round_status") != PARTIAL_OPEN:
        return False
    last_user_ts = state.get("last_user_ts") or 0.0
    if last_user_ts <= 0:
        return False
    return (now_ts - last_user_ts) > round_idle_close_s


# ========================
# 核心：分配 round_id / step_id
# ========================
def assign_round(
    msg: Dict[str, Any],
    state: Dict[str, Any],
    metadata: Dict[str, Any],
    now_ts: float,
    msg_tokens: int = 0,
    round_max_steps: int = DEFAULT_ROUND_MAX_STEPS,
    round_max_tokens: int = DEFAULT_ROUND_MAX_TOKENS,
) -> Dict[str, Any]:
    """为单条 message 分配 round_id / step_id，并就地更新 state / metadata。

    这是「唯一取号入口」：所有 round_id / step_id 只在此函数内从 metadata 取号。
    调用方（F1.3 _append_messages_inner）须在调用前把 state / metadata 准备好，
    调用后负责将二者持久化（state → {window}.state.json，metadata → T 文件）。

    参数
    ----
    msg            : 待分配的消息 dict，至少含 ``role``；assistant 可含 ``tool_calls``。
    state          : per-window 划轮状态（见 new_state），就地修改。
    metadata       : T 文件 metadata（含 next_round_id / next_step_id），就地修改。
    now_ts         : 当前时间戳（float，秒）。
    msg_tokens     : 本条消息估算 token（由调用方估算传入，本模块不估算，解耦）。
    round_max_steps / round_max_tokens : 双上限阈值。

    返回
    ----
    dict {
        "round_id":        str,         本条消息所属轮
        "step_id":         str,         本条消息的 step_id
        "first_reply":     bool,        本条是否为该轮第 1 条 assistant
        "new_round_opened":bool,        本条是否开启了一个新轮
        "closed_round":    str | None,  若本条触发上一轮闭合，则为被闭合的 round_id
    }
    """
    role = msg.get("role")
    is_user = role == "user"
    is_assistant = role == "assistant"

    closed_round: Optional[str] = None
    new_round_opened = False

    # ---- 1) 先判定是否需要在「分配本条 step 之前」闭合上一轮 + 开新轮 ----
    need_open = False

    if state.get("current_round_id") is None or \
            state.get("partial_round_status") == PARTIAL_CLOSED:
        # 还没有轮，或上一轮已闭合 → 任何消息都开新轮
        need_open = True
    else:
        # 当前有一个 open 轮，按规则判断是否切轮
        if is_user and state.get("first_reply_step_id") is not None:
            # 规则 (a)：下条 user 出现且上轮已有 first_reply → 闭合上轮 + 开新轮
            closed_round = state["current_round_id"]
            close_round(state, "next_user")
            need_open = True
        else:
            # 规则 (c)：双上限。注意以「即将容纳本条」的视角判断——
            # 当前轮 step_count 已达上限，或累计 token 已超阈值，则本条另起新轮，
            # 避免单轮无限膨胀。连续 user（无 first_reply）不会走规则 (a)，
            # 但仍受双上限保护。
            over_steps = state.get("round_step_count", 0) >= round_max_steps
            over_tokens = state.get("round_token_count", 0) >= round_max_tokens
            if over_steps or over_tokens:
                closed_round = state["current_round_id"]
                close_round(
                    state,
                    "max_steps" if over_steps else "max_tokens",
                )
                need_open = True
            # 否则：沿用当前 open 轮（连续 user / ReAct 段都走这里）

    now_iso = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%dT%H:%M:%S.%f")

    if need_open:
        rid = _take_round_id(metadata)
        state["current_round_id"] = rid
        state["current_round_started_at"] = now_iso
        state["first_reply_step_id"] = None
        state["partial_round_status"] = PARTIAL_OPEN
        state["round_step_count"] = 0
        state["round_token_count"] = 0
        state["closed_by"] = None
        new_round_opened = True

    round_id = state["current_round_id"]

    # ---- 2) 分配 step_id（每条消息都 +1，空轮/撤回也不跳号）----
    step_id = _take_step_id(metadata)
    state["round_step_count"] = state.get("round_step_count", 0) + 1
    state["round_token_count"] = state.get("round_token_count", 0) + int(msg_tokens or 0)

    # ---- 3) first_reply 锚定：该轮第 1 条 assistant（含纯 tool_calls 那条）----
    first_reply = False
    if is_assistant and state.get("first_reply_step_id") is None:
        state["first_reply_step_id"] = step_id
        first_reply = True

    # ---- 4) 更新 last_* 状态 ----
    state["last_role"] = role
    if is_user:
        state["last_user_step_id"] = step_id
        state["last_user_ts"] = now_ts
    elif is_assistant:
        state["last_assistant_step_id"] = step_id

    return {
        "round_id": round_id,
        "step_id": step_id,
        "first_reply": first_reply,
        "new_round_opened": new_round_opened,
        "closed_round": closed_round,
    }


# ========================
# state.json 持久化辅助（原子写：临时文件 + os.replace，参照 checkpoint.py save 风格）
# ========================
def state_file_path(checkpoints_dir: str, window_key: str) -> str:
    """计算 {window}.state.json 路径（与 T 文件 {window}.json 并列）。"""
    safe_name = window_key.replace(":", "_")
    return os.path.join(checkpoints_dir, f"{safe_name}.state.json")


def load_state(checkpoints_dir: str, window_key: str) -> Dict[str, Any]:
    """读取 per-window 划轮状态；文件不存在或损坏时返回一份全新空状态。"""
    fp = state_file_path(checkpoints_dir, window_key)
    if not os.path.exists(fp):
        return new_state()
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return new_state()
        # 补齐缺失字段（向后兼容：旧 state 文件可能缺新字段）
        base = new_state()
        base.update({k: v for k, v in data.items() if k in base})
        return base
    except Exception:
        # 损坏文件不应阻断主链路；返回空状态，由崩溃恢复（F2.2）后续修正
        return new_state()


def save_state(checkpoints_dir: str, window_key: str, state: Dict[str, Any]) -> None:
    """原子保存 per-window 划轮状态（临时文件 → os.replace，参照 checkpoint.save）。"""
    os.makedirs(checkpoints_dir, exist_ok=True)
    fp = state_file_path(checkpoints_dir, window_key)

    fd, tmp_path = tempfile.mkstemp(
        dir=checkpoints_dir, prefix=".state_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        # 原子重命名（Windows 上 os.replace 可覆盖已存在目标）
        os.replace(tmp_path, fp)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
