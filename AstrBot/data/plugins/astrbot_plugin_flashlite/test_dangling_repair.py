"""
S3 F1.5 dangling tool_calls 防御 单元测试
==========================================
验证 _repair_tool_call_pairs 纯函数：覆盖找茬 D 边界（末尾 dangling / 中段
parallel 部分缺失 / orphan 缺头 / 多 dangling / 正常配对 / 普通消息 / 修复后不变量）。

跑法：AstrBot/.venv/Scripts/python.exe test_dangling_repair.py
"""
import os
import sys
import types

# Windows 控制台默认 GBK，emoji 打印会 UnicodeEncodeError；切 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Patch astrbot.api.logger 依赖（与 test_checkpoint_v2.py 一致）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class MockLogger:
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def debug(self, msg): pass


astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = MockLogger()
astrbot = types.ModuleType("astrbot")
astrbot.api = astrbot_api
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = astrbot_api

from checkpoint import _repair_tool_call_pairs, DANGLING_TOOL_PLACEHOLDER


# ========================
# 辅助：构造消息
# ========================

def asst_tc(*tcids, content=None):
    """assistant 消息携带 tool_calls"""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": tcid, "type": "function",
             "function": {"name": "f", "arguments": "{}"}}
            for tcid in tcids
        ],
    }


def tool_result(tcid, content="result"):
    return {"role": "tool", "tool_call_id": tcid, "content": content}


def user(text):
    return {"role": "user", "content": text}


def asst(text):
    return {"role": "assistant", "content": text}


def assert_no_400(contexts):
    """模拟 provider 不会 400 的不变量：
    1. 每个 assistant.tool_calls[*].id 都有紧随其后（在下一条非 tool 消息之前）的
       role=tool 结果
    2. 每个 role=tool 都有前序 assistant.tool_calls 发起（无 orphan）
    """
    pending = {}  # tcid -> True（已发起未配对）
    i = 0
    n = len(contexts)
    while i < n:
        msg = contexts[i]
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            # 收集本 assistant 发起的 tcid
            issued = [tc["id"] for tc in msg["tool_calls"]]
            # 紧随其后的 tool 段必须覆盖全部 issued（集合相等）
            j = i + 1
            covered = set()
            while j < n and contexts[j].get("role") == "tool":
                covered.add(contexts[j]["tool_call_id"])
                j += 1
            missing = set(issued) - covered
            assert not missing, f"位置 {i} assistant.tool_calls 缺配对: {missing}"
            i = j
        elif role == "tool":
            # 走到这说明前面不是 assistant.tool_calls（否则被上面消费）→ orphan
            raise AssertionError(f"位置 {i} 出现 orphan tool: {msg.get('tool_call_id')}")
        else:
            i += 1


# ========================
# 7 个测试 case
# ========================

def test_1_tail_dangling():
    """case 1: 末尾 dangling — assistant.tool_calls=[A] 无 tool → 补 A 占位"""
    print("🧪 case1 末尾 dangling")
    ctx = [user("hi"), asst_tc("A")]
    out, repairs = _repair_tool_call_pairs(ctx)
    assert len(out) == 3, out
    assert out[2] == {"role": "tool", "tool_call_id": "A",
                      "content": DANGLING_TOOL_PLACEHOLDER}, out[2]
    assert len(repairs) == 1 and repairs[0]["type"] == "dangling_placeholder"
    assert repairs[0]["tool_call_id"] == "A"
    assert_no_400(out)
    print("   ✅ 补 A 占位")


def test_2_parallel_partial():
    """case 2: 中段 parallel 部分缺失 — [A,B,C] + tool(A) + tool(B) → 只补 C"""
    print("🧪 case2 parallel 部分缺失")
    ctx = [
        user("hi"),
        asst_tc("A", "B", "C"),
        tool_result("A", "ra"),
        tool_result("B", "rb"),
        user("next"),
    ]
    out, repairs = _repair_tool_call_pairs(ctx)
    # 只补 C，且 A/B 原结果保留
    danglings = [r for r in repairs if r["type"] == "dangling_placeholder"]
    assert len(danglings) == 1 and danglings[0]["tool_call_id"] == "C", repairs
    # 占位 C 应紧跟在 tool(B) 之后、user("next") 之前
    c_msgs = [m for m in out if m.get("tool_call_id") == "C"]
    assert len(c_msgs) == 1 and c_msgs[0]["content"] == DANGLING_TOOL_PLACEHOLDER
    # A/B 原结果未被破坏
    assert any(m.get("tool_call_id") == "A" and m.get("content") == "ra" for m in out)
    assert any(m.get("tool_call_id") == "B" and m.get("content") == "rb" for m in out)
    # user("next") 仍在末尾
    assert out[-1] == user("next"), out[-1]
    assert_no_400(out)
    print("   ✅ 只补 C，保留 A/B 结果")


