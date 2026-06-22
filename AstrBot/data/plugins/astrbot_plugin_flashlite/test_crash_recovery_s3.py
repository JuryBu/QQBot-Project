"""
S3 批3a 崩溃恢复核心单元测试
================================
覆盖 F1.4 buffer WAL 持久化 + F2.2 崩溃恢复（C2 critical「崩溃可恢复」）。

跑法（PowerShell；git-bash fork .venv python 有问题，一律用 PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_crash_recovery_s3.py -q

测试策略
--------
- 「崩溃」用「丢弃旧 TFileManager 实例 + 新建实例 load」模拟：
  新实例 _recovered 为空 → 首次 load 触发 _recover_window_if_needed。
- buffer 未 flush + WAL 存在 = 崩在 flush 之前的状态。
- 所有用例 monkeypatch checkpoint.CHECKPOINTS_DIR → 临时目录，绝不写真实现场。
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
import round_tracker  # noqa: E402
from checkpoint import (  # noqa: E402
    TFileManager,
    wal_file_path,
    wal_read,
    gc_orphan_tmp_files,
    DANGLING_TOOL_PLACEHOLDER,
)


# ========================
# fixture：隔离 CHECKPOINTS_DIR
# ========================
@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s3_crash_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


# 全模块共用一个 event loop（per-window asyncio.Lock 惰性绑 loop，跨 asyncio.run 会炸）
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _mgr():
    """每次 new 一个 TFileManager（模拟进程重启：__init__ 跑启动 GC，_recovered 清空）。"""
    return TFileManager()


# ========================
# 1. WAL 写：buffer_message → wal.jsonl 出现且含消息
# ========================
def test_wal_written_on_buffer(tmp_ckpt):
    mgr = _mgr()
    wk = "GroupMessage:201"
    mgr.buffer_message(wk, {"role": "user", "content": "嗨", "message_id": "m1"})
    mgr.buffer_message(wk, {"role": "user", "content": "在吗", "message_id": "m2"})

    fp = wal_file_path(tmp_ckpt, wk)
    assert os.path.exists(fp), "WAL 文件应在 buffer_message 后出现"

    entries = wal_read(tmp_ckpt, wk)
    assert len(entries) == 2
    assert entries[0]["msg"]["content"] == "嗨"
    assert entries[0]["msg"]["message_id"] == "m1"
    # 去重键：有 message_id 用 mid:
    assert entries[0]["_wal_key"] == "mid:m1"
    assert entries[1]["_wal_key"] == "mid:m2"


def test_wal_fallback_key_without_message_id(tmp_ckpt):
    """无 message_id 的消息：用 {window}#seq{N} 临时键，序号单调递增。"""
    mgr = _mgr()
    wk = "GroupMessage:202"
    mgr.buffer_message(wk, {"role": "assistant", "content": "回1"})
    mgr.buffer_message(wk, {"role": "assistant", "content": "回2"})

    entries = wal_read(tmp_ckpt, wk)
    assert entries[0]["_wal_key"] == f"{wk}#seq0"
    assert entries[1]["_wal_key"] == f"{wk}#seq1"


# ========================
# 2. WAL 清理：flush_buffer 后 wal 删除
# ========================
def test_wal_cleared_after_flush(tmp_ckpt):
    mgr = _mgr()
    wk = "GroupMessage:203"
    mgr.buffer_message(wk, {"role": "user", "content": "x", "message_id": "mx"})
    fp = wal_file_path(tmp_ckpt, wk)
    assert os.path.exists(fp)

    _run(mgr.flush_buffer(wk))
    assert not os.path.exists(fp), "flush 落盘成功后 WAL 应被清理"
    # 消息已进 T 文件
    t = _run(mgr.load(wk))
    assert any(m.get("content") == "x" for m in t["messages"])
    # seq 计数器也清空
    assert wk not in mgr._wal_seq


