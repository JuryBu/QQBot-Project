"""
S4 批4（收官批）单测：hit 命中子系统（M4 + D9 + D10 + 收尾事务）
================================================================================
覆盖 7 个验收点（依据 QQBotPlan/Plan_5/S4_实现方案.md §一 M4 / §二 R7 / §五 D10 +
S4_设计决策.md D9 持久化/时间衰减/组粒度 + D10 封顶/锁定/命中类型）：
  ① record_hit 进队列（不实时写）+ 收尾落盘（compose 收尾 + 独立 flush_hit_queue）
  ② 崩溃恢复 hit_table（旧 T 文件缺字段经 _ensure_metadata_v2_fields 补齐、已落盘 hit 不丢）
  ③ 重编号 key 迁移（compose 致 rg_id 变 → hit 按 round_range 重叠映射不丢）
  ④ 命中升档（hit 后 tier 升档；apply_hit_to_table → hit_score → tier_for_group）
  ⑤ 命中锁定 hit_keep_rounds（命中后 N 轮强制不降，衰减到阈值下仍锁定；N 轮后释放）
  ⑥ 命中类型权重（原文 raw 1.0 > record 0.5；同热度 raw 升档、record 不升）
  ⑦ 收尾事务（compose 替换 round_groups + hit 队列 flush + 重编号迁移 锁内同一 save 协调无竞态）

纯函数 / mock，不调真模型。
跑法：AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_batch4.py -q
"""
import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# mock astrbot 包（checkpoint.py / context_mixin.py 顶部 from astrbot.api import logger）
if "astrbot" not in sys.modules:
    import logging
    import types

    _a = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("flashlite_test")
    _a.api = _api
    sys.modules["astrbot"] = _a
    sys.modules["astrbot.api"] = _api

import asyncio  # noqa: E402

import pytest  # noqa: E402

import checkpoint  # noqa: E402
import record  # noqa: E402
from checkpoint import TFileManager  # noqa: E402


CFG = {
    "tier_summary_age": 20,
    "tier_brief_age": 60,
    "tier_hysteresis": 5,
    "hit_upgrade_threshold": 1.0,
    "hit_weight_raw": 1.0,
    "hit_weight_record": 0.5,
    "hit_halflife": 86400,
    "hit_keep_rounds": 3,
}


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s4_b4_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


def _fp(d, wk):
    return os.path.join(d, wk.replace(":", "_") + ".json")


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


