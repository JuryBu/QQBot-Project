"""
S4 批7 单测：D8 守护解耦「降档」与「封板」（真机修复）
============================================================================
背景：tier_for_group 的 D8 守护原为「未封板(sealed!=True)即强制 full」，把【降档】
错误耦合到【封板】。真机 force_seal 阈值(15轮/24000token/40轮龄)远高于 tier_summary_age，
致真轮组在 [summary_age, force_seal_age] 巨大缝隙内永远 full，D7 分级对真轮组沦为死代码。

修复：D8 守护改为「无 summary_text 才强制 full」(record.py ~828)。防空洞的真正条件是
summary 文本存在性（compose 已为每组产 summary_text），而非 sealed。

覆盖 6 个验收点（依据真机群 <GID> rg000001 永 full bug + S4 批7 设计）：
  ① 有 summary_text + 轮龄超 tier_summary_age → 降 summary（不再强制 full）
  ② 无 summary_text → 仍强制 full（防空洞）
  ③ sealed 与降档解耦（未 sealed 但有 summary_text 也能降）
  ④ brief 档（轮龄超 brief_age + 有 summary 无 brief_text）→ _select_tier_body 回退
     summary_text，不空洞
  ⑤ hit 升档在降档 base 上正常（score 超阈值抬一档）
  ⑥ 滞回 prev_tier 正常（边界带内粘住上一档）

全部纯函数 / mock，不调真模型。

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_batch7.py -q
"""
import os
import sys

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


# 真机同款 cfg（与真机群 <GID> 验证一致；含 hit 阈值便于 ⑤）
CFG = {
    "tier_summary_age": 3,
    "tier_brief_age": 6,
    "tier_hysteresis": 5,
    "hit_upgrade_threshold": 1.0,
    "hit_weight_raw": 1.0,
    "hit_weight_record": 0.5,
    "hit_halflife": 86400,
}
# 滞回关闭版（隔离测纯 age 阶梯，不被滞回带粘住）
CFG_NO_HYS = dict(CFG, tier_hysteresis=0)


def _group(rg_id, s, e, *, sealed=False, summary="某组摘要", brief="",
           full="某组完整原文", tier="full", legacy=False):
    """构造 round-group。默认 sealed=False（模拟真机未封板态），有 summary 无 brief
    （真机所有组 brief_text 为空），便于覆盖修复后的真机场景。"""
    return {
        "rg_id": rg_id,
        "round_range": [s, e],
        "tier": tier,
        "sealed": sealed,
        "legacy_rg": legacy,
        "full_text": full,
        "summary_text": summary,
        "brief_text": brief,
    }


# ============================================================
# ① 有 summary_text + 轮龄超 tier_summary_age → 降 summary（不再强制 full）
# ============================================================
def test_has_summary_age_over_summary_age_downgrades_to_summary():
    """核心修复：组有 summary_text、轮龄 ∈ [summary_age, brief_age)、未封板，
    应降到 summary（旧逻辑因 sealed=False 会卡 full）。
    age=4：summary_age=3 ≤ 4 < brief_age=6 → base=summary。"""
    g = _group("rg000001", 1, 1, sealed=False, summary="有摘要", brief="")
    # now_round=5, end=1 → age=4。关滞回避免被边界带粘回 full。
    tier = record.tier_for_group(g, now_round=5, hit_table=None, cfg=CFG_NO_HYS,
                                 now_ts=1.0, prev_tier=None)
    assert tier == record.TIER_SUMMARY, f"期望 summary，实际 {tier}"


# ============================================================
# ② 无 summary_text → 仍强制 full（防空洞）
# ============================================================
def test_no_summary_text_forced_full():
    """无 summary_text 的组（模型未产出），即便轮龄够老也强制 full，
    防降档读到空摘要造空洞。age=4 本应 summary，但无 summary_text → full。"""
    g = _group("rg000099", 1, 1, sealed=False, summary="", brief="")
    tier = record.tier_for_group(g, now_round=5, hit_table=None, cfg=CFG_NO_HYS,
                                 now_ts=1.0, prev_tier=None)
    assert tier == record.TIER_FULL, f"无 summary 应强制 full，实际 {tier}"


