"""
S3 批2 checkpoint 集成单元测试
================================
覆盖 F1.1 schema 迁移 / F1.2 message v2 字段 / F1.3 唯一取号入口（集成 round_tracker）。

跑法（PowerShell；git-bash fork .venv python 有问题，一律用 PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_checkpoint_s3.py -q

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
# 测试进程里 stub 掉 astrbot，避免加载 quart/faiss 等重型栈（启动慢且内存峰值
# 易 MemoryError）。正式运行时 astrbot 已真实加载，本 stub 仅作用于单测进程。
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
from checkpoint import (  # noqa: E402
    TFileManager,
    _create_empty_t_file,
    _migrate_v1_to_v2,
    _MESSAGE_V2_DEFAULTS,
    _METADATA_V2_DEFAULTS,
)


# ========================
# fixture：隔离 CHECKPOINTS_DIR 到临时目录
# ========================
@pytest.fixture()
def tmp_ckpt(monkeypatch):
    """把模块级 CHECKPOINTS_DIR 指向临时目录，绝不碰真实现场。"""
    d = tempfile.mkdtemp(prefix="s3_ckpt_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d
    # tempfile.mkdtemp 残留交给系统清理，避免误删


# 全模块共用一个 event loop：TFileManager 的 per-window asyncio.Lock 惰性绑定
# 到首次使用的 loop，跨多次 asyncio.run（每次新 loop）会触发
# "bound to a different event loop"。共用单 loop 规避。
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _mgr():
    """构造 TFileManager（其 __init__ 会 makedirs(CHECKPOINTS_DIR)）。"""
    return TFileManager()


# ========================
# F1.1 schema 迁移
# ========================
def test_create_empty_is_v2():
    t = _create_empty_t_file("GroupMessage:123")
    assert t["version"] == 2
    md = t["metadata"]
    assert md["next_round_id"] == 1
    assert md["next_step_id"] == 1
    # S4 R1: record_state 扩五锚。压缩锚(S3 语义)仍 None，新增四锚补默认。
    assert md["record_state"]["last_compressed_round_id"] is None
    assert md["record_state"]["last_grouped_rg_id"] is None
    assert md["record_state"]["round_groups"] == []
    assert md["record_state"]["hit_table"] == {}
    assert md["record_state"]["summary_watermark_rg_id"] is None
    assert md["bpc_state"] == {}
    assert md["concurrency_state"] == {}


def test_migration_preserves_old_messages():
    """构造 v1 T 文件（含几条旧消息）→ migrate → 旧消息保留 + 补 legacy/round_id=None。"""
    v1 = {
        "version": 1,
        "window_key": "GroupMessage:999",
        "window_type": "group",
        "window_id": "999",
        "T1": {"compressed_summary": "", "compress_history": []},
        "messages": [
            {"role": "user", "content": "老消息1", "timestamp": "2025-01-01T00:00:00"},
            {"role": "assistant", "content": "老回复1", "timestamp": "2025-01-01T00:00:01"},
            {"role": "user", "content": "老消息2", "timestamp": "2025-01-01T00:00:02"},
        ],
        "metadata": {
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:02",
            "total_messages_ever": 3,
            "dangling_repair_history": [{"type": "x"}],  # 已有字段须保留
        },
    }
    before_count = len(v1["messages"])
    before_contents = [m["content"] for m in v1["messages"]]

    migrated = _migrate_v1_to_v2(v1)

    # version 升 2
    assert migrated["version"] == 2
    # 旧消息条数不变 + 原内容保留
    assert len(migrated["messages"]) == before_count
    assert [m["content"] for m in migrated["messages"]] == before_contents
    # 每条补 legacy + round_id/step_id 永久 None
    for m in migrated["messages"]:
        assert m["legacy"] is True
        assert m["round_id"] is None
        assert m["step_id"] is None
        assert m["first_reply"] is False
        assert m["recalled"] is False
        assert m["has_multimodal"] is False
    # metadata 补 next_round_id=1（不回填旧消息号）
    md = migrated["metadata"]
    assert md["next_round_id"] == 1
    assert md["next_step_id"] == 1
    # S4 R1: 迁移后 record_state 五锚齐全，压缩锚仍 None。
    assert md["record_state"]["last_compressed_round_id"] is None
    assert md["record_state"]["last_grouped_rg_id"] is None
    assert md["record_state"]["round_groups"] == []
    assert md["record_state"]["hit_table"] == {}
    assert md["record_state"]["summary_watermark_rg_id"] is None
    # 已有的 dangling_repair_history 保留不被覆盖
    assert md["dangling_repair_history"] == [{"type": "x"}]


def test_load_triggers_migration(tmp_ckpt):
    """落一份 v1 文件到磁盘 → load → 自动迁移为 v2 并回写。"""
    mgr = _mgr()
    wk = "GroupMessage:888"
    fp = mgr._file_path(wk)
    v1 = {
        "version": 1,
        "window_key": wk,
        "window_type": "group",
        "window_id": "888",
        "T1": {"compressed_summary": "", "compress_history": []},
        "messages": [
            {"role": "user", "content": "x", "timestamp": "2025-01-01T00:00:00"},
        ],
        "metadata": {"total_messages_ever": 1},
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(v1, f, ensure_ascii=False)

    t = _run(mgr.load(wk))
    assert t["version"] == 2
    assert len(t["messages"]) == 1
    assert t["messages"][0]["legacy"] is True
    assert t["metadata"]["next_round_id"] == 1

    # 回写后磁盘也应是 v2
    with open(fp, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["version"] == 2


# ========================
# F1.3 取号：单调递增、无重号
# ========================
def test_round_step_monotonic_no_dup(tmp_ckpt):
    """append 多条 → round_id/step_id 单调递增无重号。"""
    mgr = _mgr()
    wk = "GroupMessage:100"
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]
    t = _run(mgr.append_messages(wk, msgs))
    stored = t["messages"]
    step_ids = [m["step_id"] for m in stored]
    round_ids = [m["round_id"] for m in stored]
    # 全部有值
    assert all(s is not None for s in step_ids)
    assert all(r is not None for r in round_ids)
    # step_id 严格单调递增、无重号
    assert step_ids == sorted(step_ids)
    assert len(set(step_ids)) == len(step_ids)
    # round_id 单调不减
    assert round_ids == sorted(round_ids)


def test_no_renumber_across_loads(tmp_ckpt):
    """约束 1 核心：buffer 消息多次 load（经 _merge_buffer）不取号、不重号。"""
    mgr = _mgr()
    wk = "GroupMessage:101"
    # 走 buffer 路径
    mgr.buffer_message(wk, {"role": "user", "content": "buf1"})
    mgr.buffer_message(wk, {"role": "user", "content": "buf2"})

    # 多次 load（每次都会 _merge_buffer 合并视图，但绝不取号）
    t1 = _run(mgr.load(wk))
    t2 = _run(mgr.load(wk))
    # merge 视图里的 buffer 消息 round_id/step_id 应为 None（未取号）
    buffered = [m for m in t1["messages"] if m.get("content") in ("buf1", "buf2")]
    assert len(buffered) == 2
    assert all(m["round_id"] is None and m["step_id"] is None for m in buffered)
    # 多次 load 号源不被推进
    assert t1["metadata"]["next_step_id"] == 1
    assert t2["metadata"]["next_step_id"] == 1

    # flush 后才唯一取号
    _run(mgr.flush_buffer(wk))
    t3 = _run(mgr.load(wk))
    flushed = [m for m in t3["messages"] if m.get("content") in ("buf1", "buf2")]
    assert len(flushed) == 2  # 只落盘一次，不重复
    step_ids = [m["step_id"] for m in flushed]
    assert all(s is not None for s in step_ids)
    assert len(set(step_ids)) == 2  # 无重号


# ========================
# first_reply 锚定
# ========================
def test_first_reply(tmp_ckpt):
    """user → assistant：assistant 条 first_reply=True，user 条 False。"""
    mgr = _mgr()
    wk = "GroupMessage:102"
    t = _run(mgr.append_messages(wk, [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "答"},
    ]))
    u, a = t["messages"]
    assert u["first_reply"] is False
    assert a["first_reply"] is True


def test_consecutive_user_same_round(tmp_ckpt):
    """连续 user → user → assistant：前两 user 同轮（round_tracker 连续 user 逻辑）。"""
    mgr = _mgr()
    wk = "GroupMessage:103"
    t = _run(mgr.append_messages(wk, [
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a1"},
    ]))
    u1, u2, a1 = t["messages"]
    # 前两 user 落同一轮（bot 未回，不闭合不开新轮）
    assert u1["round_id"] == u2["round_id"]
    # assistant 吸入同轮
    assert a1["round_id"] == u1["round_id"]
    assert a1["first_reply"] is True


# ========================
# state 持久化
# ========================
def test_state_file_persisted(tmp_ckpt):
    """append 后 {window}.state.json 存在 + 字段正确。"""
    import round_tracker
    mgr = _mgr()
    wk = "GroupMessage:104"
    _run(mgr.append_messages(wk, [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]))
    sp = round_tracker.state_file_path(tmp_ckpt, wk)
    assert os.path.exists(sp), f"state 文件应存在: {sp}"
    with open(sp, "r", encoding="utf-8") as f:
        st = json.load(f)
    # 当前轮有效 + 已记录 first_reply
    assert st["current_round_id"] is not None
    assert st["first_reply_step_id"] is not None
    assert st["partial_round_status"] == "open"
    assert st["last_role"] == "assistant"


# ========================
# F1.2 v2 字段完整
# ========================
def test_v2_fields_complete(tmp_ckpt):
    """append 的 message 含全部 v2 字段。"""
    mgr = _mgr()
    wk = "GroupMessage:105"
    t = _run(mgr.append_messages(wk, [
        {"role": "user", "content": "hi", "message_id": "m1",
         "sender": {"qq": "111", "name": "甲", "is_bot": False},
         "receive_seq": 42, "has_multimodal": True},
    ]))
    m = t["messages"][0]
    # v2 终态字段全在
    for k in _MESSAGE_V2_DEFAULTS:
        assert k in m, f"缺 v2 字段: {k}"
    assert "timestamp" in m
    # 毫秒时间戳格式（含小数点）
    assert "." in m["timestamp"]
    # 上游传入的字段被采纳
    assert m["message_id"] == "m1"
    assert m["sender"] == {"qq": "111", "name": "甲", "is_bot": False}
    assert m["receive_seq"] == 42
    assert m["has_multimodal"] is True
    # 取号字段有值
    assert m["round_id"] is not None
    assert m["step_id"] is not None
    # S3 不用的留默认
    assert m["compressed"] is False
    assert m["rg_id"] is None
    assert m["recalled"] is False


def test_metadata_defaults_const_complete():
    """metadata v2 默认常量包含约定的全部新键（含 S3 批3a F2.2 新增 generation）。"""
    assert set(_METADATA_V2_DEFAULTS) == {
        "next_round_id", "next_step_id", "generation",
        "record_state", "bpc_state", "concurrency_state",
    }


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
