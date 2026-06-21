"""
对话内存浏览器 API
- 消息查看/搜索/统计/清理
- 直接读写 QQ_data/messages.db
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/messages", tags=["messages"])

_BASE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DB_CANDIDATES = [
    os.path.join(_BASE, "QQ_data", "messages.db"),    # 自建持久化层
    os.path.join(_BASE, "AstrBot", "data", "data_v4.db"),  # AstrBot 原生 v4
    os.path.join(_BASE, "AstrBot", "data", "data_v3.db"),  # AstrBot 原生 v3
]

# 运行时选择第一个存在的数据库
def _find_db():
    for p in _DB_CANDIDATES:
        if os.path.exists(p):
            return p
    return _DB_CANDIDATES[0]


DB_PATH = _find_db()
_TABLE = "qq_messages"


def _extract_card_title(extra: dict) -> str:
    """从 extra_data 的 json_data 中提取卡片标题"""
    json_str = extra.get("json_data", "")
    if not json_str:
        return ""
    try:
        import json as _j
        jd = _j.loads(json_str) if isinstance(json_str, str) else json_str
        return (
            jd.get("prompt", "")
            or jd.get("meta", {}).get("detail_1", {}).get("title", "")
            or jd.get("meta", {}).get("news", {}).get("title", "")
            or ""
        )
    except Exception:
        return ""


@asynccontextmanager
async def _open_db():
    """每次请求创建新连接、自动关闭，避免线程复用冲突"""
    global DB_PATH, _TABLE
    DB_PATH = _find_db()
    if not os.path.exists(DB_PATH):
        yield None
        return
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA query_only=ON")
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in await cursor.fetchall()]
        if "qq_messages" in tables:
            _TABLE = "qq_messages"
        elif "message" in tables:
            _TABLE = "message"
        elif tables:
            _TABLE = tables[0]
        yield db
    finally:
        await db.close()


@router.get("/windows")
async def list_windows():
    """列出所有对话窗口"""
    async with _open_db() as db:
        if not db:
            return {"windows": [], "error": "数据库未初始化"}
        try:
            cursor = await db.execute(f"""
                SELECT window_type, window_id, 
                       COUNT(*) as msg_count,
                       MAX(created_at) as last_msg,
                       MIN(created_at) as first_msg
                FROM {_TABLE}
                GROUP BY window_type, window_id
                ORDER BY last_msg DESC
            """)
            rows = await cursor.fetchall()
            return {
                "windows": [
                    {
                        "type": r[0],
                        "id": r[1],
                        "count": r[2],
                        "last_msg": r[3],
                        "first_msg": r[4],
                    }
                    for r in rows
                ]
            }
        except Exception as e:
            return {"windows": [], "error": str(e)}


@router.get("/search")
async def search_messages(
    q: str = Query("", description="搜索关键词"),
    window_id: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """搜索消息"""
    async with _open_db() as db:
        if not db:
            return {"messages": [], "total": 0}
        try:
            conditions = []
            params = []
            if q:
                conditions.append("content_text LIKE ?")
                params.append(f"%{q}%")
            if window_id:
                conditions.append("window_id = ?")
                params.append(window_id)

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            cursor = await db.execute(
                f"SELECT COUNT(*) FROM {_TABLE} {where}", params
            )
            total = (await cursor.fetchone())[0]

            cursor = await db.execute(
                f"""SELECT id, window_type, window_id, sender_id, sender_name,
                           content_text, has_image, is_recalled, created_at,
                           image_urls, content_raw, extra_data
                    FROM {_TABLE} {where}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            )
            rows = await cursor.fetchall()
            import json as _json
            messages = []
            for r in rows:
                # 尝试从 content_raw 提取 group_name
                group_name = ""
                try:
                    raw = _json.loads(r[10]) if r[10] else {}
                    group_name = raw.get("group_name", "")
                except Exception:
                    pass
                # 解析 image_urls
                img_urls = []
                try:
                    img_urls = _json.loads(r[9]) if r[9] else []
                except Exception:
                    pass
                # 解析 extra_data
                extra = {}
                try:
                    extra = _json.loads(r[11]) if r[11] else {}
                except Exception:
                    pass
                messages.append({
                    "id": r[0], "window_type": r[1], "window_id": r[2],
                    "sender_id": r[3], "sender_name": r[4],
                    "content": r[5], "has_image": bool(r[6]),
                    "recalled": bool(r[7]), "time": r[8],
                    "image_urls": img_urls, "group_name": group_name,
                    "extra_data": extra,
                    # 结构化特殊消息字段
                    "video_url": extra.get("video_url", ""),
                    "voice_url": extra.get("voice_url", ""),
                    "card_title": _extract_card_title(extra),
                    "files": extra.get("files", []),
                })
            return {
                "messages": messages,
                "total": total,
            }
        except Exception as e:
            return {"messages": [], "total": 0, "error": str(e)}


