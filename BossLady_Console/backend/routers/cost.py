"""
成本监控 API 路由
直接读取 CostTracker 产生的 JSON 日志文件，计算统计
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query

from .exchange_rate import get_exchange_rate_service

router = APIRouter()

# 项目根 → AstrBot/data/plugins/astrbot_plugin_flashlite/Sandbox/cost_logs/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ASTRBOT_DIR = PROJECT_ROOT / "AstrBot"
COST_LOGS_DIR = ASTRBOT_DIR / "data" / "plugins" / "astrbot_plugin_flashlite" / "Sandbox" / "cost_logs"


def _get_period_range(period: str):
    """返回 (start_date, end_date) 字符串"""
    today = datetime.now().date()
    if period == "week":
        start = today - timedelta(days=6)
    elif period == "month":
        start = today - timedelta(days=29)
    else:  # today
        start = today
    return start.isoformat(), today.isoformat()


def _load_records(period: str = "today") -> list:
    """加载指定时间段的所有记录"""
    if not COST_LOGS_DIR.exists():
        return []
    start_str, end_str = _get_period_range(period)
    records = []
    for f in sorted(COST_LOGS_DIR.glob("*.json")):
        date_str = f.stem  # e.g. "2026-04-13"
        if date_str < start_str or date_str > end_str:
            continue
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    records.extend(data)
        except (json.JSONDecodeError, IOError):
            continue
    return records


@router.get("/summary")
async def cost_summary(period: str = Query("today", regex="^(today|week|month)$")):
    """概览：总成本、调用次数、缓存命中率"""
    records = _load_records(period)
    total_cost = sum(r.get("cost_usd", 0) for r in records)
    total_storage = sum(r.get("storage_cost_usd", 0) for r in records)
    total_calls = len(records)
    total_prompt = sum(r.get("prompt_tokens", 0) for r in records)
    total_cached = sum(r.get("cached_tokens", 0) for r in records)
    total_output = sum(r.get("output_tokens", 0) for r in records)

    cache_rate = (total_cached / total_prompt * 100) if total_prompt > 0 else 0

    # FlashLite 采样效率
    fl_calls = sum(1 for r in records if r.get("call_type", "").startswith("flashlite"))

    rate = await get_exchange_rate_service().get_rate()
    return {
        "period": period,
        "total_cost_usd": round(total_cost, 6),
        "total_cost_cny": round(total_cost * rate, 4),
        "storage_cost_usd": round(total_storage, 6),
        "total_calls": total_calls,
        "flashlite_calls": fl_calls,
        "total_prompt_tokens": total_prompt,
        "total_cached_tokens": total_cached,
        "total_output_tokens": total_output,
        "cache_hit_rate": round(cache_rate, 1),
        "usd_to_cny": round(rate, 4),
        "rate_live": get_exchange_rate_service().is_live,
    }


@router.get("/by-model")
async def cost_by_model(period: str = Query("today", regex="^(today|week|month)$")):
    """按模型分类统计"""
    records = _load_records(period)
    groups = {}
    for r in records:
        model = r.get("model", "unknown")
        if model not in groups:
            groups[model] = {
                "calls": 0, "prompt_tokens": 0, "cached_tokens": 0,
                "output_tokens": 0, "cost_usd": 0,
            }
        g = groups[model]
        g["calls"] += 1
        g["prompt_tokens"] += r.get("prompt_tokens", 0)
        g["cached_tokens"] += r.get("cached_tokens", 0)
        g["output_tokens"] += r.get("output_tokens", 0)
        g["cost_usd"] += r.get("cost_usd", 0)

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
            "cost_cny": round(g["cost_usd"] * (await get_exchange_rate_service().get_rate()), 4),
        })
    return {"models": result}


@router.get("/by-window")
async def cost_by_window(period: str = Query("today", regex="^(today|week|month)$")):
    """按窗口分类统计"""
    records = _load_records(period)
    groups = {}
    for r in records:
        wk = r.get("window_key", "unknown")
        if wk not in groups:
            groups[wk] = {
                "calls": 0, "flashlite_calls": 0, "main_calls": 0,
                "tool_calls": 0, "prompt_tokens": 0, "output_tokens": 0,
                "cost_usd": 0,
            }
        g = groups[wk]
        g["calls"] += 1
        ct = r.get("call_type", "")
        if ct.startswith("flashlite"):
            g["flashlite_calls"] += 1
        elif ct.startswith("main_model"):
            g["main_calls"] += 1
        elif ct.startswith("tool_model"):
            g["tool_calls"] += 1
        g["prompt_tokens"] += r.get("prompt_tokens", 0)
        g["output_tokens"] += r.get("output_tokens", 0)
        g["cost_usd"] += r.get("cost_usd", 0)

    result = []
    for wk, g in sorted(groups.items(), key=lambda x: -x[1]["cost_usd"]):
        result.append({
            "window_key": wk,
            "calls": g["calls"],
            "flashlite_calls": g["flashlite_calls"],
            "main_calls": g["main_calls"],
            "tool_calls": g["tool_calls"],
            "total_tokens": g["prompt_tokens"] + g["output_tokens"],
            "cost_usd": round(g["cost_usd"], 6),
            "cost_cny": round(g["cost_usd"] * (await get_exchange_rate_service().get_rate()), 4),
        })
    return {"windows": result}


@router.get("/timeline")
async def cost_timeline(
    period: str = Query("week", regex="^(today|week|month)$"),
    granularity: str = Query("hour", regex="^(hour|day)$"),
):
    """时间轴数据"""
    records = _load_records(period)
    buckets = {}
    for r in records:
        ts = r.get("timestamp", "")
        if granularity == "day":
            key = ts[:10]  # YYYY-MM-DD
        else:
            key = ts[:13]  # YYYY-MM-DDTHH
        if key not in buckets:
            buckets[key] = {"calls": 0, "cost_usd": 0, "prompt_tokens": 0, "cached_tokens": 0}
        b = buckets[key]
        b["calls"] += 1
        b["cost_usd"] += r.get("cost_usd", 0)
        b["prompt_tokens"] += r.get("prompt_tokens", 0)
        b["cached_tokens"] += r.get("cached_tokens", 0)

    result = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        result.append({
            "time": key,
            "calls": b["calls"],
            "cost_usd": round(b["cost_usd"], 6),
            "prompt_tokens": b["prompt_tokens"],
            "cached_tokens": b["cached_tokens"],
        })
    return {"timeline": result, "granularity": granularity}


@router.get("/pricing")
async def get_pricing():
    """获取当前定价信息和汇率"""
    svc = get_exchange_rate_service()
    rate = await svc.get_rate()
    return {
        "usd_to_cny": round(rate, 4),
        "rate_live": svc.is_live,
    }


@router.get("/known-groups")
async def get_known_groups():
    """从成本日志中提取所有已知群号（用于前端 datalist 自动补全）"""
    groups = set()
    if COST_LOGS_DIR.exists():
        for f in COST_LOGS_DIR.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    for r in json.load(fp):
                        wk = r.get("window_key", "")
                        if wk.startswith("GroupMessage:"):
                            groups.add(wk.split(":", 1)[1])
            except Exception:
                pass
    return {"groups": sorted(groups)}