def _seed_t_file_with_rounds(fp, *, n_rounds=14, gen=7, next_rid=30, next_sid=60,
                             round_groups=None, hit_table=None):
    """构造带真轮号 + message_id 的 T 文件（够 token 触发 record 聚合）。"""
    msgs = []
    for rn in range(1, n_rounds + 1):
        msgs.append({
            "role": "user",
            "content": f"用户第{rn}轮消息：" + "内容" * 200,
            "round_id": f"r{rn:06d}", "step_id": f"s{rn * 2:08d}",
            "message_id": f"u{rn}",
        })
        msgs.append({
            "role": "assistant",
            "content": f"老板娘第{rn}轮回复：" + "回应" * 200,
            "round_id": f"r{rn:06d}", "step_id": f"s{rn * 2 + 1:08d}",
            "message_id": f"a{rn}",
        })
    t_file = {
        "version": 2,
        "window_key": "GroupMessage:b4001",
        "T1": {"compressed_summary": "", "token_count": 0},
        "messages": msgs,
        "metadata": {
            "next_round_id": next_rid,
            "next_step_id": next_sid,
            "generation": gen,
            "total_messages_ever": len(msgs),
            "record_state": {
                "last_compressed_round_id": None,
                "last_grouped_rg_id": None,
                "round_groups": round_groups if round_groups is not None else [],
                "hit_table": hit_table if hit_table is not None else {},
            },
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)
    return t_file


def _json_caller_text(batch_rounds, group_size=8):
    ints = sorted(int(r["round_int"]) for r in batch_rounds)
    specs = []
    i = 0
    while i < len(ints):
        chunk = ints[i:i + group_size]
        s, e = chunk[0], chunk[-1]
        specs.append({
            "round_start": s, "round_end": e,
            "title": f"段{s}-{e}",
            "full_text": f"FULL[{s}-{e}]", "summary_text": f"SUM[{s}-{e}]",
        })
        i += group_size
    return json.dumps(specs, ensure_ascii=False)


# ============================================================
# ① record_hit 进队列（不实时写）+ 收尾落盘
# ============================================================
def test_record_hit_enqueues_not_written(tmp_ckpt):
    """record_hit 只入内存队列，磁盘 hit_table 此刻保持空（不实时写，防三方竞态）。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, round_groups=[_sealed_group("rg000001", 1, 8)])

    ok = mgr.record_hit(wk, round_int=5, hit_type="raw", now_round=20)
    assert ok is True
    # 队列有 1 项
    assert len(mgr._hit_queue.get(wk, [])) == 1
    # 磁盘 hit_table 仍空（未实时写）
    disk = json.load(open(fp, encoding="utf-8"))
    assert disk["metadata"]["record_state"]["hit_table"] == {}


def test_flush_hit_queue_lands_on_disk(tmp_ckpt):
    """独立收尾 flush_hit_queue：队列项按最新 round_groups 现算 rg_id 落 hit_table 并写盘。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, round_groups=[_sealed_group("rg000001", 1, 8)])

    # round 5 落在 rg000001 [1,8]
    mgr.record_hit(wk, round_int=5, hit_type="raw", now_round=20)
    applied = asyncio.run(mgr.flush_hit_queue(wk))
    assert applied == 1
    # 队列已清空
    assert mgr._hit_queue.get(wk) == []
    # 磁盘 hit_table 落定，key=rg000001
    disk = json.load(open(fp, encoding="utf-8"))
    ht = disk["metadata"]["record_state"]["hit_table"]
    assert "rg000001" in ht
    assert ht["rg000001"]["hit_count"] == 1
    assert ht["rg000001"]["last_hit_type"] == "raw"
    assert ht["rg000001"]["last_hit_round"] == 20


def test_flush_hit_queue_empty_noop(tmp_ckpt):
    """无队列项 → flush 直接返回 0、不动磁盘。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, round_groups=[_sealed_group("rg000001", 1, 8)])
    applied = asyncio.run(mgr.flush_hit_queue(wk))
    assert applied == 0


def test_hit_on_round_not_yet_grouped_deferred(tmp_ckpt):
    """命中 round 落在末尾未聚合原文区（无覆盖组）→ flush 不落盘但【回填队列】（不丢弃），
    待将来该 round 被聚合进组后再落盘（防『命中早于聚合 → hit 永久丢』软缺陷）。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, round_groups=[_sealed_group("rg000001", 1, 8)])
    # round 20 不在 [1,8] 任何组里 → 本次落不进，但应回填
    mgr.record_hit(wk, round_int=20, hit_type="raw", now_round=25)
    applied = asyncio.run(mgr.flush_hit_queue(wk))
    assert applied == 0
    disk = json.load(open(fp, encoding="utf-8"))
    assert disk["metadata"]["record_state"]["hit_table"] == {}
    # 队列回填保留（不清空）
    assert len(mgr._hit_queue.get(wk, [])) == 1

    # 将来 round 20 被聚合进新组 rg000003 [9,24] → 再 flush 应落盘
    disk["metadata"]["record_state"]["round_groups"].append(
        _sealed_group("rg000003", 9, 24)
    )
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False)
    applied2 = asyncio.run(mgr.flush_hit_queue(wk))
    assert applied2 == 1
    disk2 = json.load(open(fp, encoding="utf-8"))
    assert disk2["metadata"]["record_state"]["hit_table"]["rg000003"]["hit_count"] == 1
    # 落盘后队列清空
    assert mgr._hit_queue.get(wk) == []