@router.get("/stats")
async def message_stats():
    """消息统计（总量/今日/撤回/窗口数/数据库大小）"""
    async with _open_db() as db:
        if not db:
            return {"total": 0}
        try:
            stats = {}
            cursor = await db.execute(f"SELECT COUNT(*) FROM {_TABLE}")
            stats["total"] = (await cursor.fetchone())[0]

            today = datetime.now().strftime("%Y-%m-%d")
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM {_TABLE} WHERE created_at >= ?", (today,)
            )
            stats["today"] = (await cursor.fetchone())[0]

            cursor = await db.execute(
                f"SELECT COUNT(*) FROM {_TABLE} WHERE is_recalled = 1"
            )
            stats["recalled"] = (await cursor.fetchone())[0]

            cursor = await db.execute(
                f"SELECT COUNT(DISTINCT window_id) FROM {_TABLE}"
            )
            stats["windows"] = (await cursor.fetchone())[0]

            if os.path.exists(DB_PATH):
                stats["db_size_mb"] = round(os.path.getsize(DB_PATH) / 1024 / 1024, 2)
            else:
                stats["db_size_mb"] = 0

            daily = []
            for i in range(7):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                next_day = (datetime.now() - timedelta(days=i - 1)).strftime("%Y-%m-%d")
                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE created_at >= ? AND created_at < ?",
                    (day, next_day),
                )
                count = (await cursor.fetchone())[0]
                daily.append({"date": day, "count": count})
            stats["daily"] = list(reversed(daily))

            return stats
        except Exception as e:
            return {"total": 0, "error": str(e)}


@router.delete("/cleanup")
async def cleanup_messages(days: int = Query(30, description="清理多少天前的消息")):
    """清理旧消息"""
    async with _open_db() as db:
        if not db:
            return {"deleted": 0, "error": "数据库未初始化"}
        try:
            # cleanup 需要写权限，重新开连接不设 query_only
            pass
        except Exception:
            pass

    # 清理操作单独开写连接
    if not os.path.exists(DB_PATH):
        return {"deleted": 0, "error": "数据库未初始化"}
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        cursor = await db.execute(
            f"DELETE FROM {_TABLE} WHERE created_at < ?", (cutoff,)
        )
        await db.commit()
        return {"deleted": cursor.rowcount, "cutoff": cutoff}
    except Exception as e:
        return {"deleted": 0, "error": str(e)}
    finally:
        await db.close()


# ========================
# 图片本地缓存服务
# ========================

_QQ_DATA_DIR = os.path.normpath(os.path.join(_BASE, "QQ_data"))

@router.get("/image/{filename:path}")
async def serve_image(filename: str):
    """提供本地缓存的图片文件

    路径安全：只允许访问 QQ_data/images/ 下的文件
    """
    from fastapi.responses import FileResponse
    import mimetypes

    # 安全校验：只允许 images/ 下的文件
    if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
        return {"error": "非法路径"}

    # 构建完整路径
    image_path = os.path.normpath(os.path.join(_QQ_DATA_DIR, "images", filename))

    # 二次路径穿越检查
    images_root = os.path.normpath(os.path.join(_QQ_DATA_DIR, "images"))
    if not image_path.startswith(images_root + os.sep) and image_path != images_root:
        return {"error": "路径安全拒绝"}

    if not os.path.isfile(image_path):
        return {"error": "图片不存在", "path": filename}

    # 推断 MIME 类型
    mime, _ = mimetypes.guess_type(image_path)
    if not mime:
        mime = "image/jpeg"

    return FileResponse(
        image_path,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/image-cache/stats")
async def image_cache_stats():
    """图片缓存统计"""
    images_dir = os.path.join(_QQ_DATA_DIR, "images")
    if not os.path.isdir(images_dir):
        return {"exists": False, "count": 0, "size_mb": 0}

    total_size = 0
    count = 0
    for fname in os.listdir(images_dir):
        fpath = os.path.join(images_dir, fname)
        if os.path.isfile(fpath):
            total_size += os.path.getsize(fpath)
            count += 1

    return {
        "exists": True,
        "count": count,
        "size_mb": round(total_size / 1024 / 1024, 2),
        "path": images_dir,
    }
