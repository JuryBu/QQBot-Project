"""
S4 批2b 单测：R2 退役旧 T1 全量压缩 + compose_record 真接线
============================================================
覆盖 5 个验收点（依据 QQBotPlan/Plan_5/S4_实现方案.md §二 R2/R4 + S4_设计决策.md D5/D6）：
  ① R2 迁移——现存 T1（compressed_summary blob）→ 第 0 号 legacy_rg 组（sealed，不再压）
  ② 迁移幂等——已迁移（有 legacy_rg 组）不重复迁移、现存 T1 不丢不重压
  ③ 两阶段提交 generation 校验——压缩期并发推进磁盘号源，提交后号源/generation 不回退
  ④ 接力中止判据——连续 compose 后 token 降量 < compress_delta_floor 即停（非「仍超阈值」）
  ⑤ LLM 真接线 JSON 解析——mock provider 响应（裸 JSON / ```json 围栏 / 前后赘述 /
     尾逗号 / 坏 JSON）跨 provider 健壮解析 + 容错

LLM 全部用 mock（async fake _call_flash_lite 返回构造的 provider 文本）。

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_batch2b.py -q
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


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s4_b2b_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


def _fp(d, wk):
    return os.path.join(d, wk.replace(":", "_") + ".json")


# ============================================================
# ① R2 迁移：现存 T1 → legacy_rg 组
# ============================================================
def test_r2_migrate_legacy_t1_basic():
    """有 T1.compressed_summary → 封成 rg000000 legacy_rg 组（sealed，summary_text=摘要）。"""
    t_file = {
        "version": 2,
        "T1": {"compressed_summary": "这是旧的历史压缩摘要文本。", "token_count": 123},
        "messages": [],
        "metadata": {"record_state": {"last_compressed_round_id": None}},
    }
    changed, t2 = TFileManager._migrate_legacy_t1_to_record_group(t_file)
    assert changed is True
    rgs = t2["metadata"]["record_state"]["round_groups"]
    assert len(rgs) == 1
    g = rgs[0]
    assert g["rg_id"] == "rg000000"
    assert g["legacy_rg"] is True
    assert g["sealed"] is True
    assert g["summary_text"] == "这是旧的历史压缩摘要文本。"
    assert g["full_text"] == "这是旧的历史压缩摘要文本。"  # 无更细 full 回退用摘要
    assert g["round_range"] == [0, 0]  # last_compressed=None → 终点 0
    assert g["token_est"] == 123
    # 聚合锚指向 legacy 组
    assert t2["metadata"]["record_state"]["last_grouped_rg_id"] == "rg000000"
    # T1 本体不动（build_llm_contexts 仍读，兼容）
    assert t_file["T1"]["compressed_summary"] == "这是旧的历史压缩摘要文本。"


def test_r2_migrate_legacy_range_uses_lcr():
    """有 last_compressed_round_id → legacy 组终点取其数值（[0, N]）。"""
    t_file = {
        "T1": {"compressed_summary": "摘要", "token_count": 0},
        "messages": [],
        "metadata": {"record_state": {"last_compressed_round_id": "r000042"}},
    }
    changed, t2 = TFileManager._migrate_legacy_t1_to_record_group(t_file)
    assert changed is True
    assert t2["metadata"]["record_state"]["round_groups"][0]["round_range"] == [0, 42]


def test_r2_migrate_empty_t1_noop():
    """无 T1 摘要 → 不迁移（首次 compose 直接从 r1 起，不建空 legacy 组）。"""
    t_file = {
        "T1": {"compressed_summary": "", "token_count": 0},
        "messages": [],
        "metadata": {"record_state": {}},
    }
    changed, t2 = TFileManager._migrate_legacy_t1_to_record_group(t_file)
    assert changed is False
    assert t2["metadata"]["record_state"].get("round_groups") in (None, [])


# ============================================================
# ② 迁移幂等：已迁移不重复、现存 T1 不丢不重压
# ============================================================
def test_r2_migrate_idempotent():
    """连调两次迁移 → 第二次 changed=False，legacy 组不重复、不重压。"""
    t_file = {
        "T1": {"compressed_summary": "历史摘要", "token_count": 50},
        "messages": [],
        "metadata": {"record_state": {"last_compressed_round_id": None}},
    }
    changed1, t_file = TFileManager._migrate_legacy_t1_to_record_group(t_file)
    assert changed1 is True
    rgs1 = list(t_file["metadata"]["record_state"]["round_groups"])

    changed2, t_file = TFileManager._migrate_legacy_t1_to_record_group(t_file)
    assert changed2 is False  # 已迁移 → 不重复
    rgs2 = t_file["metadata"]["record_state"]["round_groups"]
    # legacy 组仅 1 个，内容不变（不丢不重压）
    assert len(rgs2) == 1
    assert rgs2 == rgs1
    assert rgs2[0]["summary_text"] == "历史摘要"


def test_r2_migrate_idempotent_with_existing_real_groups():
    """已有 legacy 组 + 后续真组 → 再迁移不动任何组（幂等屏障认 legacy_rg）。"""
    t_file = {
        "T1": {"compressed_summary": "摘要", "token_count": 0},
        "messages": [],
        "metadata": {"record_state": {
            "round_groups": [
                {"rg_id": "rg000000", "round_range": [0, 0], "legacy_rg": True,
                 "sealed": True, "summary_text": "已存在的 legacy"},
                {"rg_id": "rg000001", "round_range": [1, 8], "sealed": True},
            ],
            "last_grouped_rg_id": "rg000001",
        }},
    }
    changed, t2 = TFileManager._migrate_legacy_t1_to_record_group(t_file)
    assert changed is False
    rgs = t2["metadata"]["record_state"]["round_groups"]
    assert len(rgs) == 2
    assert rgs[0]["summary_text"] == "已存在的 legacy"  # 旧 legacy 不被覆盖


# ============================================================
# ③ 两阶段提交 generation 校验（号源不回退）
# ============================================================
def _seed_t_file_with_rounds(fp, *, n_rounds=14, gen=7, next_rid=30, next_sid=60):
    """构造带真轮号的 T 文件（够 token 触发 record 聚合）。"""
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
        "window_key": "GroupMessage:2b001",
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
                "round_groups": [],
                "hit_table": {},
            },
        },
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(t_file, f, ensure_ascii=False)


def _json_caller_text(batch_rounds, group_size=8):
    """按 group_size 把一批轮切组，产出合法 JSON 数组文本（模拟 provider 裸 JSON）。"""
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


def test_two_phase_commit_no_generation_regression(tmp_ckpt):
    """compose 期间并发推进【磁盘】generation/号源，提交后磁盘必须 >= 推进值（不回退）。"""
    mgr = TFileManager()
    wk = "GroupMessage:2b001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, gen=7, next_rid=30, next_sid=60)

    bumped = {"done": False}

    async def fake_call_flash_lite(prompt, max_output_tokens=4096, window_key="x"):
        # 分段调用瞬间，模拟并发 append 推进【磁盘】号源/generation
        if not bumped["done"]:
            disk = json.load(open(fp, encoding="utf-8"))
            disk["metadata"]["next_round_id"] = 99
            disk["metadata"]["next_step_id"] = 199
            disk["metadata"]["generation"] = 12
            disk["metadata"]["total_messages_ever"] = 999
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(disk, f, ensure_ascii=False)
            bumped["done"] = True
        # 解析 prompt 里的 [round N] 行，回 JSON 分段
        import re
        rounds = [{"round_int": int(m)} for m in re.findall(r"\[round (\d+)\]", prompt)]
        return _json_caller_text(rounds, group_size=8)

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call_flash_lite,
            cfg={"record_compose_token_limit": 100, "compress_delta_floor": 200},
        )

    t_file, result = asyncio.run(run())
    assert result is not None, "record 聚合应被触发（token 超 100）"

    disk = json.load(open(fp, encoding="utf-8"))
    md = disk["metadata"]
    # 号源/generation 只进不退（批3.5-A 教训：陈旧快照绝不覆盖磁盘号源）
    assert md["next_round_id"] >= 99, f"next_round_id 回退！{md['next_round_id']}"
    assert md["next_step_id"] >= 199, f"next_step_id 回退！{md['next_step_id']}"
    assert md["generation"] >= 12, f"generation 回退！{md['generation']}"
    assert md["total_messages_ever"] >= 999, "total_messages_ever 回退！"
    # record 聚合产物落盘：round_groups 非空 + 锚点推进
    rgs = md["record_state"]["round_groups"]
    assert len(rgs) >= 1
    assert md["record_state"]["last_grouped_rg_id"] is not None
    # last_compressed_round_id 单调推进到已聚合最大轮终点
    assert md["record_state"]["last_compressed_round_id"] is not None


def test_two_phase_commit_guard_discards_stale_candidate(tmp_ckpt):
    """A① 单调守卫：compose 期间磁盘 round_groups 被并发推得更超前 → 候选作废、
    绝不用陈旧候选覆盖磁盘更超前的边界表（防 round_groups 整份替换吞并发结果）。"""
    mgr = TFileManager()
    wk = "GroupMessage:2b001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, n_rounds=10)

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        # 分段瞬间，模拟并发 compose 已把磁盘 round_groups 聚合到 r000015（远超本候选
        # 将产出的最大终点 r000010），制造「磁盘更超前」(disk_end=15 > cand_end=10)。
        disk = json.load(open(fp, encoding="utf-8"))
        disk["metadata"]["record_state"]["round_groups"] = [
            {"rg_id": "rg000099", "round_range": [1, 15], "tier": "summary",
             "sealed": True, "legacy_rg": False, "full_text": "并发已聚合更超前"},
        ]
        disk["metadata"]["record_state"]["last_grouped_rg_id"] = "rg000099"
        disk["metadata"]["record_state"]["last_compressed_round_id"] = "r000015"
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(disk, f, ensure_ascii=False)
        import re
        rounds = [{"round_int": int(m)} for m in re.findall(r"\[round (\d+)\]", prompt)]
        return _json_caller_text(rounds, group_size=8)

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={"record_compose_token_limit": 100, "compress_delta_floor": 200},
        )

    t_file, result = asyncio.run(run())
    # 候选作废（无提交）→ result is None；磁盘超前边界表完整保留、绝不被陈旧候选覆盖
    assert result is None, "磁盘更超前时候选应作废"
    disk = json.load(open(fp, encoding="utf-8"))
    rgs = disk["metadata"]["record_state"]["round_groups"]
    assert len(rgs) == 1 and rgs[0]["rg_id"] == "rg000099", "磁盘超前边界表被覆盖！"
    assert disk["metadata"]["record_state"]["last_compressed_round_id"] == "r000015"


def test_two_phase_commit_messages_not_truncated(tmp_ckpt):
    """R2 安全中间态：record 聚合【绝不裁 messages 原文】（D1 铁律，批3 R3 才裁）。"""
    mgr = TFileManager()
    wk = "GroupMessage:2b001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, n_rounds=14)
    before = len(json.load(open(fp, encoding="utf-8"))["messages"])

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        import re
        rounds = [{"round_int": int(m)} for m in re.findall(r"\[round (\d+)\]", prompt)]
        return _json_caller_text(rounds, group_size=8)

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={"record_compose_token_limit": 100, "compress_delta_floor": 200},
        )

    asyncio.run(run())
    after = len(json.load(open(fp, encoding="utf-8"))["messages"])
    assert after == before, "批2b record 路径绝不裁 messages（裁=丢轮违反 D1）"


# ============================================================
# ④ 接力中止判据：token 降量 < floor 即停
# ============================================================
def test_relay_stops_on_insufficient_delta(tmp_ckpt):
    """批2b 不裁 messages → token 不降（delta=0 < floor）→ 接力跑 1 轮即停（防死循环）。

    判据是『token 降量不足』而非『仍超阈值』：即便聚合后请求体仍超阈值，只要降量
    低于 compress_delta_floor 就中止，绝不无限接力（防巨图死循环）。
    """
    mgr = TFileManager()
    wk = "GroupMessage:2b001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, n_rounds=14)

    call_count = {"n": 0}

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        call_count["n"] += 1
        import re
        rounds = [{"round_int": int(m)} for m in re.findall(r"\[round (\d+)\]", prompt)]
        return _json_caller_text(rounds, group_size=8)

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={
                "record_compose_token_limit": 100,  # 远低于实际 → 触发
                "compress_delta_floor": 200,        # 降量门槛
                "record_max_relay_rounds": 5,       # 允许最多 5 次接力
            },
        )

    t_file, result = asyncio.run(run())
    assert result is not None
    # 批2b 不裁 messages → build_llm_contexts token 不降 → 接力第 1 轮后 delta≈0<200 停。
    # relay 应为 1（跑了 1 次就因降量不足中止），绝不耗到 max_relay=5。
    assert result["relay_rounds"] == 1, f"接力应在降量不足时停，得 {result['relay_rounds']}"


def test_relay_respects_max_rounds_cap(tmp_ckpt):
    """次数兜底：即便降量持续达标，接力也不超过 record_max_relay_rounds（防死循环）。"""
    # 用 floor=-1 强制「降量永远达标」（delta>=0>-1），验次数硬上限生效。
    mgr = TFileManager()
    wk = "GroupMessage:2b001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, n_rounds=40)

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        import re
        rounds = [{"round_int": int(m)} for m in re.findall(r"\[round (\d+)\]", prompt)]
        return _json_caller_text(rounds, group_size=8)

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={
                "record_compose_token_limit": 100,
                "compress_delta_floor": -1,    # 降量永远达标 → 只剩次数兜底
                "record_max_relay_rounds": 2,
            },
        )

    t_file, result = asyncio.run(run())
    assert result is not None
    assert result["relay_rounds"] <= 2, f"接力超次数上限！{result['relay_rounds']}"


def test_no_trigger_below_threshold(tmp_ckpt):
    """请求体 token 未超阈值 → 不触发 record 聚合（result=None）。"""
    mgr = TFileManager()
    wk = "GroupMessage:2b001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, n_rounds=2)  # 很小

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        raise AssertionError("不应被调用（未触发）")

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={"record_compose_token_limit": 10_000_000},  # 超高阈值
        )

    t_file, result = asyncio.run(run())
    assert result is None


# ============================================================
# ⑤ LLM 真接线 JSON 解析（跨 provider 健壮 + 容错）
# ============================================================
def _rv(rn, text="对话内容"):
    return {"round_int": rn, "round_id": f"r{rn:06d}", "text": text,
            "char_len": len(text), "token_est": len(text) // 2}


def test_parse_bare_json_array():
    """裸 JSON 数组 → 正常解析。"""
    raw = ('[{"round_start":1,"round_end":3,"title":"t","full_text":"f",'
           '"summary_text":"s"}]')
    specs = record.parse_group_specs(raw, [_rv(1), _rv(2), _rv(3)])
    assert len(specs) == 1
    assert specs[0]["round_start"] == 1 and specs[0]["round_end"] == 3
    assert specs[0]["full_text"] == "f" and specs[0]["summary_text"] == "s"


def test_parse_json_fenced():
    """```json 围栏包裹 → 剥围栏后解析。"""
    raw = ("好的，分段结果如下：\n```json\n"
           '[{"round_start":1,"round_end":2,"title":"x","full_text":"a","summary_text":"b"}]'
           "\n```\n以上。")
    specs = record.parse_group_specs(raw, [_rv(1), _rv(2)])
    assert len(specs) == 1
    assert specs[0]["round_end"] == 2


