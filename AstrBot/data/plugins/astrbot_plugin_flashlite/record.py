"""
S4 record 机制 — record.py（M1 RecordStore 骨架 + M2 sidecar）
============================================================
record.md 读写 / 渲染 / 写入门禁 / record_index sidecar 权威映射。

设计依据：QQBotPlan/Plan_5/S4_实现方案.md §一 M1/M2 + §二 R1。
设计决策：
  - D1：record.md 是【派生物】，可重建。真理源 = metadata.record_state（边界表/锚点）
        + record_index sidecar（软轮号 rg_id ↔ 硬 round_id 权威映射）。record.md 坏了
        能从结构化 rg_index 经 render_record_md 重渲，从 record_state 经
        render_index_from_state 重建 sidecar。
  - D2：sidecar 是权威映射。定位/增量/hit 全靠它，不解析模型自由文本。

批1a【地基】（已完成）：
  - 读写（read_record / write_record_atomic 候选隔离）
  - 渲染（render_record_md）
  - 门禁（validate_composed_record）
  - sidecar（load_index / save_index / rebuild_index_if_stale）

批2a【确定性聚合】（本批新增）：
  - compose_record（D3 Local Compose 增量聚合：代码确定性回滚最后 N 组划重写窗口，
    模型只在窗口内重新分段，**绝不逐轮问模型**——照 mcp selectLocalComposeBoundary）
  - force_seal_check（D3 强制收敛：组达 rg_force_seal_rounds(15)/token/age 任一 →
    强制封档，无视「话题未结」开放信号，防群聊反复重压漂移）
  - 预算切批（M3：字符/token 预算硬约束切批 + rg_target_rounds 软目标；单轮超预算
    走 step 级切批 fallback，巨轮独占一批并强制单组封档防撑爆）
  - LLM 失败兜底：该批维持未分组态、回退 full 直读、带重试 + 冷却建议（不实际 sleep）

LLM caller 注入契约（mock 可替身，单测用确定性 mock）：
  llm_caller(batch_rounds: List[RoundView], cfg: dict) -> List[GroupSpec]
    RoundView : {round_int:int, round_id:str, text:str, char_len:int, token_est:int,
                 oversize:bool}
    GroupSpec : {round_start:int, round_end:int, full_text:str, summary_text:str,
                 title:str}（round_start/end 为 round_id 整数闭区间，必须落在本批轮内）
  约定：compose 先做预算硬切批，caller 只在「给定一批轮」内吐分段；caller 抛异常 /
  返回空 / 返回越界区间 → 触发失败兜底（不写盘、维持未分组态）。

本模块为**纯逻辑**（参照 round_tracker.py）：不依赖 astrbot.api，I/O 仅用标准库，
便于单测。日志走传入的 logger 或标准库 logging。复用 round_tracker.parse_round_id
比较轮号（rg_id/round_id 全限定字符串，零填充字典序==数值序，但统一走数值比较）。

文件命名（照 checkpoint.py / round_tracker.py 约定 window_key.replace(":", "_")）：
  - record.md    : {safe_name}.record.md
  - sidecar      : {safe_name}.record.index.json
  - 候选隔离写   : {safe_name}.record.md.tmp（校验通过才 os.replace 成正式文件）
"""

import hashlib
import json
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# 兼容包内导入与测试/直跑顶层导入（同 checkpoint.py 模式）。
try:
    from . import round_tracker
except ImportError:  # pragma: no cover - 直跑/单测路径
    import round_tracker

_DEFAULT_LOGGER = logging.getLogger("flashlite_record")


# ========================
# 路径辅助（照 checkpoint.wal_file_path / round_tracker.state_file_path 约定）
# ========================
def _safe_name(window_key: str) -> str:
    """window_key → 文件名安全片段（: → _），与 T 文件/state/WAL 同约定。"""
    return window_key.replace(":", "_")


def record_md_path(checkpoints_dir: str, window_key: str) -> str:
    """{window}.record.md 路径（与 T 文件 {window}.json 并列）。"""
    return os.path.join(checkpoints_dir, f"{_safe_name(window_key)}.record.md")


def record_index_path(checkpoints_dir: str, window_key: str) -> str:
    """{window}.record.index.json sidecar 路径。"""
    return os.path.join(checkpoints_dir, f"{_safe_name(window_key)}.record.index.json")


