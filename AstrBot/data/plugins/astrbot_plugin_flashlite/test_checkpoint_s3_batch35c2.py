"""
S3 批3.5-C2 单测：对抗复查 low 副作用补强（C-1 lcr / C-2 orphan tool）
======================================================================
  C-1 clamp 切单个大轮中间时，record_state.last_compressed_round_id 不标被切的轮
      （该轮尾部仍在 remaining，标了会让 F2.x record 增量漏写残留）
  C-2 clamp 切 ReAct 大轮时，切分点回退到 step 边界，remaining[0] 不是 orphan tool
      （避免读侧 _repair_tool_call_pairs 静默删除合法配对的 tool 结果）

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_checkpoint_s3_batch35c2.py -q
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


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s3_b35c2_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _fp(d, wk):
    return os.path.join(d, wk.replace(":", "_") + ".json")


def _seed(fp, msgs):
    t_file = {"version": 2, "window_key": "x",
              "T1": {"compressed_summary": "", "original_msg_count": 0},
              "messages": msgs,
              "metadata": {"next_round_id": 50, "next_step_id": 80, "generation": 1,
                           "total_messages_ever": len(msgs),
                           "record_state": {"last_compressed_round_id": None}}}
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)


async def _fake_caller(prompt, **kw):
    return "【摘要】历史。"


def test_c1_lcr_not_marked_for_split_round(tmp_ckpt):
    """单个大轮(全同 round_id)被 clamp 切中间 → lcr 不标该被切轮。"""
    mgr = TFileManager()
    wk = "GroupMessage:35c2a"
    fp = _fp(tmp_ckpt, wk)
    # 20 条全 round_id r000005（连续 user 刷屏 → 单大 partial 轮覆盖到末尾）
    msgs = [{"role": "user", "content": f"刷屏{i} " + "填" * 20,
             "round_id": "r000005", "step_id": f"s{i:08d}", "message_id": f"m{i}"}
            for i in range(20)]
    _seed(fp, msgs)

    snap = _run(mgr.load(wk))
    _run(mgr.compress_if_needed(wk, snap, _fake_caller,
                                token_limit=100, keep_recent=5, cooldown_seconds=0))

    disk = json.load(open(fp, encoding="utf-8"))
    lcr = disk["metadata"]["record_state"]["last_compressed_round_id"]
    # r000005 尾部仍在 remaining（被切），不应被标为「已完整压缩」
    assert lcr != "r000005", f"被切的轮被错误标为 last_compressed_round_id: {lcr}"


def test_c2_clamp_no_orphan_tool_in_remaining(tmp_ckpt):
    """ReAct 大轮被 clamp 切中间 → 切分点回退到 step 边界，remaining[0] 不是 orphan tool。"""
    mgr = TFileManager()
    wk = "GroupMessage:35c2b"
    fp = _fp(tmp_ckpt, wk)
    # 单大轮 ReAct: user + (assistant.tool_calls + tool)×9，全 round_id r000003
    msgs = [{"role": "user", "content": "起始问题 " + "填" * 20,
             "round_id": "r000003", "step_id": "s00000001", "message_id": "m0"}]
    for i in range(9):
        msgs.append({"role": "assistant", "content": f"调用{i} " + "填" * 20,
                     "tool_calls": [{"id": f"T{i}", "type": "function",
                                     "function": {"name": "f", "arguments": "{}"}}],
                     "round_id": "r000003", "step_id": f"s{2 * i + 2:08d}",
                     "message_id": f"a{i}"})
        msgs.append({"role": "tool", "tool_call_id": f"T{i}", "content": f"结果{i} " + "填" * 20,
                     "round_id": "r000003", "step_id": f"s{2 * i + 3:08d}",
                     "message_id": f"t{i}"})
    _seed(fp, msgs)  # 19 条

    snap = _run(mgr.load(wk))
    _run(mgr.compress_if_needed(wk, snap, _fake_caller,
                                token_limit=100, keep_recent=3, cooldown_seconds=0))

    disk = json.load(open(fp, encoding="utf-8"))
    remaining = [m for m in disk["messages"] if m.get("message_id")]  # 非 T1 摘要的原始消息
    assert remaining, "应有保留消息"
    # remaining[0] 不是 orphan tool（其配对 assistant 被压走）
    if remaining[0].get("role") == "tool":
        tcid = remaining[0].get("tool_call_id")
        # 若仍是 tool，其配对 assistant 必须也在 remaining（非 orphan）
        has_pair = any(
            m.get("role") == "assistant" and
            any(tc.get("id") == tcid for tc in (m.get("tool_calls") or []))
            for m in remaining
        )
        assert has_pair, f"remaining[0] 是 orphan tool(配对 assistant 被压走): {tcid}"
