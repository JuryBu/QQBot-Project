"""
S4 批1b 单测：R3 build_llm_contexts 改读 record（注入主路径接入 record + fallback）
====================================================================================
覆盖 5 个验收点（依据 QQBotPlan/Plan_5/S4_实现方案.md §二 R3 + §一 M3 +
S4_设计决策.md D1 派生物可重建 / D7 分级）：
  ① 有 record（round_groups 非空 + 各组带 tier 文本）→ build_llm_contexts(window_key)
     输出 = [record 概要块(已聚合) + 末尾未聚合原文]
  ② record 空 / round_groups 空 → fallback 全量（T1 + messages 全量原文）
  ③ record.md 坏（垃圾文件）→ rebuild_index_if_stale 重渲 or fallback，不崩、contexts 非空
  ④ dangling 修复保留：末尾原文以 dangling assistant.tool_calls 结尾 → 补占位 tool
  ⑤ 注入端到端：构造 t_file → build_llm_contexts(window_key) → 非空 contexts

附加：内部触发判定口径（无 window_key）不被 record 概要缩减 token —— 保护批2b 接力判据。

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_batch1b.py -q
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# checkpoint.py 顶部 `from astrbot.api import logger` —— mock 掉 astrbot 包以便单测。
if "astrbot" not in sys.modules:
    import logging
    import types

    _a = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("flashlite_test")
    _a.api = _api
    sys.modules["astrbot"] = _a
    sys.modules["astrbot.api"] = _api

import pytest  # noqa: E402

import checkpoint  # noqa: E402
import record  # noqa: E402
import round_tracker  # noqa: E402
from checkpoint import (  # noqa: E402
    TFileManager,
    T1_SUMMARY_PREFIX,
    T1_ACK_CONTENT,
    DANGLING_TOOL_PLACEHOLDER,
)

WK = "GroupMessage:1001"


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s4_b1b_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


@pytest.fixture()
def mgr():
    # __init__ 会 makedirs(CHECKPOINTS_DIR) —— 在 tmp_ckpt monkeypatch 后实例化。
    return TFileManager()


def _msg(role, content, round_n=None, **extra):
    m = {"role": role}
    if content is not None:
        m["content"] = content
    if round_n is not None:
        m["round_id"] = round_tracker.format_round_id(round_n)
    m.update(extra)
    return m


def _group(rg_num, s, e, *, tier="full", full_text="", summary_text="",
           sealed=True, legacy=False, title=""):
    return {
        "rg_id": record.format_rg_id(rg_num),
        "round_range": [s, e],
        "tier": tier,
        "sealed": sealed,
        "legacy_rg": legacy,
        "full_text": full_text,
        "summary_text": summary_text,
        "title": title,
    }


def _t_file_with_record(groups, messages, *, t1_summary=""):
    """构造带 record_state.round_groups 的 t_file。"""
    t1 = {"compressed_summary": t1_summary} if t1_summary else {}
    return {
        "version": 2,
        "T1": t1,
        "messages": messages,
        "metadata": {
            "generation": 5,
            "record_state": {
                "round_groups": groups,
                "last_grouped_rg_id": groups[-1]["rg_id"] if groups else None,
            },
        },
    }


# ============================================================
# ① 有 record → 读 record 概要块 + 末尾未聚合原文
# ============================================================
def test_record_view_summary_block_plus_tail(tmp_ckpt, mgr):
    """round_groups 聚合到 r3，messages 含 r1..r5：输出 = record 概要块(r1-3) + 末尾原文(r4,r5)。"""
    groups = [
        _group(1, 1, 3, tier="full",
               full_text="user: 早期对话A\nassistant: 回复A", title="主题A"),
    ]
    messages = [
        # 已聚合区间 r1-r3（应被概要块覆盖，原文不再注入）
        _msg("user", "早期对话A", 1),
        _msg("assistant", "回复A", 1),
        _msg("user", "对话B", 2),
        _msg("assistant", "回复B", 2),
        _msg("user", "对话C", 3),
        _msg("assistant", "回复C", 3),
        # 末尾未聚合 r4,r5（应取原文）
        _msg("user", "最新问题D", 4),
        _msg("assistant", "回复D", 4),
        _msg("user", "最新问题E", 5),
    ]
    t_file = _t_file_with_record(groups, messages)

    ctx = mgr.build_llm_contexts(t_file, window_key=WK)

    # record 概要块：T1_SUMMARY_PREFIX 头 + 组文本 + ACK
    assert ctx[0]["role"] == "user"
    assert ctx[0]["content"].startswith(T1_SUMMARY_PREFIX)
    assert "早期对话A" in ctx[0]["content"]
    assert "主题A" in ctx[0]["content"]
    assert ctx[1] == {"role": "assistant", "content": T1_ACK_CONTENT}

    # 末尾原文：r4/r5 的内容在；已聚合区间 r1-3 原文不重复出现在 user 消息体里
    tail_contents = [m.get("content", "") for m in ctx[2:]]
    joined = "\n".join(tail_contents)
    assert "最新问题D" in joined
    assert "回复D" in joined
    assert "最新问题E" in joined
    # 已聚合原文不应作为独立末尾原文消息再注入（只在概要块里）
    assert "对话B" not in joined
    assert "对话C" not in joined


# ============================================================
# ② record 空 / round_groups 空 → fallback 全量
# ============================================================
def test_fallback_when_no_record(tmp_ckpt, mgr):
    """无 record_state / round_groups 空 → 全量原文（T1 + 全部 messages）。"""
    messages = [
        _msg("user", "问题1", 1),
        _msg("assistant", "回复1", 1),
        _msg("user", "问题2", 2),
    ]
    # 无 record_state
    t_file = {
        "version": 2,
        "T1": {"compressed_summary": "历史摘要X"},
        "messages": messages,
        "metadata": {},
    }
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)

    # 全量视图：T1 摘要对 + 全部原文（含已"应聚合"但 record 空所以全量）
    assert ctx[0]["content"].startswith(T1_SUMMARY_PREFIX)
    assert "历史摘要X" in ctx[0]["content"]
    joined = "\n".join(m.get("content", "") for m in ctx)
    assert "问题1" in joined and "问题2" in joined and "回复1" in joined

    # round_groups 显式空列表 → 同样 fallback
    t_file2 = _t_file_with_record([], messages, t1_summary="历史摘要Y")
    ctx2 = mgr.build_llm_contexts(t_file2, window_key=WK)
    joined2 = "\n".join(m.get("content", "") for m in ctx2)
    assert "问题1" in joined2 and "问题2" in joined2

    # 无 window_key 一律全量
    ctx3 = mgr.build_llm_contexts(t_file)
    joined3 = "\n".join(m.get("content", "") for m in ctx3)
    assert "问题1" in joined3 and "问题2" in joined3


# ============================================================
# ③ record.md 坏 → rebuild_index_if_stale 重渲 or fallback，不崩
# ============================================================
def test_broken_record_md_does_not_crash(tmp_ckpt, mgr):
    """record.md 是垃圾文件（hash 与 sidecar 不符）→ best-effort 重渲 sidecar，
    注入不依赖 record.md 文件本身 → 仍输出有效 record 视图、绝不崩。"""
    # 写一个坏的 record.md（与任何 sidecar 不匹配）
    md_fp = record.record_md_path(tmp_ckpt, WK)
    with open(md_fp, "w", encoding="utf-8") as f:
        f.write("\x00\x00 GARBAGE 损坏内容 \xff 不是合法 record")

    groups = [
        _group(1, 1, 2, tier="full", full_text="user: A\nassistant: B", title="组1"),
    ]
    messages = [
        _msg("user", "A", 1), _msg("assistant", "B", 1),
        _msg("user", "尾部C", 3),
    ]
    t_file = _t_file_with_record(groups, messages)

    # 不抛异常
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)
    assert ctx, "坏 record.md 时 contexts 不应为空"
    # record 视图仍生效（概要块取自 metadata，不依赖坏文件）
    assert ctx[0]["content"].startswith(T1_SUMMARY_PREFIX)
    assert "组1" in ctx[0]["content"]
    joined = "\n".join(m.get("content", "") for m in ctx)
    assert "尾部C" in joined  # 末尾原文在

    # sidecar 已被 best-effort 重渲（rebuilt 写盘）
    idx = record.load_index(tmp_ckpt, WK)
    assert idx.get("groups"), "sidecar 应已从 metadata 重渲出 groups"


def test_corrupt_round_groups_falls_back(tmp_ckpt, mgr):
    """round_groups 全是坏项（round_range 不可解析）→ 无有效组 → fallback 全量不崩。"""
    bad_groups = [
        {"rg_id": "rgX", "round_range": "不是列表"},
        {"rg_id": "rgY"},  # 缺 round_range
    ]
    messages = [_msg("user", "问题Z", 1), _msg("assistant", "回复Z", 1)]
    t_file = _t_file_with_record(bad_groups, messages, t1_summary="兜底摘要")
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)
    # fallback 全量：原文都在
    joined = "\n".join(m.get("content", "") for m in ctx)
    assert "问题Z" in joined and "回复Z" in joined


# ============================================================
# ④ dangling 修复保留（末尾原文以 dangling tool_calls 结尾）
# ============================================================
def test_dangling_repair_preserved_in_record_view(tmp_ckpt, mgr):
    """末尾未聚合原文以 assistant.tool_calls（无配对 tool）结尾 → 输出补占位 tool。"""
    groups = [_group(1, 1, 2, full_text="历史段文本", title="历史")]
    tc = [{"id": "call_xyz", "type": "function",
           "function": {"name": "search", "arguments": "{}"}}]
    messages = [
        _msg("user", "历史问", 1), _msg("assistant", "历史答", 1),
        _msg("user", "查一下", 3),
        # dangling：assistant 带 tool_calls，但后面没有配对 tool 结果
        _msg("assistant", None, 3, tool_calls=tc),
    ]
    t_file = _t_file_with_record(groups, messages)
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)

    # 末尾应被补一条占位 tool 消息（tool_call_id == call_xyz）
    tool_msgs = [m for m in ctx if m.get("role") == "tool"]
    assert any(m.get("tool_call_id") == "call_xyz" for m in tool_msgs), \
        "dangling tool_calls 应被补占位 tool"
    assert any(DANGLING_TOOL_PLACEHOLDER in (m.get("content") or "")
               for m in tool_msgs)


# ============================================================
# ⑤ 注入端到端：构造 t_file → build_llm_contexts(window_key) → 非空 contexts
# ============================================================
def test_injection_end_to_end_nonempty(tmp_ckpt, mgr):
    """端到端：含 legacy 组 + 真组 + 末尾轮的完整 t_file → 非空、结构合法。"""
    groups = [
        _group(0, 0, 0, tier="summary", summary_text="远古历史摘要",
               legacy=True, title="历史对话摘要（迁移自旧 T1）"),
        _group(1, 1, 4, tier="full", full_text="user: 段1\nassistant: 段1答",
               title="主题一"),
    ]
    messages = [
        _msg("user", "段1", 1), _msg("assistant", "段1答", 1),
        _msg("user", "段2", 2), _msg("assistant", "段2答", 2),
        _msg("user", "段3", 3), _msg("assistant", "段3答", 3),
        _msg("user", "段4", 4), _msg("assistant", "段4答", 4),
        _msg("user", "当前问题", 5),
    ]
    t_file = _t_file_with_record(groups, messages)
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)

    assert isinstance(ctx, list) and len(ctx) > 0
    # 每条都是合法 OpenAI 消息（含 role）
    for m in ctx:
        assert "role" in m
    # legacy 摘要 + 真组文本都在概要块
    assert ctx[0]["content"].startswith(T1_SUMMARY_PREFIX)
    assert "远古历史摘要" in ctx[0]["content"]
    assert "主题一" in ctx[0]["content"]
    # 末尾原文 r5 在
    joined = "\n".join(m.get("content", "") or "" for m in ctx)
    assert "当前问题" in joined


def test_legacy_only_keeps_all_real_rounds(tmp_ckpt, mgr):
    """只有 legacy 占位组 [0,0] → 水位=0 → 所有真轮 r1+ 作末尾原文（不丢轮）。"""
    groups = [
        _group(0, 0, 0, tier="summary", summary_text="历史摘要",
               legacy=True, title="历史"),
    ]
    messages = [
        _msg("user", "真轮1", 1), _msg("assistant", "答1", 1),
        _msg("user", "真轮2", 2),
    ]
    t_file = _t_file_with_record(groups, messages)
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)
    joined = "\n".join(m.get("content", "") or "" for m in ctx)
    # legacy 概要在概要块，真轮原文全在
    assert "历史摘要" in ctx[0]["content"]
    assert "真轮1" in joined and "答1" in joined and "真轮2" in joined


# ============================================================
# 附加：内部触发判定口径不变（无 window_key 全量，保护批2b 接力判据）
# ============================================================
def test_internal_full_view_token_unchanged(tmp_ckpt, mgr):
    """无 window_key（内部触发/接力判定）→ 全量原文，token 不被 record 概要缩减。"""
    groups = [_group(1, 1, 3, full_text="很长很长的历史聚合文本" * 10)]
    messages = [
        _msg("user", "x" * 200, 1), _msg("assistant", "y" * 200, 2),
        _msg("user", "z" * 200, 3), _msg("user", "尾", 4),
    ]
    t_file = _t_file_with_record(groups, messages)

    full_ctx = mgr.build_llm_contexts(t_file)               # 无 wk → 全量
    rec_ctx = mgr.build_llm_contexts(t_file, window_key=WK)  # 有 wk → record

    # 全量保留所有原文消息（4 条）；record 视图末尾只剩 r4（1 条）+ 概要对（2 条）
    full_user_assistant = [m for m in full_ctx
                           if m.get("role") in ("user", "assistant")]
    assert len(full_user_assistant) >= 4, "全量视图应含全部原文消息"
    # 全量里 r1-r3 原文在
    full_joined = "\n".join(m.get("content", "") or "" for m in full_ctx)
    assert "x" * 200 in full_joined
    # record 视图里 r1-r3 原文不再以独立消息出现（被概要替代）
    rec_joined = "\n".join(
        m.get("content", "") or "" for m in rec_ctx if m.get("role") != "user"
        or not m.get("content", "").startswith(T1_SUMMARY_PREFIX)
    )
    assert ("x" * 200) not in rec_joined


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
