"""
S3 F2.1 划轮状态机 round_tracker 单元测试
==========================================
覆盖找茬 B 关键边界（8 个 case + state.json 原子读写 + 号位单调）。

跑法：
  AstrBot/.venv/Scripts/python.exe -m pytest test_round_tracker.py -v
或直接：
  AstrBot/.venv/Scripts/python.exe test_round_tracker.py
"""
import os
import sys
import tempfile

# Windows 控制台默认 GBK，emoji/中文打印可能 UnicodeEncodeError；切 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from round_tracker import (  # noqa: E402
    assign_round,
    should_idle_close,
    close_round,
    new_state,
    load_state,
    save_state,
    state_file_path,
    format_round_id,
    format_step_id,
    PARTIAL_OPEN,
    PARTIAL_CLOSED,
)


# ========================
# 辅助：构造 msg + 一个干净的 fixture 环境
# ========================
def fresh():
    """返回 (state, metadata)：全新窗口，号源从 1 起。"""
    return new_state(), {"next_round_id": 1, "next_step_id": 1}


def user(text="hi"):
    return {"role": "user", "content": text}


def assistant(text="ok"):
    return {"role": "assistant", "content": text}


def assistant_tc(*tcids):
    """携带 tool_calls 的 assistant（纯 tool_calls，content=None）"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": t, "type": "function", "function": {"name": "f", "arguments": "{}"}}
            for t in tcids
        ],
    }


def tool(tcid, content="result"):
    return {"role": "tool", "tool_call_id": tcid, "content": content}


def run(msg, state, metadata, now_ts=1000.0, msg_tokens=0,
        round_max_steps=30, round_max_tokens=8000):
    return assign_round(
        msg, state, metadata, now_ts,
        msg_tokens=msg_tokens,
        round_max_steps=round_max_steps,
        round_max_tokens=round_max_tokens,
    )


# ========================
# Case 1：正常轮 user → assistant
# ========================
def test_case1_normal_round():
    state, meta = fresh()
    r1 = run(user("你好"), state, meta, now_ts=100.0)
    r2 = run(assistant("你也好"), state, meta, now_ts=101.0)

    assert r1["round_id"] == "r000001"
    assert r1["step_id"] == "s00000001"
    assert r1["first_reply"] is False        # user 不是 first_reply
    assert r1["new_round_opened"] is True

    assert r2["round_id"] == "r000001"        # 同一轮
    assert r2["step_id"] == "s00000002"
    assert r2["first_reply"] is True          # first_reply 锚在 assistant
    assert r2["new_round_opened"] is False

    assert state["first_reply_step_id"] == "s00000002"
    assert state["partial_round_status"] == PARTIAL_OPEN
    assert state["round_step_count"] == 2


# ========================
# Case 2：连续 user × 3 无回复 → 同 1 partial 轮，不开新轮
# ========================
def test_case2_consecutive_users_same_round():
    state, meta = fresh()
    r1 = run(user("在吗"), state, meta, now_ts=100.0)
    r2 = run(user("？？"), state, meta, now_ts=101.0)
    r3 = run(user("有人吗"), state, meta, now_ts=102.0)

    assert r1["round_id"] == r2["round_id"] == r3["round_id"] == "r000001"
    assert r2["new_round_opened"] is False
    assert r3["new_round_opened"] is False
    # step 各自单调 +1，不跳号
    assert [r1["step_id"], r2["step_id"], r3["step_id"]] == \
        ["s00000001", "s00000002", "s00000003"]
    # 仍是 open 轮，无 first_reply
    assert state["partial_round_status"] == PARTIAL_OPEN
    assert state["first_reply_step_id"] is None
    assert meta["next_round_id"] == 2   # 只取了 1 个 round 号


# ========================
# Case 3：idle → user 后超 round_idle_close_s 无 activity → should_idle_close=True
# ========================
def test_case3_idle_close():
    state, meta = fresh()
    run(user("你好"), state, meta, now_ts=100.0)

    # 未超时
    assert should_idle_close(state, now_ts=100.0 + 599, round_idle_close_s=600) is False
    # 超时
    assert should_idle_close(state, now_ts=100.0 + 601, round_idle_close_s=600) is True

    # 调用方据此闭合
    close_round(state, "idle_timeout")
    assert state["partial_round_status"] == PARTIAL_CLOSED
    assert state["closed_by"] == "idle_timeout"
    # 闭合后再判定返回 False（不重复闭合）
    assert should_idle_close(state, now_ts=100.0 + 999, round_idle_close_s=600) is False


# ========================
# Case 4：下条 user 闭合 user → assistant → user → 第 2 个 user 开第 2 轮
# ========================
def test_case4_next_user_opens_new_round():
    state, meta = fresh()
    r1 = run(user("问题1"), state, meta, now_ts=100.0)
    r2 = run(assistant("回答1"), state, meta, now_ts=101.0)
    r3 = run(user("问题2"), state, meta, now_ts=102.0)

    assert r1["round_id"] == "r000001"
    assert r2["round_id"] == "r000001"
    assert r2["first_reply"] is True
    # 第 2 个 user 闭合上轮 + 开第 2 轮
    assert r3["round_id"] == "r000002"
    assert r3["new_round_opened"] is True
    assert r3["closed_round"] == "r000001"
    # step 全程单调
    assert [r1["step_id"], r2["step_id"], r3["step_id"]] == \
        ["s00000001", "s00000002", "s00000003"]
    assert state["round_step_count"] == 1   # 新轮只有 1 条


# ========================
# Case 5：ReAct user → assistant(tool_calls) → tool → assistant → 全 1 轮，
#         first_reply 标第 1 个 assistant
# ========================
def test_case5_react_all_in_one_round():
    state, meta = fresh()
    r1 = run(user("帮我查天气"), state, meta, now_ts=100.0)
    r2 = run(assistant_tc("call_A"), state, meta, now_ts=101.0)   # 纯 tool_calls 那条
    r3 = run(tool("call_A", "晴"), state, meta, now_ts=102.0)
    r4 = run(assistant("今天晴天"), state, meta, now_ts=103.0)

    rounds = {r1["round_id"], r2["round_id"], r3["round_id"], r4["round_id"]}
    assert rounds == {"r000001"}              # 全在 1 轮
    assert r2["first_reply"] is True          # first_reply 锚在第 1 个 assistant（纯 tool_calls）
    assert r4["first_reply"] is False         # 后续 assistant 不再是 first_reply
    assert state["first_reply_step_id"] == r2["step_id"]
    assert state["round_step_count"] == 4


# ========================
# Case 6：双上限 step → 31 条 → 切轮
# ========================
def test_case6_max_steps_split():
    state, meta = fresh()
    # round_max_steps=30：前 30 条在 r000001，第 31 条触发切轮
    results = []
    for i in range(31):
        # 全部 user（连续 user，本不会因规则 a 切轮，验证纯双上限）
        results.append(run(user(f"m{i}"), state, meta, now_ts=100.0 + i,
                           round_max_steps=30, round_max_tokens=10 ** 9))
    first_round = results[0]["round_id"]
    assert first_round == "r000001"
    # 前 30 条同轮
    assert all(r["round_id"] == "r000001" for r in results[:30])
    # 第 31 条切到新轮
    assert results[30]["round_id"] == "r000002"
    assert results[30]["new_round_opened"] is True
    assert results[30]["closed_round"] == "r000001"


# ========================
# Case 7：双上限 token → 累计 > 8000 → 切轮
# ========================
def test_case7_max_tokens_split():
    state, meta = fresh()
    # 每条 3000 token，round_max_tokens=8000
    # 第1条：累计3000(轮内0→3000)；第2条：6000；第3条：9000；
    # 第4条进来时 round_token_count=9000 >= 8000 → 切轮
    r1 = run(user("a"), state, meta, now_ts=100.0, msg_tokens=3000,
             round_max_steps=10 ** 9, round_max_tokens=8000)
    r2 = run(user("b"), state, meta, now_ts=101.0, msg_tokens=3000,
             round_max_steps=10 ** 9, round_max_tokens=8000)
    r3 = run(user("c"), state, meta, now_ts=102.0, msg_tokens=3000,
             round_max_steps=10 ** 9, round_max_tokens=8000)
    r4 = run(user("d"), state, meta, now_ts=103.0, msg_tokens=3000,
             round_max_steps=10 ** 9, round_max_tokens=8000)

    assert r1["round_id"] == r2["round_id"] == r3["round_id"] == "r000001"
    assert r4["round_id"] == "r000002"        # 累计超 8000 后切轮
    assert r4["closed_round"] == "r000001"


# ========================
# Case 8：跨窗口独立 → 两 window 各自 round_id 从 r000001 起，互不干扰
# ========================
def test_case8_per_window_independent():
    state_a, meta_a = fresh()
    state_b, meta_b = fresh()

    # 窗口 A 跑 2 轮（user→assistant→user）
    run(user("A1"), state_a, meta_a, now_ts=100.0)
    run(assistant("a1"), state_a, meta_a, now_ts=101.0)
    ra3 = run(user("A2"), state_a, meta_a, now_ts=102.0)

    # 窗口 B 第一条 user
    rb1 = run(user("B1"), state_b, meta_b, now_ts=200.0)

    assert ra3["round_id"] == "r000002"       # A 已到第 2 轮
    assert rb1["round_id"] == "r000001"       # B 从头起，不受 A 影响
    assert rb1["step_id"] == "s00000001"      # B 的 step 也独立从 1 起
    assert meta_a["next_round_id"] == 3
    assert meta_b["next_round_id"] == 2


# ========================
# 附加：state.json 原子读写 round-trip
# ========================
def test_state_persistence_roundtrip():
    state, meta = fresh()
    run(user("hi"), state, meta, now_ts=100.0)
    run(assistant("yo"), state, meta, now_ts=101.0)

    with tempfile.TemporaryDirectory() as d:
        wk = "GroupMessage:12345"
        save_state(d, wk, state)
        fp = state_file_path(d, wk)
        assert os.path.exists(fp)
        assert fp.endswith("GroupMessage_12345.state.json")

        loaded = load_state(d, wk)
        assert loaded["current_round_id"] == "r000001"
        assert loaded["first_reply_step_id"] == "s00000002"
        assert loaded["partial_round_status"] == PARTIAL_OPEN

        # 不存在的窗口返回全新空状态
        empty = load_state(d, "PrivateMessage:999")
        assert empty["current_round_id"] is None
        assert empty["partial_round_status"] == PARTIAL_CLOSED


# ========================
# 附加：号位格式 + round_id 单调（撤回/空轮不跳号语义的回归保护）
# ========================
def test_id_format_and_monotonic():
    assert format_round_id(1) == "r000001"
    assert format_round_id(123) == "r000123"
    assert format_step_id(1) == "s00000001"
    assert format_step_id(42) == "s00000042"

    state, meta = fresh()
    ids = []
    # user → assistant → user → assistant ... 4 轮交替
    for i in range(8):
        msg = user(f"u{i}") if i % 2 == 0 else assistant(f"a{i}")
        ids.append(run(msg, state, meta, now_ts=100.0 + i)["step_id"])
    # step_id 严格单调连续，无跳号无重号
    nums = [int(s[1:]) for s in ids]
    assert nums == list(range(1, 9))


# ========================
# 直接运行入口（不依赖 pytest 也能跑）
# ========================
if __name__ == "__main__":
    tests = [
        ("Case1 正常轮 user→assistant", test_case1_normal_round),
        ("Case2 连续 user×3 同轮", test_case2_consecutive_users_same_round),
        ("Case3 idle 闭合", test_case3_idle_close),
        ("Case4 下条 user 闭合开新轮", test_case4_next_user_opens_new_round),
        ("Case5 ReAct 全 1 轮 first_reply 锚定", test_case5_react_all_in_one_round),
        ("Case6 双上限 step 切轮", test_case6_max_steps_split),
        ("Case7 双上限 token 切轮", test_case7_max_tokens_split),
        ("Case8 跨窗口独立", test_case8_per_window_independent),
        ("Extra state.json 原子读写", test_state_persistence_roundtrip),
        ("Extra 号位格式+单调", test_id_format_and_monotonic),
    ]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n结果：{passed}/{len(tests)} 通过")
    sys.exit(0 if passed == len(tests) else 1)
