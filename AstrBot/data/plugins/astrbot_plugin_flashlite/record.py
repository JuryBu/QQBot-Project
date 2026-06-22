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

本批（批1a）只做【地基】：
  - 读写（read_record / write_record_atomic 候选隔离）
  - 渲染（render_record_md）
  - 门禁（validate_composed_record）
  - sidecar（load_index / save_index / rebuild_index_if_stale）
**不实现** compose_record（D3 Local Compose 增量聚合，批2）、force_seal（D3 强制收敛，批2）。

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