def test_3_orphan_head():
    """case 3: orphan 缺头 — 首条 role=tool（无前序 assistant）→ 删除"""
    print("🧪 case3 orphan 缺头")
    ctx = [tool_result("X", "rx"), user("hi"), asst("ok")]
    out, repairs = _repair_tool_call_pairs(ctx)
    assert len(out) == 2, out
    assert all(m.get("role") != "tool" for m in out), out
    orphans = [r for r in repairs if r["type"] == "orphan_dropped"]
    assert len(orphans) == 1 and orphans[0]["tool_call_id"] == "X"
    assert_no_400(out)
    print("   ✅ orphan X 已删除")


def test_4_multi_dangling():
    """case 4: 多 dangling — 两个 assistant 各有未配对 tool_calls → 各补"""
    print("🧪 case4 多 dangling")
    ctx = [
        user("u1"),
        asst_tc("A"),          # dangling A
        user("u2"),
        asst_tc("B"),          # dangling B
    ]
    out, repairs = _repair_tool_call_pairs(ctx)
    danglings = {r["tool_call_id"] for r in repairs if r["type"] == "dangling_placeholder"}
    assert danglings == {"A", "B"}, repairs
    # A 占位紧跟第一个 assistant、在 u2 之前
    idx_a_asst = next(i for i, m in enumerate(out)
                      if m.get("role") == "assistant" and m.get("tool_calls")
                      and m["tool_calls"][0]["id"] == "A")
    assert out[idx_a_asst + 1] == {"role": "tool", "tool_call_id": "A",
                                   "content": DANGLING_TOOL_PLACEHOLDER}
    assert_no_400(out)
    print("   ✅ A/B 各补占位")


def test_5_normal_pair():
    """case 5: 正常配对 — assistant.tool_calls=[A] + tool(A) → 不动"""
    print("🧪 case5 正常配对")
    ctx = [user("hi"), asst_tc("A"), tool_result("A", "ra"), asst("done")]
    out, repairs = _repair_tool_call_pairs(ctx)
    assert repairs == [], repairs
    # 不动：返回原对象（identity 相等，零拷贝路径）
    assert out is ctx, "正常配对应原样返回（不复制）"
    assert_no_400(out)
    print("   ✅ 无修复，原样返回")


def test_6_plain_messages():
    """case 6: 空 / 无 tool_calls — 普通 user/assistant → 不动"""
    print("🧪 case6 普通消息 / 空")
    # 空
    out, repairs = _repair_tool_call_pairs([])
    assert out == [] and repairs == []
    # 纯对话
    ctx = [user("hi"), asst("hello"), user("bye"), asst("see ya")]
    out, repairs = _repair_tool_call_pairs(ctx)
    assert repairs == [] and out is ctx
    assert_no_400(out)
    print("   ✅ 普通消息不动")


def test_7_post_repair_invariant():
    """case 7: 修复后不变量 — 复杂混合场景修复后所有 assistant.tool_calls
    都有紧随的 tool 结果（模拟 OpenAI 不会 400）"""
    print("🧪 case7 修复后不变量")
    ctx = [
        tool_result("ORPHAN", "leak"),       # orphan 缺头 → 删
        user("u1"),
        asst_tc("A", "B"),                   # B dangling（只有 A 配对）
        tool_result("A", "ra"),
        user("u2"),
        asst_tc("C"),                        # C dangling 末尾
    ]
    out, repairs = _repair_tool_call_pairs(ctx)
    # 不变量：整体过 assert_no_400
    assert_no_400(out)
    # orphan 被删
    assert not any(m.get("tool_call_id") == "ORPHAN" for m in out)
    # B、C 补占位
    placeholders = {m["tool_call_id"] for m in out
                    if m.get("role") == "tool" and m.get("content") == DANGLING_TOOL_PLACEHOLDER}
    assert placeholders == {"B", "C"}, placeholders
    # A 真实结果保留
    assert any(m.get("tool_call_id") == "A" and m.get("content") == "ra" for m in out)
    types_ = sorted(r["type"] for r in repairs)
    assert types_ == ["dangling_placeholder", "dangling_placeholder", "orphan_dropped"], types_
    print("   ✅ 混合场景修复后满足 no-400 不变量")


def test_extra_custom_placeholder():
    """附加：自定义 placeholder 文案生效"""
    print("🧪 extra 自定义占位文案")
    ctx = [asst_tc("A")]
    out, _ = _repair_tool_call_pairs(ctx, placeholder="[CUSTOM]")
    assert out[1]["content"] == "[CUSTOM]"
    print("   ✅ 自定义文案生效")


if __name__ == "__main__":
    tests = [
        test_1_tail_dangling,
        test_2_parallel_partial,
        test_3_orphan_head,
        test_4_multi_dangling,
        test_5_normal_pair,
        test_6_plain_messages,
        test_7_post_repair_invariant,
        test_extra_custom_placeholder,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"   ❌ {t.__name__} FAILED: {e}")
            import traceback
            traceback.print_exc()
    print()
    if failed == 0:
        print(f"✅ 全部 {len(tests)} 个测试通过")
    else:
        print(f"❌ {failed}/{len(tests)} 个测试失败")
        sys.exit(1)