# ========================
# 通用原子写（照 round_tracker.save_state：mkstemp → flush+fsync → os.replace，失败清 tmp）
# ========================
def _atomic_write_text(path: str, text: str, *, prefix: str = ".record_") -> None:
    """原子写文本文件：临时文件 → flush+fsync → os.replace；失败清理 tmp。

    与 round_tracker.save_state / checkpoint.save 同模板——防 rename 已生效但内容
    仍在 OS page cache 未落盘时断电导致撕裂。
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


# ========================
# M2 record_index sidecar（D2 权威映射）
# ========================
# sidecar 结构：
#   {
#     "source_hash": str,        # 对应 record.md 内容的 hash（懒重建判定）
#     "generation": int,         # 与 T 文件 generation 对齐的交叉校验代号
#     "groups": [                # 软轮号 rg_id ↔ 硬 round_id 区间的权威映射
#       {
#         "rg_id": str,          # round-group 软轮号（如 "rg000001"）
#         "round_range": [s, e], # 硬 round_id 整数区间 [起, 止]（闭区间）
#         "char_offset": [s, e], # 该组正文在 record.md 中的字符偏移 [起, 止]
#         "tier": str,           # 当前定档（full/summary/brief）
#         "hit_count": int,      # 命中次数（M4 维护，地基只透传）
#         "sealed": bool,        # 是否已封档
#         "legacy_rg": bool,     # 是否第 0 号 legacy round-group（历史段冷冻）
#       }, ...
#     ]
#   }

def new_index(generation: int = 0) -> Dict[str, Any]:
    """创建一份空 sidecar。"""
    return {
        "source_hash": "",
        "generation": int(generation or 0),
        "groups": [],
    }


def compute_source_hash(record_md_text: str) -> str:
    """record.md 正文的内容 hash（sha256 hex）。空串也产稳定 hash。"""
    if record_md_text is None:
        record_md_text = ""
    return hashlib.sha256(record_md_text.encode("utf-8")).hexdigest()


def load_index(checkpoints_dir: str, window_key: str) -> Dict[str, Any]:
    """读取 sidecar；文件不存在或损坏时返回一份全新空 sidecar（不阻断主链路）。"""
    fp = record_index_path(checkpoints_dir, window_key)
    if not os.path.exists(fp):
        return new_index()
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return new_index()
        # 补齐缺失字段（向后兼容）
        base = new_index()
        for k in base:
            if k in data:
                base[k] = data[k]
        if not isinstance(base.get("groups"), list):
            base["groups"] = []
        return base
    except Exception:
        # 损坏 sidecar 不阻断；由 rebuild_index_if_stale 从 record_state 重建。
        return new_index()


def save_index(
    checkpoints_dir: str, window_key: str, index: Dict[str, Any]
) -> None:
    """原子保存 sidecar（照 round_tracker.save_state：mkstemp→fsync→os.replace）。"""
    fp = record_index_path(checkpoints_dir, window_key)
    text = json.dumps(index, ensure_ascii=False, indent=2)
    _atomic_write_text(fp, text, prefix=".record_index_")


def render_index_from_state(
    record_state: Dict[str, Any],
    *,
    generation: int = 0,
    source_hash: str = "",
) -> Dict[str, Any]:
    """D1：从 metadata.record_state（真理源）重建 sidecar groups。

    record_state.round_groups 是边界表（每项含 rg_id/round_range/tier/sealed/legacy_rg
    等），sidecar 是它的「带 char_offset 与 source_hash 的查询投影」。本批地基只做
    字段透传 + hit_table 注入 hit_count；char_offset 需由 record.md 渲染产物回填
    （render_record_md 提供），重建场景下缺省给 [0, 0]，由后续重渲补正。
    """
    idx = new_index(generation)
    idx["source_hash"] = source_hash or ""
    hit_table = record_state.get("hit_table") or {}
    groups_out: List[Dict[str, Any]] = []
    for g in record_state.get("round_groups") or []:
        if not isinstance(g, dict):
            continue
        rg_id = g.get("rg_id")
        rr = g.get("round_range") or [None, None]
        hit_entry = hit_table.get(rg_id) if isinstance(hit_table, dict) else None
        hit_count = 0
        if isinstance(hit_entry, dict):
            hit_count = int(hit_entry.get("hit_count", 0) or 0)
        groups_out.append({
            "rg_id": rg_id,
            "round_range": [rr[0], rr[1]] if len(rr) >= 2 else [None, None],
            "char_offset": list(g.get("char_offset", [0, 0]))[:2] or [0, 0],
            "tier": g.get("tier", "full"),
            "hit_count": hit_count,
            "sealed": bool(g.get("sealed", False)),
            "legacy_rg": bool(g.get("legacy_rg", False)),
        })
    idx["groups"] = groups_out
    return idx


def rebuild_index_if_stale(
    checkpoints_dir: str,
    window_key: str,
    record_state: Dict[str, Any],
    *,
    generation: int = 0,
    logger: Optional[logging.Logger] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """mcp sourceHash 懒重建：record.md 的 hash 变了（或 sidecar 缺失/损坏）就从
    metadata.record_state（真理源）重渲 sidecar 并落盘。

    返回 (rebuilt, index)：rebuilt=True 表示本次重建并写盘。

    判定：读 record.md 现内容算 hash，与 sidecar.source_hash 比对。不一致 → 陈旧，
    从 record_state 重建 groups + 写入新 source_hash。一致 → 直接返回现 sidecar。
    """
    log = logger or _DEFAULT_LOGGER
    md_text = read_record(checkpoints_dir, window_key)
    cur_hash = compute_source_hash(md_text)
    idx = load_index(checkpoints_dir, window_key)

    if idx.get("source_hash") == cur_hash and idx.get("groups"):
        return False, idx

    # 陈旧 / 缺失 / 损坏 → 从真理源重建
    new_idx = render_index_from_state(
        record_state, generation=generation, source_hash=cur_hash
    )
    try:
        save_index(checkpoints_dir, window_key, new_idx)
    except Exception as e:  # noqa: BLE001
        log.error(f"[RECORD] sidecar 重建落盘失败 {window_key}: {e}")
        return False, idx
    log.warning(
        f"[RECORD] sidecar 陈旧重建 {window_key}: "
        f"groups={len(new_idx['groups'])} hash={cur_hash[:8]}"
    )
    return True, new_idx


# ========================
# M1 渲染（D1 派生物）
# ========================
# tier 常量
TIER_FULL = "full"
TIER_SUMMARY = "summary"
TIER_BRIEF = "brief"

_RECORD_MD_HEADER = "# Conversation Record"


def render_record_md(
    rg_index: List[Dict[str, Any]],
    tier_map: Optional[Dict[str, str]] = None,
) -> str:
    """D1：从结构化 rg_index 按 tier 渲染 record.md 正文（派生物，可重建）。

    rg_index：有序的 round-group 列表，每项至少含：
      - rg_id          : str，软轮号
      - round_range    : [s, e]，硬 round_id 整数区间
      - tier           : str（可选，缺省 full），若 tier_map 提供则以 tier_map 覆盖
      - full_text      : str（可选），full 档原文块
      - summary_text   : str（可选），summary 档摘要块
      - brief_text     : str（可选），brief 档一句话
      - sealed         : bool（可选）
      - legacy_rg      : bool（可选）

    tier_map：{rg_id: tier} 覆盖表（分级读取层 M3 传入；本批可不传，用组内 tier）。

    渲染策略：每组一个 `## [rg_id] rounds s-e (tier)` 小标题 + 对应档正文。full
    取 full_text，summary 取 summary_text，brief 取 brief_text；缺对应档文本时
    回退到更高档已有文本（full>summary>brief），仍无则留空块。**确定性**：同输入
    必产同输出（无时间戳/随机），保证 D1 重渲可复现、source_hash 稳定。
    """
    tier_map = tier_map or {}
    lines: List[str] = [_RECORD_MD_HEADER, ""]

    for g in rg_index:
        if not isinstance(g, dict):
            continue
        rg_id = g.get("rg_id", "?")
        rr = g.get("round_range") or [None, None]
        s = rr[0] if len(rr) >= 1 else None
        e = rr[1] if len(rr) >= 2 else None
        tier = tier_map.get(rg_id) or g.get("tier") or TIER_FULL

        flags = []
        if g.get("legacy_rg"):
            flags.append("legacy")
        if g.get("sealed"):
            flags.append("sealed")
        flag_str = (" " + " ".join(f"[{f}]" for f in flags)) if flags else ""

        lines.append(f"## {rg_id} rounds {s}-{e} ({tier}){flag_str}")

        body = _select_tier_body(g, tier)
        if body:
            lines.append(body)
        lines.append("")  # 组间空行

    # 去掉末尾多余空行，保留单一换行结尾（确定性）
    text = "\n".join(lines).rstrip("\n") + "\n"
    return text


def _select_tier_body(group: Dict[str, Any], tier: str) -> str:
    """按 tier 选正文，缺对应档文本时向更高档回退（full>summary>brief）。"""
    full_t = group.get("full_text") or ""
    summary_t = group.get("summary_text") or ""
    brief_t = group.get("brief_text") or ""

    if tier == TIER_FULL:
        return (full_t or summary_t or brief_t).strip()
    if tier == TIER_SUMMARY:
        return (summary_t or full_t or brief_t).strip()
    if tier == TIER_BRIEF:
        return (brief_t or summary_t or full_t).strip()
    # 未知 tier → 当 full 处理
    return (full_t or summary_t or brief_t).strip()


# ========================
# M1 读写（候选隔离）
# ========================
def read_record(checkpoints_dir: str, window_key: str) -> str:
    """读 record.md 正文；文件不存在或读失败返回空串（D1：坏了可重渲，不阻断）。"""
    fp = record_md_path(checkpoints_dir, window_key)
    if not os.path.exists(fp):
        return ""
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def write_record_atomic(
    checkpoints_dir: str,
    window_key: str,
    candidate_text: str,
    prev_state: Optional[Dict[str, Any]] = None,
    *,
    candidate_index: Optional[List[Dict[str, Any]]] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[bool, List[str]]:
    """候选隔离写：先写 .record.md.tmp，validate 门禁通过才 os.replace 成正式文件；
    门禁不过【绝不覆盖】正式文件，并清理 tmp，返回 (False, errors)。

    candidate_index：候选边界索引（compose_record 在批2 产出）。本批地基允许为 None，
    此时只做「文本写入 + tmp 隔离」，validate 仅在提供 candidate_index 时执行结构门禁。

    一致性合约：成功时返回 (True, [])；失败时正式 record.md 维持原样、tmp 已清。
    """
    log = logger or _DEFAULT_LOGGER

    # 1) 门禁校验（提供候选索引时执行结构性门禁）
    if candidate_index is not None:
        ok, errors = validate_composed_record(candidate_index, prev_state)
        if not ok:
            log.warning(
                f"[RECORD] 候选写入门禁拒收 {window_key}: {errors}"
            )
            return False, errors

    # 2) 候选隔离写：mkstemp 临时文件 → fsync → os.replace（整体原子）
    fp = record_md_path(checkpoints_dir, window_key)
    try:
        _atomic_write_text(fp, candidate_text, prefix=".record_md_")
    except Exception as e:  # noqa: BLE001
        log.error(f"[RECORD] 候选落盘失败 {window_key}: {e}")
        return False, [f"write_failed: {e}"]
    return True, []


# ========================
# M1 写入门禁（继承 mcp validateComposedRecord）
# ========================
def validate_composed_record(
    candidate: List[Dict[str, Any]],
    prev_state: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str]]:
    """写入门禁：检查候选 round-group 边界表是否自洽且与上一状态衔接。
    任一违规 → (False, [errors...])，候选绝不落盘覆盖正式文件。

    candidate：候选 round-group 列表（有序），每项至少含 rg_id + round_range=[s,e]。

    门禁规则（继承 mcp validateComposedRecord）：
      1. 每组 round_range 自身合法：s/e 为可解析整数且 s <= e。
      2. 组间【不重叠】：后一组 s 必须 > 前一组 e（轮次重叠 → 拒收）。
      3. 组间【不倒退】：rg_id / round_range 单调递增（倒退 → 拒收）。
      4. 组间【无空洞】：相邻组 round 区间必须连续（后组 s == 前组 e + 1；
         区间不连续 / 中间缺轮 → 拒收）。legacy_rg 组之后允许一次跳变（历史段冷冻，
         legacy 区间到真实段起点之间不强制连续）。
      5. 与 prev_state 衔接：候选首组 round_range 起点必须 > prev_state 已聚合水位
         （last_grouped 对应的 round 上界），不得回退覆盖已封档区间。

    返回 (ok, errors)。errors 为人读字符串列表，便于日志定位。
    """
    errors: List[str] = []

    if not isinstance(candidate, list):
        return False, ["candidate 非列表"]
    if not candidate:
        # 空候选合法（无新增聚合）——交由调用方决定是否写空 record。
        return True, []

    parse = round_tracker.parse_round_id

    def _as_int(v: Any) -> Optional[int]:
        """round_range 端点可能是裸 int 或 'r000123' 字符串，统一解析为 int。"""
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            n = parse(v)
            return n if n >= 0 else None
        return None

    # ---- 规则 1：每组区间自身合法 ----
    norm: List[Tuple[str, int, int, Dict[str, Any]]] = []
    for i, g in enumerate(candidate):
        if not isinstance(g, dict):
            errors.append(f"组[{i}] 非 dict")
            continue
        rg_id = g.get("rg_id", f"?{i}")
        rr = g.get("round_range")
        if not isinstance(rr, (list, tuple)) or len(rr) < 2:
            errors.append(f"组[{i}]({rg_id}) round_range 缺失或非 [s,e]")
            continue
        s = _as_int(rr[0])
        e = _as_int(rr[1])
        if s is None or e is None:
            errors.append(f"组[{i}]({rg_id}) round_range 端点不可解析: {rr}")
            continue
        if s > e:
            errors.append(f"组[{i}]({rg_id}) 区间倒置 s={s} > e={e}")
            continue
        norm.append((rg_id, s, e, g))

    # 端点解析失败已记错，无法继续做组间关系校验 → 直接拒收
    if errors:
        return False, errors

    # ---- 规则 2/3/4：组间不重叠 / 不倒退 / 无空洞 ----
    for j in range(1, len(norm)):
        prev_rg, ps, pe, prev_g = norm[j - 1]
        cur_rg, cs, ce, _cur_g = norm[j]

        # 不倒退：后组起点必须严格大于前组起点
        if cs <= ps:
            errors.append(
                f"组倒退: {cur_rg}(s={cs}) 未在 {prev_rg}(s={ps}) 之后"
            )
            continue

        # 不重叠：后组起点必须 > 前组终点
        if cs <= pe:
            errors.append(
                f"轮次重叠: {cur_rg}(s={cs}) 落入 {prev_rg}(e={pe}) 区间内"
            )
            continue

        # 无空洞：相邻必须连续（后组 s == 前组 e + 1）。
        # 例外：前组是 legacy_rg（历史冷冻段），允许到真实段起点的一次跳变。
        if cs != pe + 1:
            if prev_g.get("legacy_rg"):
                continue  # legacy 段后允许跳变
            errors.append(
                f"空洞: {prev_rg}(e={pe}) 与 {cur_rg}(s={cs}) 之间缺轮 "
                f"({pe + 1}..{cs - 1})"
            )

    # ---- 规则 5：与 prev_state 衔接（不回退覆盖已封档区间）----
    if prev_state and norm:
        watermark = _grouped_round_watermark(prev_state)
        if watermark is not None:
            first_rg, first_s, _fe, _fg = norm[0]
            if first_s <= watermark:
                errors.append(
                    f"回退覆盖: 候选首组 {first_rg}(s={first_s}) "
                    f"<= 已聚合水位 round={watermark}"
                )

    return (len(errors) == 0), errors


def _grouped_round_watermark(prev_state: Dict[str, Any]) -> Optional[int]:
    """从 prev_state 推「已聚合到的 round 上界」（last_grouped 对应区间的终点 e）。

    优先用 round_groups 里 last_grouped_rg_id 对应组的 round_range[1]；
    退而求其次取 round_groups 中最大的 round_range[1]。无则 None（首次聚合，无水位）。
    """
    rg_list = prev_state.get("round_groups") or []
    if not isinstance(rg_list, list) or not rg_list:
        return None

    last_grouped = prev_state.get("last_grouped_rg_id")
    parse = round_tracker.parse_round_id

    def _end_of(g: Dict[str, Any]) -> Optional[int]:
        rr = g.get("round_range")
        if not isinstance(rr, (list, tuple)) or len(rr) < 2:
            return None
        e = rr[1]
        if isinstance(e, bool):
            return None
        if isinstance(e, int):
            return e
        if isinstance(e, str):
            n = parse(e)
            return n if n >= 0 else None
        return None

    # last_grouped_rg_id 对应组的终点优先
    if last_grouped:
        for g in rg_list:
            if isinstance(g, dict) and g.get("rg_id") == last_grouped:
                end = _end_of(g)
                if end is not None:
                    return end

    # 退化：取所有组终点的最大值
    ends = [
        _end_of(g) for g in rg_list
        if isinstance(g, dict) and _end_of(g) is not None
    ]
    return max(ends) if ends else None


# ========================
# 批2a 默认参数（cfg 缺省兜底；正式值由 _conf_schema.json 提供，后续批次接线）
# ========================
DEFAULT_RG_TARGET_ROUNDS = 8          # M3 软目标：每组目标轮数
DEFAULT_RG_FORCE_SEAL_ROUNDS = 15     # D3 强制封档：组轮数上限
DEFAULT_RG_FORCE_SEAL_TOKENS = 24000  # D3 强制封档：组 token 上限
DEFAULT_RG_FORCE_SEAL_AGE = 40        # D3 强制封档：组轮龄上限（now_round - 组终点）
DEFAULT_RG_MAX_BATCH_CHARS = 60000    # M3 硬约束：单批字符预算（照 mcp 60K）
DEFAULT_RG_MAX_BATCH_TOKENS = 16000   # M3 硬约束：单批 token 预算
DEFAULT_RG_ROLLBACK_SHORT_ROUNDS = 4  # D3：尾组「短」判据（轮数 < 此值）
DEFAULT_RG_ROLLBACK2_NEW_ROUNDS = 10  # D3：触发回滚 2 组的新增轮数门槛
DEFAULT_RG_LLM_RETRIES = 1            # 失败兜底：caller 额外重试次数
DEFAULT_RG_COOLDOWN_S = 60            # 失败兜底：建议冷却秒数（不实际 sleep）

# 批3 D7 分级定档默认参数（cfg 缺省兜底；正式值由 _conf_schema.json 提供）。
DEFAULT_TIER_SUMMARY_AGE = 20         # D7 age 阶梯：轮龄 >= 此值降到 summary
DEFAULT_TIER_BRIEF_AGE = 60           # D7 age 阶梯：轮龄 >= 此值降到 brief
DEFAULT_TIER_HYSTERESIS = 5           # D7 滞回：边界 ±此轮龄不翻档（防横跳）
DEFAULT_HIT_UPGRADE_THRESHOLD = 1.0   # D7 hit 升档：hit_score 超此值才在 base 抬一档
DEFAULT_HIT_WEIGHT_RAW = 1.0          # D7 hit 权重：原文召回命中（主动检索）
DEFAULT_HIT_WEIGHT_RECORD = 0.5       # D7 hit 权重：record 被动读命中（弱于原文召回）
DEFAULT_HIT_HALFLIFE_S = 86400        # D9 hit 衰减半衰期（秒，1 天；时间口径而非轮）
DEFAULT_HIT_KEEP_ROUNDS = 3           # D10 命中即锁定保持轮数（替代双阈值滞回，本批占位）

_RG_PREFIX = "rg"
_RG_WIDTH = 6


def _cfg_int(cfg: Optional[Dict[str, Any]], key: str, default: int) -> int:
    """从 cfg 取 int，缺失/非法回退默认。"""
    if not isinstance(cfg, dict):
        return default
    try:
        v = cfg.get(key, default)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _cfg_float(cfg: Optional[Dict[str, Any]], key: str, default: float) -> float:
    """从 cfg 取 float，缺失/非法回退默认。"""
    if not isinstance(cfg, dict):
        return default
    try:
        v = cfg.get(key, default)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def format_rg_id(n: int) -> str:
    """round-group 软轮号全限定字符串：rg{int:06d}。"""
    return f"{_RG_PREFIX}{n:0{_RG_WIDTH}d}"


def parse_rg_id(rg_id: Any) -> int:
    """解析 rg_id（'rg000123' → 123）；非法/None 返回 -1。"""
    if not rg_id or not isinstance(rg_id, str) or not rg_id.startswith(_RG_PREFIX):
        return -1
    try:
        return int(rg_id[len(_RG_PREFIX):])
    except ValueError:
        return -1


# ========================
# 批2a D3 强制收敛：force_seal_check
# ========================
def force_seal_check(
    group: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
    *,
    now_round: Optional[int] = None,
) -> bool:
    """D3 强制收敛：组达「轮数 / token / age」任一上限 → 应强制封档。

    **无视「话题未结」开放信号**——群聊里 @未回复 / 话题未结长期为真，若任由模型判
    「开放」则末尾组永远不封、反复重写重压 → 上下文漂移（主人最怕胡说八道）。故达硬
    阈值即封，与模型的开放意愿无关。

    判据（任一为真即返回 True）：
      1. 轮数：组覆盖轮数（round_range 跨度 + 1）>= rg_force_seal_rounds（默认 15）。
      2. token：组累计 token 估计 >= rg_force_seal_tokens（默认 24000；组需带
         token_est 字段，无则跳过此判据）。
      3. age ：now_round 给定时，组轮龄（now_round - round_range[1]）>= rg_force_seal_age
         （默认 40）。age 衡量「这组离最新对话多远」，太老的开放组也强制收敛。

    返回 True=应封档（调用方据此置 sealed=True）。纯函数，无副作用。
    """
    if not isinstance(group, dict):
        return False

    seal_rounds = _cfg_int(cfg, "rg_force_seal_rounds", DEFAULT_RG_FORCE_SEAL_ROUNDS)
    seal_tokens = _cfg_int(cfg, "rg_force_seal_tokens", DEFAULT_RG_FORCE_SEAL_TOKENS)
    seal_age = _cfg_int(cfg, "rg_force_seal_age", DEFAULT_RG_FORCE_SEAL_AGE)

    rr = group.get("round_range") or [None, None]
    s = _coerce_round_int(rr[0]) if len(rr) >= 1 else None
    e = _coerce_round_int(rr[1]) if len(rr) >= 2 else None

    # 1) 轮数上限
    if s is not None and e is not None and e >= s:
        if (e - s + 1) >= seal_rounds:
            return True

    # 2) token 上限（组带 token_est 时）
    tok = group.get("token_est")
    if isinstance(tok, (int, float)) and tok >= seal_tokens:
        return True

    # 3) age 上限（给定 now_round 时）
    if now_round is not None and e is not None:
        if (now_round - e) >= seal_age:
            return True

    return False


def _coerce_round_int(v: Any) -> Optional[int]:
    """round_range 端点统一成 int（裸 int 或 'r000123' 字符串）；非法返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        n = round_tracker.parse_round_id(v)
        return n if n >= 0 else None
    return None