# ========================
# 3. 崩溃 replay：buffer 有未 flush 消息 + WAL 存在 → 新 TFileManager → 恢复 + 取号
# ========================
def test_crash_replay_recovers_and_assigns_numbers(tmp_ckpt):
    # --- 进程 A：buffer 消息但未 flush（模拟崩在 flush 前）---
    mgrA = _mgr()
    wk = "GroupMessage:204"
    mgrA.buffer_message(wk, {"role": "user", "content": "崩前1", "message_id": "c1"})
    mgrA.buffer_message(wk, {"role": "user", "content": "崩前2", "message_id": "c2"})
    # 故意不 flush；WAL 已落盘
    assert os.path.exists(wal_file_path(tmp_ckpt, wk))
    del mgrA  # 进程崩溃

    # --- 进程 B：新实例 load → 触发恢复 replay ---
    mgrB = _mgr()
    t = _run(mgrB.load(wk))
    contents = [m.get("content") for m in t["messages"]]
    assert "崩前1" in contents and "崩前2" in contents, "WAL 消息应被 replay 补回"
    # 取了真号
    replayed = [m for m in t["messages"] if m.get("content") in ("崩前1", "崩前2")]
    assert all(m["round_id"] is not None for m in replayed)
    assert all(m["step_id"] is not None for m in replayed)
    assert len({m["step_id"] for m in replayed}) == 2  # 无重号
    # WAL 已清理
    assert not os.path.exists(wal_file_path(tmp_ckpt, wk))


# ========================
# 4. replay 去重：WAL 消息已在 T 文件（message_id 匹配）→ 跳过不重复 append
# ========================
def test_crash_replay_dedup_by_message_id(tmp_ckpt):
    mgrA = _mgr()
    wk = "GroupMessage:205"
    # 先正常落盘一条 message_id=d1
    _run(mgrA.append_messages(wk, [
        {"role": "user", "content": "已落盘", "message_id": "d1"},
    ]))
    # 再手工往 WAL 塞同 message_id=d1（模拟：flush 已落盘但 WAL 还没来得及清就崩了）
    #   外加一条新的 d2（未落盘）
    mgrA.buffer_message(wk, {"role": "user", "content": "已落盘", "message_id": "d1"})
    mgrA.buffer_message(wk, {"role": "user", "content": "新消息", "message_id": "d2"})
    del mgrA

    mgrB = _mgr()
    t = _run(mgrB.load(wk))
    # d1 只出现一次（去重），d2 补回
    d1_msgs = [m for m in t["messages"] if m.get("message_id") == "d1"]
    d2_msgs = [m for m in t["messages"] if m.get("message_id") == "d2"]
    assert len(d1_msgs) == 1, "已落盘的 d1 不应被 replay 重复 append"
    assert len(d2_msgs) == 1, "未落盘的 d2 应被 replay 补回"


# ========================
# 5. .tmp GC：构造 .t_file_xxx.tmp / .state_xxx.tmp → 恢复（启动）时删
# ========================
def test_tmp_gc_on_startup(tmp_ckpt):
    # 手工造半写临时文件
    junk1 = os.path.join(tmp_ckpt, ".t_file_abc123.tmp")
    junk2 = os.path.join(tmp_ckpt, ".state_xyz789.tmp")
    keep = os.path.join(tmp_ckpt, "GroupMessage_206.json")  # 正常文件不许动
    for p in (junk1, junk2):
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}")
    with open(keep, "w", encoding="utf-8") as f:
        f.write("{}")

    # 直接调 GC（__init__ 也会调，这里显式验证返回数）
    removed = gc_orphan_tmp_files(tmp_ckpt)
    assert removed == 2
    assert not os.path.exists(junk1)
    assert not os.path.exists(junk2)
    assert os.path.exists(keep), "正常 .json 文件不应被 GC 删除"


def test_tmp_gc_runs_in_init(tmp_ckpt):
    """TFileManager.__init__ 启动期自动跑一次 tmp GC。"""
    junk = os.path.join(tmp_ckpt, ".t_file_init.tmp")
    with open(junk, "w", encoding="utf-8") as f:
        f.write("{}")
    _ = _mgr()  # __init__ 触发 GC
    assert not os.path.exists(junk)


# ========================
# 6. gen 校验：T 文件 gen=N，state gen=N-1 → 取大者
# ========================
def test_generation_cross_check_takes_max(tmp_ckpt):
    mgr = _mgr()
    wk = "GroupMessage:207"
    # 正常落盘一批，gen 推进到 1，T 文件与 state 一致
    _run(mgr.append_messages(wk, [
        {"role": "user", "content": "u", "message_id": "g1"},
        {"role": "assistant", "content": "a", "message_id": "g2"},
    ]))

    # 手工制造不一致：T 文件 gen=5，state gen=4（模拟 state 落后一次写盘）
    fp = mgr._file_path(wk)
    with open(fp, "r", encoding="utf-8") as f:
        tf = json.load(f)
    tf["metadata"]["generation"] = 5
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(tf, f, ensure_ascii=False)

    st = round_tracker.load_state(tmp_ckpt, wk)
    st["generation"] = 4
    round_tracker.save_state(tmp_ckpt, wk, st)

    # 新实例 load → 恢复 gen 校验
    mgr2 = _mgr()
    t = _run(mgr2.load(wk))
    assert t["metadata"]["generation"] == 5, "应取大者 5"
    st_after = round_tracker.load_state(tmp_ckpt, wk)
    assert st_after["generation"] == 5, "state 应被修正为 5"