def test_parse_json_with_prose_around():
    """前后赘述 + 裸数组（无围栏）→ 定位 [..] 解析。"""
    raw = ('分段如下 [{"round_start":5,"round_end":7,"full_text":"ff","summary_text":"ss"}] 完毕')
    specs = record.parse_group_specs(raw, [_rv(5), _rv(6), _rv(7)])
    assert len(specs) == 1
    assert specs[0]["round_start"] == 5


def test_parse_trailing_comma_tolerated():
    """尾逗号（常见 LLM 漂移）→ 容错修复后解析。"""
    raw = '[{"round_start":1,"round_end":2,"full_text":"a","summary_text":"b",},]'
    specs = record.parse_group_specs(raw, [_rv(1), _rv(2)])
    assert len(specs) == 1


def test_parse_single_object_wrapped():
    """provider 只回单对象（非数组）→ 包成单组。"""
    raw = '{"round_start":1,"round_end":1,"full_text":"x","summary_text":"y"}'
    specs = record.parse_group_specs(raw, [_rv(1)])
    assert len(specs) == 1
    assert specs[0]["round_start"] == 1


def test_parse_groups_key_wrapper():
    """provider 回 {"groups":[...]} 包裹 → 取 groups。"""
    raw = '{"groups":[{"round_start":2,"round_end":4,"full_text":"a","summary_text":"b"}]}'
    specs = record.parse_group_specs(raw, [_rv(2), _rv(3), _rv(4)])
    assert len(specs) == 1
    assert specs[0]["round_end"] == 4


