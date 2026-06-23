"""
S4 record 降档摘要召回指针单测（D7 不自知缺口修复）
====================================================
背景：真机实测 D7 降档把老话题压成 summary/brief 注入后，主模型不知道这是压缩摘要、
误以为是完整逐字记忆，被要求逐字复述时凭摘要 + 人设幻觉编造冒充原文。本补丁在 record
概要块开头统一加召回指针（T1_RECORD_RECALL_HINT），并给降档组 head 加「（摘要）」标记。

覆盖验收点：
  ① 概要块开头含召回指针（T1_RECORD_RECALL_HINT），位置在 T1_SUMMARY_PREFIX 之后、
     分级 block 之前。
  ② 召回指针措辞含关键约束词：QQ_data_original / 逐字原文 / 严禁……编造。
  ③ 降档组（summary/brief）head 含「（摘要）」标记；full 组不含。
  ④ record full 档措辞区分「record 聚合记录 ↔ messages 逐字原文」（指针提到 full 也非逐字）。

跑法（PowerShell）：
  AstrBot/.venv/Scripts/python.exe -m pytest test_record_s4_recall_hint.py -q
"""
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
import round_tracker  # noqa: E402
from checkpoint import (  # noqa: E402
    TFileManager,
    T1_SUMMARY_PREFIX,
    T1_RECORD_RECALL_HINT,
)

WK = "GroupMessage:<GID>"


@pytest.fixture()
def tmp_ckpt(monkeypatch):
    d = tempfile.mkdtemp(prefix="s4_recall_test_")
    monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", d)
    yield d


@pytest.fixture()
def mgr():
    return TFileManager()


def _msg(role, content, round_n=None, **extra):
    m = {"role": role}
    if content is not None:
        m["content"] = content
    if round_n is not None:
        m["round_id"] = round_tracker.format_round_id(round_n)
    m.update(extra)
    return m


def _group(rg_num, s, e, *, tier="full", full_text="", summary_text="",
           brief_text="", sealed=True, legacy=False, title=""):
    return {
        "rg_id": record.format_rg_id(rg_num),
        "round_range": [s, e],
        "tier": tier,
        "sealed": sealed,
        "legacy_rg": legacy,
        "full_text": full_text,
        "summary_text": summary_text,
        "brief_text": brief_text,
        "title": title,
    }


def _t_file_with_record(groups, messages):
    return {
        "version": 2,
        "T1": {},
        "messages": messages,
        "metadata": {
            "generation": 5,
            # next_round_id 拉大，配合组 round_range 制造高轮龄 → 触发 D7 降档。
            "next_round_id": 500,
            "record_state": {
                "round_groups": groups,
                "last_grouped_rg_id": groups[-1]["rg_id"] if groups else None,
            },
        },
    }


# ============================================================
# ① 概要块开头含召回指针（任意档都注入；与位置）
# ============================================================
def test_recall_hint_present_in_summary_block(tmp_ckpt, mgr):
    """full 组也注入召回指针（指针为块级总说明，不依赖是否降档）。"""
    groups = [
        _group(1, 1, 3, tier="full",
               full_text="user: 早期A\nassistant: 回复A", title="主题A"),
    ]
    messages = [
        _msg("user", "早期A", 1), _msg("assistant", "回复A", 1),
        _msg("user", "尾部B", 490),
    ]
    t_file = _t_file_with_record(groups, messages)
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)

    head = ctx[0]["content"]
    assert head.startswith(T1_SUMMARY_PREFIX)
    # 召回指针在 prefix 之后、组文本之前
    assert T1_RECORD_RECALL_HINT in head
    assert head.index(T1_RECORD_RECALL_HINT) < head.index("主题A")
    assert head.index(T1_SUMMARY_PREFIX) < head.index(T1_RECORD_RECALL_HINT)


# ============================================================
# ② 召回指针措辞含关键约束词
# ============================================================
def test_recall_hint_wording():
    """指针必须点名工具、区分逐字原文、明令严禁编造。"""
    assert "QQ_data_original" in T1_RECORD_RECALL_HINT
    assert "逐字原文" in T1_RECORD_RECALL_HINT
    assert "严禁" in T1_RECORD_RECALL_HINT and "编造" in T1_RECORD_RECALL_HINT
    # 措辞精准区分：full 是聚合记录、summary/brief 是摘要，均非逐字原文
    assert "summary/brief" in T1_RECORD_RECALL_HINT
    assert "非逐字原文" in T1_RECORD_RECALL_HINT


# ============================================================
# ③ 降档组 head 标「（摘要）」；full 组不标
# ============================================================
def test_downgraded_group_head_marked(tmp_ckpt, mgr):
    """高轮龄组被 D7 降到 summary → head 含「（摘要）」；同时给 full 组验证不标。

    用两组：一组高轮龄（降 summary），一组贴近水位（留 full）。
    """
    # 组2 round_range 紧贴 now_round(=499) → age 小 → 留 full
    # 组1 round_range 远小于 now_round → age 大 → 降 summary
    groups = [
        _group(1, 1, 3, tier="full",
               full_text="user: 老话题全文\nassistant: 老回复全文",
               summary_text="老话题摘要XYZ", title="老主题"),
        _group(2, 495, 497, tier="full",
               full_text="user: 近话题全文\nassistant: 近回复全文",
               summary_text="近话题摘要", title="近主题"),
    ]
    messages = [
        _msg("user", "老话题全文", 1), _msg("assistant", "老回复全文", 1),
        _msg("user", "近话题全文", 495), _msg("assistant", "近回复全文", 495),
        _msg("user", "尾部最新", 499),
    ]
    t_file = _t_file_with_record(groups, messages)
    ctx = mgr.build_llm_contexts(t_file, window_key=WK)

    head = ctx[0]["content"]
    # 组1 降到 summary：注入摘要文本 + head 标「（摘要）」
    assert "老话题摘要XYZ" in head
    assert "summary" in head and "（摘要）" in head
    # 组2 留 full：注入全文 + head 不带「（摘要）」（该组行是 full）
    assert "近话题全文" in head
    # 校验「（摘要）」只贴在 summary/brief 行：full 行不应带该标记
    for line in head.splitlines():
        if line.startswith("[") and "full]" in line:
            assert "（摘要）" not in line, f"full 组 head 误标摘要: {line}"
