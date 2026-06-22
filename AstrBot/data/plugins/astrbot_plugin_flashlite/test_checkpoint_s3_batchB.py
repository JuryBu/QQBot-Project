"""
S3 批3.5-B 单测：buffer/WAL 崩溃恢复去重重构（主题 B 六缺陷）
==============================================================
对抗审查发现 6 个 buffer/WAL 崩溃恢复缺陷，根因 = 缺全局唯一去重键。F3.1 给每条
消息 receive_seq（time.time_ns 全局单调唯一）当去重键根治。本测覆盖各崩溃时点：

  ① receive_seq replay 去重（message_id=None 也去重）        — replay-dedup-ignores-wal-key
  ② first-touch 吸收残留 + 重写 WAL（二次崩溃不丢）          — first-touch / residual-not-rewal
  ③ flush 空 buffer 分支保残留 WAL 先 replay（不无脑删）     — flush-empty-wipes-residual-wal(critical)
  ④ flush 并发不丢（pop 进锁内 + leftover 重写 WAL）          — flush-walclear-race
  ⑤ T 损坏从 state + .corrupt 恢复 next_round_id 不重号       — numbering-3

跑法（PowerShell；git-bash fork .venv python 有问题，一律 PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_checkpoint_s3_batchB.py -q

崩溃模拟：丢弃旧 TFileManager + 新建实例 load（新实例 _recovered/_wal_owned 为空，
首次 load 触发 _recover_window_if_needed）。所有用例 monkeypatch CHECKPOINTS_DIR →
临时目录，绝不写真实现场。
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

# astrbot stub（仅用到 astrbot.api.logger，避免加载重型栈）
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
from checkpoint import (  # noqa: E402
    TFileManager,
    wal_file_path,
    wal_read,
    wal_append,
    wal_dedup_key,
)


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s3_bB_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


# 全模块共用一个 event loop（per-window asyncio.Lock 惰性绑 loop，跨 asyncio.run 会炸）
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _mgr():
    """new 一个 TFileManager（模拟进程重启：__init__ 跑启动 GC，_recovered/_wal_owned 清空）。"""
    return TFileManager()


# 单调纳秒戳生成器（模拟 router_mixin 的 _receive_ts_ns / main 的 time.time_ns）
_seq_counter = [1_700_000_000_000_000_000]


def _next_rseq():
    _seq_counter[0] += 1
    return _seq_counter[0]


# ============================================================
# ① receive_seq replay 去重（message_id=None 也去重）
# ============================================================
def test_replay_dedup_by_receive_seq_message_id_none(tmp_ckpt):
    """已落盘的 assistant 补录（message_id=None，但有 receive_seq）再次出现在 WAL →
    旧逻辑按 message_id 去重对 None 恒失效会重复落盘；新逻辑按 receive_seq 去重挡住。"""
    wk = "GroupMessage:B101"
    rseq_a = _next_rseq()
    rseq_u = _next_rseq()

    # 进程 A：正常落盘 user + assistant（assistant message_id=None 但有 receive_seq）
    mgrA = _mgr()
    _run(mgrA.append_messages(wk, [
        {"role": "user", "content": "问题", "message_id": "u1", "receive_seq": rseq_u},
        {"role": "assistant", "content": "回答", "message_id": None, "receive_seq": rseq_a},
    ]))
    # 手工往 WAL 塞回同 receive_seq 的两条（模拟：落盘后 WAL 还没清就崩了）
    wal_append(tmp_ckpt, wk,
               {"role": "user", "content": "问题", "message_id": "u1", "receive_seq": rseq_u},
               wal_dedup_key({"message_id": "u1"}, wk, 0))
    wal_append(tmp_ckpt, wk,
               {"role": "assistant", "content": "回答", "message_id": None, "receive_seq": rseq_a},
               f"{wk}#seq1")
    del mgrA

    # 进程 B：新实例 load → 恢复 replay，两条都应被去重（不重复落盘）
    mgrB = _mgr()
    t = _run(mgrB.load(wk))
    asst = [m for m in t["messages"] if m.get("role") == "assistant"]
    user = [m for m in t["messages"] if m.get("role") == "user"]
    assert len(asst) == 1, "message_id=None 的 assistant 应按 receive_seq 去重，不重复落盘"
    assert len(user) == 1, "user 也应去重"
    assert not os.path.exists(wal_file_path(tmp_ckpt, wk)), "replay 后 WAL 清理"


def test_replay_appends_new_dedups_existing_mixed(tmp_ckpt):
    """WAL 含已落盘（去重）+ 未落盘（补回）混合；未落盘的 message_id=None 也要补回并取号。"""
    wk = "GroupMessage:B102"
    rseq_old = _next_rseq()
    rseq_new = _next_rseq()

    mgrA = _mgr()
    _run(mgrA.append_messages(wk, [
        {"role": "user", "content": "旧", "message_id": "o1", "receive_seq": rseq_old},
    ]))
    # WAL: 旧（已落盘，去重）+ 新（未落盘 assistant，message_id=None，应补回）
    wal_append(tmp_ckpt, wk,
               {"role": "user", "content": "旧", "message_id": "o1", "receive_seq": rseq_old},
               "mid:o1")
    wal_append(tmp_ckpt, wk,
               {"role": "assistant", "content": "新补录", "message_id": None, "receive_seq": rseq_new},
               f"{wk}#seq1")
    del mgrA

    mgrB = _mgr()
    t = _run(mgrB.load(wk))
    olds = [m for m in t["messages"] if m.get("receive_seq") == rseq_old]
    news = [m for m in t["messages"] if m.get("receive_seq") == rseq_new]
    assert len(olds) == 1, "已落盘的旧消息去重，不重复"
    assert len(news) == 1, "未落盘的新 assistant（message_id=None）应被补回"
    assert news[0]["round_id"] is not None, "补回的消息取了真号"
    assert news[0]["step_id"] is not None


# ============================================================
# ② first-touch 吸收残留 + 重写 WAL（二次崩溃不丢）
# ============================================================
def test_first_touch_absorb_rewrites_wal(tmp_ckpt):
    """first_touch 吸收上次崩溃残留 WAL 后，重写进新 in-flight WAL（不抢标 _recovered）。"""
    wk = "GroupMessage:B201"
    # 进程 A 崩溃残留：两条 WAL（未 flush）
    mgrA = _mgr()
    mgrA.buffer_message(wk, {"role": "user", "content": "残留1", "message_id": "r1",
                             "receive_seq": _next_rseq()})
    mgrA.buffer_message(wk, {"role": "user", "content": "残留2", "message_id": "r2",
                             "receive_seq": _next_rseq()})
    del mgrA
    assert len(wal_read(tmp_ckpt, wk)) == 2

    # 进程 B：first_touch buffer 新消息 → 吸收残留 + 重写 WAL
    mgrB = _mgr()
    mgrB.buffer_message(wk, {"role": "user", "content": "新消息", "message_id": "n1",
                            "receive_seq": _next_rseq()})
    # WAL 应含残留2条 + 新1条 = 3 条（残留被重写，不是被删丢）
    entries = wal_read(tmp_ckpt, wk)
    contents = [e["msg"]["content"] for e in entries]
    assert "残留1" in contents and "残留2" in contents and "新消息" in contents
    assert len(entries) == 3, "吸收残留后应重写进 WAL（防二次崩溃丢），共3条"
    # 不抢标 _recovered（让 _do_recover 仍能做 gen 校验/自愈）
    assert wk not in mgrB._recovered, "first_touch 不应抢标 _recovered"
    assert wk in mgrB._wal_owned, "应认领该窗口为本进程 in-flight"


def test_first_touch_second_crash_no_loss(tmp_ckpt):
    """first_touch 吸收残留 + 重写 WAL 后【二次崩溃】（flush 前再挂）→ 第三个进程 replay 不丢。"""
    wk = "GroupMessage:B202"
    # 进程 A 崩溃残留
    mgrA = _mgr()
    mgrA.buffer_message(wk, {"role": "user", "content": "A残留", "message_id": "a1",
                             "receive_seq": _next_rseq()})
    del mgrA

    # 进程 B first_touch 吸收 + 新消息，然后【二次崩溃】（不 flush）
    mgrB = _mgr()
    mgrB.buffer_message(wk, {"role": "user", "content": "B新增", "message_id": "b1",
                             "receive_seq": _next_rseq()})
    del mgrB  # 二次崩溃，未 flush

    # 进程 C：新实例 load → replay 重写的 WAL，两条都补回，不丢
    mgrC = _mgr()
    t = _run(mgrC.load(wk))
    contents = [m.get("content") for m in t["messages"]]
    assert "A残留" in contents, "一次崩溃残留经二次崩溃仍不丢"
    assert "B新增" in contents, "二次崩溃前 buffer 的新消息也不丢"
    # 无重复
    assert contents.count("A残留") == 1 and contents.count("B新增") == 1


def test_first_touch_flush_no_duplicate(tmp_ckpt):
    """first_touch 吸收残留后正常 flush：消息只落盘一次（_wal_owned 守卫让 _do_recover 跳过 replay）。"""
    wk = "GroupMessage:B203"
    mgrA = _mgr()
    mgrA.buffer_message(wk, {"role": "user", "content": "残留X", "message_id": "x1",
                             "receive_seq": _next_rseq()})
    del mgrA

    mgrB = _mgr()
    mgrB.buffer_message(wk, {"role": "user", "content": "新Y", "message_id": "y1",
                            "receive_seq": _next_rseq()})
    _run(mgrB.flush_buffer(wk))  # 正常 flush

    t = _run(mgrB.load(wk))
    contents = [m.get("content") for m in t["messages"]]
    assert contents.count("残留X") == 1, "残留 flush 后只落盘一次（不因 replay 重复）"
    assert contents.count("新Y") == 1
    # flush 后 WAL 清理、不再认领
    assert not os.path.exists(wal_file_path(tmp_ckpt, wk))
    assert wk not in mgrB._wal_owned
    # 取号唯一（无重号）
    sids = [m["step_id"] for m in t["messages"] if m.get("step_id")]
    assert len(sids) == len(set(sids)), "step_id 无重复"


# ============================================================
# ③ flush 空 buffer 分支保残留 WAL 先 replay（critical）
# ============================================================
def test_flush_empty_preserves_residual_wal(tmp_ckpt):
    """flush 空 buffer 时，若 WAL 有【上次崩溃残留】，绝不无脑删 → 先 replay 落盘。"""
    wk = "GroupMessage:B301"
    # 手工造残留 WAL（模拟上次崩溃，本进程没 buffer 过该窗口）
    wal_append(tmp_ckpt, wk,
               {"role": "user", "content": "残留待救", "message_id": "s1",
                "receive_seq": _next_rseq()},
               "mid:s1")
    assert os.path.exists(wal_file_path(tmp_ckpt, wk))

    mgr = _mgr()
    # buffer 空（没 buffer_message）→ flush 空分支
    _run(mgr.flush_buffer(wk))

    # 残留消息应被 replay 落盘，而非被删丢
    t = _run(mgr.load(wk))
    assert any(m.get("content") == "残留待救" for m in t["messages"]), \
        "flush 空分支必须先 replay 残留 WAL，绝不无脑删"
    # replay 后 WAL 清理
    assert not os.path.exists(wal_file_path(tmp_ckpt, wk))


def test_flush_empty_no_wal_clean_noop(tmp_ckpt):
    """flush 空 buffer 且无 WAL：正常清理认领，不报错。"""
    wk = "GroupMessage:B302"
    mgr = _mgr()
    _run(mgr.flush_buffer(wk))  # 空 buffer 空 WAL
    assert not os.path.exists(wal_file_path(tmp_ckpt, wk))
    assert wk not in mgr._wal_owned


# ============================================================
# ④ flush 并发不丢（pop 进锁内 + leftover 重写 WAL）
# ============================================================
def test_flush_concurrent_buffer_message_no_wal_loss(tmp_ckpt):
    """flush 持锁 await 期间并发 buffer_message 写新消息 → 新消息的 WAL 兜底不被 wal_clear 丢。

    手段：monkeypatch mgr.save 在落盘时（flush 持锁的 await 点）注入一条新 buffer_message，
    模拟无锁同步热路径在 flush 让出时插入。验证 flush 结束后新消息仍在 buffer 且有 WAL。"""
    wk = "GroupMessage:B401"
    mgr = _mgr()
    mgr.buffer_message(wk, {"role": "user", "content": "首批", "message_id": "f1",
                            "receive_seq": _next_rseq()})

    orig_save = mgr.save
    injected = {"done": False}

    async def _save_with_injection(window_key, t_file):
        # 仅在【真正落盘 pending（t_file 已含"首批"）】那次 save 注入，模拟 flush 落盘
        # 让出事件循环时无锁同步热路径 buffer_message 插入新消息（更贴近真实并发时序，
        # 而非 load 阶段创建空文件的 save）。
        _has_first = any(
            m.get("content") == "首批" for m in t_file.get("messages", [])
        )
        if window_key == wk and _has_first and not injected["done"]:
            injected["done"] = True
            mgr.buffer_message(wk, {"role": "user", "content": "并发插入",
                                    "message_id": "c1", "receive_seq": _next_rseq()})
        return await orig_save(window_key, t_file)

    mgr.save = _save_with_injection
    _run(mgr.flush_buffer(wk))
    mgr.save = orig_save

    # 首批已落盘
    t = _run(mgr.load(wk))
    assert any(m.get("content") == "首批" for m in t["messages"])
    # 并发插入的消息：仍在 buffer（未落盘），且 WAL 兜底未丢
    assert any(m.get("content") == "并发插入" for m in mgr._msg_buffer.get(wk, [])), \
        "并发插入的消息应仍在 buffer 等下次 flush"
    wal_entries = wal_read(tmp_ckpt, wk)
    wal_contents = [e["msg"]["content"] for e in wal_entries]
    assert "并发插入" in wal_contents, "并发插入消息的 WAL 兜底不应被 wal_clear 丢（leftover 重写）"
    assert "首批" not in wal_contents, "已落盘的首批不应再留 WAL"

    # 下次 flush：并发插入的消息落盘，无重复
    _run(mgr.flush_buffer(wk))
    t2 = _run(mgr.load(wk))
    c = [m.get("content") for m in t2["messages"]]
    assert c.count("并发插入") == 1 and c.count("首批") == 1


def test_flush_pop_inside_lock_no_double_flush(tmp_ckpt):
    """pop 进锁内：两个并发 flush 不重复落盘同一批 pending。"""
    wk = "GroupMessage:B402"
    mgr = _mgr()
    for i in range(3):
        mgr.buffer_message(wk, {"role": "user", "content": f"m{i}", "message_id": f"id{i}",
                                "receive_seq": _next_rseq()})

    async def _two_flushes():
        await asyncio.gather(mgr.flush_buffer(wk), mgr.flush_buffer(wk))

    _run(_two_flushes())
    t = _run(mgr.load(wk))
    for i in range(3):
        cnt = sum(1 for m in t["messages"] if m.get("content") == f"m{i}")
        assert cnt == 1, f"m{i} 应只落盘一次（pop 进锁内防双 flush 重复）"


# ============================================================
# ⑤ T 损坏从 state + .corrupt 恢复 next_round_id 不重号
# ============================================================
def test_corrupt_rebuild_recovers_number_source_from_state(tmp_ckpt):
    """T 文件损坏重建：从 state.json current_round_id + .corrupt messages 恢复号源，绝不重号。"""
    wk = "GroupMessage:B501"
    mgr = _mgr()
    # 正常落盘若干，把号源推高（next_round_id 推进到 >1）
    _run(mgr.append_messages(wk, [
        {"role": "user", "content": "u1", "message_id": "a", "receive_seq": _next_rseq()},
        {"role": "assistant", "content": "a1", "message_id": None, "receive_seq": _next_rseq()},
        {"role": "user", "content": "u2", "message_id": "b", "receive_seq": _next_rseq()},
        {"role": "assistant", "content": "a2", "message_id": None, "receive_seq": _next_rseq()},
    ]))
    fp = mgr._file_path(wk)
    with open(fp, "r", encoding="utf-8") as f:
        tf = json.load(f)
    used_next_round = tf["metadata"]["next_round_id"]
    used_next_step = tf["metadata"]["next_step_id"]
    assert used_next_round > 1 and used_next_step > 1

    # state.json 此刻含 current_round_id（如 r000002），存活
    st = round_tracker.load_state(tmp_ckpt, wk)
    assert round_tracker.parse_round_id(st.get("current_round_id")) >= 1

    # 损坏 T 文件（写入非法 JSON）
    with open(fp, "w", encoding="utf-8") as f:
        f.write("{ this is corrupt json ]]]")

    # 新实例 load → 损坏兜底重建 + 号源恢复
    mgr2 = _mgr()
    t = _run(mgr2.load(wk))
    new_next_round = t["metadata"]["next_round_id"]
    new_next_step = t["metadata"]["next_step_id"]
    # 号源绝不回退到 1（否则重号）；应 >= 损坏前已用到的水位
    assert new_next_round >= used_next_round, \
        f"损坏重建 next_round_id 不应回退：重建={new_next_round} 损坏前={used_next_round}"
    assert new_next_step >= used_next_step, \
        f"损坏重建 next_step_id 不应回退：重建={new_next_step} 损坏前={used_next_step}"

    # 重建后新 append 的 round_id 绝不复用历史轮号
    t2 = _run(mgr2.append_messages(wk, [
        {"role": "user", "content": "崩后新消息", "message_id": "z", "receive_seq": _next_rseq()},
    ]))
    new_msg = [m for m in t2["messages"] if m.get("message_id") == "z"][0]
    new_rid_int = round_tracker.parse_round_id(new_msg["round_id"])
    assert new_rid_int >= used_next_round, \
        f"崩后新轮号 {new_msg['round_id']} 应 >= 历史水位 r{used_next_round:06d}，不复用"


def test_corrupt_rebuild_no_state_uses_corrupt_messages(tmp_ckpt):
    """无 state.json 时，损坏重建从【现存 .corrupt 文件】messages 的 max round_id/step_id 恢复号源。

    模拟：上次崩溃已留下一个 .corrupt 文件（带高号历史），本次 T 文件又损坏 → load 把
    当前损坏文件 rename 成新 .corrupt（无号），helper 扫【所有】.corrupt 取已见 max+1。"""
    wk = "GroupMessage:B502"
    fp = os.path.join(tmp_ckpt, wk.replace(":", "_") + ".json")
    # 先放一个【现存的】.corrupt 文件，带高轮号/步号历史（模拟之前崩溃留存）
    historic = {
        "version": 2,
        "messages": [
            {"role": "user", "content": "hi", "round_id": "r000007",
             "step_id": "s00000015", "receive_seq": _next_rseq()},
        ],
        "metadata": {"next_round_id": 8, "next_step_id": 16},
    }
    with open(f"{fp}.corrupt.1699999999", "w", encoding="utf-8") as f:
        json.dump(historic, f, ensure_ascii=False)
    # 删 state.json（确保无 state 来源，逼 helper 只能靠 .corrupt）
    sp = round_tracker.state_file_path(tmp_ckpt, wk)
    if os.path.exists(sp):
        os.remove(sp)
    # 当前 T 文件损坏
    with open(fp, "w", encoding="utf-8") as f:
        f.write("not json at all <<<")

    mgr = _mgr()
    t = _run(mgr.load(wk))
    # 从现存 .corrupt 的 max round_id=7/step=15（及 metadata next 8/16）恢复 → next>=8/16
    assert t["metadata"]["next_round_id"] >= 8, \
        f"应从 .corrupt 恢复 next_round_id>=8，实际 {t['metadata']['next_round_id']}"
    assert t["metadata"]["next_step_id"] >= 16, \
        f"应从 .corrupt 恢复 next_step_id>=16，实际 {t['metadata']['next_step_id']}"


def test_corrupt_rebuild_helper_unit(tmp_ckpt):
    """单元：_recover_number_source_after_corruption 取 state/.corrupt/下限 三者 max+1。"""
    wk = "GroupMessage:B503"
    fp = os.path.join(tmp_ckpt, wk.replace(":", "_") + ".json")
    # state 含 current_round_id=r000010
    st = round_tracker.new_state()
    st["current_round_id"] = "r000010"
    round_tracker.save_state(tmp_ckpt, wk, st)
    # .corrupt 文件含 max round_id=r000005/step s00000020
    corrupt = {
        "messages": [
            {"round_id": "r000005", "step_id": "s00000020"},
            {"round_id": "r000003", "step_id": "s00000009"},
        ],
        "metadata": {"next_round_id": 4, "next_step_id": 18},
    }
    with open(f"{fp}.corrupt.1700000000", "w", encoding="utf-8") as f:
        json.dump(corrupt, f, ensure_ascii=False)

    nr, ns = checkpoint._recover_number_source_after_corruption(tmp_ckpt, wk, fp)
    # round：max(state 10, corrupt msg 5, corrupt meta next-1=3) + 1 = 11
    assert nr == 11, f"next_round_id 应为 11（state r000010 主导），实际 {nr}"
    # step：max(corrupt msg 20, corrupt meta next-1=17) + 1 = 21
    assert ns == 21, f"next_step_id 应为 21（corrupt msg s00000020 主导），实际 {ns}"


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
