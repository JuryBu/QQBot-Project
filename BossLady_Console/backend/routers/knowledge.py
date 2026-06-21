"""
Knowledge 知识缓存 API 路由
直接读取 FlashLite Knowledge 系统的 knowledge_cache.json
"""

import json
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query

router = APIRouter()

# Knowledge 缓存文件路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
KNOWLEDGE_FILE = PROJECT_ROOT / "Knowledge" / "knowledge_cache.json"


def _load_knowledge() -> dict:
    """加载 Knowledge 缓存"""
    if not KNOWLEDGE_FILE.exists():
        return {"windows": {}, "user_profiles": {}, "recent_operations": [], "last_updated": ""}
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"windows": {}, "user_profiles": {}, "recent_operations": [], "last_updated": ""}


def _time_ago(ts: float) -> str:
    """将 unix 时间戳转为人类可读格式"""
    if ts == 0:
        return "未知"
    diff = time.time() - ts
    if diff < 60:
        return "刚刚"
    elif diff < 3600:
        return f"{int(diff / 60)} 分钟前"
    elif diff < 86400:
        return f"{int(diff / 3600)} 小时前"
    else:
        return f"{int(diff / 86400)} 天前"


@router.get("/overview")
async def knowledge_overview():
    """Knowledge 概览：窗口数、用户画像数、最近更新"""
    data = _load_knowledge()
    windows = data.get("windows", {})
    profiles = data.get("user_profiles", {})
    now = time.time()

    active_windows = sum(
        1 for v in windows.values()
        if now - v.get("last_active_ts", 0) < 86400
    )
    active_profiles = sum(
        1 for v in profiles.values()
        if v.get("status", "active") == "active"
    )
    total_facts = sum(
        len(v.get("facts", []))
        for v in profiles.values()
    )

    return {
        "last_updated": data.get("last_updated", ""),
        "total_windows": len(windows),
        "active_windows": active_windows,
        "total_profiles": len(profiles),
        "active_profiles": active_profiles,
        "total_facts": total_facts,
        "recent_operations": data.get("recent_operations", [])[-10:],
    }


@router.get("/windows")
async def knowledge_windows():
    """所有窗口的 Knowledge 摘要"""
    data = _load_knowledge()
    windows = data.get("windows", {})

    result = []
    for key, info in sorted(
        windows.items(),
        key=lambda x: x[1].get("last_active_ts", 0),
        reverse=True,
    ):
        result.append({
            "window_key": key,
            "name": info.get("name", key),
            "summary": info.get("summary", ""),
            "mood": info.get("mood", ""),
            "active_users": info.get("active_users", []),
            "recent_topics": info.get("recent_topics", []),
            "last_active": _time_ago(info.get("last_active_ts", 0)),
            "last_active_ts": info.get("last_active_ts", 0),
        })
    return {"windows": result}


@router.get("/profiles")
async def knowledge_profiles(status: Optional[str] = Query(None)):
    """用户画像列表"""
    data = _load_knowledge()
    profiles = data.get("user_profiles", {})

    result = []
    for qq_id, pf in sorted(
        profiles.items(),
        key=lambda x: x[1].get("interaction_count", 0),
        reverse=True,
    ):
        if status and pf.get("status", "active") != status:
            continue
        facts = pf.get("facts", [])
        pinned = sum(1 for f in facts if f.get("category") == "pinned")
        dynamic = sum(1 for f in facts if f.get("category") == "dynamic")
        archived = sum(1 for f in facts if f.get("category") == "archived")

        result.append({
            "qq_id": qq_id,
            "nickname": pf.get("nickname", qq_id),
            "status": pf.get("status", "active"),
            "interaction_count": pf.get("interaction_count", 0),
            "first_seen": (pf.get("first_seen", ""))[:10],
            "last_seen": (pf.get("last_seen", ""))[:10],
            "facts_count": len(facts),
            "pinned": pinned,
            "dynamic": dynamic,
            "archived": archived,
        })
    return {"profiles": result}


@router.get("/profile/{qq_id}")
async def knowledge_profile_detail(qq_id: str):
    """单个用户画像详情"""
    data = _load_knowledge()
    profiles = data.get("user_profiles", {})
    pf = profiles.get(qq_id)
    if not pf:
        return {"error": "用户不存在", "profile": None}
    return {"profile": {
        "qq_id": qq_id,
        **pf,
    }}