# ============================================================
# ② 崩溃恢复 hit_table
# ============================================================
def test_crash_recovery_ensures_hit_table(tmp_ckpt):
    """旧 T 文件 record_state 只含 last_compressed_round_id → load 经
    _ensure_metadata_v2_fields 嵌套补默认补上 hit_table（崩溃恢复不报错）。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4old"
    fp = _fp(tmp_ckpt, wk)
    # 旧式 T 文件：record_state 顶层存在但只含压缩锚（S3 落盘格式）
    old = {
        "version": 2,
        "messages": [],
        "metadata": {
            "next_round_id": 1, "next_step_id": 1, "generation": 0,
            "record_state": {"last_compressed_round_id": "r000005"},
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False)

    t_file = asyncio.run(mgr.load(wk))
    rec = t_file["metadata"]["record_state"]
    # 压缩锚保留、hit_table 等四新锚补上
    assert rec["last_compressed_round_id"] == "r000005"
    assert rec.get("hit_table") == {}
    assert "round_groups" in rec
    assert "last_grouped_rg_id" in rec


def test_persisted_hit_table_survives_reload(tmp_ckpt):
    """已落盘 hit_table 经 load 不丢（崩溃恢复保留命中热度）。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    fp = _fp(tmp_ckpt, wk)
    ht = {"rg000001": {"hit_count": 3, "last_hit_ts": 1000.0,
                       "last_hit_type": "raw", "last_hit_round": 12}}
    _seed_t_file_with_rounds(fp, round_groups=[_sealed_group("rg000001", 1, 8)],
                             hit_table=ht)
    t_file = asyncio.run(mgr.load(wk))
    got = t_file["metadata"]["record_state"]["hit_table"]
    assert got["rg000001"]["hit_count"] == 3
    assert got["rg000001"]["last_hit_round"] == 12


# ============================================================
# ③ 重编号 key 迁移（rg_id 变 hit 按 round_range 映射不丢）
# ============================================================
def test_migrate_hit_keep_unchanged_key():
    """老 rg_id 在新组里仍存在（kept 段）→ entry 原样保留。"""
    old_ht = {"rg000001": {"hit_count": 2, "last_hit_ts": 100.0, "last_hit_type": "raw"}}
    old_g = [_sealed_group("rg000001", 1, 8)]
    new_g = [_sealed_group("rg000001", 1, 8), _sealed_group("rg000002", 9, 16)]
    out = record.migrate_hit_table_on_renumber(old_ht, old_g, new_g)
    assert out["rg000001"]["hit_count"] == 2


def test_migrate_hit_renumbered_by_overlap():
    """老 rg 被重写（rg_id 变）→ 用 round_range 重叠把 hit 迁到新 rg_id 不丢。"""
    # 老组 rg000005 覆盖 [9,16]；compose 重写窗口后该段变成新 rg000010 覆盖 [9,16]
    old_ht = {"rg000005": {"hit_count": 4, "last_hit_ts": 200.0,
                           "last_hit_type": "raw", "last_hit_round": 18}}
    old_g = [_sealed_group("rg000001", 1, 8), _sealed_group("rg000005", 9, 16)]
    new_g = [_sealed_group("rg000001", 1, 8), _sealed_group("rg000010", 9, 16)]
    out = record.migrate_hit_table_on_renumber(old_ht, old_g, new_g)
    # 老 key 消失、热度迁到覆盖 [9,16] 的新 key rg000010
    assert "rg000005" not in out
    assert out["rg000010"]["hit_count"] == 4
    assert out["rg000010"]["last_hit_round"] == 18


def test_migrate_hit_merge_into_one():
    """两老组被合并成一个新组 → entry 合并：count 求和、ts 取最近、type/round 跟最近。"""
    old_ht = {
        "rg000003": {"hit_count": 2, "last_hit_ts": 100.0, "last_hit_type": "record",
                     "last_hit_round": 10},
        "rg000004": {"hit_count": 5, "last_hit_ts": 300.0, "last_hit_type": "raw",
                     "last_hit_round": 14},
    }
    old_g = [_sealed_group("rg000003", 1, 4), _sealed_group("rg000004", 5, 8)]
    new_g = [_sealed_group("rg000009", 1, 8)]  # 合并 [1,8]
    out = record.migrate_hit_table_on_renumber(old_ht, old_g, new_g)
    assert "rg000009" in out
    assert out["rg000009"]["hit_count"] == 7          # 2+5 求和
    assert out["rg000009"]["last_hit_ts"] == 300.0     # 取最近
    assert out["rg000009"]["last_hit_type"] == "raw"   # 跟最近那条
    assert out["rg000009"]["last_hit_round"] == 14