# ========================
# 批3 R6 / D7：分级定档公式 tier = f(age, hit_score, hysteresis)
# ========================
# 台阶 + 偏移（D7 主人拍板「轮龄为主、命中修正」）：
#   ① base_tier 由 age 阶梯定（轮龄=now_round-组终点；越老越降档）：
#        轮龄 <  tier_summary_age → full
#        轮龄 <  tier_brief_age   → summary
#        else                     → brief
#   ② hit_score（读 hit_table，时间衰减）超 hit_upgrade_threshold → 在 base 上抬【至多一档】，
#      封顶 full（brief→summary→full→full）。批3 hit_table 可能空 → hit_score=0 → 纯 age 兜底。
#   ③ tier_hysteresis 滞回作用在【合成分】（这里=轮龄轴）：边界 ±hysteresis 轮龄内不翻档，
#      防同一组在「刚好跨阈值」的相邻轮反复升降档横跳。靠传入 prev_tier 锚定方向：
#        - prev=full、轮龄刚过 summary_age 但未过 (summary_age+hysteresis) → 仍判 full（粘住高档）
#        - prev=summary、轮龄刚跌回 summary_age 以下但未跌破 (summary_age-hysteresis) → 仍判 summary
#      无 prev_tier（首次定档）→ 纯阶梯，不滞回。

# tier 数值序（越大档越高/越详细）：brief < summary < full。便于「抬一档」「封顶」运算。
_TIER_ORDER = {TIER_BRIEF: 0, TIER_SUMMARY: 1, TIER_FULL: 2}
_TIER_BY_ORDER = {0: TIER_BRIEF, 1: TIER_SUMMARY, 2: TIER_FULL}


def hit_score(
    rg_id: Optional[str],
    hit_table: Optional[Dict[str, Any]],
    now_ts: float,
    cfg: Optional[Dict[str, Any]] = None,
) -> float:
    """D7/D9：某 round-group 的命中热度分（时间衰减，越近越热）。

    公式：score = hit_count × weight × 0.5^(Δt / halflife)
      - Δt = now_ts - last_hit_ts（秒；D9 衰减按【时间】而非轮，贴合「记忆随时间淡」）。
      - weight 按 last_hit_type 取 raw（原文召回，强）/ record（被动读，弱）。
      - hit_count 把同组多次命中累加进强度（次数越多越热）。
    hit_table 缺该组 / 为空 / 字段缺失 → 返回 0.0（**纯 age 兜底，绝不报错**——批3 hit_table
    必空，批4 填 hit 后升档才生效）。Δt<0（时钟回拨）按 0 处理（不衰减、不放大）。
    """
    if not rg_id or not isinstance(hit_table, dict):
        return 0.0
    entry = hit_table.get(rg_id)
    if not isinstance(entry, dict):
        return 0.0
    try:
        hit_count = float(entry.get("hit_count", 0) or 0)
    except (TypeError, ValueError):
        hit_count = 0.0
    if hit_count <= 0:
        return 0.0

    halflife = _cfg_float(cfg, "hit_halflife", DEFAULT_HIT_HALFLIFE_S)
    if halflife <= 0:
        halflife = DEFAULT_HIT_HALFLIFE_S

    last_type = entry.get("last_hit_type") or "raw"
    if last_type == "record":
        weight = _cfg_float(cfg, "hit_weight_record", DEFAULT_HIT_WEIGHT_RECORD)
    else:
        weight = _cfg_float(cfg, "hit_weight_raw", DEFAULT_HIT_WEIGHT_RAW)

    last_ts = entry.get("last_hit_ts")
    decay = 1.0
    if isinstance(last_ts, (int, float)):
        dt = float(now_ts) - float(last_ts)
        if dt > 0:
            decay = 0.5 ** (dt / halflife)
        # dt<=0（同刻 / 时钟回拨）→ decay=1.0（不放大、不衰减）
    return hit_count * weight * decay


def _base_tier_by_age(age: int, summary_age: int, brief_age: int) -> str:
    """D7 step①：纯 age 阶梯定 base tier（不含滞回/hit）。age=轮龄（越大越老）。"""
    if age < summary_age:
        return TIER_FULL
    if age < brief_age:
        return TIER_SUMMARY
    return TIER_BRIEF


