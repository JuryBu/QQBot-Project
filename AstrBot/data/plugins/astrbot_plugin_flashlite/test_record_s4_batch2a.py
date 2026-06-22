"""
S4 批2a 单测：round-group 确定性聚合骨架
=========================================
覆盖 7 个验收点（依据 QQBotPlan/Plan_5/S4_实现方案.md §三批2 + S4_设计决策.md D3/D4）：
  ① compose 确定性——同输入同分组（代码确定性回滚窗口 + 模型只窗内分段，绝不逐轮问）
  ② 回滚窗口——默认回滚 1 组；尾组短 + 新增 >= 10 → 回滚 2 组
  ③ 增量——只重组「最老未聚合 + 回滚窗口」，已 sealed 组绝不动
  ④ force_seal——组达 15轮 / token / age 任一上限 → 强制封档（无视开放信号）
  ⑤ 预算切批——单轮超预算走 step 级 fallback（巨轮独占一批 + 单组 + 封档）
  ⑥ LLM 失败兜底——caller 抛异常 / 返回空 → 不写盘、维持未分组态、带冷却建议
  ⑦ validate 门禁——坏候选（区间越界 / 重叠）拒收，绝不覆盖正式 record.md

LLM 全部用确定性 mock caller（不接真 _call_flash_lite）。

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_batch2a.py -q
或直接：
  AstrBot/.venv/Scripts/python.exe test_record_s4_batch2a.py
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

import record  # noqa: E402
import checkpoint  # noqa: E402


WK = "GroupMessage:222333"


# ========================
# 测试夹具：构造 message / mock caller
# ========================
def _msgs(round_range, *, per_round=2, char_len=20):
    """构造 [s, e] 闭区间内每轮 per_round 条 message（带 round_id）。

    每条 content 长度约 char_len，便于控制预算切批。
    """
    s, e = round_range
    out = []
    for rn in range(s, e + 1):
        rid = record.format_rg_id  # noqa: F841  (占位防误用)
        round_id = f"r{rn:06d}"
        for k in range(per_round):
            role = "user" if k == 0 else "assistant"
            out.append({
                "role": role,
                "content": ("x" * char_len) + f"#{rn}.{k}",
                "round_id": round_id,
                "step_id": f"s{rn * 10 + k:08d}",
            })
    return out


def _state(groups=None, last_grouped=None):
    return {
        "round_groups": list(groups or []),
        "last_grouped_rg_id": last_grouped,
        "hit_table": {},
    }


def make_caller(group_size=8, calls_log=None):
    """确定性 mock caller：把一批轮按 group_size 切成若干组，每组吐固定文本。

    不依赖任何随机/时间，同输入必同输出（验证确定性）。
    """
    def caller(batch_rounds, cfg):
        if calls_log is not None:
            calls_log.append([r["round_int"] for r in batch_rounds])
        ints = sorted(r["round_int"] for r in batch_rounds)
        specs = []
        i = 0
        while i < len(ints):
            chunk = ints[i:i + group_size]
            s, e = chunk[0], chunk[-1]
            specs.append({
                "round_start": s,
                "round_end": e,
                "full_text": f"FULL[{s}-{e}]",
                "summary_text": f"SUM[{s}-{e}]",
                "title": f"T[{s}-{e}]",
            })
            i += group_size
        return specs
    return caller


# ========================
# ① compose 确定性
# ========================
def test_compose_deterministic_same_input():
    """同输入两次 compose → round_groups 边界完全一致（确定性）。"""
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        msgs = _msgs([1, 16])
        r1 = record.compose_record(d1, WK, msgs, _state(), make_caller(8))
        r2 = record.compose_record(d2, WK, msgs, _state(), make_caller(8))
        assert r1.wrote and r2.wrote
        b1 = [(g["rg_id"], g["round_range"]) for g in r1.round_groups]
        b2 = [(g["rg_id"], g["round_range"]) for g in r2.round_groups]
        assert b1 == b2
        # 16 轮 / 每组 8 → 2 组
        assert len(r1.round_groups) == 2
        assert r1.round_groups[0]["round_range"] == [1, 8]
        assert r1.round_groups[1]["round_range"] == [9, 16]
        assert r1.last_grouped_rg_id == r1.round_groups[-1]["rg_id"]


def test_compose_caller_not_called_per_round():
    """caller 收到的是『一批轮』而非逐轮——验证不逐轮问模型。"""
    with tempfile.TemporaryDirectory() as d:
        calls = []
        msgs = _msgs([1, 10])
        record.compose_record(d, WK, msgs, _state(), make_caller(8, calls))
        # 一批就涵盖全部 10 轮（远少于 10 次调用）
        assert len(calls) == 1
        assert calls[0] == list(range(1, 11))


# ========================
# ② 回滚窗口
# ========================
def test_rollback_default_one():
    """已有 2 组（未封档），新增轮 → 默认回滚最后 1 组重写。"""
    with tempfile.TemporaryDirectory() as d:
        prev = _state(
            groups=[
                {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
                 "sealed": True},
                {"rg_id": "rg000002", "round_range": [9, 14], "tier": "full",
                 "sealed": False},
            ],
            last_grouped="rg000002",
        )
        # 新增 15-20（6 轮，不足回滚 2 的门槛）
        msgs = _msgs([9, 20])
        res = record.compose_record(d, WK, msgs, prev, make_caller(8))
        assert res.wrote
        # rg000001(sealed) 保留不动；rg000002 被回滚重写
        assert res.round_groups[0]["rg_id"] == "rg000001"
        assert res.round_groups[0]["round_range"] == [1, 8]
        # 回滚窗口 = 9..20，按 8 切 → [9,16],[17,20]
        rest = [g["round_range"] for g in res.round_groups[1:]]
        assert rest == [[9, 16], [17, 20]]


def test_rollback_two_when_short_tail_and_many_new():
    """尾组短（<4 轮）且新增 >= 10 → 回滚 2 组。"""
    with tempfile.TemporaryDirectory() as d:
        prev = _state(
            groups=[
                {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
                 "sealed": False},
                {"rg_id": "rg000002", "round_range": [9, 10], "tier": "full",
                 "sealed": False},  # 短尾组（2 轮 < 4）
            ],
            last_grouped="rg000002",
        )
        # 新增 11-22（12 轮 >= 10）
        msgs = _msgs([1, 22])
        res = record.compose_record(d, WK, msgs, prev, make_caller(8))
        assert res.wrote
        # 两组都被回滚 → 窗口从 round 1 起重写
        assert res.round_groups[0]["round_range"][0] == 1
        # 重切：1-22 按 8 → [1,8],[9,16],[17,22]
        ranges = [g["round_range"] for g in res.round_groups]
        assert ranges == [[1, 8], [9, 16], [17, 22]]


# ========================
# ③ 增量：只重组最老未聚合 + sealed 组不动
# ========================
def test_incremental_sealed_groups_untouched():
    """已 sealed 组绝不进回滚窗口，原文本/边界/rg_id 全保留。"""
    with tempfile.TemporaryDirectory() as d:
        prev = _state(
            groups=[
                {"rg_id": "rg000001", "round_range": [1, 8], "tier": "summary",
                 "sealed": True, "full_text": "OLD1"},
                {"rg_id": "rg000002", "round_range": [9, 16], "tier": "summary",
                 "sealed": True, "full_text": "OLD2"},
            ],
            last_grouped="rg000002",
        )
        msgs = _msgs([17, 24])
        res = record.compose_record(d, WK, msgs, prev, make_caller(8))
        assert res.wrote
        # 两 sealed 组原样保留（含 rg_id / range / 旧文本）
        assert res.round_groups[0]["rg_id"] == "rg000001"
        assert res.round_groups[0]["full_text"] == "OLD1"
        assert res.round_groups[0]["sealed"] is True
        assert res.round_groups[1]["rg_id"] == "rg000002"
        assert res.round_groups[1]["full_text"] == "OLD2"
        # 新组接在 rg000003
        assert res.round_groups[2]["rg_id"] == "rg000003"
        assert res.round_groups[2]["round_range"] == [17, 24]


def test_incremental_sealed_barrier_blocks_rollback():
    """sealed 组是回滚屏障：[open, sealed, open] 时只回滚尾部 open，
    绝不跨越中间 sealed 组回滚（否则破坏已封档区间）。"""
    with tempfile.TemporaryDirectory() as d:
        prev = _state(
            groups=[
                {"rg_id": "rg000001", "round_range": [1, 8], "sealed": False,
                 "full_text": "G1"},
                {"rg_id": "rg000002", "round_range": [9, 16], "sealed": True,
                 "full_text": "G2"},  # 中间 sealed 屏障
                {"rg_id": "rg000003", "round_range": [17, 20], "sealed": False,
                 "full_text": "G3"},  # 尾部可回滚
            ],
            last_grouped="rg000003",
        )
        msgs = _msgs([17, 28])  # 新增 21-28
        res = record.compose_record(d, WK, msgs, prev, make_caller(8))
        assert res.wrote
        # rg000001 / rg000002 都在 sealed 屏障保护下保留（rg000001 在 sealed 之前）
        assert res.round_groups[0]["rg_id"] == "rg000001"
        assert res.round_groups[0]["full_text"] == "G1"
        assert res.round_groups[1]["rg_id"] == "rg000002"
        assert res.round_groups[1]["full_text"] == "G2"
        # 只有尾部 rg000003 被回滚重写（窗口从 17 起）
        assert res.round_groups[2]["round_range"][0] == 17


def test_incremental_no_new_rounds_noop():
    """无新增未聚合轮 → 不写盘、维持原状。"""
    with tempfile.TemporaryDirectory() as d:
        prev = _state(
            groups=[{"rg_id": "rg000001", "round_range": [1, 8], "sealed": True}],
            last_grouped="rg000001",
        )
        msgs = _msgs([1, 8])  # 全在水位内
        res = record.compose_record(d, WK, msgs, prev, make_caller(8))
        assert not res.wrote
        assert res.round_groups == prev["round_groups"]


def test_incremental_only_consumes_new_above_watermark():
    """增量起点 = 水位之后；水位内的轮不参与（除非被回滚）。"""
    with tempfile.TemporaryDirectory() as d:
        prev = _state(
            groups=[{"rg_id": "rg000001", "round_range": [1, 8], "sealed": True}],
            last_grouped="rg000001",
        )
        # 传入 1-16 的全部消息，但 1-8 已聚合且 sealed → 窗口只含 9-16
        msgs = _msgs([1, 16])
        calls = []
        res = record.compose_record(d, WK, msgs, prev, make_caller(8, calls))
        assert res.wrote
        # caller 只看到 9..16（水位之后），没有 1..8
        assert calls[0] == list(range(9, 17))


# ========================
# ④ force_seal 强制封档
# ========================
def test_force_seal_by_rounds():
    """组覆盖 15 轮 → 强制封档（无视开放信号）。"""
    g = {"round_range": [1, 15]}  # 跨度 15 轮
    assert record.force_seal_check(g) is True
    g2 = {"round_range": [1, 8]}  # 8 轮
    assert record.force_seal_check(g2) is False


def test_force_seal_by_tokens():
    g = {"round_range": [1, 3], "token_est": 30000}
    assert record.force_seal_check(g) is True
    g2 = {"round_range": [1, 3], "token_est": 100}
    assert record.force_seal_check(g2) is False


def test_force_seal_by_age():
    """now_round 远超组终点 → age 超限封档。"""
    g = {"round_range": [1, 8]}
    assert record.force_seal_check(g, now_round=100) is True   # age=92
    assert record.force_seal_check(g, now_round=20) is False   # age=12


def test_compose_applies_force_seal_on_big_group():
    """compose 内 caller 产 15+ 轮大组 → 自动 sealed=True。"""
    with tempfile.TemporaryDirectory() as d:
        msgs = _msgs([1, 18])
        # caller group_size=18 → 一组 18 轮（>=15）
        res = record.compose_record(d, WK, msgs, _state(), make_caller(18))
        assert res.wrote
        assert len(res.round_groups) == 1
        assert res.round_groups[0]["round_range"] == [1, 18]
        assert res.round_groups[0]["sealed"] is True  # 强制封档


# ========================
# ⑤ 预算切批：单轮超限 step fallback
# ========================
def test_oversize_round_isolated_and_sealed():
    """巨轮（单轮字符超预算）独占一批 + 单组 + 封档，不与别人挤批。"""
    with tempfile.TemporaryDirectory() as d:
        # round 2 是巨轮：单条 content 70000 字符 > rg_max_batch_chars(60000)
        msgs = (
            _msgs([1, 1]) +
            [{"role": "user", "content": "y" * 70000, "round_id": "r000002",
              "step_id": "s00000020"}] +
            _msgs([3, 3])
        )
        calls = []
        res = record.compose_record(
            d, WK, msgs, _state(), make_single_group_caller_log(calls)
        )
        assert res.wrote
        # 巨轮 round 2 应独占一批（在 calls 中单独出现）
        assert [2] in calls
        # round 2 对应组 sealed=True
        g2 = [g for g in res.round_groups if g["round_range"] == [2, 2]]
        assert len(g2) == 1 and g2[0]["sealed"] is True
        # 三批（[1],[2]巨轮,[3]）拼接后整体连续无空洞、通过 validate
        ranges = [g["round_range"] for g in res.round_groups]
        assert ranges == [[1, 1], [2, 2], [3, 3]]
        # rg_id 单调连续
        assert [g["rg_id"] for g in res.round_groups] == [
            "rg000001", "rg000002", "rg000003"]


def test_budget_split_multiple_batches():
    """普通轮按字符预算切多批：每轮 ~30K，2 轮即超 60K → 多批。"""
    with tempfile.TemporaryDirectory() as d:
        # 4 轮，每轮单条 content 30000 字符 → 2 轮约 60K 触发切批
        msgs = []
        for rn in range(1, 5):
            msgs.append({"role": "user", "content": "z" * 30000,
                         "round_id": f"r{rn:06d}", "step_id": f"s{rn:08d}"})
        calls = []
        res = record.compose_record(d, WK, msgs, _state(),
                                    make_single_group_caller_log(calls))
        assert res.wrote
        # 切成多批（>1 次 caller 调用）
        assert len(calls) >= 2


# ========================
# ⑥ LLM 失败兜底
# ========================
def test_llm_failure_keeps_unstructured():
    """caller 抛异常 → 不写盘、维持未分组态（回传 prev round_groups）、带冷却建议。"""
    with tempfile.TemporaryDirectory() as d:
        def boom(batch_rounds, cfg):
            raise RuntimeError("flash lite down")

        prev = _state(
            groups=[{"rg_id": "rg000001", "round_range": [1, 8], "sealed": True}],
            last_grouped="rg000001",
        )
        msgs = _msgs([9, 16])
        res = record.compose_record(d, WK, msgs, prev, boom)
        assert not res.wrote
        assert res.fallback is True
        assert res.cooldown_until is not None and res.cooldown_until > 0
        # 维持未分组态：round_groups 不变
        assert res.round_groups == prev["round_groups"]
        # record.md 没被写出
        assert record.read_record(d, WK) == ""


def test_llm_empty_return_fallback():
    """caller 返回空列表 → 兜底（视为无产出）。"""
    with tempfile.TemporaryDirectory() as d:
        res = record.compose_record(
            d, WK, _msgs([1, 8]), _state(), lambda b, c: []
        )
        assert not res.wrote
        assert res.fallback is True


def test_llm_retry_then_success():
    """首次失败、重试成功 → 正常写盘（验证有限重试）。"""
    with tempfile.TemporaryDirectory() as d:
        state = {"n": 0}

        def flaky(batch_rounds, cfg):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("transient")
            ints = sorted(r["round_int"] for r in batch_rounds)
            return [{"round_start": ints[0], "round_end": ints[-1],
                     "full_text": "ok", "summary_text": "", "title": ""}]

        res = record.compose_record(d, WK, _msgs([1, 8]), _state(), flaky,
                                    {"rg_llm_retries": 2})
        assert res.wrote
        assert state["n"] == 2  # 失败 1 次 + 成功 1 次


# ========================
# ⑦ validate 门禁拒收坏候选
# ========================
def test_validate_rejects_out_of_window_spec():
    """caller 产出越界窗口的区间 → 落组阶段拒收，不写盘。"""
    with tempfile.TemporaryDirectory() as d:
        def bad(batch_rounds, cfg):
            # 区间 [1, 999]，999 越出窗口
            return [{"round_start": 1, "round_end": 999,
                     "full_text": "x", "summary_text": "", "title": ""}]

        res = record.compose_record(d, WK, _msgs([1, 8]), _state(), bad)
        assert not res.wrote
        assert res.errors
        assert record.read_record(d, WK) == ""


def test_validate_rejects_overlapping_specs():
    """caller 产出重叠的两组 → validate 门禁拒收，不覆盖正式文件。"""
    with tempfile.TemporaryDirectory() as d:
        # 先放一份合法正式 record.md
        ok0 = record.compose_record(d, WK, _msgs([1, 8]), _state(), make_caller(8))
        assert ok0.wrote
        before = record.read_record(d, WK)

        def overlap(batch_rounds, cfg):
            return [
                {"round_start": 9, "round_end": 14, "full_text": "a",
                 "summary_text": "", "title": ""},
                {"round_start": 12, "round_end": 16, "full_text": "b",  # 重叠
                 "summary_text": "", "title": ""},
            ]

        prev = _state(groups=ok0.round_groups, last_grouped=ok0.last_grouped_rg_id)
        res = record.compose_record(d, WK, _msgs([1, 16]), prev, overlap)
        assert not res.wrote
        assert res.errors
        # 正式 record.md 维持原样
        assert record.read_record(d, WK) == before


# ========================
# 辅助：带日志的单组 caller（⑤ 用，记录每批轮号）
# ========================
def make_single_group_caller_log(calls_log):
    def caller(batch_rounds, cfg):
        calls_log.append([r["round_int"] for r in batch_rounds])
        ints = sorted(r["round_int"] for r in batch_rounds)
        s, e = ints[0], ints[-1]
        return [{"round_start": s, "round_end": e,
                 "full_text": f"FULL[{s}-{e}]", "summary_text": "", "title": ""}]
    return caller


# ========================
# D4：message.rg_id 已砍
# ========================
def test_d4_message_rg_id_removed():
    """_MESSAGE_V2_DEFAULTS 不再含 rg_id 字段（组归属按 round_id 区间推断）。"""
    assert "rg_id" not in checkpoint._MESSAGE_V2_DEFAULTS
    m = checkpoint._ensure_message_v2_fields({"role": "user", "content": "hi"})
    assert "rg_id" not in m


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
