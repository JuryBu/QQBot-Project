"""
S4 批6 单测：对抗审查确认的 9 个真 bug（4 类根因）修复回归
=============================================================
覆盖验收 6 场景：
  ① 窗口全覆盖门禁（Cluster A #1/#4/#5）：造漏前导轮的候选 → 被拒 + fallback + 原文不丢
  ② load merge_buffer=False（Cluster B #2）：落盘不含 round_id=None 的 buffer 消息
  ③ flush_hit_queue 不泄漏 buffer（Cluster B #3）
  ④ 多模态组 base=full 时 hit 升档保持 full 不降（Cluster C #8）
  ⑤ 滞回 prev_tier 取真实上次档（非恒 full）（Cluster C #9）
  ⑥ fallback 空 list → 触发全量（Cluster D #6）

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_audit_fix.py -q
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

# mock astrbot 包（checkpoint.py 顶部 from astrbot.api import logger）
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
from checkpoint import TFileManager  # noqa: E402


WK = "GroupMessage:audit6"


# ============================================================
# fixtures / helpers
# ============================================================
@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s4_b6_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    # record.py 内部用自己模块级 CHECKPOINTS_DIR？compose_record 走传参，不依赖全局。
    yield d


def _fp(d, wk):
    return os.path.join(d, wk.replace(":", "_") + ".json")


def _msg(rn, role, content_mul=1):
    """构造带真轮号的 message（够长以便 compose 能聚合）。"""
    return {
        "role": role,
        "content": f"第{rn}轮{role}：" + "内容" * (50 * content_mul),
        "round_id": f"r{rn:06d}",
        "step_id": f"s{rn * 2:08d}",
        "message_id": f"{role[0]}{rn}",
    }


def _msgs_for_rounds(round_ints):
    msgs = []
    for rn in round_ints:
        msgs.append(_msg(rn, "user"))
        msgs.append(_msg(rn, "assistant"))
    return msgs


def _g(rg_id, s, e, **kw):
    d = {"rg_id": rg_id, "round_range": [s, e]}
    d.update(kw)
    return d


def _sealed_group(rg_id, s, e, *, tier="full", legacy=False, multimodal=False):
    g = {
        "rg_id": rg_id,
        "round_range": [s, e],
        "tier": tier,
        "sealed": True,
        "legacy_rg": legacy,
        "full_text": f"FULL[{rg_id}]",
        "summary_text": f"SUM[{rg_id}]",
        "brief_text": f"BRIEF[{rg_id}]",
    }
    if multimodal:
        g["has_multimodal"] = True
    return g


CFG = {
    "tier_summary_age": 20,
    "tier_brief_age": 60,
    "tier_hysteresis": 3,
    "hit_upgrade_threshold": 1.0,
    "hit_halflife": 3600.0,
    "hit_weight_raw": 1.0,
    "hit_weight_record": 0.5,
}


# ============================================================
# ① Cluster A — 窗口全覆盖门禁：漏前导轮的候选被拒 + fallback + 原文不丢
# ============================================================
def test_validate_rejects_seam_gap_lower_bound():
    """validate 规则5 下界：prev 水位16，候选首组 s=18（漏轮17）→ 拒收『接缝空洞』。
    对照 s=17(=水位+1) 通过、s=10(<水位) 拒『回退覆盖』。"""
    prev_state = {
        "last_grouped_rg_id": "rg000002",
        "round_groups": [
            _g("rg000001", 1, 8),
            _g("rg000002", 9, 16),
        ],
    }
    # s=18：漏掉接缝轮17 → 接缝空洞拒收（修复前 ok=True 放过）
    ok, errs = record.validate_composed_record([_g("rg000003", 18, 24)], prev_state)
    assert not ok
    assert any("接缝空洞" in e for e in errs), errs

    # s=17 == 水位+1 → 通过
    ok2, errs2 = record.validate_composed_record([_g("rg000003", 17, 24)], prev_state)
    assert ok2, errs2

    # s=10 <= 水位 → 回退覆盖（上界仍生效）
    ok3, errs3 = record.validate_composed_record([_g("rg000003", 10, 20)], prev_state)
    assert not ok3
    assert any("回退覆盖" in e for e in errs3), errs3


def test_validate_legacy_tail_exempts_seam_check():
    """水位锚点组是 legacy_rg 历史冷冻段时，豁免接缝连续校验（允许跳变）。"""
    prev_state = {
        "last_grouped_rg_id": "rg000000",
        "round_groups": [
            _g("rg000000", 1, 30, legacy_rg=True),
        ],
    }
    # 首组 s=100，跳过 31-99，但 legacy 尾豁免 → 通过
    ok, errs = record.validate_composed_record([_g("rg000001", 100, 108)], prev_state)
    assert ok, errs


def test_compose_window_coverage_gate_rejects_missing_leading(tmp_ckpt):
    """compose_record 窗口全覆盖门禁：LLM 漏掉窗口前导轮 → 拒收 wrote=False +
    fallback=True，维持未分组态（prev round_groups 不变，原文不丢）。"""
    # 全新窗口（无 prev），窗口轮 = r1..r6。LLM 漏掉前导 r1,r2 只吐 [3,6]。
    msgs = _msgs_for_rounds([1, 2, 3, 4, 5, 6])
    prev_state = {"round_groups": [], "last_grouped_rg_id": None}

    def bad_caller(batch_rounds, cfg):
        # 漏掉前导轮：只覆盖 3..6，丢 1,2
        return [{
            "round_start": 3, "round_end": 6,
            "title": "漏前导", "full_text": "F", "summary_text": "S",
        }]

    res = record.compose_record(
        tmp_ckpt, WK, msgs, prev_state, bad_caller,
        cfg={"rg_target_rounds": 20, "rg_force_seal_rounds": 50},
        now_round=6,
    )
    assert res.wrote is False, "漏前导轮的候选必须被拒收"
    assert res.fallback is True, "拒收应 fallback=True 维持未分组态"
    # 原文不丢：维持 prev（空）round_groups，未推进水位 → 原文仍在消费侧 tail loop
    assert res.round_groups == [], res.round_groups
    # 错误信息含 window_uncovered（漏轮被门禁拦下）
    assert res.errors and any("window_uncovered" in e for e in res.errors), res.errors


def test_compose_full_coverage_passes(tmp_ckpt):
    """对照：LLM 全覆盖窗口（首段=最小轮号、连续无空洞）→ 正常落盘 wrote=True。"""
    msgs = _msgs_for_rounds([1, 2, 3, 4, 5, 6])
    prev_state = {"round_groups": [], "last_grouped_rg_id": None}

    def good_caller(batch_rounds, cfg):
        ints = sorted(int(r["round_int"]) for r in batch_rounds)
        return [{
            "round_start": ints[0], "round_end": ints[-1],
            "title": "全覆盖", "full_text": "F", "summary_text": "S",
        }]

    res = record.compose_record(
        tmp_ckpt, WK, msgs, prev_state, good_caller,
        cfg={"rg_target_rounds": 20, "rg_force_seal_rounds": 50},
        now_round=6,
    )
    assert res.wrote is True, res.errors
    cov = set()
    for g in res.round_groups:
        s, e = g["round_range"]
        cov |= set(range(s, e + 1))
    assert cov == {1, 2, 3, 4, 5, 6}, cov


# ============================================================
# ② Cluster B — load merge_buffer=False 落盘不含未取号 buffer 消息
# ============================================================
def test_load_merge_buffer_false_excludes_pending(tmp_ckpt):
    """load(merge_buffer=False) 返回纯磁盘视图，不含内存 buffer 未取号消息；
    merge_buffer=True（默认）则含。落盘前路径用 False，save 不持久化 round_id=None。"""
    mgr = TFileManager()

    async def run():
        # 先落盘一条带号消息
        await mgr.append_messages(WK, [{"role": "user", "content": "已落盘"}])
        # 往 buffer 塞一条未取号消息（不 flush）
        mgr.buffer_message(WK, {"role": "user", "content": "buffer未取号"})

        # merge_buffer=True：含 buffer 消息
        t_merged = await mgr.load(WK, merge_buffer=True)
        contents_merged = [m.get("content") for m in t_merged["messages"]]
        assert "buffer未取号" in contents_merged

        # merge_buffer=False：不含 buffer 消息
        t_raw = await mgr.load(WK, merge_buffer=False)
        contents_raw = [m.get("content") for m in t_raw["messages"]]
        assert "buffer未取号" not in contents_raw
        assert "已落盘" in contents_raw

        # 关键：把 merge_buffer=False 的视图 save 回盘，磁盘绝不含 round_id=None 的非 legacy 消息
        await mgr.save(WK, t_raw)
        disk = await mgr._load_t_file_raw(WK)
        none_rid = [
            m for m in disk["messages"]
            if m.get("round_id") is None and not m.get("legacy", False)
        ]
        assert none_rid == [], f"落盘不应含未取号 buffer 镜像: {none_rid}"
        # buffer 仍在内存（未被 load 清空），下次 flush 正常取号
        assert len(mgr._msg_buffer.get(WK, [])) == 1

    asyncio.run(run())


def test_compose_commit_does_not_persist_buffer(tmp_ckpt):
    """compose 提交块（merge_buffer=False）落盘后，磁盘不含 round_id=None 的 buffer 镜像。
    模拟 compose 进行期间有并发未取号消息进 buffer。"""
    mgr = TFileManager()
    wk = "GroupMessage:b6cmp"
    fp = _fp(tmp_ckpt, wk)
    # 预置足量真轮以触发 compose（token 超阈值）
    msgs = []
    for rn in range(1, 15):
        msgs.append({"role": "user", "content": f"用户第{rn}轮：" + "字" * 300,
                     "round_id": f"r{rn:06d}", "step_id": f"s{rn*2:08d}",
                     "message_id": f"u{rn}"})
        msgs.append({"role": "assistant", "content": f"回复第{rn}轮：" + "字" * 300,
                     "round_id": f"r{rn:06d}", "step_id": f"s{rn*2+1:08d}",
                     "message_id": f"a{rn}"})
    t_file = {
        "version": 2, "window_key": wk,
        "T1": {"compressed_summary": "", "token_count": 0},
        "messages": msgs,
        "metadata": {
            "next_round_id": 15, "next_step_id": 30, "generation": 1,
            "total_messages_ever": len(msgs),
            "record_state": {
                "last_compressed_round_id": None, "last_grouped_rg_id": None,
                "round_groups": [], "hit_table": {},
            },
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        import re
        ints = [int(m) for m in re.findall(r"\[round (\d+)\]", prompt)]
        ints = sorted(set(ints))
        if not ints:
            return "[]"
        return json.dumps([{
            "round_start": ints[0], "round_end": ints[-1],
            "title": "seg", "full_text": "F", "summary_text": "S",
        }], ensure_ascii=False)

    async def run():
        # 模拟并发：compose 前往 buffer 注入未取号消息
        mgr.buffer_message(wk, {"role": "user", "content": "并发未取号消息"})
        snap = await mgr.load(wk)
        await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={"record_compose_token_limit": 100, "compress_delta_floor": 999999,
                 "record_max_relay_rounds": 1},
        )
        disk = await mgr._load_t_file_raw(wk)
        none_rid = [
            m for m in disk["messages"]
            if m.get("round_id") is None and not m.get("legacy", False)
        ]
        assert none_rid == [], f"compose 落盘不应含未取号 buffer 镜像: {none_rid}"

    asyncio.run(run())


# ============================================================
# ③ Cluster B — flush_hit_queue 不泄漏 buffer
# ============================================================
def test_flush_hit_queue_does_not_persist_buffer(tmp_ckpt):
    """flush_hit_queue 落盘 hit_table 时不把未取号 buffer 消息写盘。"""
    mgr = TFileManager()
    wk = "GroupMessage:b6hit"
    fp = _fp(tmp_ckpt, wk)
    pre_groups = [_sealed_group("rg000001", 1, 8)]
    t_file = {
        "version": 2, "window_key": wk,
        "T1": {"compressed_summary": "", "token_count": 0},
        "messages": [{"role": "user", "content": "已落盘", "round_id": "r000001",
                      "step_id": "s00000002", "message_id": "u1"}],
        "metadata": {
            "next_round_id": 9, "next_step_id": 18, "generation": 1,
            "total_messages_ever": 1,
            "record_state": {
                "last_compressed_round_id": None, "last_grouped_rg_id": "rg000001",
                "round_groups": pre_groups, "hit_table": {},
            },
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)

    async def run():
        # 命中入队
        mgr.record_hit(wk, round_int=5, hit_type="raw", now_round=20)
        # 并发：往 buffer 注入未取号消息
        mgr.buffer_message(wk, {"role": "user", "content": "hit并发未取号"})
        applied = await mgr.flush_hit_queue(wk)
        assert applied >= 1, "hit 应落定"
        disk = await mgr._load_t_file_raw(wk)
        none_rid = [
            m for m in disk["messages"]
            if m.get("round_id") is None and not m.get("legacy", False)
        ]
        assert none_rid == [], f"flush_hit_queue 落盘不应含未取号 buffer 镜像: {none_rid}"
        # hit_table 已写
        assert disk["metadata"]["record_state"]["hit_table"], "hit_table 应非空"

    asyncio.run(run())


# ============================================================
# ④ Cluster C #8 — 多模态组 base=full 时 hit 升档保持 full 不降
# ============================================================
def test_multimodal_young_hit_stays_full(tmp_ckpt=None):
    """年轻多模态组（base=full）命中 → 仍 full（修复前 min(full+1,summary)=summary 降档）。"""
    # round_range=[10,10]、now=15 → age=5 < summary_age(20) → base=FULL
    g = _sealed_group("rg000010", 10, 10, multimodal=True)
    ht = {"rg000010": {"hit_count": 9, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    # WITH hit：仍 full（不被多模态 cap=summary 降档）
    tier = record.tier_for_group(g, now_round=15, hit_table=ht, cfg=CFG, now_ts=1000.0)
    assert tier == record.TIER_FULL, f"年轻多模态命中应保持 full，实得 {tier}"
    # NO hit 对照：也 full
    tier_nohit = record.tier_for_group(g, now_round=15, hit_table={}, cfg=CFG, now_ts=1000.0)
    assert tier_nohit == record.TIER_FULL


def test_multimodal_old_hit_caps_summary(tmp_ckpt=None):
    """老组多模态命中封顶仍 summary（不因 max 钳位过头升 full）——保持既有行为。"""
    # base=summary(age=30) 多模态命中 → summary
    g = _sealed_group("rg000011", 1, 10, multimodal=True)
    ht = {"rg000011": {"hit_count": 9, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    tier = record.tier_for_group(g, now_round=40, hit_table=ht, cfg=CFG, now_ts=1000.0)
    assert tier == record.TIER_SUMMARY, f"老多模态命中应封顶 summary，实得 {tier}"
    # base=brief(age=90) 多模态命中 → summary（升一档封顶）
    g2 = _sealed_group("rg000012", 1, 10, multimodal=True)
    tier2 = record.tier_for_group(g2, now_round=100, hit_table={
        "rg000012": {"hit_count": 9, "last_hit_ts": 1000.0, "last_hit_type": "raw"}},
        cfg=CFG, now_ts=1000.0)
    assert tier2 == record.TIER_SUMMARY


# ============================================================
# ⑤ Cluster C #9 — 滞回 prev_tier 取真实上次档（读路径回写内存视图）
# ============================================================
def test_hysteresis_prev_tier_writeback(tmp_ckpt):
    """读路径回写 g['tier'] 使滞回 prev_tier 取真实上次档（非恒 full）。
    设计能区分修复前后：
      summary_age=20, brief_age=60, hysteresis=3 → brief 边界滞回带 [57,63)。
      组终点=10。第一次 build 在 age=70（>brief_age）算 base=brief，回写 g['tier']='brief'。
      第二次 build 在 age=58（brief 带内，base=summary）：
        - 修复后 prev=brief（真实回写）→ {summary,brief} 边界=60，age58∈[57,63) 粘 brief。
        - 修复前 prev=full（恒 init）→ {full,summary} 边界=20，age58∉[17,23) 不滞回 → summary。
      故修复前后第二次结果不同（brief vs summary），强区分。"""
    mgr = TFileManager()
    wk = "GroupMessage:b6hys"
    fp = _fp(tmp_ckpt, wk)
    g = _sealed_group("rg000001", 1, 10, tier="full")
    t_file = {
        "version": 2, "window_key": wk,
        "T1": {"compressed_summary": "", "token_count": 0},
        "messages": [],
        "metadata": {
            "next_round_id": 81, "next_step_id": 160, "generation": 1,
            "total_messages_ever": 0,
            "record_state": {
                "last_compressed_round_id": "r000010", "last_grouped_rg_id": "rg000001",
                "round_groups": [g], "hit_table": {},
                "summary_watermark_rg_id": "rg000001",
            },
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)

    async def run():
        snap = await mgr.load(wk)
        grp = snap["metadata"]["record_state"]["round_groups"][0]
        # 第一次 build：now=80, age=70 > brief_age(60) → base=brief，回写 g['tier']='brief'
        mgr.build_llm_contexts(snap, window_key=wk, record_cfg=CFG)
        assert grp["tier"] == record.TIER_BRIEF, (
            f"第一次 build 应把组 tier 从 full 回写为真实档 brief，实得 {grp['tier']}"
        )

        # 第二次 build：now=68, age=58 落在 brief 滞回带 [57,63)，base=summary。
        snap["metadata"]["next_round_id"] = 69  # now=68, age=58
        mgr.build_llm_contexts(snap, window_key=wk, record_cfg=CFG)
        # 修复后 prev=brief（真实回写）→ 滞回粘 brief；修复前 prev=full（恒）→ summary。
        assert grp["tier"] == record.TIER_BRIEF, (
            f"滞回应以真实 prev_tier=brief 粘 brief（证明 prev 非恒 full），实得 {grp['tier']}"
        )

    asyncio.run(run())


def test_hysteresis_writeback_not_persisted(tmp_ckpt):
    """回写仅作用于内存视图，不持久化到磁盘（方案 b）：build 后磁盘组 tier 不被改写。"""
    mgr = TFileManager()
    wk = "GroupMessage:b6hys2"
    fp = _fp(tmp_ckpt, wk)
    g = _sealed_group("rg000001", 1, 10, tier="full")
    t_file = {
        "version": 2, "window_key": wk,
        "T1": {"compressed_summary": "", "token_count": 0},
        "messages": [],
        "metadata": {
            "next_round_id": 80, "next_step_id": 160, "generation": 1,
            "total_messages_ever": 0,
            "record_state": {
                "last_compressed_round_id": "r000010", "last_grouped_rg_id": "rg000001",
                "round_groups": [g], "hit_table": {},
                "summary_watermark_rg_id": "rg000001",
            },
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)

    async def run():
        snap = await mgr.load(wk)
        # now=79, age=69 > brief_age(60) → base=brief，组会被回写为 brief（内存）
        mgr.build_llm_contexts(snap, window_key=wk, record_cfg=CFG)
        assert snap["metadata"]["record_state"]["round_groups"][0]["tier"] == record.TIER_BRIEF
        # 磁盘未被改写（build_llm_contexts 不 save）
        disk = await mgr._load_t_file_raw(wk)
        assert disk["metadata"]["record_state"]["round_groups"][0]["tier"] == "full", (
            "回写不应持久化到磁盘（方案 b）"
        )

    asyncio.run(run())


# ============================================================
# ⑥ Cluster D #6 — fallback 空 list → 触发全量
# ============================================================
def test_empty_record_view_falls_back_to_full(tmp_ckpt):
    """round_range 合法但 tier 三档文本全空 + 所有 message 已聚合（无尾部原文）→
    record 视图产出空 list → 触发全量 fallback（T1 摘要出现在结果中），绝不清空上下文。"""
    mgr = TFileManager()
    wk = "GroupMessage:b6empty"
    fp = _fp(tmp_ckpt, wk)
    # 组 round_range 合法 [1,10]，但 full/summary/brief 三档全空（损坏态）
    bad_group = {
        "rg_id": "rg000001", "round_range": [1, 10], "tier": "full", "sealed": True,
        "legacy_rg": False, "full_text": "", "summary_text": "", "brief_text": "",
    }
    # 所有 message round_id <= 水位(10) → 无尾部原文
    msgs = [{"role": "user", "content": "原文1", "round_id": "r000001",
             "step_id": "s00000002", "message_id": "u1"}]
    t_file = {
        "version": 2, "window_key": wk,
        "T1": {"compressed_summary": "这是 T1 全量摘要内容", "token_count": 10},
        "messages": msgs,
        "metadata": {
            "next_round_id": 11, "next_step_id": 22, "generation": 1,
            "total_messages_ever": 1,
            "record_state": {
                "last_compressed_round_id": "r000010", "last_grouped_rg_id": "rg000001",
                "round_groups": [bad_group], "hit_table": {},
                "summary_watermark_rg_id": "rg000001",
            },
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)

    async def run():
        snap = await mgr.load(wk)
        contexts = mgr.build_llm_contexts(snap, window_key=wk, record_cfg=CFG)
        # 不应是空：必须 fallback 全量（含 T1 摘要 + 原文）
        assert contexts, "空 record 视图必须 fallback 全量，绝不清空"
        joined = json.dumps(contexts, ensure_ascii=False)
        assert "这是 T1 全量摘要内容" in joined, "全量 fallback 应含 T1 摘要"
        assert "原文1" in joined, "全量 fallback 应含原文"

    asyncio.run(run())


def test_fresh_empty_window_no_crash(tmp_ckpt):
    """合法真空窗口（全新窗口零历史，无 round_groups）：valid_groups 空 → 返回 None →
    全量 fallback 也为空 → 不触发异常 / 不死循环。"""
    mgr = TFileManager()
    wk = "GroupMessage:b6fresh"

    async def run():
        snap = await mgr.load(wk)  # 全新空窗口
        contexts = mgr.build_llm_contexts(snap, window_key=wk, record_cfg=CFG)
        # 全新空窗口合法返回空 list（无历史），不崩
        assert isinstance(contexts, list)

    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