def tier_for_group(
    group: Dict[str, Any],
    now_round: Optional[int],
    hit_table: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Optional[float] = None,
    prev_tier: Optional[str] = None,
) -> str:
    """D7 定档公式（纯函数，可缓存）：tier = f(age, hit_score, hysteresis)。

    参数：
      group     : round-group dict（需 round_range；sealed 决定能否降档，见下「D8 守护」）。
      now_round : 当前最新轮号（轮龄=now_round-组终点）。None / 组终点不可解析 → full（保守不降档）。
      hit_table : metadata.record_state.hit_table（批3 常空 → 纯 age 兜底）。
      cfg       : 配置（tier_summary_age/tier_brief_age/tier_hysteresis/hit_* ）。
      now_ts    : 命中衰减用的当前时间戳（秒）；None → time.time()。
      prev_tier : 该组上一次的定档（滞回锚点）；None=首次定档，纯阶梯。

    台阶 + 偏移：
      ① base = age 阶梯（full/summary/brief）。
      ② hit_score 超阈值 → base 抬【至多一档】、封顶 full。
      ③ tier_hysteresis 滞回作用在 age 轴：边界 ±hysteresis 轮龄内、且方向与 prev 一致时粘住 prev，
         防边界轮反复横跳。hit 升档后再应用「只升不降」收敛（命中过的组本轮不因滞回掉回 base）。
      ④ D10 命中锁定（hit_keep_active）：命中后 hit_keep_rounds 轮内强制至少抬一档不降，
         替代 hit_score 时间衰减在相邻轮跨阈值造成的升降横跳（需 now_round + entry.last_hit_round）。

    **D8 守护（只 sealed 组允许降档）**：未封板（sealed!=True）的组**强制留 full**——summary/brief
    文本只在封板后预生成，未封板降档会读到空/旧摘要造空洞。故 sealed=False → 直接 full，不进阶梯。
    """
    if not isinstance(group, dict):
        return TIER_FULL

    # D8 守护：未封板组绝不降档（summary/brief 尚未预生成）。
    if not group.get("sealed", False):
        return TIER_FULL

    rr = group.get("round_range") or [None, None]
    end = _coerce_round_int(rr[1]) if len(rr) >= 2 else None
    if now_round is None or end is None:
        return TIER_FULL  # 无法算轮龄 → 保守留 full

    age = int(now_round) - int(end)
    if age < 0:
        age = 0

    summary_age = _cfg_int(cfg, "tier_summary_age", DEFAULT_TIER_SUMMARY_AGE)
    brief_age = _cfg_int(cfg, "tier_brief_age", DEFAULT_TIER_BRIEF_AGE)
    hysteresis = _cfg_int(cfg, "tier_hysteresis", DEFAULT_TIER_HYSTERESIS)
    if hysteresis < 0:
        hysteresis = 0

    # ---- ① base：纯 age 阶梯 ----
    base = _base_tier_by_age(age, summary_age, brief_age)

    # ---- ③a 滞回（age 轴）：相邻边界轮粘住 prev_tier，防横跳 ----
    # 仅当 prev_tier 已知且 base 与 prev 相差一档、且 age 落在边界 ±hysteresis 带内时粘住。
    if prev_tier in _TIER_ORDER and hysteresis > 0:
        prev_ord = _TIER_ORDER[prev_tier]
        base_ord = _TIER_ORDER[base]
        if abs(prev_ord - base_ord) == 1:
            # 判断 base 是否处于「刚跨阈值」的滞回带：用相关阈值 ±hysteresis 判定。
            # full↔summary 边界 = summary_age；summary↔brief 边界 = brief_age。
            if {prev_ord, base_ord} == {_TIER_ORDER[TIER_FULL], _TIER_ORDER[TIER_SUMMARY]}:
                boundary = summary_age
            else:
                boundary = brief_age
            if (boundary - hysteresis) <= age < (boundary + hysteresis):
                base = prev_tier  # 滞回带内粘住上一档

    # ---- ② hit 升档：hit_score 超阈值 → 抬一档、封顶（D10 拆两线）----
    base_ord = _TIER_ORDER.get(base, _TIER_ORDER[TIER_FULL])
    ts = now_ts if now_ts is not None else _now_ts()
    rg_id = group.get("rg_id")
    score = hit_score(rg_id, hit_table, ts, cfg)
    upgrade_thresh = _cfg_float(cfg, "hit_upgrade_threshold", DEFAULT_HIT_UPGRADE_THRESHOLD)
    cap_ord = _hit_upgrade_cap_ord(group)  # D10 封顶：文字线 full / 多模态原图线 summary
    final_ord = base_ord
    if score >= upgrade_thresh:
        final_ord = min(base_ord + 1, cap_ord)  # 抬一档，封顶到拆线上限

    # ---- ④ D10 命中锁定（hit_keep_rounds 滞回）：命中即锁定保持 N 轮不降 ----
    # hit_score 按时间衰减会在「相邻轮」掉回阈值下导致升档忽有忽无（横跳）。命中锁定
    # 用「命中后 hit_keep_rounds 轮内强制至少抬一档」替代纯衰减，杜绝横跳。锁定不叠加
    # hit 升档（二者都只抬一档、封顶同上），仅在 score 已衰减、但仍处锁定窗口时兜底升档。
    if hit_keep_active(rg_id, hit_table, now_round, cfg):
        final_ord = max(final_ord, min(base_ord + 1, cap_ord))

    return _TIER_BY_ORDER.get(final_ord, TIER_FULL)


def _hit_upgrade_cap_ord(group: Dict[str, Any]) -> int:
    """D10 升档封顶拆两线（返回 tier 序值上限）：
      - 文字 record hit → 封顶 full（_TIER_ORDER[TIER_FULL]）。本批主线。
      - 多模态原图 hit → 封顶 summary（_TIER_ORDER[TIER_SUMMARY]）：原图召回代价高，
        升档只到 summary（带写入时快照摘要的文字层，RFS-07 自包含），不强拉回 full 原图。
    判定组是否「多模态原图主导」靠组级 has_multimodal 标记——**S7 占位**：当前 compose
    不产组级 has_multimodal（只 message 级有），故本批一律走文字线封顶 full；S7 填充组级
    标记后此函数自动分流，无需再改 tier_for_group。
    """
    if isinstance(group, dict) and group.get("has_multimodal") is True:
        return _TIER_ORDER[TIER_SUMMARY]  # 多模态原图线（S7 启用）
    return _TIER_ORDER[TIER_FULL]         # 文字线（本批主线）