def test_whitespace_only_summary_forced_full():
    """summary_text 仅空白字符也算无（.strip() 为空）→ 强制 full。"""
    g = _group("rg000098", 1, 1, sealed=False, summary="   \n\t ", brief="")
    tier = record.tier_for_group(g, now_round=5, hit_table=None, cfg=CFG_NO_HYS,
                                 now_ts=1.0, prev_tier=None)
    assert tier == record.TIER_FULL


# ============================================================
# ③ sealed 与降档解耦（未 sealed 但有 summary_text 也能降）
# ============================================================
def test_sealed_decoupled_from_downgrade():
    """同一组（有 summary、轮龄够），sealed=True 与 sealed=False 定档结果相同——
    证明降档不再依赖 sealed（旧逻辑下两者会不同：False→full、True→summary）。"""
    base = dict(rg_id="rg000007", round_range=[1, 1], tier="full",
                legacy_rg=False, full_text="F", summary_text="有摘要",
                brief_text="")
    g_unsealed = dict(base, sealed=False)
    g_sealed = dict(base, sealed=True)
    t_unsealed = record.tier_for_group(g_unsealed, now_round=5, hit_table=None,
                                       cfg=CFG_NO_HYS, now_ts=1.0, prev_tier=None)
    t_sealed = record.tier_for_group(g_sealed, now_round=5, hit_table=None,
                                     cfg=CFG_NO_HYS, now_ts=1.0, prev_tier=None)
    assert t_unsealed == t_sealed == record.TIER_SUMMARY, \
        f"sealed 不应影响定档：unsealed={t_unsealed} sealed={t_sealed}"


# ============================================================
# ④ brief 档：轮龄超 brief_age + 有 summary 无 brief_text → 定档 brief，
#    _select_tier_body 回退 summary_text 不空洞
# ============================================================
def test_brief_tier_falls_back_to_summary_not_hollow():
    """真机所有组 brief_text 为空。组轮龄超 brief_age 定档 brief，但 brief_text 空，
    _select_tier_body 须回退 summary_text，绝不返回空串。
    age=7：≥ brief_age=6 → base=brief。"""
    g = _group("rg000001", 1, 1, sealed=False, summary="这是摘要文本", brief="")
    # now_round=8, end=1 → age=7。关滞回。
    tier = record.tier_for_group(g, now_round=8, hit_table=None, cfg=CFG_NO_HYS,
                                 now_ts=1.0, prev_tier=None)
    assert tier == record.TIER_BRIEF, f"age=7 应定档 brief，实际 {tier}"
    body = record._select_tier_body(g, tier)
    assert body == "这是摘要文本", f"brief 缺文本应回退 summary_text，实际 {body!r}"
    assert body, "回退后正文不应为空（防空洞）"


def test_brief_tier_uses_brief_text_when_present():
    """对照：brief_text 存在时 brief 档直接读 brief_text（回退链不抢）。"""
    g = _group("rg000002", 1, 1, sealed=False, summary="摘要", brief="一句话简报")
    tier = record.tier_for_group(g, now_round=8, hit_table=None, cfg=CFG_NO_HYS,
                                 now_ts=1.0, prev_tier=None)
    assert tier == record.TIER_BRIEF
    assert record._select_tier_body(g, tier) == "一句话简报"


# ============================================================
# ⑤ hit 升档在降档 base 上正常（score 超阈值抬一档，封顶 full）
# ============================================================
def test_hit_upgrade_on_downgraded_base():
    """组有 summary、轮龄 age=4 定档 base=summary；命中 score 超阈值 → 抬一档到 full。
    验证 hit 升档建立在【新降档 base】之上正常工作（旧逻辑这组根本进不了阶梯）。"""
    g = _group("rg000010", 1, 1, sealed=False, summary="摘要", brief="")
    # 构造命中：hit_count 足够使 score >= upgrade_threshold(1.0)。
    # hit_score = hit_count * weight_record(0.5) * decay(同刻≈1) → 需 count>=2。
    hit_table = {"rg000010": {"hit_count": 4, "last_hit_ts": 1.0}}
    # 先确认无 hit 时 base=summary
    base_t = record.tier_for_group(g, now_round=5, hit_table=None, cfg=CFG_NO_HYS,
                                   now_ts=1.0, prev_tier=None)
    assert base_t == record.TIER_SUMMARY
    # 有 hit → 抬一档到 full（文字线封顶 full）
    hit_t = record.tier_for_group(g, now_round=5, hit_table=hit_table, cfg=CFG_NO_HYS,
                                  now_ts=1.0, prev_tier=None)
    assert hit_t == record.TIER_FULL, f"命中应抬一档到 full，实际 {hit_t}"


