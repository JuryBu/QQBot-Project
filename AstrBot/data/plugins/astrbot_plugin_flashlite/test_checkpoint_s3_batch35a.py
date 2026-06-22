"""
S3 批3.5-A 单测：压缩 merge-save 号源「只进不退」
=================================================
覆盖 compress-clobbers-numbersource / compress-metadata-clobber /
compress-generation-regression（对抗审查 critical）：

压缩期间 flash_lite 网络调用不持业务锁，并发 append 经唯一取号入口推进了
【磁盘】metadata 的 next_round_id/next_step_id/generation（mid_arrival 消息已带
新号）。压缩 merge-save 落盘的是压缩前【快照】t_file（旧号源）。修复要求这三者
与 total_messages_ever 一样从磁盘最新值取 max——绝不让号源回退（否则下次 append
重发已用 round_id，违反「round_id 全局单调绝不复用」铁律，且不可自愈）。

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_checkpoint_s3_batch35a.py -q
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
from checkpoint import TFileManager  # noqa: E402


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s3_b35a_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _fp(d, wk):
    return os.path.join(d, wk.replace(":", "_") + ".json")


def _seed_t_file(fp):
    """20 条长消息（够 token 触发压缩），号源快照 45/80/7。"""
    msgs = []
    for i in range(20):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({
            "role": role,
            "content": f"历史消息编号{i}：" + "内容填充" * 30,
            "round_id": f"r{(i // 2) + 1:06d}",
            "step_id": f"s{i + 1:08d}",
            "first_reply": (role == "assistant"),
            "legacy": False,
            "message_id": f"m{i}",
        })
    t_file = {
        "version": 2,
        "window_key": "GroupMessage:35001",
        "T1": {"compressed_summary": "", "original_msg_count": 0},
        "messages": msgs,
        "metadata": {
            "next_round_id": 45,
            "next_step_id": 80,
            "generation": 7,
            "total_messages_ever": 20,
            "record_state": {"last_compressed_round_id": None},
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)


def test_b35a_compress_no_numbersource_regression(tmp_ckpt):
    """压缩期间磁盘号源被并发推进到 50/90/9，merge-save 后磁盘必须 >= 50/90/9。"""
    mgr = TFileManager()
    wk = "GroupMessage:35001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file(fp)

    async def fake_caller(prompt, **kw):
        # 压缩网络调用瞬间，模拟并发 append 推进【磁盘】号源 + 落一条 mid_arrival
        disk = json.load(open(fp, encoding="utf-8"))
        disk["metadata"]["next_round_id"] = 50
        disk["metadata"]["next_step_id"] = 90
        disk["metadata"]["generation"] = 9
        disk["metadata"]["total_messages_ever"] = 21
        disk["messages"].append({
            "role": "user", "content": "压缩期间到达的新消息",
            "round_id": "r000045", "step_id": "s00000080",
            "first_reply": False, "legacy": False, "message_id": "mid1",
        })
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(disk, f, ensure_ascii=False)
        return "【压缩摘要】这是压缩后的历史摘要文本，覆盖前若干轮对话。"

    snap = _run(mgr.load(wk))
    assert snap["metadata"]["next_round_id"] == 45, "前置：快照应是旧号源 45"

    updated, result = _run(mgr.compress_if_needed(
        wk, snap, fake_caller,
        token_limit=100, keep_recent=5, cooldown_seconds=0,
    ))
    assert result is not None, "压缩应被触发（token 超限）"

    disk = json.load(open(fp, encoding="utf-8"))
    md = disk["metadata"]
    assert md["next_round_id"] >= 50, f"next_round_id 回退！得到 {md['next_round_id']}"
    assert md["next_step_id"] >= 90, f"next_step_id 回退！得到 {md['next_step_id']}"
    assert md["generation"] >= 9, f"generation 回退！得到 {md['generation']}"
    # total_messages_ever 取 max 的原有逻辑没被破坏
    assert md["total_messages_ever"] >= 21
    # mid_arrival 消息被合并保留（不丢）
    assert any(m.get("message_id") == "mid1" for m in disk["messages"]), "mid_arrival 丢失"


def test_b35a_no_concurrent_append_still_compresses(tmp_ckpt):
    """无并发推进时（caller 不改磁盘），压缩仍正常、号源不被错误抬高。"""
    mgr = TFileManager()
    wk = "GroupMessage:35002"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file(fp)

    async def fake_caller(prompt, **kw):
        return "【压缩摘要】历史摘要。"

    snap = _run(mgr.load(wk))
    updated, result = _run(mgr.compress_if_needed(
        wk, snap, fake_caller,
        token_limit=100, keep_recent=5, cooldown_seconds=0,
    ))
    assert result is not None
    disk = json.load(open(fp, encoding="utf-8"))
    md = disk["metadata"]
    # 无并发 → 号源保持快照值 45/80/7（既不回退也不凭空抬高）
    assert md["next_round_id"] == 45
    assert md["next_step_id"] == 80
    assert md["generation"] == 7
    # 压缩确实发生：T1 有摘要、messages 变少
    assert disk["T1"]["compressed_summary"]
    assert len(disk["messages"]) < 20
