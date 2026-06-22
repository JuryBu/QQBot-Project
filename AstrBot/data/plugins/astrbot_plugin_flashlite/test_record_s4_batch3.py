"""
S4 批3 单测：分级读取 + 定档公式（D7 tier_for_group + D8 summary 封板 + M3）
============================================================================
覆盖 7 个验收点（依据 QQBotPlan/Plan_5/S4_实现方案.md §一 M3 / §二 R6 / §四 schema /
§五 D8 + S4_设计决策.md D7 轮龄为主+命中修正 / D8 summary 封板）：
  ① tier_for_group 定档：age 阶梯 full/summary/brief 各档（轮龄为主）
  ② hit 升档：hit_score 超阈值在 base 抬一档、封顶 full
  ③ 滞回防横跳：边界轮 ±hysteresis 内粘住 prev_tier，不反复升降档
  ④ build_llm_contexts 分级：record 视图逐组按定档读对应档文本（full/summary/brief）
  ⑤ D8 summary 封板生成 + watermark 防空洞：只 sealed 组生成、watermark 推进、空洞不越过
  ⑥ hit_table 空 → 纯 age 兜底（不报错，hit_score=0）
  ⑦ 只 sealed 组允许降档：未封板组强制 full（D8 防读空摘要）

全部纯函数 / mock，不调真模型。

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_batch3.py -q
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
from checkpoint import TFileManager  # noqa: E402


# 统一 cfg（与 _conf_schema.json 默认对齐）
CFG = {
    "tier_summary_age": 20,
    "tier_brief_age": 60,
    "tier_hysteresis": 5,
    "hit_upgrade_threshold": 1.0,
    "hit_weight_raw": 1.0,
    "hit_weight_record": 0.5,
    "hit_halflife": 86400,
}


def _sealed_group(rg_id, s, e, *, full="FULL正文", summary="SUMMARY摘要",
                  brief="BRIEF一句话", tier="full", legacy=False):
    return {
        "rg_id": rg_id,
        "round_range": [s, e],
        "tier": tier,
        "sealed": True,
        "legacy_rg": legacy,
        "full_text": full,
        "summary_text": summary,
        "brief_text": brief,
    }


# ============================================================
# ① tier_for_group 定档：age 阶梯 full/summary/brief
# ============================================================
def test_age_ladder_full():
    """轮龄 < summary_age(20) → full。"""
    g = _sealed_group("rg000001", 1, 10)
    # now_round=15 → 轮龄 = 15-10 = 5 < 20 → full
    assert record.tier_for_group(g, now_round=15, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL


def test_age_ladder_summary():
    """summary_age(20) <= 轮龄 < brief_age(60) → summary。"""
    g = _sealed_group("rg000001", 1, 10)
    # now_round=40 → 轮龄 = 40-10 = 30 → summary
    assert record.tier_for_group(g, now_round=40, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY


def test_age_ladder_brief():
    """轮龄 >= brief_age(60) → brief。"""
    g = _sealed_group("rg000001", 1, 10)
    # now_round=100 → 轮龄 = 100-10 = 90 >= 60 → brief
    assert record.tier_for_group(g, now_round=100, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_BRIEF


def test_age_ladder_boundary_exact():
    """轮龄恰等于阈值取下档（< 严格小于）：age==summary_age → summary，age==brief_age → brief。"""
    g = _sealed_group("rg000001", 10, 10)
    # age==20 → 不再 full（< 20 才 full）→ summary
    assert record.tier_for_group(g, now_round=30, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY
    # age==60 → brief
    assert record.tier_for_group(g, now_round=70, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_BRIEF


# ============================================================
# ② hit 升档：hit_score 超阈值抬一档、封顶 full
# ============================================================
def test_hit_upgrade_summary_to_full():
    """base=summary 的组，hit_score 超阈值 → 抬到 full。"""
    g = _sealed_group("rg000007", 1, 10)
    # base age=30 → summary。hit_table 给足热度（hit_count=5, raw, 同刻无衰减）
    hit_table = {"rg000007": {"hit_count": 5, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    # score = 5*1.0*0.5^0 = 5 >= 1.0 → 抬一档 summary→full
    assert record.tier_for_group(g, now_round=40, hit_table=hit_table, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL


def test_hit_upgrade_brief_to_summary_only_one_step():
    """base=brief 的组，hit 升【一档】到 summary，不直接跳 full（封顶=base+1）。"""
    g = _sealed_group("rg000007", 1, 10)
    # base age=90 → brief。hit_score=5 >= 1 → brief→summary（只抬一档）
    hit_table = {"rg000007": {"hit_count": 5, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    assert record.tier_for_group(g, now_round=100, hit_table=hit_table, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY


def test_hit_upgrade_capped_at_full():
    """base=full 的组命中 → 仍 full（封顶，不溢出）。"""
    g = _sealed_group("rg000007", 1, 10)
    hit_table = {"rg000007": {"hit_count": 9, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    # base age=5 → full；命中封顶 full
    assert record.tier_for_group(g, now_round=15, hit_table=hit_table, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL


def test_hit_below_threshold_no_upgrade():
    """hit_score 不到阈值（record 弱权重 + 时间衰减）→ 不升档，留 base。"""
    g = _sealed_group("rg000007", 1, 10)
    # record 权重 0.5，且已衰减很久（Δt = 2 个半衰期 → 0.25）
    # score = 1 * 0.5 * 0.25 = 0.125 < 1.0 → 不升
    hit_table = {"rg000007": {"hit_count": 1, "last_hit_ts": 0.0, "last_hit_type": "record"}}
    now_ts = 2 * 86400.0  # 2 个半衰期
    assert record.tier_for_group(g, now_round=40, hit_table=hit_table, cfg=CFG,
                                 now_ts=now_ts) == record.TIER_SUMMARY


def test_hit_time_decay():
    """hit_score 随时间半衰期衰减（同 hit_count，时间越久 score 越小）。"""
    rg = "rg000003"
    ht = {rg: {"hit_count": 4, "last_hit_ts": 0.0, "last_hit_type": "raw"}}
    s_now = record.hit_score(rg, ht, 0.0, CFG)        # Δt=0 → 4*1*1 = 4
    s_1hl = record.hit_score(rg, ht, 86400.0, CFG)    # Δt=1HL → 4*0.5 = 2
    s_2hl = record.hit_score(rg, ht, 2 * 86400.0, CFG)  # Δt=2HL → 4*0.25 = 1
    assert s_now == pytest.approx(4.0)
    assert s_1hl == pytest.approx(2.0)
    assert s_2hl == pytest.approx(1.0)
    assert s_now > s_1hl > s_2hl


# ============================================================
# ③ 滞回防横跳：边界轮 ±hysteresis 内粘住 prev_tier
# ============================================================
def test_hysteresis_sticky_high_tier():
    """prev=full、age 刚过 summary_age 但在滞回带内 → 仍 full（粘住高档不掉）。"""
    g = _sealed_group("rg000005", 10, 10, tier="full")
    # age = 32-10 = 22，summary_age=20，hysteresis=5 → 带 [15,25)。22 在带内。
    # 纯阶梯 age=22 本应 summary，但 prev=full 粘住 → full
    assert record.tier_for_group(g, now_round=32, hit_table={}, cfg=CFG,
                                 now_ts=1000.0, prev_tier=record.TIER_FULL) == record.TIER_FULL


def test_hysteresis_sticky_low_tier():
    """prev=summary、age 刚跌回 summary_age 以下但仍在滞回带内 → 仍 summary（不弹回 full）。"""
    g = _sealed_group("rg000005", 10, 10, tier="summary")
    # age = 28-10 = 18，带 [15,25)，18 在带内。纯阶梯 age=18 应 full，prev=summary 粘住 → summary
    assert record.tier_for_group(g, now_round=28, hit_table={}, cfg=CFG,
                                 now_ts=1000.0, prev_tier=record.TIER_SUMMARY) == record.TIER_SUMMARY


def test_hysteresis_breaks_out_of_band():
    """age 跨出滞回带 → 正常翻档（滞回只在边界附近粘，远离则照阶梯）。"""
    g = _sealed_group("rg000005", 10, 10, tier="full")
    # age = 40-10 = 30，带 [15,25)，30 在带外 → 照阶梯 summary（不再粘 full）
    assert record.tier_for_group(g, now_round=40, hit_table={}, cfg=CFG,
                                 now_ts=1000.0, prev_tier=record.TIER_FULL) == record.TIER_SUMMARY


def test_hysteresis_no_flapping_across_boundary_rounds():
    """边界来回轮：在 [15,25) 带内随 now_round 微动，prev=full 时档位稳定不横跳。"""
    g = _sealed_group("rg000005", 0, 0, tier="full")
    prev = record.TIER_FULL
    results = []
    for nr in (18, 22, 19, 24, 16, 23):  # age 全落 [15,25) 带内
        t = record.tier_for_group(g, now_round=nr, hit_table={}, cfg=CFG,
                                  now_ts=1000.0, prev_tier=prev)
        results.append(t)
        prev = t
    # 全程粘 full，无任何一轮翻 summary（防横跳）
    assert all(r == record.TIER_FULL for r in results)


# ============================================================
# ④ build_llm_contexts 分级：逐组按定档读对应档文本
# ============================================================
@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s4_b3_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


def _make_t_file(round_groups, *, next_round_id, hit_table=None,
                 summary_wm=None, messages=None):
    return {
        "version": 2,
        "T1": {"compressed_summary": ""},
        "messages": messages or [],
        "metadata": {
            "next_round_id": next_round_id,
            "generation": 1,
            "record_state": {
                "round_groups": round_groups,
                "hit_table": hit_table or {},
                "summary_watermark_rg_id": summary_wm,
                "last_grouped_rg_id": round_groups[-1]["rg_id"] if round_groups else None,
            },
        },
    }


def test_build_contexts_tiered_reads(tmp_ckpt):
    """三组分别落 full/summary/brief 档，注入概要块逐组读对应档文本。"""
    mgr = TFileManager()
    # rg1 终点 10（很老 → brief）、rg2 终点 50（中 → summary）、rg3 终点 95（新 → full）
    groups = [
        _sealed_group("rg000001", 1, 10, full="A_FULL", summary="A_SUM", brief="A_BRIEF"),
        _sealed_group("rg000002", 11, 50, full="B_FULL", summary="B_SUM", brief="B_BRIEF"),
        _sealed_group("rg000003", 51, 95, full="C_FULL", summary="C_SUM", brief="C_BRIEF"),
    ]
    # next_round_id=101 → now_round=100。age: rg1=90(brief) rg2=50(summary) rg3=5(full)
    t_file = _make_t_file(groups, next_round_id=101)
    ctxs = mgr.build_llm_contexts(t_file, window_key="GroupMessage:g1", record_cfg=CFG)
    blob = "\n".join(c.get("content", "") for c in ctxs if isinstance(c, dict))
    # rg1 读 brief，rg2 读 summary，rg3 读 full
    assert "A_BRIEF" in blob and "A_FULL" not in blob
    assert "B_SUM" in blob and "B_FULL" not in blob
    assert "C_FULL" in blob


def test_build_contexts_unsealed_forced_full(tmp_ckpt):
    """未封板组即便很老也强制读 full（D8：summary 未预生成不降档）。"""
    mgr = TFileManager()
    g = _sealed_group("rg000001", 1, 10, full="OPEN_FULL", summary="OPEN_SUM")
    g["sealed"] = False  # 未封板
    t_file = _make_t_file([g], next_round_id=101)  # age=90 本应 brief
    ctxs = mgr.build_llm_contexts(t_file, window_key="GroupMessage:g2", record_cfg=CFG)
    blob = "\n".join(c.get("content", "") for c in ctxs if isinstance(c, dict))
    assert "OPEN_FULL" in blob  # 强制 full，没降档


# ============================================================
# ⑤ D8 summary 封板生成 + watermark 防空洞
# ============================================================
def _summary_caller_ok(view, cfg):
    """mock：根据 full_text 产 summary + title。"""
    return {
        "summary_text": f"摘要[{view['rg_id']}]",
        "title": f"标题[{view['rg_id']}]",
    }


def test_d8_generate_summaries_sealed_only():
    """只为【sealed 且无 summary】的组生成；未封板组不动。"""
    groups = [
        _sealed_group("rg000001", 1, 10, summary=""),                  # sealed 无 summary → 生成
        dict(_sealed_group("rg000002", 11, 20, summary=""), sealed=False),  # 未封板 → 跳过
    ]
    out, wm, errs = record.generate_summaries_for_sealed(
        groups, None, _summary_caller_ok, CFG)
    assert errs == []
    assert out[0]["summary_text"] == "摘要[rg000001]"
    assert out[0]["title"] == "标题[rg000001]"
    assert out[1].get("summary_text", "") == ""  # 未封板组未生成
    assert wm == "rg000001"  # 水位推到第一组（连续已生成的最后一个 sealed 组）


def test_d8_watermark_stops_at_gap():
    """watermark 推进遇到「封板但无 summary」空洞即停（不越过）。"""
    # caller 只对 rg1 成功，对 rg3 返回空（模拟生成失败留空洞）
    def _partial_caller(view, cfg):
        if view["rg_id"] == "rg000003":
            return {"summary_text": "", "title": ""}  # 空 → 留空洞
        return {"summary_text": f"摘要[{view['rg_id']}]", "title": "t"}

    groups = [
        _sealed_group("rg000001", 1, 10, summary=""),
        _sealed_group("rg000002", 11, 20, summary=""),
        _sealed_group("rg000003", 21, 30, summary=""),
        _sealed_group("rg000004", 31, 40, summary=""),
    ]
    out, wm, errs = record.generate_summaries_for_sealed(
        groups, None, _partial_caller, CFG)
    # rg1/rg2 生成成功，rg3 空洞，rg4 即便生成成功也不能越过 rg3 空洞
    assert out[0]["summary_text"] and out[1]["summary_text"]
    assert not (out[2].get("summary_text") or "")  # rg3 空洞
    assert wm == "rg000002"  # 水位停在 rg2，不越过 rg3 空洞
    assert any("rg000003" in e for e in errs)


def test_d8_idempotent_below_watermark():
    """水位之下的组不重复调 caller（幂等）。"""
    calls = []

    def _track_caller(view, cfg):
        calls.append(view["rg_id"])
        return {"summary_text": "新摘要", "title": "t"}

    groups = [
        _sealed_group("rg000001", 1, 10, summary="已有摘要1"),
        _sealed_group("rg000002", 11, 20, summary=""),
    ]
    # 水位已到 rg1 → rg1 不再生成
    out, wm, errs = record.generate_summaries_for_sealed(
        groups, "rg000001", _track_caller, CFG)
    assert "rg000001" not in calls  # 水位之下幂等跳过
    assert "rg000002" in calls
    assert out[0]["summary_text"] == "已有摘要1"  # 原摘要不被覆盖
    assert wm == "rg000002"


def test_d8_summary_gap_detector():
    """group_has_summary_gap：封板/水位后/无 summary = 空洞。"""
    g_gap = _sealed_group("rg000005", 1, 10, summary="")
    assert record.group_has_summary_gap(g_gap, watermark_rg_id="rg000003") is True
    # 水位之内 → 非空洞
    assert record.group_has_summary_gap(g_gap, watermark_rg_id="rg000005") is False
    # 有 summary → 非空洞
    g_ok = _sealed_group("rg000006", 1, 10, summary="有摘要")
    assert record.group_has_summary_gap(g_ok, watermark_rg_id="rg000003") is False
    # 未封板 → 非空洞（由 tier_for_group 强制 full）
    g_open = dict(_sealed_group("rg000007", 1, 10, summary=""), sealed=False)
    assert record.group_has_summary_gap(g_open, watermark_rg_id="rg000003") is False


def test_d8_build_contexts_gap_forces_full(tmp_ckpt):
    """注入时遇 summary 空洞组（封板/水位后/无 summary）强制读 full 防空块。"""
    mgr = TFileManager()
    # 组很老（age 想降 brief），但无 summary 且在水位之后 → 强制 full
    g = _sealed_group("rg000005", 1, 10, full="GAP_FULL", summary="", brief="")
    t_file = _make_t_file([g], next_round_id=101, summary_wm="rg000003")  # rg5 > 水位 rg3
    ctxs = mgr.build_llm_contexts(t_file, window_key="GroupMessage:g3", record_cfg=CFG)
    blob = "\n".join(c.get("content", "") for c in ctxs if isinstance(c, dict))
    assert "GAP_FULL" in blob  # 空洞 → 强制 full，读到 full_text


# ============================================================
# ⑥ hit_table 空 → 纯 age 兜底（不报错）
# ============================================================
def test_empty_hit_table_age_fallback():
    """hit_table 为 {} / None → hit_score=0，纯按 age 定档，不报错。"""
    g = _sealed_group("rg000001", 1, 10)
    # age=30 → summary（无 hit 升档）
    assert record.tier_for_group(g, now_round=40, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY
    assert record.tier_for_group(g, now_round=40, hit_table=None, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY
    # hit_score 对空表/缺组返回 0
    assert record.hit_score("rg000001", {}, 1000.0, CFG) == 0.0
    assert record.hit_score("rg000001", None, 1000.0, CFG) == 0.0
    assert record.hit_score(None, {"x": {}}, 1000.0, CFG) == 0.0
    # 表里有别的组、没本组 → 0
    assert record.hit_score("rg000001", {"rg000099": {"hit_count": 9}}, 1000.0, CFG) == 0.0


def test_malformed_hit_entry_no_crash():
    """hit_table 项畸形（非 dict / 缺字段 / 坏类型）→ score=0，不抛异常。"""
    assert record.hit_score("r", {"r": "not_a_dict"}, 1000.0, CFG) == 0.0
    assert record.hit_score("r", {"r": {"hit_count": "bad"}}, 1000.0, CFG) == 0.0
    assert record.hit_score("r", {"r": {"hit_count": 3}}, 1000.0, CFG) >= 0.0  # 缺 ts → decay=1


# ============================================================
# ⑦ 只 sealed 组允许降档（未封板强制 full）
# ============================================================
def test_unsealed_group_forced_full():
    """未封板组无论多老都返回 full（D8 守护，纯函数级）。"""
    g = dict(_sealed_group("rg000001", 1, 10), sealed=False)
    # age=200 极老，本应 brief，但未封板 → full
    assert record.tier_for_group(g, now_round=210, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL


def test_sealed_group_can_downgrade():
    """已封板组可正常降档（对照组，证明 sealed 守护不误伤）。"""
    g = _sealed_group("rg000001", 1, 10)  # sealed=True
    assert record.tier_for_group(g, now_round=210, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_BRIEF


def test_no_now_round_forced_full():
    """now_round 缺失（无法算轮龄）→ 保守留 full。"""
    g = _sealed_group("rg000001", 1, 10)
    assert record.tier_for_group(g, now_round=None, hit_table={}, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL


# ============================================================
# build_tier_map：批量定档（render_record_md / 分级层共用）
# ============================================================
def test_build_tier_map():
    """build_tier_map 对一批组逐组定档产 {rg_id: tier}。"""
    groups = [
        _sealed_group("rg000001", 1, 10),    # age 90 → brief
        _sealed_group("rg000002", 11, 50),   # age 50 → summary
        _sealed_group("rg000003", 51, 95),   # age 5 → full
    ]
    tm = record.build_tier_map(groups, now_round=100, hit_table={}, cfg=CFG,
                               now_ts=1000.0)
    assert tm == {
        "rg000001": record.TIER_BRIEF,
        "rg000002": record.TIER_SUMMARY,
        "rg000003": record.TIER_FULL,
    }


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