def test_hit_upgrade_only_one_step():
    """hit 升档至多一档：base=brief(age=7) + 命中 → 抬到 summary（不直跳 full）。"""
    g = _group("rg000011", 1, 1, sealed=False, summary="摘要", brief="")
    hit_table = {"rg000011": {"hit_count": 4, "last_hit_ts": 1.0}}
    base_t = record.tier_for_group(g, now_round=8, hit_table=None, cfg=CFG_NO_HYS,
                                   now_ts=1.0, prev_tier=None)
    assert base_t == record.TIER_BRIEF
    hit_t = record.tier_for_group(g, now_round=8, hit_table=hit_table, cfg=CFG_NO_HYS,
                                  now_ts=1.0, prev_tier=None)
    assert hit_t == record.TIER_SUMMARY, f"brief 命中应抬一档到 summary，实际 {hit_t}"


# ============================================================
# ⑥ 滞回 prev_tier 正常（边界带内粘住上一档）
# ============================================================
def test_hysteresis_sticks_prev_tier_in_band():
    """边界滞回：age 落在 summary↔brief 边界(brief_age=6) ±hysteresis(5) 带 [1,11) 内，
    prev_tier=summary 时粘住 summary（不掉到 base 应得的 brief）。
    age=7 base=brief，但 prev=summary 且在带内 → 粘 summary。
    （复现真机 rg000000：age=9、prev=summary、滞回带内 → summary 而非 brief）"""
    g = _group("rg000001", 1, 1, sealed=False, summary="摘要", brief="")
    # 开滞回（CFG hysteresis=5），prev_tier=summary
    tier = record.tier_for_group(g, now_round=8, hit_table=None, cfg=CFG,
                                 now_ts=1.0, prev_tier=record.TIER_SUMMARY)
    assert tier == record.TIER_SUMMARY, \
        f"滞回带内应粘住 prev=summary，实际 {tier}"


def test_hysteresis_no_stick_outside_band():
    """对照：age 超出滞回带 [1,11) 时不粘，回落 base=brief。
    age=12（end=1, now=13）> brief_age+hysteresis=11 → 不在带内 → base=brief。"""
    g = _group("rg000001", 1, 1, sealed=False, summary="摘要", brief="")
    tier = record.tier_for_group(g, now_round=13, hit_table=None, cfg=CFG,
                                 now_ts=1.0, prev_tier=record.TIER_SUMMARY)
    assert tier == record.TIER_BRIEF, \
        f"超出滞回带应回落 base=brief，实际 {tier}"


def test_hysteresis_full_summary_boundary():
    """full↔summary 边界(summary_age=3)滞回：age=4 base=summary，prev=full、
    带 [-2,8) 内 → 粘 full。证明两条边界滞回都正常。"""
    g = _group("rg000003", 1, 1, sealed=False, summary="摘要", brief="")
    tier = record.tier_for_group(g, now_round=5, hit_table=None, cfg=CFG,
                                 now_ts=1.0, prev_tier=record.TIER_FULL)
    assert tier == record.TIER_FULL, f"full↔summary 边界滞回应粘 full，实际 {tier}"


# ============================================================
# 真机回归锚点：复刻真机群 <GID> rg000001（rounds1-2、有summary、未sealed）
# now_round=9, cfg=真机同款 → 应降档（brief），非旧逻辑的 full
# ============================================================
def test_realmachine_rg000001_downgrades():
    """真机锚点：rg000001 round_range=[1,2]、sealed=False、有 summary_text、
    now_round=9 → age=7 ≥ brief_age=6 → 定档 brief（prev=full 不在反向滞回方向）。
    旧逻辑因 sealed=False 强制 full（bug）。"""
    g = _group("rg000001", 1, 2, sealed=False, summary="真机摘要文本", brief="",
               tier="full")
    tier = record.tier_for_group(g, now_round=9, hit_table=None, cfg=CFG,
                                 now_ts=1.0, prev_tier="full")
    assert tier in (record.TIER_SUMMARY, record.TIER_BRIEF), \
        f"真机 rg000001 应降档（非 full），实际 {tier}"
    assert tier != record.TIER_FULL


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