def test_migrate_hit_dropped_when_no_target():
    """老组内容被裁掉（新组里无覆盖该 round 的组）→ entry 丢弃（内容已不在 record）。"""
    old_ht = {"rg000007": {"hit_count": 9, "last_hit_ts": 100.0, "last_hit_type": "raw"}}
    old_g = [_sealed_group("rg000007", 50, 60)]
    new_g = [_sealed_group("rg000001", 1, 8)]  # 不覆盖 [50,60]
    out = record.migrate_hit_table_on_renumber(old_ht, old_g, new_g)
    assert out == {}


def test_round_id_to_rg_id_mapping():
    """round_id_to_rg_id：round 整数按 round_range 闭区间映射到所属 rg_id。"""
    groups = [_sealed_group("rg000001", 1, 8), _sealed_group("rg000002", 9, 16)]
    assert record.round_id_to_rg_id(5, groups) == "rg000001"
    assert record.round_id_to_rg_id(8, groups) == "rg000001"   # 闭区间端点
    assert record.round_id_to_rg_id(9, groups) == "rg000002"
    assert record.round_id_to_rg_id(100, groups) is None        # 无覆盖组


# ============================================================
# ④ 命中升档（hit 后 tier 升）
# ============================================================
def test_hit_upgrade_after_apply():
    """apply_hit_to_table 累加命中 → hit_score 超阈值 → tier_for_group 升档。"""
    g = _sealed_group("rg000007", 1, 10)
    ht = {}
    # base age=30(now=40,end=10) → summary。命中前应是 summary。
    assert record.tier_for_group(g, now_round=40, hit_table=ht, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY
    # 命中 2 次 raw（同刻无衰减，score=2*1.0=2 >= 1.0）
    record.apply_hit_to_table(ht, "rg000007", "raw", 1000.0, now_round=40)
    record.apply_hit_to_table(ht, "rg000007", "raw", 1000.0, now_round=40)
    assert ht["rg000007"]["hit_count"] == 2
    # 命中后升档 summary→full
    assert record.tier_for_group(g, now_round=40, hit_table=ht, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL


def test_apply_hit_invalid_inputs_safe():
    """apply_hit_to_table 非法输入安全降级：空 rg / 非 dict 不报错；非法 type 归 raw。"""
    assert record.apply_hit_to_table({}, "", "raw", 1.0) == {}
    out = record.apply_hit_to_table({}, "rg000001", "bogus", 1.0, now_round=3)
    assert out["rg000001"]["last_hit_type"] == "raw"  # 非法 type 归一
    assert out["rg000001"]["last_hit_round"] == 3


# ============================================================
# ⑤ 命中锁定 hit_keep_rounds（命中后 N 轮强制不降）
# ============================================================
def test_hit_keep_active_window():
    """hit_keep_active：命中后 <keep_rounds 轮内 True，到期 False。"""
    ht = {"rg000001": {"hit_count": 1, "last_hit_ts": 1000.0, "last_hit_type": "raw",
                       "last_hit_round": 10}}
    # keep=3：now=10(Δ0)/11(Δ1)/12(Δ2) 锁定；13(Δ3) 释放
    assert record.hit_keep_active("rg000001", ht, 10, CFG) is True
    assert record.hit_keep_active("rg000001", ht, 12, CFG) is True
    assert record.hit_keep_active("rg000001", ht, 13, CFG) is False
    # 缺 last_hit_round → 不锁定
    ht2 = {"rg000001": {"hit_count": 1, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    assert record.hit_keep_active("rg000001", ht2, 11, CFG) is False


def test_hit_keep_locks_tier_when_score_decayed():
    """命中锁定核心：hit_score 已衰减到阈值下（不再触发 hit 升档），但仍在 keep 窗口内
    → tier 仍强制抬一档不降（替代衰减横跳）。"""
    g = _sealed_group("rg000007", 1, 10)
    # 命中很久前（Δt 远大于半衰期 → score≈0，不触发 ② hit 升档）
    # 但 last_hit_round=38、now_round=40（Δ2 < keep 3）→ ④ 锁定仍强制升档
    ht = {"rg000007": {"hit_count": 1, "last_hit_ts": 0.0, "last_hit_type": "raw",
                       "last_hit_round": 38}}
    now_ts = 10 * 86400.0  # 10 个半衰期，score≈0.001 << 1.0
    # base age=30 → summary；hit_score 不触发，但锁定窗口内 → 强制 full
    assert record.tier_for_group(g, now_round=40, hit_table=ht, cfg=CFG,
                                 now_ts=now_ts) == record.TIER_FULL
    # 锁定窗口过后（now_round=41，Δ3 >= keep 3）→ 不再锁定、score 也≈0 → 回落 summary
    assert record.tier_for_group(g, now_round=41, hit_table=ht, cfg=CFG,
                                 now_ts=now_ts) == record.TIER_SUMMARY


def test_hit_keep_disabled_when_zero():
    """hit_keep_rounds<=0 → 永不锁定（关闭锁定特性）。"""
    ht = {"rg000001": {"hit_count": 1, "last_hit_ts": 1.0, "last_hit_type": "raw",
                       "last_hit_round": 10}}
    cfg0 = dict(CFG, hit_keep_rounds=0)
    assert record.hit_keep_active("rg000001", ht, 10, cfg0) is False


# ============================================================
# ⑥ 命中类型权重（原文 raw > record）
# ============================================================
def test_hit_type_weight_raw_gt_record():
    """同 hit_count/同时刻：raw 权重 1.0 > record 0.5；raw 升档而 record 不升。"""
    g = _sealed_group("rg000007", 1, 10)  # base age=30 → summary
    ht_raw = {"rg000007": {"hit_count": 1, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    ht_rec = {"rg000007": {"hit_count": 1, "last_hit_ts": 1000.0, "last_hit_type": "record"}}
    # raw: score=1*1.0=1.0 >= 1.0 → 升档 full
    assert record.tier_for_group(g, now_round=40, hit_table=ht_raw, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL
    # record: score=1*0.5=0.5 < 1.0 → 不升、留 summary
    assert record.tier_for_group(g, now_round=40, hit_table=ht_rec, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY
    # hit_score 直接对比：raw > record
    s_raw = record.hit_score("rg000007", ht_raw, 1000.0, CFG)
    s_rec = record.hit_score("rg000007", ht_rec, 1000.0, CFG)
    assert s_raw > s_rec
    assert s_raw == pytest.approx(1.0)
    assert s_rec == pytest.approx(0.5)


# ============================================================
# D10 升档封顶拆两线
# ============================================================
def test_hit_cap_text_line_full():
    """文字组（无 has_multimodal）命中升档封顶 full。"""
    g = _sealed_group("rg000007", 1, 10)  # base age=30 → summary
    ht = {"rg000007": {"hit_count": 5, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    assert record.tier_for_group(g, now_round=40, hit_table=ht, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_FULL


def test_hit_cap_multimodal_line_summary():
    """多模态原图组（has_multimodal=True，S7 占位标记）命中升档封顶 summary，不拉回 full。"""
    g = _sealed_group("rg000007", 1, 10, multimodal=True)  # base age=30 → summary
    ht = {"rg000007": {"hit_count": 9, "last_hit_ts": 1000.0, "last_hit_type": "raw"}}
    # base 已是 summary、封顶也是 summary → 维持 summary（不升 full）
    assert record.tier_for_group(g, now_round=40, hit_table=ht, cfg=CFG,
                                 now_ts=1000.0) == record.TIER_SUMMARY
    # 更老组 base=brief，多模态命中升一档到 summary（封顶 summary，不到 full）
    g2 = _sealed_group("rg000008", 1, 10, multimodal=True)
    assert record.tier_for_group(g2, now_round=100, hit_table={
        "rg000008": {"hit_count": 9, "last_hit_ts": 1000.0, "last_hit_type": "raw"}},
        cfg=CFG, now_ts=1000.0) == record.TIER_SUMMARY


# ============================================================
# ⑦ 收尾事务：compose 替换 round_groups + hit 队列 flush + 重编号迁移 锁内协调无竞态
# ============================================================
def test_finalize_transaction_compose_flushes_hit_queue(tmp_ckpt):
    """compose_record_if_needed 收尾事务：同一锁内同一 save 完成
    ①边界表替换 ②旧 hit_table 重编号迁移 ③内存 hit 队列 flush。无三方竞态。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    fp = _fp(tmp_ckpt, wk)
    # 预置：一个已有的 legacy 组 rg000000 [0,0] 带历史 hit + 14 真轮待聚合
    pre_groups = [{
        "rg_id": "rg000000", "round_range": [0, 0], "tier": "summary",
        "sealed": True, "legacy_rg": True, "full_text": "历史", "summary_text": "历史摘要",
    }]
    pre_ht = {"rg000000": {"hit_count": 2, "last_hit_ts": 50.0, "last_hit_type": "raw",
                           "last_hit_round": 3}}
    _seed_t_file_with_rounds(fp, n_rounds=14, round_groups=pre_groups, hit_table=pre_ht)

    # 命中 round 7（聚合后会落在某个新组里）入队 —— compose 收尾应 flush 它
    mgr.record_hit(wk, round_int=7, hit_type="raw", now_round=29)

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        import re
        rounds = [{"round_int": int(m)} for m in re.findall(r"\[round (\d+)\]", prompt)]
        return _json_caller_text(rounds, group_size=8)

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={"record_compose_token_limit": 100, "compress_delta_floor": 200,
                 "record_max_relay_rounds": 3},
        )

    t_file, result = asyncio.run(run())
    assert result is not None, "record 聚合应被触发"

    disk = json.load(open(fp, encoding="utf-8"))
    rec = disk["metadata"]["record_state"]
    rgs = rec["round_groups"]
    ht = rec["hit_table"]
    # 队列已被 compose 收尾清空（无残留）
    assert mgr._hit_queue.get(wk) in (None, [])
    # ② legacy 组 rg000000 的历史 hit 经重编号迁移保留（rg000000 仍在 kept 段）
    assert "rg000000" in ht, f"legacy hit 丢失！hit_table={ht}"
    assert ht["rg000000"]["hit_count"] == 2
    # ③ 命中 round 7 经 flush 落进覆盖它的新组（按 round_range 现算 rg_id）
    target = record.round_id_to_rg_id(7, rgs)
    assert target is not None, "round 7 应已聚合进某组"
    assert target in ht, f"round 7 命中未落盘！target={target} hit_table={ht}"
    assert ht[target]["hit_count"] >= 1
    assert ht[target]["last_hit_type"] == "raw"


def test_finalize_transaction_no_compose_independent_flush(tmp_ckpt):
    """无 compose 触发（token 未超阈值）→ 独立 flush_hit_queue 落盘（不丢命中）。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, n_rounds=4,
                             round_groups=[_sealed_group("rg000001", 1, 4)])
    mgr.record_hit(wk, round_int=2, hit_type="raw", now_round=10)
    mgr.record_hit(wk, round_int=3, hit_type="raw", now_round=10)
    applied = asyncio.run(mgr.flush_hit_queue(wk))
    assert applied == 2  # round 2、3 都在 rg000001 [1,4]
    disk = json.load(open(fp, encoding="utf-8"))
    ht = disk["metadata"]["record_state"]["hit_table"]
    # 同组两次命中累加
    assert ht["rg000001"]["hit_count"] == 2


def test_record_hit_no_anchor_returns_false(tmp_ckpt):
    """record_hit 既无 round_int 又无 rg_id → 不入队、返回 False。"""
    mgr = TFileManager()
    wk = "GroupMessage:b4001"
    assert mgr.record_hit(wk, hit_type="raw") is False
    assert mgr._hit_queue.get(wk) in (None, [])


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
