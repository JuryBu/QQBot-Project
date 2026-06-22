"""
S3 批3.5-C 单测：压缩切分下标 / 边界
=====================================
  C1 candidate-raw-index-desync   to_compress 与 remaining 同 raw 下标系，前缀有
                                  dangling(build_llm_contexts 插占位)时不丢/重消息
  C2 forward-align-overshoots     align 向后挪夹回 available_for_compress，单大轮
                                  覆盖到末尾时不把 keep_recent 最近上下文压光
  C3 react-step-split             双上限切轮延迟到 step 边界，mid-step(tool / 带
                                  tool_calls 的 assistant)不被拆轮；普通消息正常切

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_checkpoint_s3_batch35c.py -q
"""
import asyncio
import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
from checkpoint import TFileManager  # noqa: E402
from round_tracker import assign_round, new_state  # noqa: E402


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s3_b35c_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _fp(d, wk):
    return os.path.join(d, wk.replace(":", "_") + ".json")


# ============ C3：react-step 双上限切轮延迟到 step 边界 ============
def test_c3_react_step_not_split_by_double_limit():
    """双上限(steps)边界落在 tool 结果时不切轮：assistant.tool_calls + tool 必须同轮。"""
    state = new_state()
    meta = {"next_round_id": 1, "next_step_id": 1}
    user = {"role": "user", "content": "问"}
    asst_tc = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "A", "type": "function",
                               "function": {"name": "f", "arguments": "{}"}}]}
    tool = {"role": "tool", "tool_call_id": "A", "content": "结果"}

    r1 = assign_round(user, state, meta, 100.0, round_max_steps=2)
    r2 = assign_round(asst_tc, state, meta, 101.0, round_max_steps=2)
    # 此时 round_step_count=2 >= 2，下条 tool 触发 over_steps 但是 mid-step → 不切
    r3 = assign_round(tool, state, meta, 102.0, round_max_steps=2)

    assert r1["round_id"] == r2["round_id"], "user + assistant 同轮"
    assert r2["round_id"] == r3["round_id"], \
        "assistant.tool_calls + tool 被双上限拆轮了（C3 修复失效）"


def test_c3_double_limit_still_splits_consecutive_user():
    """非 mid-step（连续 user）仍受双上限保护正常切轮（修复不影响正常切轮）。"""
    state = new_state()
    meta = {"next_round_id": 1, "next_step_id": 1}
    u = lambda i: {"role": "user", "content": f"u{i}"}

    r1 = assign_round(u(1), state, meta, 100.0, round_max_steps=2)
    r2 = assign_round(u(2), state, meta, 101.0, round_max_steps=2)
    # 连续 user 无 first_reply → 不走规则(a)；round_step_count=2>=2 且 user 非 mid-step → 切轮
    r3 = assign_round(u(3), state, meta, 102.0, round_max_steps=2)

    assert r1["round_id"] == r2["round_id"], "连续 user 前两条同轮"
    assert r2["round_id"] != r3["round_id"], "连续 user 触双上限应正常切轮"


# ============ C1 / C2：压缩切分 ============
def _seed(fp, msgs, meta_extra=None):
    md = {"next_round_id": 50, "next_step_id": 80, "generation": 1,
          "total_messages_ever": len(msgs),
          "record_state": {"last_compressed_round_id": None}}
    if meta_extra:
        md.update(meta_extra)
    t_file = {"version": 2, "window_key": "x", "T1": {"compressed_summary": "",
              "original_msg_count": 0}, "messages": msgs, "metadata": md}
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)


def test_c1_no_message_loss_with_dangling_prefix(tmp_ckpt):
    """前缀有 mid-history dangling 时，压缩段(喂 flash_lite) ∪ remaining 必须覆盖全部原始消息。"""
    mgr = TFileManager()
    wk = "GroupMessage:35c01"
    fp = _fp(tmp_ckpt, wk)

    # 20 条消息，每条 content 带唯一标记 MARK{i}；第 4 条是 dangling assistant(tool_calls 无配对 tool)
    msgs = []
    for i in range(20):
        if i == 4:
            msgs.append({"role": "assistant", "content": f"MARK{i} 长内容" + "填" * 20,
                         "tool_calls": [{"id": "DANGLE", "type": "function",
                                         "function": {"name": "f", "arguments": "{}"}}],
                         "round_id": f"r{(i // 2) + 1:06d}", "step_id": f"s{i:08d}",
                         "message_id": f"m{i}"})
        else:
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": f"MARK{i} 长内容" + "填" * 20,
                         "round_id": f"r{(i // 2) + 1:06d}", "step_id": f"s{i:08d}",
                         "message_id": f"m{i}"})
    _seed(fp, msgs)

    captured = {}

    async def fake_caller(prompt, **kw):
        captured["prompt"] = prompt
        return "【摘要】历史。"

    snap = _run(mgr.load(wk))
    _run(mgr.compress_if_needed(wk, snap, fake_caller,
                                token_limit=100, keep_recent=5, cooldown_seconds=0))

    disk = json.load(open(fp, encoding="utf-8"))
    # remaining 中所有原始消息标记
    remaining_marks = set()
    for m in disk["messages"]:
        c = m.get("content") or ""
        if isinstance(c, str) and "MARK" in c:
            for tok in c.split():
                if tok.startswith("MARK"):
                    remaining_marks.add(tok)
    # 压缩段标记（在喂给 flash_lite 的 prompt 里）
    prompt_marks = set()
    for i in range(20):
        if f"MARK{i} " in captured.get("prompt", "") or f"MARK{i}\n" in captured.get("prompt", ""):
            prompt_marks.add(f"MARK{i}")
    all_marks = {f"MARK{i}" for i in range(20)}
    covered = remaining_marks | prompt_marks
    # 守恒：每条原始消息要么进了摘要(prompt)，要么留在 remaining，绝不凭空消失
    missing = all_marks - covered
    assert not missing, f"压缩丢失消息(下标系错位): {sorted(missing)}"


def test_c2_forward_align_preserves_keep_recent(tmp_ckpt):
    """单个大 partial 轮覆盖到列表末尾时，压缩保留最近 keep_recent 条（不被压光）。"""
    mgr = TFileManager()
    wk = "GroupMessage:35c02"
    fp = _fp(tmp_ckpt, wk)

    # 20 条消息全同一 round_id（模拟连续 user 刷屏，round_tracker 归同一 partial 轮）
    msgs = [{"role": "user", "content": f"刷屏{i} " + "填" * 20,
             "round_id": "r000001", "step_id": f"s{i:08d}", "message_id": f"m{i}"}
            for i in range(20)]
    _seed(fp, msgs)

    async def fake_caller(prompt, **kw):
        return "【摘要】历史。"

    snap = _run(mgr.load(wk))
    _run(mgr.compress_if_needed(wk, snap, fake_caller,
                                token_limit=100, keep_recent=5, cooldown_seconds=0))

    disk = json.load(open(fp, encoding="utf-8"))
    # 落盘的原始消息（带 message_id）数量 = remaining，必须 >= keep_recent
    kept = [m for m in disk["messages"] if m.get("message_id")]
    assert len(kept) >= 5, f"keep_recent 被压光！只剩 {len(kept)} 条原文"
