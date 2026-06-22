"""
S3 批3.5-D 单测：健壮性小项（对抗审查未验证项，主线复核为真）
=============================================================
  nondict-json                 顶层非 dict 的合法 JSON → 走损坏兜底（不抛 AttributeError）
  v2-fastpath-skips-backfill   缺字段的 v2 文件 load 时补 metadata 默认字段并持久化
  unknown-version-skips-legacy 未知/缺失版本 → 补 message v2 字段 + legacy
  lcr-string-compare           record_state 单调比较改数值（parse_round_id）防百万轮失真
  savestate-missing-fsync      save_state 往返一致（fsync 不破坏功能）

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_checkpoint_s3_batch35d.py -q
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
import round_tracker  # noqa: E402
from checkpoint import TFileManager  # noqa: E402


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s3_b35d_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _fp(d, wk):
    return os.path.join(d, wk.replace(":", "_") + ".json")


# ---- nondict-json ----
def test_d_nondict_json_goes_corrupt_fallback(tmp_ckpt):
    """顶层 list 的合法 JSON → load 不抛 AttributeError，走损坏兜底回退空 T + 保留 .corrupt。"""
    mgr = TFileManager()
    wk = "GroupMessage:35d01"
    fp = _fp(tmp_ckpt, wk)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)  # 合法 JSON，顶层非 dict

    t_file = _run(mgr.load(wk))  # 修复前：AttributeError 穿透；修复后：兜底
    assert isinstance(t_file, dict)
    assert t_file.get("version") == 2
    corrupt = [f for f in os.listdir(tmp_ckpt) if ".corrupt" in f]
    assert corrupt, "损坏文件现场应保留为 .corrupt"


# ---- v2-fastpath ----
def test_d_v2_fastpath_backfills_metadata(tmp_ckpt):
    """批3a 前的 v2 文件缺 generation → load 补齐并持久化到磁盘。"""
    mgr = TFileManager()
    wk = "GroupMessage:35d02"
    fp = _fp(tmp_ckpt, wk)
    seed = {
        "version": 2, "window_key": wk, "T1": {}, "messages": [],
        "metadata": {
            "next_round_id": 1, "next_step_id": 1,
            "record_state": {"last_compressed_round_id": None},
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    assert "generation" not in seed["metadata"], "前置：确实缺 generation"

    loaded = _run(mgr.load(wk))
    assert "generation" in loaded["metadata"], "v2 快路径应补 generation"
    disk = json.load(open(fp, encoding="utf-8"))
    assert "generation" in disk["metadata"], "应 save 持久化补的字段"


# ---- unknown-version ----
def test_d_unknown_version_backfills_message_v2(tmp_ckpt):
    """version 缺失(丢版本的真 v1) → 补 message v2 字段 + legacy=True + version=2。"""
    mgr = TFileManager()
    wk = "GroupMessage:35d03"
    fp = _fp(tmp_ckpt, wk)
    seed = {
        "window_key": wk, "T1": {},
        "messages": [
            {"role": "user", "content": "旧消息1"},
            {"role": "assistant", "content": "旧回复1"},
        ],
        "metadata": {},
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(seed, f)

    loaded = _run(mgr.load(wk))
    assert loaded["version"] == 2
    for m in loaded["messages"]:
        assert "round_id" in m, "message v2 字段应补齐"
        assert m.get("legacy") is True, "旧消息应标 legacy"


# ---- lcr 数值比较 ----
def test_d_parse_round_id_numeric_compare():
    """parse_round_id 解析 + 数值比较：r1000000 > r999999（字符串比较会失真）。"""
    assert round_tracker.parse_round_id("r000123") == 123
    assert round_tracker.parse_round_id("r1000000") == 1000000
    assert round_tracker.parse_round_id(None) == -1
    assert round_tracker.parse_round_id("garbage") == -1
    assert round_tracker.parse_round_id("") == -1
    # 字符串字典序在跨百万位失真，数值比较正确
    assert "r1000000" < "r999999"  # 确认旧 bug 存在
    assert (round_tracker.parse_round_id("r1000000")
            > round_tracker.parse_round_id("r999999"))


# ---- save_state fsync 往返 ----
def test_d_save_state_roundtrip(tmp_ckpt):
    """save_state(含 flush+fsync) + load_state 往返一致。"""
    wk = "GroupMessage:35d04"
    st = round_tracker.new_state()
    st["current_round_id"] = "r000042"
    st["generation"] = 5
    round_tracker.save_state(tmp_ckpt, wk, st)
    loaded = round_tracker.load_state(tmp_ckpt, wk)
    assert loaded["current_round_id"] == "r000042"
    assert loaded["generation"] == 5