# ========================
# 批4 M4 / D10：hit 命中锁定（hit_keep_rounds 滞回）
# ========================
def hit_keep_active(
    rg_id: Optional[str],
    hit_table: Optional[Dict[str, Any]],
    now_round: Optional[int],
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """D10：该 round-group 是否处于「命中锁定窗口」内（命中后 hit_keep_rounds 轮内）。

    命中即锁定（替代双阈值滞回）：组被命中那一刻记下 last_hit_round；此后
    (now_round - last_hit_round) < hit_keep_rounds 期间，tier_for_group 强制其至少抬
    一档不降，杜绝 hit_score 时间衰减在相邻轮反复跨阈值造成的升降档横跳。

    判 False（不锁定）的情形：rg_id/hit_table 缺、entry 无 last_hit_round、now_round
    不可用、或锁定窗口已过。窗口数 <=0 时永不锁定。**纯函数、绝不报错**（缺字段降级）。
    """
    if not rg_id or not isinstance(hit_table, dict) or now_round is None:
        return False
    entry = hit_table.get(rg_id)
    if not isinstance(entry, dict):
        return False
    last_round = entry.get("last_hit_round")
    if not isinstance(last_round, int) or isinstance(last_round, bool):
        return False
    keep = _cfg_int(cfg, "hit_keep_rounds", DEFAULT_HIT_KEEP_ROUNDS)
    if keep <= 0:
        return False
    delta = int(now_round) - last_round
    if delta < 0:
        return False  # 命中轮在未来（时钟/号源异常）→ 不锁定
    return delta < keep


def _now_ts() -> float:
    """当前时间戳（秒）。抽出便于单测 monkeypatch / 注入。"""
    import time as _t
    return _t.time()


# ========================
# 批4 M4 / D9 / D10：hit 命中记录 + 重编号 key 迁移 + round→rg 映射
# ========================
HIT_TYPE_RAW = "raw"        # 原文召回命中（QQ_data_original 查原文，强信号）
HIT_TYPE_RECORD = "record"  # record 被动读命中（cited_rounds 声明，弱信号；本批占位留 S7）


def apply_hit_to_table(
    hit_table: Dict[str, Any],
    rg_id: str,
    hit_type: str,
    now_ts: float,
    now_round: Optional[int] = None,
) -> Dict[str, Any]:
    """把一次命中累加进 hit_table（**原地修改并返回**；收尾事务 flush 队列时调用）。

    D9 entry 结构：{hit_count, last_hit_ts, last_hit_type, last_hit_round}。
      - hit_count   : 累加（次数越多越热，hit_score 据此放大）。
      - last_hit_ts : 命中时间戳（秒；hit_score 时间衰减锚）。
      - last_hit_type: raw / record（hit_score 据此取 hit_weight_raw / hit_weight_record）。
      - last_hit_round: 命中时的 now_round（D10 hit_keep_active 锁定窗口锚；None 则不写，
        锁定守降级失效但 hit_score 仍生效）。
    非 dict hit_table / 空 rg_id → 原样返回不报错。hit_type 非法 → 归一为 raw。
    """
    if not isinstance(hit_table, dict) or not rg_id:
        return hit_table if isinstance(hit_table, dict) else {}
    if hit_type not in (HIT_TYPE_RAW, HIT_TYPE_RECORD):
        hit_type = HIT_TYPE_RAW
    entry = hit_table.get(rg_id)
    if not isinstance(entry, dict):
        entry = {"hit_count": 0}
        hit_table[rg_id] = entry
    try:
        prev = int(entry.get("hit_count", 0) or 0)
    except (TypeError, ValueError):
        prev = 0
    entry["hit_count"] = prev + 1
    entry["last_hit_ts"] = float(now_ts)
    entry["last_hit_type"] = hit_type
    if isinstance(now_round, int) and not isinstance(now_round, bool):
        entry["last_hit_round"] = now_round
    return hit_table


def round_id_to_rg_id(
    round_int: int,
    round_groups: Optional[List[Dict[str, Any]]],
) -> Optional[str]:
    """给定硬 round_id 整数，从 round_groups 边界表找它所属的 rg_id（命中触发用）。

    按 round_range=[s,e] 闭区间匹配；命中多组（理论不应发生，区间应不重叠）取首个。
    round_groups 为权威边界表（metadata.record_state.round_groups）。无匹配 → None
    （该 round 尚未聚合进任何组，落在末尾未聚合原文区，不计 hit）。
    """
    if not isinstance(round_groups, list):
        return None
    for g in round_groups:
        if not isinstance(g, dict):
            continue
        rr = g.get("round_range") or [None, None]
        if len(rr) < 2:
            continue
        s = _coerce_round_int(rr[0])
        e = _coerce_round_int(rr[1])
        if s is None or e is None:
            continue
        if s <= int(round_int) <= e:
            return g.get("rg_id")
    return None


def migrate_hit_table_on_renumber(
    old_hit_table: Optional[Dict[str, Any]],
    old_groups: Optional[List[Dict[str, Any]]],
    new_groups: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """D9 重编号 key 迁移：compose 重写窗口致 rg_id 变 → 老 rg_id 的 hit 不丢。

    compose 保留 kept 组的 rg_id 不变、对回滚重写窗口内的组用 _next_rg_num 重新编号
    （rg_id 是展示软号，硬 round_id 不变）。故老 hit_table 的 key 可能在 new_groups
    里已不存在 → 直接搬运会丢命中。迁移策略（按 round_range 重叠映射）：
      ① 老 rg_id 在 new_groups 仍存在（kept 段）→ entry 原样保留。
      ② 老 rg_id 不在 new_groups（被重写）→ 用老组 round_range 的中点（或起点）找
         new_groups 里覆盖该 round 的新组，hit 迁移到新 rg_id。
      ③ 多个老组映射到同一新组（合并）→ entry 合并：hit_count 求和、last_hit_ts 取最大
         （最近）、last_hit_type/last_hit_round 跟随最近那条。
      ④ 老组找不到对应新组（被裁掉/区间消失）→ 丢弃该 entry（对应内容已不在 record）。
    纯函数，返回新 hit_table（不改入参）。任一为空/非法 → 返回 {} 或原样安全降级。
    """
    if not isinstance(old_hit_table, dict) or not old_hit_table:
        return {}
    new_groups = new_groups if isinstance(new_groups, list) else []
    old_groups = old_groups if isinstance(old_groups, list) else []

    # 新组 rg_id 集合（判断老 key 是否仍存在）。
    new_rg_ids = {
        g.get("rg_id") for g in new_groups
        if isinstance(g, dict) and g.get("rg_id")
    }
    # 老 rg_id → round_range（用于 ② 重叠映射定位锚 round）。
    old_range_by_rg: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    for g in old_groups:
        if not isinstance(g, dict):
            continue
        rid = g.get("rg_id")
        if not rid:
            continue
        rr = g.get("round_range") or [None, None]
        s = _coerce_round_int(rr[0]) if len(rr) >= 1 else None
        e = _coerce_round_int(rr[1]) if len(rr) >= 2 else None
        old_range_by_rg[rid] = (s, e)

    out: Dict[str, Any] = {}

    def _merge_into(target_rg: str, entry: Dict[str, Any]) -> None:
        """把 entry 合并进 out[target_rg]（③ 合并语义）。"""
        if not isinstance(entry, dict):
            return
        try:
            cnt = int(entry.get("hit_count", 0) or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt <= 0:
            cnt = 0
        cur = out.get(target_rg)
        if not isinstance(cur, dict):
            out[target_rg] = dict(entry)
            out[target_rg]["hit_count"] = cnt
            return
        # 合并：count 求和；ts 取最近，type/round 跟随最近那条。
        try:
            cur_cnt = int(cur.get("hit_count", 0) or 0)
        except (TypeError, ValueError):
            cur_cnt = 0
        cur["hit_count"] = cur_cnt + cnt
        new_ts = entry.get("last_hit_ts")
        cur_ts = cur.get("last_hit_ts")
        if isinstance(new_ts, (int, float)) and (
            not isinstance(cur_ts, (int, float)) or new_ts >= cur_ts
        ):
            cur["last_hit_ts"] = new_ts
            if entry.get("last_hit_type") is not None:
                cur["last_hit_type"] = entry.get("last_hit_type")
            if entry.get("last_hit_round") is not None:
                cur["last_hit_round"] = entry.get("last_hit_round")

    for old_rg, entry in old_hit_table.items():
        if not isinstance(entry, dict):
            continue
        # ① 老 key 在新组里仍存在 → 原样保留。
        if old_rg in new_rg_ids:
            _merge_into(old_rg, entry)
            continue
        # ② 老组被重写 → 用老组 round_range 锚 round 找覆盖它的新组。
        s, e = old_range_by_rg.get(old_rg, (None, None))
        anchor: Optional[int] = None
        if s is not None and e is not None and e >= s:
            anchor = (s + e) // 2  # 区间中点（更稳，避开边界）
        elif s is not None:
            anchor = s
        elif e is not None:
            anchor = e
        target = round_id_to_rg_id(anchor, new_groups) if anchor is not None else None
        if target:
            _merge_into(target, entry)
        # ④ 找不到新组（内容被裁掉）→ 丢弃 entry，不进 out。
    return out


def build_tier_map(
    round_groups: List[Dict[str, Any]],
    now_round: Optional[int],
    hit_table: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Optional[float] = None,
) -> Dict[str, str]:
    """对一批 round_groups 逐组定档，产出 {rg_id: tier} 覆盖表（供 render_record_md /
    分级读取层用）。prev_tier 取各组当前 group['tier']（滞回锚），缺则 None。"""
    out: Dict[str, str] = {}
    ts = now_ts if now_ts is not None else _now_ts()
    for g in round_groups or []:
        if not isinstance(g, dict):
            continue
        rg_id = g.get("rg_id")
        if not rg_id:
            continue
        out[rg_id] = tier_for_group(
            g, now_round, hit_table, cfg,
            now_ts=ts, prev_tier=g.get("tier"),
        )
    return out


# ========================
# 批3 D8：summary 封板生成（sealed 组才预生成 summary_text + title，watermark 防空洞）
# ========================
def generate_summaries_for_sealed(
    round_groups: List[Dict[str, Any]],
    prev_watermark_rg_id: Optional[str],
    summary_caller,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    logger: Optional[logging.Logger] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str], List[str]]:
    """D8：为【已封板且水位之后、尚无 summary】的组预生成 summary_text + title，推进
    summary_watermark_rg_id。**标题搭 summary 车**——同一次 summary_caller 调用强制吐
    title（零成本，「不单独生成标题」正解）。

    防空洞铁律：summary 只在组**封板后**预生成；目标组 > 水位且无 summary 时由分级层
    强制留 full 触发本函数补生成（杜绝「有 full 无 summary」）。本函数只补水位之后的封板组，
    水位之前的视为已生成（幂等、不重复调模型）。

    参数：
      round_groups       : 当前边界表（compose 产出，组含 sealed/summary_text/full_text）。
      prev_watermark_rg_id : 上次 summary 封板水位（该 rg_id 及之前视为已生成）。
      summary_caller     : callable(group_view: dict, cfg) -> {"summary_text":str,"title":str}
                           group_view = {rg_id, round_range, full_text, title}；
                           抛异常 / 返回空 → 该组本次跳过（留待下次补，不阻断、不写坏）。
      cfg / logger       : 配置 / 日志。

    返回 (new_groups, new_watermark_rg_id, errors)：
      - new_groups：原组的浅拷贝，已补 summary_text/title 的填回（其余不动）。
      - new_watermark_rg_id：推进后的水位（最后一个【连续已具 summary 的封板组】rg_id）；
        无可推进 → 维持 prev_watermark_rg_id。
      - errors：caller 失败的人读原因（不阻断主链路）。
    """
    log = logger or _DEFAULT_LOGGER
    errors: List[str] = []
    groups = [g for g in (round_groups or []) if isinstance(g, dict)]
    if not groups:
        return groups, prev_watermark_rg_id, errors

    wm_num = parse_rg_id(prev_watermark_rg_id) if prev_watermark_rg_id else -1
    out: List[Dict[str, Any]] = []

    for g in groups:
        ng = dict(g)  # 浅拷贝，避免就地改入参
        rg_num = parse_rg_id(ng.get("rg_id"))
        need = (
            ng.get("sealed", False)              # 只 sealed 组生成（D8）
            and rg_num > wm_num                  # 水位之后才补（幂等）
            and not (ng.get("summary_text") or "").strip()  # 尚无 summary
            and not ng.get("legacy_rg", False)   # legacy 冷冻段不补（其 summary 来自旧 T1）
        )
        if need:
            view = {
                "rg_id": ng.get("rg_id"),
                "round_range": ng.get("round_range"),
                "full_text": ng.get("full_text") or "",
                "title": ng.get("title") or "",
            }
            try:
                res = summary_caller(view, cfg)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{ng.get('rg_id')}: summary_caller 异常 {type(e).__name__}: {e}")
                res = None
            if isinstance(res, dict):
                st = str(res.get("summary_text") or "").strip()
                ti = str(res.get("title") or "").strip()
                if st:
                    ng["summary_text"] = st
                    if ti and not (ng.get("title") or "").strip():
                        ng["title"] = ti  # 标题搭车（仅在原无标题时填，不覆盖已有）
                else:
                    errors.append(f"{ng.get('rg_id')}: summary_caller 返回空 summary")
            elif res is not None:
                errors.append(f"{ng.get('rg_id')}: summary_caller 返回非 dict")
        out.append(ng)

    # 推进 watermark：从 prev 水位起，连续的「封板且已具 summary」的组可纳入水位。
    new_wm = prev_watermark_rg_id
    for ng in out:
        rg_num = parse_rg_id(ng.get("rg_id"))
        if rg_num <= wm_num:
            continue  # 水位之前，跳过
        has_summary = bool((ng.get("summary_text") or "").strip())
        if ng.get("sealed", False) and (has_summary or ng.get("legacy_rg", False)):
            new_wm = ng.get("rg_id")
            wm_num = rg_num  # 连续推进
        else:
            break  # 遇到第一个「封板但仍无 summary」的组即停（水位不越过空洞）

    if errors:
        log.warning(f"[RECORD] summary 封板生成部分失败: {errors}")
    return out, new_wm, errors


def group_has_summary_gap(
    group: Dict[str, Any],
    watermark_rg_id: Optional[str],
) -> bool:
    """D8 空洞探测：组「已封板、在水位之后、却无 summary_text」=空洞，分级层须强制留 full。

    用于 build_llm_contexts / tier_for_group 调用方判定：即便 age 够老想降 summary，
    若该组处于空洞态则不能降（会读到空摘要），强制 full 直到 generate_summaries 补上。
    """
    if not isinstance(group, dict):
        return False
    if not group.get("sealed", False):
        return False  # 未封板本就强制 full（tier_for_group 已处理）
    if group.get("legacy_rg", False):
        return False  # legacy summary 来自旧 T1，不算空洞
    rg_num = parse_rg_id(group.get("rg_id"))
    wm_num = parse_rg_id(watermark_rg_id) if watermark_rg_id else -1
    if rg_num <= wm_num:
        return False  # 水位之内视为已生成
    return not (group.get("summary_text") or "").strip()


# ========================
# 批2a：轮聚合 / 预算切批 / compose_record（D3 确定性聚合）
# ========================
class ComposeResult:
    """compose_record 的结构化返回。

    字段：
      wrote          : bool，本次是否产出并写盘（validate 通过 + 有实际变化）。
      round_groups   : List[dict]，新的边界表（回滚窗口外旧组不动 + 窗口内重分段新组）；
                       失败兜底时回传【未改动】的 prev round_groups（维持未分组态）。
      last_grouped_rg_id : str|None，单调推进后的聚合锚（取 round_groups 末组 rg_id）。
      record_md      : str，渲染后的 record.md 文本（未写盘时为已有/空）。
      errors         : List[str]，门禁拒收 / LLM 失败的原因（人读）。
      fallback       : bool，是否走了失败兜底（LLM 失败 / 无产出）。
      cooldown_until : float|None，建议冷却时间戳（now+cooldown_s）；调用方据此跳过重试，
                       compose 本身不 sleep（纯逻辑、单测友好）。
    """

    __slots__ = (
        "wrote", "round_groups", "last_grouped_rg_id", "record_md",
        "errors", "fallback", "cooldown_until",
    )

    def __init__(
        self,
        *,
        wrote: bool,
        round_groups: List[Dict[str, Any]],
        last_grouped_rg_id: Optional[str],
        record_md: str,
        errors: Optional[List[str]] = None,
        fallback: bool = False,
        cooldown_until: Optional[float] = None,
    ) -> None:
        self.wrote = wrote
        self.round_groups = round_groups
        self.last_grouped_rg_id = last_grouped_rg_id
        self.record_md = record_md
        self.errors = errors or []
        self.fallback = fallback
        self.cooldown_until = cooldown_until

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wrote": self.wrote,
            "round_groups": self.round_groups,
            "last_grouped_rg_id": self.last_grouped_rg_id,
            "record_md": self.record_md,
            "errors": list(self.errors),
            "fallback": self.fallback,
            "cooldown_until": self.cooldown_until,
        }


def _rounds_from_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把已闭合的 message 列表按 round_id 聚合成有序「轮」视图。

    - 丢弃 round_id 不可解析的消息（legacy round_id=None 永不进组，D4）。
    - 同一 round_id 的多条 message（user / assistant / tool ReAct 段）聚成一轮，
      文本按出现顺序拼接，token_est 累加（用 message 自带 token 估计，缺则按字符粗估）。
    - 输出按 round_int 升序；每轮：
        {round_int, round_id, text, char_len, token_est, msg_count}
    """
    buckets: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        rn = round_tracker.parse_round_id(m.get("round_id"))
        if rn < 0:
            continue  # legacy / 无号，不进组
        b = buckets.get(rn)
        if b is None:
            b = {
                "round_int": rn,
                "round_id": m.get("round_id"),
                "_parts": [],
                "token_est": 0,
                "msg_count": 0,
            }
            buckets[rn] = b
            order.append(rn)
        role = m.get("role") or ""
        content = m.get("content")
        text = content if isinstance(content, str) else (
            json.dumps(content, ensure_ascii=False) if content is not None else ""
        )
        b["_parts"].append(f"{role}: {text}".strip())
        b["msg_count"] += 1
        tok = m.get("token_est") or m.get("tokens")
        if isinstance(tok, (int, float)):
            b["token_est"] += int(tok)
        else:
            # 粗估：~4 字符 1 token（仅兜底，真实估算在 checkpoint.estimate_tokens）
            b["token_est"] += max(1, len(text) // 4)

    rounds: List[Dict[str, Any]] = []
    for rn in sorted(order):
        b = buckets[rn]
        joined = "\n".join(b["_parts"])
        rounds.append({
            "round_int": rn,
            "round_id": b["round_id"],
            "text": joined,
            "char_len": len(joined),
            "token_est": int(b["token_est"]),
            "msg_count": b["msg_count"],
        })
    return rounds


def _partition_groups(
    prev_groups: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把已有 round_groups 拆成「不可回滚（sealed/legacy）」与「尾部可回滚」两段。

    可回滚 = 列表尾部【连续】的非 sealed 非 legacy 组。一旦从尾往前遇到 sealed/legacy
    就停（已封档段绝不回滚，增量约束「已封档 sealed 组不动」）。
    返回 (kept_locked_prefix_unused, rollbackable_tail)：第一个返回值实际是「除可回滚尾部
    外的全部前缀」（含 sealed/legacy + 它们之前的组），调用方据 rollback_count 再切。
    """
    groups = [g for g in (prev_groups or []) if isinstance(g, dict)]
    i = len(groups)
    while i > 0:
        g = groups[i - 1]
        if g.get("sealed") or g.get("legacy_rg"):
            break
        i -= 1
    prefix = groups[:i]
    rollbackable = groups[i:]
    return prefix, rollbackable


def _decide_rollback_count(
    rollbackable: List[Dict[str, Any]],
    new_rounds_count: int,
    cfg: Optional[Dict[str, Any]] = None,
) -> int:
    """D3 确定性回滚组数：默认 1；尾组短且新增多 → 2。clamp 到可回滚组数。

    - 无可回滚组（首次聚合 / 全封档）→ 0（纯新增，不回滚）。
    - 默认回滚 1 组（最后一组并入重写窗口，吸收新增轮重新分段）。
    - 若最后一组「短」（覆盖轮数 < rg_rollback_short_rounds，默认 4）且新增未聚合轮
      >= rg_rollback2_new_rounds（默认 10）→ 回滚 2 组（把碎尾合并重切）。
    """
    if not rollbackable:
        return 0
    short_thresh = _cfg_int(cfg, "rg_rollback_short_rounds", DEFAULT_RG_ROLLBACK_SHORT_ROUNDS)
    new_thresh = _cfg_int(cfg, "rg_rollback2_new_rounds", DEFAULT_RG_ROLLBACK2_NEW_ROUNDS)

    count = 1
    last = rollbackable[-1]
    rr = last.get("round_range") or [None, None]
    s = _coerce_round_int(rr[0]) if len(rr) >= 1 else None
    e = _coerce_round_int(rr[1]) if len(rr) >= 2 else None
    last_span = (e - s + 1) if (s is not None and e is not None and e >= s) else 0
    if last_span and last_span < short_thresh and new_rounds_count >= new_thresh:
        count = 2
    return min(count, len(rollbackable))


def _split_window_into_batches(
    window_rounds: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """M3 预算硬切批：按【字符 / token 预算】把窗口轮切成多批喂给 caller。

    M3 优先级铁律（mcp createRecordChunks）：**字符/token 预算是硬约束，轮数是软目标**。
    故切批只受字符/token 预算驱动，**不以轮数硬切批**——轮数上限只在 force_seal_check
    里约束单个组别太大（rg_force_seal_rounds），不在此处割裂窗口、阻止模型在完整窗口内
    自由分段（否则 rg_target_rounds 软目标会被批边界强行打散）。

    - 硬约束（任一超即另起一批）：累计 char >= rg_max_batch_chars（默认 60K）、
      累计 token >= rg_max_batch_tokens（默认 16K）。
    - **单轮超预算（巨轮）→ step 级切批 fallback**：该轮自身 char/token 已超单批预算，
      则独占一批（oversize=True），绝不与其它轮挤一批撑爆；后续强制单组 + 封档，
      防巨图 / 超长单轮反复重压死循环。

    返回 [{rounds:[...], oversize:bool}, ...]，批内 rounds 保持 round_int 升序。
    """
    max_chars = _cfg_int(cfg, "rg_max_batch_chars", DEFAULT_RG_MAX_BATCH_CHARS)
    max_tokens = _cfg_int(cfg, "rg_max_batch_tokens", DEFAULT_RG_MAX_BATCH_TOKENS)

    batches: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []
    cur_chars = 0
    cur_tokens = 0

    def _flush():
        nonlocal cur, cur_chars, cur_tokens
        if cur:
            batches.append({"rounds": cur, "oversize": False})
            cur = []
            cur_chars = 0
            cur_tokens = 0

    for r in window_rounds:
        rc = int(r.get("char_len", 0) or 0)
        rt = int(r.get("token_est", 0) or 0)

        # 巨轮：单轮自身超单批预算 → step 级 fallback，独占一批
        if rc >= max_chars or rt >= max_tokens:
            _flush()
            batches.append({"rounds": [dict(r, oversize=True)], "oversize": True})
            continue

        # 字符/token 预算硬约束：累计已超且当前批非空 → 先封批再放本轮
        if cur and (cur_chars + rc >= max_chars or cur_tokens + rt >= max_tokens):
            _flush()
        cur.append(r)
        cur_chars += rc
        cur_tokens += rt

    _flush()
    return batches


def _call_llm_with_retry(
    llm_caller,
    batch_rounds: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]],
    log: logging.Logger,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """调 llm_caller 并带有限重试。返回 (ok, specs)。

    任一次成功（返回非空 list）即返回；全部失败（异常 / 返回空 / None）→ (False, [])。
    无 sleep（冷却由 compose 上层用 cooldown_until 协调，纯逻辑单测友好）。
    """
    retries = _cfg_int(cfg, "rg_llm_retries", DEFAULT_RG_LLM_RETRIES)
    attempts = max(1, retries + 1)
    last_err: Optional[str] = None
    for attempt in range(attempts):
        try:
            specs = llm_caller(batch_rounds, cfg)
        except Exception as e:  # noqa: BLE001
            last_err = f"caller 异常({type(e).__name__}): {e}"
            log.warning(f"[RECORD] compose LLM 第{attempt + 1}次失败: {last_err}")
            continue
        if isinstance(specs, list) and specs:
            return True, specs
        last_err = "caller 返回空 / 非列表"
        log.warning(f"[RECORD] compose LLM 第{attempt + 1}次无产出")
    return False, []


def _specs_to_groups(
    specs: List[Dict[str, Any]],
    start_rg_num: int,
    window_rounds: List[Dict[str, Any]],
    *,
    oversize: bool,
    cfg: Optional[Dict[str, Any]],
    now_round: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把 caller 产出的 GroupSpec 落成 round_groups 项（分配 rg_id + 校验区间 + 封档）。

    - rg_id 从 start_rg_num 单调递增（接在回滚点保留组之后；mcp 注解「重编号 = 组内
      展示软号重排，不碰硬 round_id」）。
    - round_range 取 spec.round_start/end（int）；越界本批窗口轮 → 记错（触发兜底）。
    - tier 默认 full（分级在批3 接线）。token_est 由窗口轮汇总，供 force_seal 判 token。
    - force_seal_check 任一达标 → sealed=True；oversize 批强制单组 + 强制 sealed。
    返回 (groups, errors)。
    """
    errors: List[str] = []
    win_ints = {int(r["round_int"]) for r in window_rounds}
    win_min = min(win_ints) if win_ints else None
    win_max = max(win_ints) if win_ints else None

    # token 汇总：按 round_int → token_est，便于按组区间求和
    tok_by_round = {int(r["round_int"]): int(r.get("token_est", 0) or 0)
                    for r in window_rounds}

    groups: List[Dict[str, Any]] = []
    n = start_rg_num
    for i, sp in enumerate(specs):
        if not isinstance(sp, dict):
            errors.append(f"spec[{i}] 非 dict")
            continue
        s = _coerce_round_int(sp.get("round_start"))
        e = _coerce_round_int(sp.get("round_end"))
        if s is None or e is None or s > e:
            errors.append(f"spec[{i}] round 区间非法: start={sp.get('round_start')} "
                          f"end={sp.get('round_end')}")
            continue
        if win_min is not None and (s < win_min or e > win_max):
            errors.append(f"spec[{i}] 区间[{s},{e}]越界窗口[{win_min},{win_max}]")
            continue
        tok_sum = sum(v for rk, v in tok_by_round.items() if s <= rk <= e)
        g: Dict[str, Any] = {
            "rg_id": format_rg_id(n),
            "round_range": [s, e],
            "tier": TIER_FULL,
            "sealed": False,
            "legacy_rg": False,
            "full_text": sp.get("full_text") or "",
            "summary_text": sp.get("summary_text") or "",
            "title": sp.get("title") or "",
            "token_est": int(tok_sum),
        }
        # 强制封档：oversize 巨轮组 / 达硬阈值
        if oversize or force_seal_check(g, cfg, now_round=now_round):
            g["sealed"] = True
        groups.append(g)
        n += 1

    if oversize and len(groups) > 1:
        # 巨轮 step fallback 约定：oversize 批应只产 1 个组；多于 1 视为 caller 违约
        errors.append(f"oversize 批产出 {len(groups)} 组（应单组）")

    return groups, errors


def compose_record(
    checkpoints_dir: str,
    window_key: str,
    messages: List[Dict[str, Any]],
    prev_state: Optional[Dict[str, Any]],
    llm_caller,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    now_round: Optional[int] = None,
    rollback_count: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> ComposeResult:
    """D3 核心：round-group 确定性增量聚合（照 mcp selectLocalComposeBoundary）。

    **确定性回滚重写窗口**（代码算，不问模型）：
      1. messages 按 round_id 聚合成有序轮；消费 last_grouped 锚做增量起点，
         只取「水位之后的新增未聚合轮」。
      2. 代码回滚最后 N 个【可回滚】组（默认 1；尾组短 + 新增≥10 → 2；已 sealed/legacy
         组绝不回滚）。重写窗口 = 回滚组覆盖轮 + 新增未聚合轮。
      3. 模型只在窗口内重新分段（按预算切批后调 llm_caller 产 GroupSpec），**绝不逐轮问**。
      4. 新组 force_seal 强制收敛 + validate 门禁（复用批1a）+ 候选隔离写（复用批1a）。

    返回 ComposeResult。无新增轮 → wrote=False 不变；LLM 失败 / 门禁拒收 → wrote=False
    + fallback/errors，**维持未分组态**（回传未改动 prev round_groups，绝不破坏已有状态）。
    """
    log = logger or _DEFAULT_LOGGER
    prev_state = prev_state or {}
    prev_groups = [g for g in (prev_state.get("round_groups") or [])
                   if isinstance(g, dict)]
    prev_last_rg = prev_state.get("last_grouped_rg_id")
    existing_md = read_record(checkpoints_dir, window_key)

    def _unchanged(
        errors: Optional[List[str]] = None,
        *,
        fallback: bool = False,
        cooldown: bool = False,
    ) -> ComposeResult:
        cd = None
        if cooldown:
            import time as _t
            cd = _t.time() + _cfg_int(cfg, "rg_cooldown_s", DEFAULT_RG_COOLDOWN_S)
        return ComposeResult(
            wrote=False,
            round_groups=prev_groups,
            last_grouped_rg_id=prev_last_rg,
            record_md=existing_md,
            errors=errors,
            fallback=fallback,
            cooldown_until=cd,
        )

    # ---- 1) 轮聚合 + 增量起点 ----
    rounds = _rounds_from_messages(messages)
    if not rounds:
        return _unchanged()
    watermark = _grouped_round_watermark(prev_state)
    new_rounds = [r for r in rounds
                  if watermark is None or r["round_int"] > watermark]
    if not new_rounds:
        return _unchanged()  # 无新增未聚合轮

    # ---- 2) 确定性划回滚窗口 ----
    prefix, rollbackable = _partition_groups(prev_groups)
    if rollback_count is None:
        rb = _decide_rollback_count(rollbackable, len(new_rounds), cfg)
    else:
        rb = max(0, min(int(rollback_count), len(rollbackable)))
    rolled = rollbackable[-rb:] if rb > 0 else []
    kept = prefix + (rollbackable[:len(rollbackable) - rb] if rb > 0 else rollbackable)

    # 窗口起点：被回滚组的最小起点；无回滚则纯新增（水位 + 1）
    window_start = None
    if rolled:
        starts = [_coerce_round_int((g.get("round_range") or [None])[0]) for g in rolled]
        starts = [x for x in starts if x is not None]
        if starts:
            window_start = min(starts)
    if window_start is None:
        window_start = new_rounds[0]["round_int"]

    window_rounds = [r for r in rounds if r["round_int"] >= window_start]
    if not window_rounds:
        return _unchanged()

    # ---- 3) 预算切批 + 模型窗内分段（绝不逐轮问）----
    batches = _split_window_into_batches(window_rounds, cfg)
    start_rg_num = _next_rg_num(kept)
    new_window_groups: List[Dict[str, Any]] = []
    now_r = now_round if now_round is not None else window_rounds[-1]["round_int"]

    for batch in batches:
        b_rounds = batch["rounds"]
        ok, specs = _call_llm_with_retry(llm_caller, b_rounds, cfg, log)
        if not ok:
            log.warning(f"[RECORD] compose 批失败兜底 {window_key}: 维持未分组态")
            return _unchanged(["llm_failed"], fallback=True, cooldown=True)
        g_list, g_errs = _specs_to_groups(
            specs, start_rg_num + len(new_window_groups), b_rounds,
            oversize=batch["oversize"], cfg=cfg, now_round=now_r,
        )
        if g_errs:
            log.warning(f"[RECORD] compose spec 落组失败 {window_key}: {g_errs}")
            return _unchanged(g_errs, fallback=True)
        new_window_groups.extend(g_list)

    if not new_window_groups:
        return _unchanged(["no_groups"], fallback=True)

    new_round_groups = kept + new_window_groups

    # ---- 4) validate 门禁（针对窗口新组，effective_prev 水位降到回滚点之前）----
    effective_prev = {
        "round_groups": kept,
        "last_grouped_rg_id": kept[-1]["rg_id"] if kept else None,
    }
    ok, errs = validate_composed_record(new_window_groups, effective_prev)
    if not ok:
        log.warning(f"[RECORD] compose 门禁拒收 {window_key}: {errs}")
        return _unchanged(errs)

    # ---- 5) 渲染 + 候选隔离写 ----
    record_md = render_record_md(new_round_groups)
    wrote_ok, w_errs = write_record_atomic(
        checkpoints_dir, window_key, record_md,
        effective_prev, candidate_index=new_window_groups, logger=log,
    )
    if not wrote_ok:
        return _unchanged(w_errs)

    last_rg = new_round_groups[-1]["rg_id"] if new_round_groups else prev_last_rg
    log.info(
        f"[RECORD] compose 成功 {window_key}: kept={len(kept)} "
        f"new={len(new_window_groups)} rollback={rb} last_grouped={last_rg}"
    )
    return ComposeResult(
        wrote=True,
        round_groups=new_round_groups,
        last_grouped_rg_id=last_rg,
        record_md=record_md,
    )


def _next_rg_num(kept_groups: List[Dict[str, Any]]) -> int:
    """新组 rg_id 起始编号 = 保留组中最大 rg 号 + 1（无则从 1 起）。"""
    mx = 0
    for g in kept_groups:
        n = parse_rg_id(g.get("rg_id"))
        if n > mx:
            mx = n
    return mx + 1


# ========================
# 批2b R4：LLM 分段 prompt 构造 + 跨 provider 健壮 JSON 解析（纯逻辑、可单测）
# ========================
# compose_record 的 llm_caller 契约要 caller 在「给定一批轮」内吐 GroupSpec 列表。
# 真接线时 caller 把这批轮渲成 prompt 调 _call_flash_lite（返回纯文本），再解析回
# GroupSpec。两步都是纯逻辑：prompt 构造确定（同输入同 prompt）、解析容错（跨 provider
# JSON 漂移：裸 JSON / ```json 围栏 / 前后赘述 / 单引号 / 尾逗号 全兜底）。

_SEGMENT_PROMPT_HEADER = (
    "你是对话归档器。下面给出若干『轮』（每轮含轮号 round 与文本），请把它们划分成"
    "若干连续的 round-group（话题段），并为每段产出一句话标题、完整正文、精炼摘要。\n\n"
    "硬规则（必须遵守，违反将被丢弃）：\n"
    "1. 只在给定轮号范围内分段，round_start/round_end 必须是本批出现过的轮号整数。\n"
    "2. 各段【连续且不重叠】：按轮号升序，后段 round_start = 前段 round_end + 1，"
    "首段 round_start = 最小轮号，末段 round_end = 最大轮号，全程无空洞。\n"
    "3. 每段 3-12 轮为宜；话题明显切换处断段；拿不准就少分段（宁可粗不可乱）。\n"
    "4. full_text 保留关键事实/结论/待办；summary_text 更短（约 full 的一半）；"
    "title 一句话概括。都用中文。\n\n"
    "只输出一个 JSON 数组，不要任何解释/Markdown 围栏。格式：\n"
    '[{"round_start":<int>,"round_end":<int>,"title":"...","full_text":"...","summary_text":"..."}]\n\n'
    "=== 待分段的轮 ===\n"
)


def build_segment_prompt(
    batch_rounds: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """把一批轮（RoundView）渲成分段 prompt（确定性，同输入同 prompt）。

    每轮渲一行 `[round N] <text>`；text 过长按 cfg.rg_round_text_cap（默认 4000 字符）
    截断（防单轮巨文撑爆 prompt——巨轮已在 _split_window_into_batches 独占批，这里再兜底
    一层）。轮按 round_int 升序。
    """
    cap = _cfg_int(cfg, "rg_round_text_cap", 4000)
    lines: List[str] = [_SEGMENT_PROMPT_HEADER]
    target = _cfg_int(cfg, "rg_target_rounds", DEFAULT_RG_TARGET_ROUNDS)
    lines.append(f"（目标：每段约 {target} 轮，话题切换处可断段）\n")
    for r in sorted(batch_rounds, key=lambda x: int(x.get("round_int", 0))):
        rn = int(r.get("round_int", 0))
        text = r.get("text") or ""
        if cap and len(text) > cap:
            text = text[:cap] + " …(截断)"
        lines.append(f"[round {rn}] {text}")
    return "\n".join(lines)


def _strip_json_envelope(raw: str) -> str:
    """剥离常见非 JSON 包裹：```json 围栏、前后赘述，定位第一个 [ 到最后一个 ]。"""
    if not raw:
        return ""
    s = raw.strip()
    # 去 markdown 围栏
    if "```" in s:
        # 取第一个围栏块内内容
        parts = s.split("```")
        for p in parts:
            p2 = p.strip()
            if p2.lower().startswith("json"):
                p2 = p2[4:].strip()
            if p2.startswith("[") or p2.startswith("{"):
                s = p2
                break
    # 定位最外层数组 [ ... ]
    li = s.find("[")
    ri = s.rfind("]")
    if li != -1 and ri != -1 and ri > li:
        return s[li:ri + 1]
    # 退化：单对象 { ... } 包成数组
    lo = s.find("{")
    ro = s.rfind("}")
    if lo != -1 and ro != -1 and ro > lo:
        return "[" + s[lo:ro + 1] + "]"
    return s


def _loose_json_loads(text: str) -> Optional[Any]:
    """容错 JSON 解析：先严格 json.loads，失败则修常见漂移（尾逗号 / 单引号）再试。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    import re as _re
    fixed = text
    # 去对象/数组尾逗号： ,}  ,]
    fixed = _re.sub(r",\s*([}\]])", r"\1", fixed)
    try:
        return json.loads(fixed)
    except Exception:
        pass
    # 单引号 → 双引号（粗暴兜底，仅当不含双引号时安全）
    if '"' not in fixed and "'" in fixed:
        try:
            return json.loads(fixed.replace("'", '"'))
        except Exception:
            pass
    return None


def parse_group_specs(
    raw_text: str,
    batch_rounds: List[Dict[str, Any]],
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """把模型返回的纯文本解析成 GroupSpec 列表（跨 provider 健壮）。

    流程：剥包裹 → 容错 loads → 规整每项字段（round_start/end 强转 int，文本兜底空串）。
    无法解析 / 非列表 / 空 → 返回 []（触发 compose 失败兜底，不写盘）。
    单项缺字段但 round_start/end 可解析仍保留（full/summary/title 缺省空）。
    """
    log = logger or _DEFAULT_LOGGER
    stripped = _strip_json_envelope(raw_text or "")
    data = _loose_json_loads(stripped)
    if data is None:
        log.warning("[RECORD] LLM 分段响应无法解析为 JSON（已尝试容错）")
        return []
    if isinstance(data, dict):
        # 单对象或 {"groups":[...]} 包裹
        if isinstance(data.get("groups"), list):
            data = data["groups"]
        else:
            data = [data]
    if not isinstance(data, list):
        log.warning("[RECORD] LLM 分段响应非数组")
        return []

    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        s = _coerce_round_int(item.get("round_start"))
        e = _coerce_round_int(item.get("round_end"))
        if s is None or e is None:
            continue
        out.append({
            "round_start": s,
            "round_end": e,
            "title": str(item.get("title") or "")[:200],
            "full_text": str(item.get("full_text") or item.get("full") or ""),
            "summary_text": str(item.get("summary_text")
                                 or item.get("summary") or ""),
        })
    return out


# ========================
# M1 RecordStore（地基薄封装：把 checkpoints_dir 绑定到实例，方法转发上面纯函数）
# ========================
class RecordStore:
    """record.md / sidecar 的实例化门面。

    地基批（批1a）只承载读写/渲染/门禁/sidecar；**不实现** compose_record（D3 聚合，
    批2）与 force_seal（D3 强制收敛，批2）。compose 接线时会复用 checkpoint 的
    _get_lock / round_segmenting 锁（批2 注入），地基这里不持锁、纯文件操作。
    """

    def __init__(
        self,
        checkpoints_dir: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.checkpoints_dir = checkpoints_dir
        self.logger = logger or _DEFAULT_LOGGER

    # ---- 路径 ----
    def md_path(self, window_key: str) -> str:
        return record_md_path(self.checkpoints_dir, window_key)

    def index_path(self, window_key: str) -> str:
        return record_index_path(self.checkpoints_dir, window_key)

    # ---- 读 ----
    def read_record(self, window_key: str) -> str:
        return read_record(self.checkpoints_dir, window_key)

    def load_index(self, window_key: str) -> Dict[str, Any]:
        return load_index(self.checkpoints_dir, window_key)

    # ---- 写（候选隔离）----
    def write_record_atomic(
        self,
        window_key: str,
        candidate_text: str,
        prev_state: Optional[Dict[str, Any]] = None,
        *,
        candidate_index: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, List[str]]:
        return write_record_atomic(
            self.checkpoints_dir,
            window_key,
            candidate_text,
            prev_state,
            candidate_index=candidate_index,
            logger=self.logger,
        )

    def save_index(self, window_key: str, index: Dict[str, Any]) -> None:
        save_index(self.checkpoints_dir, window_key, index)

    # ---- 渲染 / 门禁 / 重建 ----
    @staticmethod
    def render_record_md(
        rg_index: List[Dict[str, Any]],
        tier_map: Optional[Dict[str, str]] = None,
    ) -> str:
        return render_record_md(rg_index, tier_map)

    @staticmethod
    def validate_composed_record(
        candidate: List[Dict[str, Any]],
        prev_state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, List[str]]:
        return validate_composed_record(candidate, prev_state)

    def rebuild_index_if_stale(
        self,
        window_key: str,
        record_state: Dict[str, Any],
        *,
        generation: int = 0,
    ) -> Tuple[bool, Dict[str, Any]]:
        return rebuild_index_if_stale(
            self.checkpoints_dir,
            window_key,
            record_state,
            generation=generation,
            logger=self.logger,
        )

    # ---- 批2a：确定性聚合 / 强制收敛 ----
    def compose_record(
        self,
        window_key: str,
        messages: List[Dict[str, Any]],
        prev_state: Optional[Dict[str, Any]],
        llm_caller,
        cfg: Optional[Dict[str, Any]] = None,
        *,
        now_round: Optional[int] = None,
        rollback_count: Optional[int] = None,
    ) -> "ComposeResult":
        return compose_record(
            self.checkpoints_dir,
            window_key,
            messages,
            prev_state,
            llm_caller,
            cfg,
            now_round=now_round,
            rollback_count=rollback_count,
            logger=self.logger,
        )

    @staticmethod
    def force_seal_check(
        group: Dict[str, Any],
        cfg: Optional[Dict[str, Any]] = None,
        *,
        now_round: Optional[int] = None,
    ) -> bool:
        return force_seal_check(group, cfg, now_round=now_round)

    # ---- 批3 D7：分级定档 / D8：summary 封板 ----
    @staticmethod
    def tier_for_group(
        group: Dict[str, Any],
        now_round: Optional[int],
        hit_table: Optional[Dict[str, Any]] = None,
        cfg: Optional[Dict[str, Any]] = None,
        *,
        now_ts: Optional[float] = None,
        prev_tier: Optional[str] = None,
    ) -> str:
        return tier_for_group(
            group, now_round, hit_table, cfg, now_ts=now_ts, prev_tier=prev_tier
        )

    @staticmethod
    def build_tier_map(
        round_groups: List[Dict[str, Any]],
        now_round: Optional[int],
        hit_table: Optional[Dict[str, Any]] = None,
        cfg: Optional[Dict[str, Any]] = None,
        *,
        now_ts: Optional[float] = None,
    ) -> Dict[str, str]:
        return build_tier_map(
            round_groups, now_round, hit_table, cfg, now_ts=now_ts
        )

    @staticmethod
    def hit_score(
        rg_id: Optional[str],
        hit_table: Optional[Dict[str, Any]],
        now_ts: float,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> float:
        return hit_score(rg_id, hit_table, now_ts, cfg)

    # ---- 批4 M4 / D9 / D10：hit 命中记录 / 锁定 / 迁移 / 映射 ----
    @staticmethod
    def hit_keep_active(
        rg_id: Optional[str],
        hit_table: Optional[Dict[str, Any]],
        now_round: Optional[int],
        cfg: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return hit_keep_active(rg_id, hit_table, now_round, cfg)

    @staticmethod
    def apply_hit_to_table(
        hit_table: Dict[str, Any],
        rg_id: str,
        hit_type: str,
        now_ts: float,
        now_round: Optional[int] = None,
    ) -> Dict[str, Any]:
        return apply_hit_to_table(hit_table, rg_id, hit_type, now_ts, now_round)

    @staticmethod
    def round_id_to_rg_id(
        round_int: int,
        round_groups: Optional[List[Dict[str, Any]]],
    ) -> Optional[str]:
        return round_id_to_rg_id(round_int, round_groups)

    @staticmethod
    def migrate_hit_table_on_renumber(
        old_hit_table: Optional[Dict[str, Any]],
        old_groups: Optional[List[Dict[str, Any]]],
        new_groups: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        return migrate_hit_table_on_renumber(old_hit_table, old_groups, new_groups)

    def generate_summaries_for_sealed(
        self,
        round_groups: List[Dict[str, Any]],
        prev_watermark_rg_id: Optional[str],
        summary_caller,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], List[str]]:
        return generate_summaries_for_sealed(
            round_groups, prev_watermark_rg_id, summary_caller, cfg,
            logger=self.logger,
        )

    @staticmethod
    def group_has_summary_gap(
        group: Dict[str, Any],
        watermark_rg_id: Optional[str],
    ) -> bool:
        return group_has_summary_gap(group, watermark_rg_id)