def test_parse_garbage_returns_empty():
    """完全无法解析（非 JSON）→ 返回 []（触发 compose 兜底，不写盘）。"""
    assert record.parse_group_specs("这就是一段普通中文，没有 JSON。", [_rv(1)]) == []
    assert record.parse_group_specs("", [_rv(1)]) == []
    assert record.parse_group_specs("```json\n{坏掉的不是json\n```", [_rv(1)]) == []


def test_parse_missing_round_fields_skipped():
    """单项缺 round_start/end → 跳过该项，其余保留。"""
    raw = ('[{"title":"无区间"},'
           '{"round_start":3,"round_end":5,"full_text":"f","summary_text":"s"}]')
    specs = record.parse_group_specs(raw, [_rv(3), _rv(4), _rv(5)])
    assert len(specs) == 1
    assert specs[0]["round_start"] == 3


def test_build_segment_prompt_deterministic():
    """build_segment_prompt 确定性：同输入同 prompt，含每轮 [round N] 行 + 硬规则。"""
    rounds = [_rv(1, "甲说话"), _rv(2, "乙说话")]
    p1 = record.build_segment_prompt(rounds, {})
    p2 = record.build_segment_prompt(rounds, {})
    assert p1 == p2
    assert "[round 1] " in p1 and "[round 2] " in p1
    assert "round_start" in p1 and "JSON" in p1  # 含输出格式约束


