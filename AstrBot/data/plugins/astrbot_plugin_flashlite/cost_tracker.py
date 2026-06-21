"""
CostTracker — 实时 API 成本追踪与统计模块

基于 Gemini API 响应的 usageMetadata 字段记录每次调用的 token 用量，
结合内置定价表计算费用，按天归档保留 90 天历史。

数据流：
1. 每次 API 调用后 → record() 记录一条
2. 面板请求 → get_summary() / get_by_model() / get_by_window() 获取聚合统计
3. 按天归档 → 自动清理 90 天前数据
"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

logger = logging.getLogger("flashlite.cost_tracker")

# ========================
# 内置定价表（$/M tokens）
# 数据来源：QQBotPlan/参考材料_Gemini_API_定价表.md
# 最后更新：2026-04-13
# ========================
PRICING: Dict[str, Any] = {
    # === 分级定价模型（prompt ≤/> 20万 token 分档）===
    "gemini-3.1-pro-preview": {
        "tiers": [
            {"threshold": 200_000, "input": 2.00, "input_cached": 0.20, "output": 12.00},
            {"threshold": float('inf'), "input": 4.00, "input_cached": 0.40, "output": 18.00},
        ],
        "storage": 4.50,
    },
    "gemini-2.5-pro": {
        "tiers": [
            {"threshold": 200_000, "input": 1.25, "input_cached": 0.125, "output": 10.00},
            {"threshold": float('inf'), "input": 2.50, "input_cached": 0.25, "output": 15.00},
        ],
        "storage": 4.50,
    },
    # === 扁平定价模型 ===
    "gemini-3.1-flash-lite-preview": {
        "input": 0.25, "input_cached": 0.025, "output": 1.50, "storage": 1.00,
    },
    "gemini-3-flash-preview": {
        "input": 0.50, "input_cached": 0.05, "output": 3.00, "storage": 1.00,
    },
    "gemini-2.5-flash": {
        "input": 0.30, "input_cached": 0.03, "output": 2.50, "storage": 1.00,
    },
    "gemini-2.5-flash-lite": {
        "input": 0.10, "input_cached": 0.01, "output": 0.40, "storage": 1.00,
    },
    "gemini-2.0-flash": {
        "input": 0.10, "input_cached": 0.025, "output": 0.40, "storage": 1.00,
    },
    "gemini-2.0-flash-lite": {
        "input": 0.075, "input_cached": 0, "output": 0.30, "storage": 0,
    },
    # Image 模型
    "gemini-3.1-flash-image-preview": {
        "input": 0.50, "input_cached": 0, "output": 3.00, "storage": 0,
    },
    "gemini-2.5-flash-image": {
        "input": 0.30, "input_cached": 0, "output": 2.50, "storage": 0,
    },
    "gemini-3-pro-image-preview": {
        "input": 2.00, "input_cached": 0, "output": 12.00, "storage": 0,
    },
}

# 别名映射（latest / customtools 等）
_ALIASES: Dict[str, str] = {
    "gemini-flash-latest": "gemini-2.5-flash",
    "gemini-flash-lite-latest": "gemini-2.5-flash-lite",
    "gemini-pro-latest": "gemini-2.5-pro",
    "gemini-3.1-pro-preview-customtools": "gemini-3.1-pro-preview",
}

# 默认定价（未知模型的兜底，采用偏高估值防止漏计）
DEFAULT_PRICING: Dict[str, float] = {
    "input": 1.25,
    "input_cached": 0.125,
    "output": 10.00,
    "storage": 4.50,
}


class CostTracker:
    """API 成本追踪器
    
    支持：
    - record(): 记录单次 API 调用
    - get_summary(): 概览统计（今日/本周/本月）
    - get_by_model(): 按模型分组统计
    - get_by_window(): 按窗口分组统计
    - get_cache_hit_rate(): 缓存命中率
    - get_timeline(): 时间轴数据
    """
    
    def __init__(self, data_dir: str, usd_to_cny: float = 7.2, custom_pricing: Optional[Dict] = None):
        """
        Args:
            data_dir: 数据存储目录（如 Sandbox/cost_logs/）
            usd_to_cny: 美元兑人民币汇率
            custom_pricing: 自定义定价覆盖
        """
        self._data_dir = data_dir
        self._usd_to_cny = usd_to_cny
        self._pricing = {**PRICING}
        if custom_pricing:
            self._pricing.update(custom_pricing)
        
        # 内存缓存：当天的记录（避免频繁读磁盘）
        self._today_key = ""
        self._today_records: List[Dict] = []
        
        # 统计缓存
        self._stats_cache: Dict[str, Any] = {}
        self._stats_cache_time: float = 0
        self._stats_cache_ttl: float = 10.0  # 10秒缓存
        
        # 写入锁
        self._write_lock = asyncio.Lock()
        
        os.makedirs(data_dir, exist_ok=True)
        self._load_today()
        self._flush_handle = None  # debounce 调度句柄
        
        # 启动时清理旧数据（延迟 30 秒执行，不阻塞启动）
        try:
            loop = asyncio.get_event_loop()
            loop.call_later(30, lambda: asyncio.create_task(self.cleanup_old(90)))
        except RuntimeError:
            pass
        
        # 注册进程退出时的同步刷盘（防止 debounce 期间的数据丢失）
        import atexit
        atexit.register(self._sync_flush_on_exit)
    
    def _load_today(self):
        """加载当天数据到内存"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._today_key == today:
            return  # 已加载
        
        self._today_key = today
        filepath = os.path.join(self._data_dir, f"{today}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    self._today_records = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._today_records = []
        else:
            self._today_records = []
    
    def _resolve_pricing(self, model: str) -> dict:
        """模型名 → 定价条目（精确匹配 → 别名 → 前缀 → 兜底）"""
        # 1. 精确匹配
        if model in self._pricing:
            return self._pricing[model]
        # 2. 别名映射
        if model in _ALIASES:
            return self._pricing.get(_ALIASES[model], DEFAULT_PRICING)
        # 3. 前缀匹配（处理 -001 / -preview-xxx 后缀变体）
        for key in self._pricing:
            base = key.rsplit("-preview", 1)[0] if "-preview" in key else key
            if model.startswith(base):
                return self._pricing[key]
        # 4. 兜底
        logger.warning(f"模型 '{model}' 未匹配到定价条目，使用兜底价格")
        return DEFAULT_PRICING

    def _calc_cost(self, model: str, prompt_tokens: int, cached_tokens: int, output_tokens: int) -> tuple:
        """计算单次调用成本（USD），支持分级定价 + 存储费
        
        Returns:
            (total_cost, storage_cost) — 总成本和存储费分项
            storage_cost 按 cached_tokens 的最低 1 小时存储计算
        """
        pricing = self._resolve_pricing(model)
        
        # 分级定价 vs 扁平定价
        if "tiers" in pricing:
            tier = next(t for t in pricing["tiers"] if prompt_tokens <= t["threshold"])
            input_rate = tier["input"]
            cached_rate = tier["input_cached"]
            output_rate = tier["output"]
        else:
            input_rate = pricing["input"]
            cached_rate = pricing["input_cached"]
            output_rate = pricing["output"]
        
        uncached_input = max(0, prompt_tokens - cached_tokens)
        
        # 计算请求费
        request_cost = (
            uncached_input * input_rate / 1_000_000
            + cached_tokens * cached_rate / 1_000_000
            + output_tokens * output_rate / 1_000_000
        )
        
        return round(request_cost, 8)
    
    async def record_storage(
        self,
        model: str,
        cached_token_count: int,
        ttl_seconds: int,
    ):
        """记录一次缓存存储费（仅在 cache 创建/重建时调用）
        
        存储费 = cached_token_count × storage_rate × ttl_hours
        这是事件驱动的：每个 cache 生命周期只记一次。
        
        Args:
            model: 模型名
            cached_token_count: 缓存的 token 总数（来自 usageMetadata.totalTokenCount）
            ttl_seconds: TTL 秒数
        """
        pricing = self._resolve_pricing(model)
        storage_rate = pricing.get("storage", 0)
        if storage_rate <= 0 or cached_token_count <= 0:
            return
        
        ttl_hours = max(ttl_seconds / 3600, 1)  # 最小1小时（Google计费最小单位）
        storage_cost = round(cached_token_count * storage_rate / 1_000_000 * ttl_hours, 8)
        
        now = datetime.now()
        record = {
            "timestamp": now.isoformat(),
            "model": model,
            "call_type": "cache_storage",
            "window_key": "system",
            "prompt_tokens": 0,
            "cached_tokens": cached_token_count,
            "output_tokens": 0,
            "cost_usd": storage_cost,
            "storage_cost_usd": storage_cost,
        }
        
        today = now.strftime("%Y-%m-%d")
        if self._today_key != today:
            await self._flush()
            self._today_key = today
            self._today_records = []
        
        self._today_records.append(record)
        self._invalidate_cache()
        
        logger.info(
            f"[COST] 缓存存储费: model={model}, tokens={cached_token_count}, "
            f"ttl={ttl_seconds}s, cost=${storage_cost}"
        )
    
    async def record(
        self,
        model: str,
        call_type: str,
        window_key: str,
        prompt_tokens: int,
        cached_tokens: int,
        output_tokens: int,
    ):
        """异步记录单次 API 调用（不含存储费，存储费由 record_storage 负责）
        
        Args:
            model: 模型名
            call_type: flashlite_judge / flashlite_compress / main_model / tool_model
            window_key: 窗口标识 (如 GroupMessage:123456)
            prompt_tokens: 输入 token 数
            cached_tokens: 缓存命中 token 数
            output_tokens: 输出 token 数
        """
        now = datetime.now()
        cost_usd = self._calc_cost(model, prompt_tokens, cached_tokens, output_tokens)
        
        record = {
            "timestamp": now.isoformat(),
            "model": model,
            "call_type": call_type,
            "window_key": window_key,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "storage_cost_usd": 0,  # 请求本身不产生存储费
        }
        
        # 更新内存缓存
        today = now.strftime("%Y-%m-%d")
        if self._today_key != today:
            # 跨天，先刷盘再切换
            await self._flush()
            self._today_key = today
            self._today_records = []
        
        self._today_records.append(record)
        self._invalidate_cache()
        
        # Debounce 刷盘：5秒内只触发一次写入
        self._schedule_flush()
    
    def _schedule_flush(self):
        """调度延迟刷盘（debounce 5秒）"""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        try:
            loop = asyncio.get_event_loop()
            self._flush_handle = loop.call_later(5, lambda: asyncio.create_task(self._flush()))
        except RuntimeError:
            pass  # 没有事件循环时跳过调度
    
    async def _flush(self):
        """将当天数据写入磁盘（使用 to_thread 避免阻塞事件循环）"""
        async with self._write_lock:
            if not self._today_records:
                return
            filepath = os.path.join(self._data_dir, f"{self._today_key}.json")
            data_copy = list(self._today_records)  # 快照
            try:
                await asyncio.to_thread(self._write_json, filepath, data_copy)
            except (IOError, AttributeError) as e:
                # Python 3.8 没有 to_thread，降级同步写
                self._write_json(filepath, data_copy)
    
    async def shutdown(self):
        """停机前最终 flush：取消 pending debounce 并强制刷盘"""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        await self._flush()
    
    def _sync_flush_on_exit(self):
        """atexit 回调：同步刷盘残余数据"""
        if not self._today_records:
            return
        filepath = os.path.join(self._data_dir, f"{self._today_key}.json")
        self._write_json(filepath, list(self._today_records))
    
    @staticmethod
    def _write_json(filepath: str, data: list):
        """同步写 JSON（在线程内执行）"""
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except IOError as e:
            logger.error(f"CostTracker 写入失败: {e}")
    
    def _invalidate_cache(self):
        """失效统计缓存"""
        self._stats_cache_time = 0
    
    def _load_records(self, start_date: datetime, end_date: datetime) -> List[Dict]:
        """加载日期范围内的所有记录"""
        records = []
        current = start_date
        while current <= end_date:
            day_key = current.strftime("%Y-%m-%d")
            if day_key == self._today_key:
                records.extend(self._today_records)
            else:
                filepath = os.path.join(self._data_dir, f"{day_key}.json")
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            records.extend(json.load(f))
                    except (json.JSONDecodeError, IOError):
                        pass
            current += timedelta(days=1)
        return records
    
    def _get_period_range(self, period: str) -> tuple:
        """根据 period 返回 (start_date, end_date)"""
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        if period == "today":
            return (today, now)
        elif period == "week":
            start = today - timedelta(days=today.weekday())  # 本周一
            return (start, now)
        elif period == "month":
            start = today.replace(day=1)
            return (start, now)
        elif period == "all":
            return (today - timedelta(days=90), now)
        else:
            return (today, now)
    
    def get_summary(self, period: str = "today") -> Dict[str, Any]:
        """概览统计
        
        Returns:
            {
                "total_cost_usd": float,
                "total_cost_cny": float,
                "total_calls": int,
                "total_prompt_tokens": int,
                "total_cached_tokens": int,
                "total_output_tokens": int,
                "cache_hit_rate": float,  # 0-100%
            }
        """
        start, end = self._get_period_range(period)
        records = self._load_records(start, end)
        
        total_cost = sum(r["cost_usd"] for r in records)
        total_prompt = sum(r["prompt_tokens"] for r in records)
        total_cached = sum(r["cached_tokens"] for r in records)
        total_output = sum(r["output_tokens"] for r in records)
        
        cache_hit_rate = (total_cached / total_prompt * 100) if total_prompt > 0 else 0
        
        return {
            "period": period,
            "total_cost_usd": round(total_cost, 6),
            "total_cost_cny": round(total_cost * self._usd_to_cny, 4),
            "total_calls": len(records),
            "total_prompt_tokens": total_prompt,
            "total_cached_tokens": total_cached,
            "total_output_tokens": total_output,
            "cache_hit_rate": round(cache_hit_rate, 1),
        }
    
    def get_by_model(self, period: str = "today") -> List[Dict]:
        """按模型分组统计"""
        start, end = self._get_period_range(period)
        records = self._load_records(start, end)
        
        groups: Dict[str, Dict] = defaultdict(lambda: {
            "calls": 0, "prompt_tokens": 0, "cached_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0,
        })
        
        for r in records:
            g = groups[r["model"]]
            g["calls"] += 1
            g["prompt_tokens"] += r["prompt_tokens"]
            g["cached_tokens"] += r["cached_tokens"]
            g["output_tokens"] += r["output_tokens"]
            g["cost_usd"] += r["cost_usd"]
        
        result = []
        for model, g in sorted(groups.items(), key=lambda x: -x[1]["cost_usd"]):
            cache_rate = (g["cached_tokens"] / g["prompt_tokens"] * 100) if g["prompt_tokens"] > 0 else 0
            result.append({
                "model": model,
                "calls": g["calls"],
                "prompt_tokens": g["prompt_tokens"],
                "cached_tokens": g["cached_tokens"],
                "output_tokens": g["output_tokens"],
                "cache_hit_rate": round(cache_rate, 1),
                "cost_usd": round(g["cost_usd"], 6),
                "cost_cny": round(g["cost_usd"] * self._usd_to_cny, 4),
            })
        return result
    
    def get_by_window(self, period: str = "today") -> List[Dict]:
        """按窗口分组统计"""
        start, end = self._get_period_range(period)
        records = self._load_records(start, end)
        
        groups: Dict[str, Dict] = defaultdict(lambda: {
            "calls": 0, "prompt_tokens": 0, "cached_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0,
            "call_types": defaultdict(int),
        })
        
        for r in records:
            g = groups[r["window_key"]]
            g["calls"] += 1
            g["prompt_tokens"] += r["prompt_tokens"]
            g["cached_tokens"] += r["cached_tokens"]
            g["output_tokens"] += r["output_tokens"]
            g["cost_usd"] += r["cost_usd"]
            g["call_types"][r["call_type"]] += 1
        
        result = []
        for window, g in sorted(groups.items(), key=lambda x: -x[1]["cost_usd"]):
            result.append({
                "window_key": window,
                "calls": g["calls"],
                "flashlite_calls": sum(v for k, v in g["call_types"].items() if k.startswith("flashlite")),
                "main_calls": sum(v for k, v in g["call_types"].items() if k.startswith("main_model")),
                "tool_calls": sum(v for k, v in g["call_types"].items() if k.startswith("tool_model")),
                "total_tokens": g["prompt_tokens"] + g["output_tokens"],
                "cost_usd": round(g["cost_usd"], 6),
                "cost_cny": round(g["cost_usd"] * self._usd_to_cny, 4),
            })
        return result
    
    def get_cache_hit_rate(self, period: str = "today") -> Dict[str, float]:
        """缓存命中率（按模型分）"""
        start, end = self._get_period_range(period)
        records = self._load_records(start, end)
        
        by_model: Dict[str, Dict] = defaultdict(lambda: {"prompt": 0, "cached": 0})
        for r in records:
            by_model[r["model"]]["prompt"] += r["prompt_tokens"]
            by_model[r["model"]]["cached"] += r["cached_tokens"]
        
        result = {}
        for model, d in by_model.items():
            rate = (d["cached"] / d["prompt"] * 100) if d["prompt"] > 0 else 0
            result[model] = round(rate, 1)
        
        # 总体
        total_prompt = sum(d["prompt"] for d in by_model.values())
        total_cached = sum(d["cached"] for d in by_model.values())
        result["_total"] = round((total_cached / total_prompt * 100) if total_prompt > 0 else 0, 1)
        
        return result
    
    def get_timeline(self, period: str = "today", granularity: str = "hour") -> List[Dict]:
        """时间轴数据
        
        Args:
            period: today / week / month
            granularity: hour / day
        
        Returns: [{time_key, calls, cost_usd, prompt_tokens, cached_tokens, output_tokens}, ...]
        """
        start, end = self._get_period_range(period)
        records = self._load_records(start, end)
        
        buckets: Dict[str, Dict] = defaultdict(lambda: {
            "calls": 0, "cost_usd": 0.0, "prompt_tokens": 0,
            "cached_tokens": 0, "output_tokens": 0,
        })
        
        for r in records:
            ts = datetime.fromisoformat(r["timestamp"])
            if granularity == "hour":
                key = ts.strftime("%Y-%m-%d %H:00")
            else:
                key = ts.strftime("%Y-%m-%d")
            
            b = buckets[key]
            b["calls"] += 1
            b["cost_usd"] += r["cost_usd"]
            b["prompt_tokens"] += r["prompt_tokens"]
            b["cached_tokens"] += r["cached_tokens"]
            b["output_tokens"] += r["output_tokens"]
        
        return [
            {"time_key": k, **v}
            for k, v in sorted(buckets.items())
        ]
    
    async def cleanup_old(self, days: int = 90):
        """清理超过指定天数的历史数据"""
        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        
        removed = 0
        for fname in os.listdir(self._data_dir):
            if fname.endswith(".json") and fname[:10] < cutoff_str:
                try:
                    os.remove(os.path.join(self._data_dir, fname))
                    removed += 1
                except IOError:
                    pass
        
        if removed:
            logger.info(f"CostTracker: 清理 {removed} 个过期数据文件")
    
    def update_pricing(self, model: str, pricing: Dict[str, float]):
        """更新指定模型的定价"""
        self._pricing[model] = pricing
    
    def update_exchange_rate(self, usd_to_cny: float):
        """更新汇率"""
        self._usd_to_cny = usd_to_cny
    
    def get_pricing(self) -> Dict:
        """获取当前定价表"""
        return {
            "pricing": self._pricing,
            "usd_to_cny": self._usd_to_cny,
        }
    
    def format_report(self, period: str = "today") -> str:
        """生成人类可读的成本报告（Markdown 格式）
        
        用于 Bot 对话命令回复或生成报告文件。
        """
        summary = self.get_summary(period)
        by_model = self.get_by_model(period)
        cache_rates = self.get_cache_hit_rate(period)
        
        period_names = {"today": "今日", "week": "本周", "month": "本月", "all": "全部"}
        period_name = period_names.get(period, period)
        
        lines = [
            f"## 📊 {period_name}成本报告",
            "",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 总成本 | ${summary['total_cost_usd']:.4f} (¥{summary['total_cost_cny']:.2f}) |",
            f"| API 调用次数 | {summary['total_calls']} |",
            f"| 输入 tokens | {summary['total_prompt_tokens']:,} |",
            f"| 缓存命中 tokens | {summary['total_cached_tokens']:,} |",
            f"| 输出 tokens | {summary['total_output_tokens']:,} |",
            f"| 缓存命中率 | {summary['cache_hit_rate']}% |",
        ]
        
        if by_model:
            lines.extend([
                "",
                "### 按模型分类",
                "",
                "| 模型 | 调用 | 缓存率 | 成本 |",
                "|------|------|--------|------|",
            ])
            for m in by_model:
                short_name = m["model"].split("-")[-1] if "-" in m["model"] else m["model"]
                lines.append(
                    f"| {short_name} | {m['calls']} | {m['cache_hit_rate']}% | ${m['cost_usd']:.4f} |"
                )
        
        return "\n".join(lines)

