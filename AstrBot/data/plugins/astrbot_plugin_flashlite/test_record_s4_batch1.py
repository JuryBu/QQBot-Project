"""
S4 批1a 单测：record 机制地基（R1 扩锚 + M1 record.py + M2 sidecar）
=================================================================
覆盖 5 个验收点：
  ① R1 record_state 扩锚——旧 T 文件缺字段 load 自动补默认（不破坏 last_compressed_round_id）
  ② sidecar load-save 往返一致
  ③ render_record_md 渲染（确定性、分档）
  ④ validate 门禁——轮次重叠 / 倒退 / 空洞 各拒收 + 合法通过 + 衔接水位
  ⑤ 候选隔离——写门禁失败绝不覆盖正式文件

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_batch1.py -q
或直接：
  AstrBot/.venv/Scripts/python.exe test_record_s4_batch1.py
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


WK = "GroupMessage:123456"


# ========================
# ① R1 record_state 扩锚
# ========================
def test_r1_new_t_file_has_five_anchors():
    """新建 T 文件 record_state 含五锚，默认值正确。"""
    t = checkpoint._create_empty_t_file(WK)
    rs = t["metadata"]["record_state"]
    assert rs["last_compressed_round_id"] is None
    assert rs["last_grouped_rg_id"] is None
    assert rs["round_groups"] == []
    assert rs["hit_table"] == {}
    assert rs["summary_watermark_rg_id"] is None


def test_r1_old_t_file_missing_anchors_get_filled():
    """旧 T 文件 record_state 只含 last_compressed_round_id（S3 落盘形态），
    _ensure_metadata_v2_fields 嵌套补四个新锚，**绝不覆盖** last_compressed。"""
    old_meta = {
        "next_round_id": 50,
        "next_step_id": 800,
        "generation": 3,
        # S3 旧形态：record_state 顶层已存在，只含压缩锚且有非空值
        "record_state": {"last_compressed_round_id": "r000042"},
        "bpc_state": {},
        "concurrency_state": {},
    }
    checkpoint._ensure_metadata_v2_fields(old_meta)
    rs = old_meta["record_state"]
    # 压缩锚原值保留（不被默认 None 覆盖）
    assert rs["last_compressed_round_id"] == "r000042"
    # 四个新锚补上默认
    assert rs["last_grouped_rg_id"] is None
    assert rs["round_groups"] == []
    assert rs["hit_table"] == {}
    assert rs["summary_watermark_rg_id"] is None


def test_r1_empty_record_state_dict_gets_all_anchors():
    """record_state 是空 dict（极端旧态）时五锚全补。"""
    meta = {"record_state": {}}
    checkpoint._ensure_metadata_v2_fields(meta)
    rs = meta["record_state"]
    for k in ("last_compressed_round_id", "last_grouped_rg_id",
              "round_groups", "hit_table", "summary_watermark_rg_id"):
        assert k in rs


def test_r1_defaults_are_independent_copies():
    """两个 T 文件的 record_state 不共享同一可变对象（深拷贝隔离）。"""
    t1 = checkpoint._create_empty_t_file(WK)
    t2 = checkpoint._create_empty_t_file(WK)
    t1["metadata"]["record_state"]["round_groups"].append({"rg_id": "rg000001"})
    assert t2["metadata"]["record_state"]["round_groups"] == []


# ========================
# ② sidecar load-save 往返一致
# ========================
def test_sidecar_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        idx = record.new_index(generation=7)
        idx["source_hash"] = "deadbeef"
        idx["groups"] = [
            {
                "rg_id": "rg000001",
                "round_range": [1, 8],
                "char_offset": [0, 120],
                "tier": "full",
                "hit_count": 2,
                "sealed": True,
                "legacy_rg": False,
            },
            {
                "rg_id": "rg000002",
                "round_range": [9, 16],
                "char_offset": [121, 260],
                "tier": "summary",
                "hit_count": 0,
                "sealed": False,
                "legacy_rg": False,
            },
        ]
        record.save_index(d, WK, idx)
        loaded = record.load_index(d, WK)
        assert loaded == idx


def test_sidecar_load_missing_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        idx = record.load_index(d, WK)
        assert idx["source_hash"] == ""
        assert idx["groups"] == []


def test_sidecar_load_corrupt_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        fp = record.record_index_path(d, WK)
        with open(fp, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json !!!")
        idx = record.load_index(d, WK)
        assert idx["groups"] == []  # 损坏不抛、返回空


def test_sidecar_rebuild_if_stale():
    """record.md 内容变了 → sidecar 从 record_state 重建。"""
    with tempfile.TemporaryDirectory() as d:
        # 先写一份 record.md
        rg_index = [
            {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
             "full_text": "hello world", "sealed": True},
        ]
        md = record.render_record_md(rg_index)
        record.write_record_atomic(d, WK, md)

        record_state = {
            "round_groups": [
                {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
                 "sealed": True, "legacy_rg": False},
            ],
            "hit_table": {"rg000001": {"hit_count": 5}},
        }
        rebuilt, idx = record.rebuild_index_if_stale(d, WK, record_state, generation=2)
        assert rebuilt is True
        assert len(idx["groups"]) == 1
        assert idx["groups"][0]["rg_id"] == "rg000001"
        assert idx["groups"][0]["hit_count"] == 5  # 从 hit_table 注入
        assert idx["generation"] == 2

        # 再调一次：hash 未变 → 不重建
        rebuilt2, _ = record.rebuild_index_if_stale(d, WK, record_state, generation=2)
        assert rebuilt2 is False


# ========================
# ③ render_record_md 渲染
# ========================
def test_render_basic():
    rg_index = [
        {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
         "full_text": "完整原文块 A", "sealed": True},
        {"rg_id": "rg000002", "round_range": [9, 16], "tier": "summary",
         "summary_text": "摘要块 B"},
    ]
    md = record.render_record_md(rg_index)
    assert "# Conversation Record" in md
    assert "## rg000001 rounds 1-8 (full)" in md
    assert "完整原文块 A" in md
    assert "## rg000002 rounds 9-16 (summary)" in md
    assert "摘要块 B" in md
    assert "[sealed]" in md  # rg000001 sealed flag


def test_render_deterministic():
    """同输入必产同输出（无时间戳/随机），保证 D1 重渲 source_hash 稳定。"""
    rg_index = [
        {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
         "full_text": "x"},
    ]
    a = record.render_record_md(rg_index)
    b = record.render_record_md(rg_index)
    assert a == b
    assert record.compute_source_hash(a) == record.compute_source_hash(b)


def test_render_tier_fallback():
    """summary 档缺 summary_text 时回退 full_text。"""
    rg_index = [
        {"rg_id": "rg000001", "round_range": [1, 8], "tier": "summary",
         "full_text": "只有原文"},
    ]
    md = record.render_record_md(rg_index)
    assert "只有原文" in md


def test_render_tier_map_override():
    """tier_map 覆盖组内 tier。"""
    rg_index = [
        {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
         "full_text": "F", "brief_text": "B"},
    ]
    md = record.render_record_md(rg_index, tier_map={"rg000001": "brief"})
    assert "(brief)" in md
    assert "B" in md


# ========================
# ④ validate 门禁
# ========================
def _g(rg_id, s, e, **kw):
    d = {"rg_id": rg_id, "round_range": [s, e]}
    d.update(kw)
    return d


def test_validate_ok_continuous():
    cand = [_g("rg000001", 1, 8), _g("rg000002", 9, 16), _g("rg000003", 17, 24)]
    ok, errs = record.validate_composed_record(cand)
    assert ok, errs


def test_validate_empty_ok():
    ok, errs = record.validate_composed_record([])
    assert ok and errs == []


def test_validate_reject_overlap():
    """轮次重叠：rg000002 起点落入 rg000001 区间内。"""
    cand = [_g("rg000001", 1, 10), _g("rg000002", 8, 16)]
    ok, errs = record.validate_composed_record(cand)
    assert not ok
    assert any("重叠" in e for e in errs)


def test_validate_reject_regression():
    """倒退：后组起点 <= 前组起点。"""
    cand = [_g("rg000001", 9, 16), _g("rg000002", 1, 8)]
    ok, errs = record.validate_composed_record(cand)
    assert not ok
    assert any("倒退" in e or "重叠" in e for e in errs)


def test_validate_reject_gap():
    """空洞：rg000001 到 9，rg000002 从 12 起，缺 10-11。"""
    cand = [_g("rg000001", 1, 9), _g("rg000002", 12, 20)]
    ok, errs = record.validate_composed_record(cand)
    assert not ok
    assert any("空洞" in e for e in errs)


def test_validate_reject_bad_range():
    """区间倒置 s > e。"""
    cand = [_g("rg000001", 10, 5)]
    ok, errs = record.validate_composed_record(cand)
    assert not ok
    assert any("倒置" in e for e in errs)


def test_validate_legacy_allows_jump():
    """legacy_rg 组之后允许跳变（历史冷冻段不强制连续）。"""
    cand = [
        _g("rg000000", 1, 30, legacy_rg=True),
        _g("rg000001", 100, 108),  # 跳过 31-99 也合法
    ]
    ok, errs = record.validate_composed_record(cand)
    assert ok, errs


def test_validate_watermark_no_regress():
    """与 prev_state 衔接：候选首组不得回退覆盖已聚合水位。"""
    prev_state = {
        "last_grouped_rg_id": "rg000002",
        "round_groups": [
            {"rg_id": "rg000001", "round_range": [1, 8]},
            {"rg_id": "rg000002", "round_range": [9, 16]},
        ],
    }
    # 首组从 10 起 <= 水位 16 → 拒收
    cand = [_g("rg000003", 10, 20)]
    ok, errs = record.validate_composed_record(cand, prev_state)
    assert not ok
    assert any("回退覆盖" in e for e in errs)

    # 首组从 17 起 > 水位 16 → 通过
    cand_ok = [_g("rg000003", 17, 24)]
    ok2, errs2 = record.validate_composed_record(cand_ok, prev_state)
    assert ok2, errs2


def test_validate_round_range_string_ids():
    """round_range 端点为 'r000123' 字符串时也能解析。"""
    cand = [
        {"rg_id": "rg000001", "round_range": ["r000001", "r000008"]},
        {"rg_id": "rg000002", "round_range": ["r000009", "r000016"]},
    ]
    ok, errs = record.validate_composed_record(cand)
    assert ok, errs


# ========================
# ⑤ 候选隔离——写门禁失败绝不覆盖正式文件
# ========================
def test_candidate_isolation_reject_keeps_official():
    with tempfile.TemporaryDirectory() as d:
        # 先写一份合法正式 record.md
        good_index = [_g("rg000001", 1, 8)]
        good_md = record.render_record_md([
            {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
             "full_text": "正式内容"},
        ])
        ok, _ = record.write_record_atomic(
            d, WK, good_md, candidate_index=good_index
        )
        assert ok
        official_before = record.read_record(d, WK)
        assert "正式内容" in official_before

        # 用非法候选（重叠）尝试覆盖 → 门禁拒收
        bad_index = [_g("rg000001", 1, 10), _g("rg000002", 8, 16)]
        bad_md = "# Conversation Record\n\n坏内容不该落盘\n"
        ok2, errs = record.write_record_atomic(
            d, WK, bad_md, candidate_index=bad_index
        )
        assert not ok2
        assert errs

        # 正式文件维持原样，坏内容没落盘
        official_after = record.read_record(d, WK)
        assert official_after == official_before
        assert "坏内容" not in official_after

        # tmp 也已清理（目录下无残留 .record_md_*.tmp）
        leftovers = [
            n for n in os.listdir(d)
            if n.startswith(".record_md_") and n.endswith(".tmp")
        ]
        assert leftovers == []


def test_candidate_no_index_writes_directly():
    """candidate_index=None 时只做文本隔离写（地基允许，不做结构门禁）。"""
    with tempfile.TemporaryDirectory() as d:
        ok, errs = record.write_record_atomic(d, WK, "# Conversation Record\n\nplain\n")
        assert ok and errs == []
        assert "plain" in record.read_record(d, WK)


def test_recordstore_facade():
    """RecordStore 门面方法转发正确。"""
    with tempfile.TemporaryDirectory() as d:
        store = record.RecordStore(d)
        md = store.render_record_md([
            {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
             "full_text": "facade"},
        ])
        ok, _ = store.write_record_atomic(WK, md, candidate_index=[_g("rg000001", 1, 8)])
        assert ok
        assert "facade" in store.read_record(WK)


# ========================
# D1 重渲闭环：record.md 删了能从结构化 rg_index 重渲回来
# ========================
def test_d1_record_md_rebuildable():
    with tempfile.TemporaryDirectory() as d:
        rg_index = [
            {"rg_id": "rg000001", "round_range": [1, 8], "tier": "full",
             "full_text": "可重建内容", "sealed": True},
        ]
        md1 = record.render_record_md(rg_index)
        record.write_record_atomic(d, WK, md1)
        # 模拟 record.md 损坏/丢失
        os.remove(record.record_md_path(d, WK))
        assert record.read_record(d, WK) == ""
        # 从同一 rg_index 重渲，内容与原先一致
        md2 = record.render_record_md(rg_index)
        assert md2 == md1
        record.write_record_atomic(d, WK, md2)
        assert record.read_record(d, WK) == md1


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