def test_generation_state_ahead_of_tfile(tmp_ckpt):
    """反向：state gen 大于 T 文件 gen → T 文件被修正为大者。"""
    mgr = _mgr()
    wk = "GroupMessage:208"
    _run(mgr.append_messages(wk, [
        {"role": "user", "content": "u", "message_id": "h1"},
    ]))
    fp = mgr._file_path(wk)
    with open(fp, "r", encoding="utf-8") as f:
        tf = json.load(f)
    tf["metadata"]["generation"] = 3
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(tf, f, ensure_ascii=False)
    st = round_tracker.load_state(tmp_ckpt, wk)
    st["generation"] = 9
    round_tracker.save_state(tmp_ckpt, wk, st)

    mgr2 = _mgr()
    t = _run(mgr2.load(wk))
    assert t["metadata"]["generation"] == 9, "T 文件 gen 应被修正为大者 9"


# ========================
# 7. round_id 崩后单调：崩前 r000005，重启新消息 → r000006 不复用
# ========================
def test_round_id_monotonic_across_crash(tmp_ckpt):
    mgr = _mgr()
    wk = "GroupMessage:209"
    # 手工把 next_round_id 推到 6（模拟崩前已用到 r000005）
    fp = mgr._file_path(wk)
    _run(mgr.load(wk))  # 先建文件
    with open(fp, "r", encoding="utf-8") as f:
        tf = json.load(f)
    tf["metadata"]["next_round_id"] = 6
    tf["metadata"]["next_step_id"] = 20
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(tf, f, ensure_ascii=False)

    # 崩前 buffer 一条 user（未 flush），WAL 落盘
    mgr.buffer_message(wk, {"role": "user", "content": "崩后第一条", "message_id": "r1"})
    del mgr

    # 重启 replay
    mgr2 = _mgr()
    t = _run(mgr2.load(wk))
    new_msg = [m for m in t["messages"] if m.get("message_id") == "r1"][0]
    # round_id 必须是 r000006（续号，不复用 r000005，也不重号）
    assert new_msg["round_id"] == "r000006", f"崩后应跳到 r000006，实际 {new_msg['round_id']}"
    # 号源继续推进
    assert t["metadata"]["next_round_id"] == 7


# ========================
# 附加：崩溃自愈 — dangling tool_calls 落盘修复（F2.2 步骤 5）
# ========================
def test_crash_self_heal_dangling(tmp_ckpt):
    """T 文件末尾停在 assistant.tool_calls 无配对 tool → 恢复时落盘补占位。"""
    mgr = _mgr()
    wk = "GroupMessage:210"
    _run(mgr.load(wk))  # 建文件
    fp = mgr._file_path(wk)
    with open(fp, "r", encoding="utf-8") as f:
        tf = json.load(f)
    # 手工构造 dangling：assistant 发起 tool_call=TA，但无 role=tool 结果
    tf["messages"] = [
        {"role": "user", "content": "查天气", "round_id": "r000001",
         "step_id": "s00000001"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "TA", "function": {"name": "weather", "arguments": "{}"}}],
         "round_id": "r000001", "step_id": "s00000002"},
    ]
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(tf, f, ensure_ascii=False)

    mgr2 = _mgr()
    t = _run(mgr2.load(wk))
    # 落盘 messages 里应已补一条占位 tool（tool_call_id=TA）
    tool_msgs = [m for m in t["messages"]
                 if m.get("role") == "tool" and m.get("tool_call_id") == "TA"]
    assert len(tool_msgs) == 1, "dangling tool_call 应被补占位"
    assert tool_msgs[0]["content"] == DANGLING_TOOL_PLACEHOLDER
    # 占位消息补齐了 v2 字段
    assert "first_reply" in tool_msgs[0]
    # 修复历史落盘
    assert any(r.get("type") == "dangling_placeholder"
               for r in t["metadata"].get("dangling_repair_history", []))

    # 二次 load 不应再触发修复（已落盘修好；新实例验证幂等）
    mgr3 = _mgr()
    t2 = _run(mgr3.load(wk))
    tool_msgs2 = [m for m in t2["messages"]
                  if m.get("role") == "tool" and m.get("tool_call_id") == "TA"]
    assert len(tool_msgs2) == 1, "已修好的 dangling 不应被重复补占位"


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
