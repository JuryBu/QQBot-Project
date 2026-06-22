"""
S3 批3b checkpoint 健壮性辅助单元测试
========================================
覆盖：
  F1.6 step(round)-aligned 压缩切分 —— 切分点对齐 round 边界，绝不切碎一轮；
       legacy（round_id=None）消息不报错按原逻辑切。
  F1.7 save 原子性增强 —— gen 同步一致（gen++ 唯一入口在 _append_messages_inner，
       save 本身不碰 gen）；写盘异常时不残留 .t_file_*.tmp。
  F1.8 拆 _compressing 锁 —— per-window 压缩锁（不同窗口不同 Lock，同窗口同一 Lock）。

跑法（PowerShell；git-bash fork .venv python 有问题，一律用 PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_checkpoint_s3_batch3b.py -q

关键隔离：所有用例 monkeypatch checkpoint.CHECKPOINTS_DIR → 临时目录，
绝不写真实 QQ_data/checkpoints 现场。
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

# ---- astrbot stub：checkpoint 仅用到 astrbot.api.logger。----
if "astrbot" not in sys.modules:
    import logging
    import types

    _astrbot = types.ModuleType("astrbot")
    _astrbot_api = types.ModuleType("astrbot.api")
    _astrbot_api.logger = logging.getLogger("flashlite_test")
    _astrbot.api = _astrbot_api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _astrbot_api

import pytest  # noqa: E402

import checkpoint  # noqa: E402
import round_tracker  # noqa: E402
from checkpoint import (  # noqa: E402
    TFileManager,
    _align_compress_count_to_round_boundary,
    _is_round_boundary,
    serialize_messages_for_compress,
)


# ========================
# fixture：隔离 CHECKPOINTS_DIR 到临时目录
# ========================
@pytest.fixture()
def tmp_ckpt(monkeypatch):
    """把模块级 CHECKPOINTS_DIR 指向临时目录，绝不碰真实现场。"""
    d = tempfile.mkdtemp(prefix="s3_b3b_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


# 全模块共用一个 event loop（per-window asyncio.Lock 惰性绑定首个 loop）。
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _mgr():
    return TFileManager()


def _round_segments_intact(messages):
    """断言 messages 内每个非 None round_id 的轮在序列中是连续块（不被打断）。

    返回每条消息的 round_id 列表（None 原样保留）。
    """
    return [m.get("round_id") for m in messages]


# ========================
# F1.6 round 边界对齐切分
# ========================
def test_f16_align_split_lands_on_round_boundary(tmp_ckpt):
    """构造跨 3 轮 messages，落在轮中间的初步切分点对齐后必落 round 边界，
    切出的两段各自 round 完整（无半轮）。"""
    mgr = _mgr()
    wk = "GroupMessage:b3b_1"
    # 3 轮：每轮 user + assistant，bot 已回 → 下条 user 开新轮
    t = _run(mgr.append_messages(wk, [
        {"role": "user", "content": "u1"},        # round A
        {"role": "assistant", "content": "a1"},   # round A
        {"role": "user", "content": "u2"},        # round B
        {"role": "assistant", "content": "a2"},   # round B
        {"role": "user", "content": "u3"},        # round C
        {"role": "assistant", "content": "a3"},   # round C
    ]))
    msgs = t["messages"]
    rids = _round_segments_intact(msgs)
    # 应得 3 个不同 round_id，各占 2 条
    assert rids[0] == rids[1] != rids[2]
    assert rids[2] == rids[3] != rids[4]
    assert rids[4] == rids[5]
    boundaries = {0, 2, 4, 6}  # round 边界下标（含两端）

    # 对所有可能初步切分点，对齐后必落在 round 边界（绝不切碎一轮）。
    # idx=1 落第一轮内部 → 向前退到 0 → 改向后挪到本轮结束边界 2（仍是边界、非空）。
    for idx in range(0, len(msgs) + 1):
        aligned = _align_compress_count_to_round_boundary(msgs, idx)
        assert _is_round_boundary(msgs, aligned), (
            f"idx={idx} 对齐到 {aligned} 不在 round 边界"
        )
        assert aligned in boundaries, f"idx={idx} → {aligned} 不在 {boundaries}"

    # 关键：idx=3（落 round B 的 assistant 前，即 round B 中间）→ 向前对齐到 2
    assert _align_compress_count_to_round_boundary(msgs, 3) == 2
    # idx=5（round C 中间）→ 向前对齐到 4
    assert _align_compress_count_to_round_boundary(msgs, 5) == 4
    # idx=1（落第一轮内部，向前退到 0）→ 改向后到本轮结束边界 2
    assert _align_compress_count_to_round_boundary(msgs, 1) == 2
    # idx=2/4（已在边界）→ 原样
    assert _align_compress_count_to_round_boundary(msgs, 2) == 2
    assert _align_compress_count_to_round_boundary(msgs, 4) == 4


def test_f16_aligned_segments_each_round_complete(tmp_ckpt):
    """对齐后切出的两段：被压缩段与保留段各自 round 完整（同一轮不跨段）。"""
    mgr = _mgr()
    wk = "GroupMessage:b3b_2"
    t = _run(mgr.append_messages(wk, [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]))
    msgs = t["messages"]
    # 初步切分点 3（round B 中间）→ 对齐 2
    aligned = _align_compress_count_to_round_boundary(msgs, 3)
    compressed = msgs[:aligned]
    kept = msgs[aligned:]
    comp_rounds = set(m["round_id"] for m in compressed)
    kept_rounds = set(m["round_id"] for m in kept)
    # 两段 round 集合不相交 → 没有任何一轮被切碎横跨两段
    assert comp_rounds.isdisjoint(kept_rounds), (
        f"轮被切碎: 压缩段 {comp_rounds} 与保留段 {kept_rounds} 有交集"
    )


def test_f16_legacy_round_none_no_error(tmp_ckpt):
    """legacy 消息（round_id=None）参与对齐与序列化不报错，对齐返回原 idx。"""
    legacy_msgs = [
        {"role": "user", "content": "old1", "round_id": None},
        {"role": "assistant", "content": "old2", "round_id": None},
        {"role": "user", "content": "old3", "round_id": None},
        {"role": "assistant", "content": "old4", "round_id": None},
    ]
    # 对齐：前一条 round_id=None → 不挪，返回原 idx（不报错）
    for idx in range(0, len(legacy_msgs) + 1):
        out = _align_compress_count_to_round_boundary(legacy_msgs, idx)
        assert out == idx, f"legacy idx={idx} 不应被挪动，得 {out}"
    # 序列化 legacy 消息不抛异常
    text = serialize_messages_for_compress(legacy_msgs)
    assert isinstance(text, str) and "old1" in text


def test_f16_mixed_legacy_and_round(tmp_ckpt):
    """混合 legacy（前段 None）+ 真 round（后段）：legacy 段不挪，真 round 段对齐。"""
    msgs = [
        {"role": "user", "content": "L1", "round_id": None},
        {"role": "assistant", "content": "L2", "round_id": None},
        {"role": "user", "content": "u1", "round_id": "r000001"},
        {"role": "assistant", "content": "a1", "round_id": "r000001"},
        {"role": "user", "content": "u2", "round_id": "r000002"},
        {"role": "assistant", "content": "a2", "round_id": "r000002"},
    ]
    # idx=3 落 r000001 中间（前一条 r000001 非 None）→ 向前对齐到 2
    assert _align_compress_count_to_round_boundary(msgs, 3) == 2
    # idx=1 前一条 round_id=None（legacy）→ 不挪
    assert _align_compress_count_to_round_boundary(msgs, 1) == 1
    # idx=2 已在边界（None→r000001 切换）→ 原样
    assert _align_compress_count_to_round_boundary(msgs, 2) == 2


def test_f16_first_round_internal_aligns_forward(tmp_ckpt):
    """idx 落在第一轮内部（向前对齐会退到 0）→ 改向后挪到本轮结束边界：
    压缩段 = 完整第一轮（仍落 round 边界、不切碎、且非空），避免压缩段为空。"""
    msgs = [
        {"role": "user", "content": "u1", "round_id": "r000001"},
        {"role": "assistant", "content": "a1", "round_id": "r000001"},
        {"role": "assistant", "content": "a1b", "round_id": "r000001"},
        {"role": "tool", "content": "t1", "round_id": "r000001"},
    ]
    # 整段都是第一轮 r000001：idx=2 落轮内 → 向后对齐到 4（=len，本轮结束边界）
    aligned = _align_compress_count_to_round_boundary(msgs, 2)
    assert aligned == 4
    assert _is_round_boundary(msgs, aligned)
    # 压缩段非空且为完整第一轮，保留段为空（但不切碎任何一轮）
    assert msgs[:aligned] == msgs and msgs[aligned:] == []


# ========================
# F1.7 save 原子性 / gen 一致
# ========================
def test_f17_generation_consistent_after_save(tmp_ckpt):
    """append（走唯一取号入口推进 gen + save）后：磁盘 metadata.generation
    与 state.generation 相等（gen++ 唯一入口在 _append_messages_inner，save 不重复 ++）。"""
    mgr = _mgr()
    wk = "GroupMessage:b3b_gen"
    _run(mgr.append_messages(wk, [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]))
    # 读盘 T 文件 metadata.generation
    fp = mgr._file_path(wk)
    with open(fp, "r", encoding="utf-8") as f:
        t_disk = json.load(f)
    t_gen = t_disk["metadata"]["generation"]
    # 读盘 state.generation
    st = round_tracker.load_state(tmp_ckpt, wk)
    s_gen = st["generation"]
    assert t_gen == s_gen, f"gen 不一致: metadata={t_gen} state={s_gen}"
    assert t_gen > 0, "append 后 gen 应已推进"

    # 再 append 一批 → 仍保持一致
    _run(mgr.append_messages(wk, [
        {"role": "user", "content": "x2"},
        {"role": "assistant", "content": "y2"},
    ]))
    with open(fp, "r", encoding="utf-8") as f:
        t_disk2 = json.load(f)
    st2 = round_tracker.load_state(tmp_ckpt, wk)
    assert t_disk2["metadata"]["generation"] == st2["generation"]
    # gen 单调推进（第二批 > 第一批）
    assert t_disk2["metadata"]["generation"] > t_gen


def test_f17_no_tmp_residue_on_write_failure(tmp_ckpt, monkeypatch):
    """mock json.dump 写盘抛异常 → save 抛错但目录下不残留 .t_file_*.tmp。"""
    mgr = _mgr()
    wk = "GroupMessage:b3b_tmp"
    t_file = checkpoint._create_empty_t_file(wk)

    def _boom(*a, **k):
        raise RuntimeError("simulated disk write failure")

    monkeypatch.setattr(checkpoint.json, "dump", _boom)

    with pytest.raises(RuntimeError):
        _run(mgr.save(wk, t_file))

    # 目录下不应残留任何 .t_file_*.tmp（except 分支已清理）
    leftovers = [
        n for n in os.listdir(tmp_ckpt)
        if n.startswith(".t_file_") and n.endswith(".tmp")
    ]
    assert leftovers == [], f"写失败后残留 tmp: {leftovers}"


# ========================
# F1.8 per-window 压缩锁
# ========================
def test_f18_compress_lock_per_window(tmp_ckpt):
    """不同 window_key 取到不同 Lock 对象；同 window_key 取同一 Lock（惰性建锁）。"""
    mgr = _mgr()
    lock_a1 = mgr._get_compress_lock("GroupMessage:A")
    lock_a2 = mgr._get_compress_lock("GroupMessage:A")
    lock_b = mgr._get_compress_lock("GroupMessage:B")

    # 同窗口同一对象
    assert lock_a1 is lock_a2
    # 不同窗口不同对象
    assert lock_a1 is not lock_b
    # 都是 asyncio.Lock
    assert isinstance(lock_a1, asyncio.Lock)
    assert isinstance(lock_b, asyncio.Lock)


def test_f18_old_compressing_set_removed():
    """旧全局 _compressing set 已彻底移除（不再有该实例属性）。"""
    mgr = TFileManager()
    assert not hasattr(mgr, "_compressing"), "旧 _compressing set 应已删除"
    assert hasattr(mgr, "_compress_locks"), "应有新 _compress_locks dict"
    assert isinstance(mgr._compress_locks, dict)