def test_build_segment_prompt_caps_long_text():
    """单轮超长文本按 cap 截断（防 prompt 撑爆）。"""
    long_text = "x" * 9000
    p = record.build_segment_prompt([_rv(1, long_text)], {"rg_round_text_cap": 100})
    assert "…(截断)" in p
    assert len(p) < 2000  # 已截断


def test_parse_end_to_end_via_compose(tmp_ckpt):
    """端到端：真 caller 包装（build_segment_prompt → mock provider JSON → parse）
    经 compose_record 产出正确 round_groups。"""
    mgr = TFileManager()
    wk = "GroupMessage:2b001"
    fp = _fp(tmp_ckpt, wk)
    _seed_t_file_with_rounds(fp, n_rounds=16)

    seen_prompts = []

    async def fake_call(prompt, max_output_tokens=4096, window_key="x"):
        seen_prompts.append(prompt)
        import re
        rounds = [{"round_int": int(m)} for m in re.findall(r"\[round (\d+)\]", prompt)]
        # 模拟 provider 用围栏包裹（验真接线容错）
        return "```json\n" + _json_caller_text(rounds, group_size=8) + "\n```"

    async def run():
        snap = await mgr.load(wk)
        return await mgr.compose_record_if_needed(
            wk, snap, fake_call,
            cfg={"record_compose_token_limit": 100, "compress_delta_floor": 200},
        )

    t_file, result = asyncio.run(run())
    assert result is not None
    # caller 收到的是分段 prompt（含 round 行），非旧压缩 prompt
    assert seen_prompts and "[round 1]" in seen_prompts[0]
    rgs = t_file["metadata"]["record_state"]["round_groups"]
    # 16 轮 / 每组 8 → 2 组
    assert len(rgs) == 2
    assert rgs[0]["round_range"] == [1, 8]
    assert rgs[1]["round_range"] == [9, 16]


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
