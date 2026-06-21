"""
Memory 管理 + Knowledge 查看 + Sandbox 浏览器 API
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, Body

router = APIRouter(prefix="/api/data", tags=["data"])

# 路径
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
MEMORY_DB = BASE_DIR / "Memory" / "memory.db"
KNOWLEDGE_DIR = BASE_DIR / "AstrBot" / "data" / "plugins" / "astrbot_plugin_flashlite"
SANDBOX_ROOT = BASE_DIR / "Sandbox"

# 动态导入 Memory 和 Knowledge
PLUGIN_DIR = str(BASE_DIR / "AstrBot" / "data" / "plugins" / "astrbot_plugin_flashlite")
ASTRBOT_DIR = str(BASE_DIR / "AstrBot")
for p in [PLUGIN_DIR, ASTRBOT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ========================
# Memory 管理（直接 DB 操作，不导入 AstrBot）
# ========================


@router.get("/memory/list")
async def list_memories(
    workspace: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = Query(20, le=100),
):
    """搜索/列出记忆"""
    try:
        import aiosqlite
        if not MEMORY_DB.exists():
            return {"memories": [], "stats": {}, "error": "Memory DB 未创建"}

        async with aiosqlite.connect(str(MEMORY_DB)) as db:
            # 检查表结构
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in await cursor.fetchall()]
            if not tables:
                return {"memories": [], "stats": {"total": 0}}

            table = tables[0]
            # 获取列名
            cursor = await db.execute(f"PRAGMA table_info([{table}])")
            cols = [r[1] for r in await cursor.fetchall()]

            # 查询
            sql = f"SELECT * FROM [{table}]"
            params = []
            conditions = []
            if query and "title" in cols:
                conditions.append("(title LIKE ? OR content LIKE ?)")
                params.extend([f"%{query}%", f"%{query}%"])
            if workspace and "workspace" in cols:
                conditions.append("workspace = ?")
                params.append(workspace)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += f" LIMIT {limit}"

            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            memories = [dict(zip(cols, row)) for row in rows]

            # 统计
            cursor = await db.execute(f"SELECT COUNT(*) FROM [{table}]")
            total = (await cursor.fetchone())[0]

            return {"memories": memories, "stats": {"total": total, "table": table}}
    except Exception as e:
        return {"memories": [], "stats": {}, "error": str(e)}


@router.get("/memory/{mem_id}")
async def read_memory(mem_id: str):
    """读取完整记忆"""
    try:
        import aiosqlite
        if not MEMORY_DB.exists():
            return {"memory": None, "error": "Memory DB 未创建"}

        async with aiosqlite.connect(str(MEMORY_DB)) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in await cursor.fetchall()]
            if not tables:
                return {"memory": None, "error": "无表"}

            table = tables[0]
            cursor = await db.execute(f"PRAGMA table_info([{table}])")
            cols = [r[1] for r in await cursor.fetchall()]

            # 尝试用 id 列查询
            id_col = "id" if "id" in cols else cols[0]
            cursor = await db.execute(f"SELECT * FROM [{table}] WHERE [{id_col}] = ?", (mem_id,))
            row = await cursor.fetchone()
            if row:
                return {"memory": dict(zip(cols, row))}
            return {"memory": None, "error": "记忆不存在"}
    except Exception as e:
        return {"memory": None, "error": str(e)}


@router.post("/memory")
async def write_memory(body: dict = Body(...)):
    """写入新记忆（简化版，直接写 DB）"""
    return {"id": None, "error": "独立模式下暂不支持写入，请在 AstrBot 运行时使用"}


@router.put("/memory/{mem_id}")
async def update_memory(mem_id: str, body: dict = Body(...)):
    """更新记忆"""
    return {"success": False, "error": "独立模式下暂不支持更新，请在 AstrBot 运行时使用"}


@router.delete("/memory/{mem_id}")
async def delete_memory(mem_id: str, workspace: Optional[str] = None):
    """删除记忆"""
    return {"success": False, "error": "独立模式下暂不支持删除，请在 AstrBot 运行时使用"}


# ========================
# Knowledge 查看
# ========================

KNOWLEDGE_DB = BASE_DIR / "AstrBot" / "data" / "plugins" / "astrbot_plugin_flashlite" / "knowledge.db"


@router.get("/knowledge")
async def get_knowledge():
    """获取 Knowledge 缓存状态——优先从 JSON，fallback 到 DB"""
    # 1. 优先从 JSON 文件读取
    for json_path in KNOWLEDGE_JSON_PATHS:
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                windows = data.get("windows", {})
                operations = data.get("recent_operations", [])
                profiles = data.get("user_profiles", {})
                return {
                    "data": data,
                    "stats": {
                        "windows": len(windows),
                        "operations": len(operations),
                        "profiles": len(profiles),
                        "recent_operations": operations[-10:],  # 最近10条
                        "last_updated": data.get("last_updated", ""),
                        "source": "json",
                        "db_path": str(json_path),
                    },
                }
            except Exception as e:
                return {"data": {}, "stats": {"windows": 0}, "error": f"JSON 解析失败: {e}"}

    # 2. Fallback 到 DB
    try:
        import aiosqlite
        db_path = None
        for candidate in [
            KNOWLEDGE_DB,
            KNOWLEDGE_DIR / "knowledge.db",
            KNOWLEDGE_DIR / "cache" / "knowledge.db",
        ]:
            if candidate.exists():
                db_path = candidate
                break

        if not db_path:
            return {"data": {}, "stats": {"windows": 0}, "error": "Knowledge 数据源未找到"}

        async with aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in await cursor.fetchall()]
            data = {}
            for table in tables[:5]:
                cursor = await db.execute(f"SELECT COUNT(*) FROM [{table}]")
                count = (await cursor.fetchone())[0]
                data[table] = {"count": count}
            return {
                "data": data,
                "stats": {
                    "windows": len(data),
                    "tables": tables,
                    "source": "db",
                    "db_path": str(db_path),
                },
            }
    except Exception as e:
        return {"data": {}, "stats": {}, "error": str(e)}


# ========================
# Sandbox 浏览器
# ========================

@router.get("/sandbox/tree")
async def sandbox_tree(path: str = ""):
    """获取 Sandbox 文件树"""
    try:
        target = SANDBOX_ROOT / path if path else SANDBOX_ROOT
        if not target.exists():
            return {"files": [], "error": "路径不存在"}

        # 安全检查：精确匹配 + os.sep 后缀防止同前缀兄弟目录逃逸
        resolved_sandbox = str(SANDBOX_ROOT.resolve())
        resolved_target = str(target.resolve())
        if resolved_target != resolved_sandbox and not resolved_target.startswith(resolved_sandbox + os.sep):
            return {"files": [], "error": "路径逃逸检测"}

        items = []
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
            info = {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "path": str(entry.relative_to(SANDBOX_ROOT)).replace("\\", "/"),
            }
            if entry.is_file():
                info["size"] = entry.stat().st_size
                info["modified"] = entry.stat().st_mtime
            elif entry.is_dir():
                try:
                    info["children"] = sum(1 for _ in entry.iterdir())
                except PermissionError:
                    info["children"] = 0
            items.append(info)

        return {"files": items, "root": str(SANDBOX_ROOT)}
    except Exception as e:
        return {"files": [], "error": str(e)}


@router.get("/sandbox/file")
async def sandbox_file(path: str):
    """读取 Sandbox 内文件内容"""
    try:
        target = SANDBOX_ROOT / path
        if not target.exists() or not target.is_file():
            return {"content": None, "error": "文件不存在"}

        if not str(target.resolve()).startswith(str(SANDBOX_ROOT.resolve()) + os.sep):
            return {"content": None, "error": "路径逃逸"}

        # 限制文件大小
        if target.stat().st_size > 1024 * 1024:  # 1MB
            return {"content": None, "error": "文件过大 (>1MB)"}

        content = target.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "size": len(content), "path": path}
    except Exception as e:
        return {"content": None, "error": str(e)}


@router.get("/sandbox/stats")
async def sandbox_stats():
    """Sandbox 统计"""
    try:
        if not SANDBOX_ROOT.exists():
            return {"exists": False}

        total_size = 0
        file_count = 0
        workspace = SANDBOX_ROOT / "workspace"
        if workspace.exists():
            for f in workspace.rglob("*"):
                if f.is_file():
                    total_size += f.stat().st_size
                    file_count += 1

        return {
            "exists": True,
            "root": str(SANDBOX_ROOT),
            "workspace_size_mb": round(total_size / 1024 / 1024, 2),
            "workspace_files": file_count,
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


@router.get("/sandbox/tools")
async def sandbox_tools():
    """获取 Sandbox 基础工具列表"""
    tools = []
    base_tools_dir = SANDBOX_ROOT / "base_tools"

    if base_tools_dir.exists():
        for f in sorted(base_tools_dir.glob("*.tool.json")):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    tool_def = json.load(fp)
                    tools.append({
                        "name": tool_def.get("name", f.stem),
                        "description": tool_def.get("description", ""),
                        "category": tool_def.get("category", "other"),
                        "timeout_ms": tool_def.get("timeout_ms", 30000),
                        "read_only": tool_def.get("read_only", False),
                        "parallel": tool_def.get("parallel", False),
                        "param_count": len(tool_def.get("parameters", {})),
                    })
            except Exception:
                continue

    # 自定义工具
    custom_tools = []
    custom_dir = SANDBOX_ROOT / "workspace" / "custom_tools"
    if custom_dir.exists():
        for f in sorted(custom_dir.glob("*.tool.json")):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    tool_def = json.load(fp)
                    custom_tools.append({
                        "name": tool_def.get("name", f.stem),
                        "description": tool_def.get("description", ""),
                        "category": tool_def.get("category", "custom"),
                    })
            except Exception:
                continue

    return {
        "base_tools": tools,
        "base_count": len(tools),
        "custom_tools": custom_tools,
        "custom_count": len(custom_tools),
    }


# ========================
# CHECKPOINT 历史（对话内存页增强）
# ========================

QQ_DATA_DB = BASE_DIR / "QQ_data" / "messages.db"


@router.get("/checkpoint/list")
async def list_checkpoints(limit: int = Query(30, le=100)):
    """获取 CHECKPOINT 压缩历史列表"""
    try:
        import aiosqlite
        if not QQ_DATA_DB.exists():
            return {"checkpoints": [], "total": 0, "error": "QQ_data DB 未找到"}

        async with aiosqlite.connect(str(QQ_DATA_DB)) as db:
            # 确认表存在
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoint_history'"
            )
            if not await cursor.fetchone():
                return {"checkpoints": [], "total": 0, "error": "checkpoint_history 表不存在"}

            # 总数
            cursor = await db.execute("SELECT COUNT(*) FROM checkpoint_history")
            total = (await cursor.fetchone())[0]

            # 列表（按创建时间倒序）
            cursor = await db.execute(
                "SELECT id, window_type, window_id, compression_ratio, token_estimate, "
                "original_msg_range_start, original_msg_range_end, created_at "
                "FROM checkpoint_history ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            checkpoints = []
            for r in rows:
                checkpoints.append({
                    "id": r[0],
                    "window_type": r[1],
                    "window_id": r[2],
                    "compression_ratio": r[3],
                    "token_estimate": r[4],
                    "msg_range": f"{r[5]}-{r[6]}",
                    "created_at": r[7],
                })

            return {"checkpoints": checkpoints, "total": total}
    except Exception as e:
        return {"checkpoints": [], "total": 0, "error": str(e)}


@router.get("/checkpoint/{cp_id}")
async def get_checkpoint_detail(cp_id: int):
    """获取单个 CHECKPOINT 压缩详情（含压缩内容摘要）"""
    try:
        import aiosqlite
        if not QQ_DATA_DB.exists():
            return {"checkpoint": None, "error": "QQ_data DB 未找到"}

        async with aiosqlite.connect(str(QQ_DATA_DB)) as db:
            cursor = await db.execute(
                "SELECT * FROM checkpoint_history WHERE id = ?", (cp_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return {"checkpoint": None, "error": "CHECKPOINT 不存在"}

            cols_cursor = await db.execute("PRAGMA table_info(checkpoint_history)")
            cols = [c[1] for c in await cols_cursor.fetchall()]
            cp = dict(zip(cols, row))
            # 截断 compressed_content 避免传输过大
            if "compressed_content" in cp and cp["compressed_content"]:
                content = str(cp["compressed_content"])
                cp["compressed_content"] = content[:2000] + ("..." if len(content) > 2000 else "")
            return {"checkpoint": cp}
    except Exception as e:
        return {"checkpoint": None, "error": str(e)}


# ========================
# Knowledge 窗口详情（展开查看每个窗口的 summary/mood/active_users）
# ========================

KNOWLEDGE_JSON_PATHS = [
    BASE_DIR / "Knowledge" / "knowledge_cache.json",  # flashlite 引擎实际写入路径
    BASE_DIR / "AstrBot" / "data" / "plugins" / "astrbot_plugin_flashlite" / "knowledge.json",
    BASE_DIR / "AstrBot" / "data" / "plugins" / "astrbot_plugin_flashlite" / "cache" / "knowledge_cache.json",
]


@router.get("/knowledge/windows")
async def get_knowledge_windows():
    """获取 Knowledge 各窗口展开详情"""
    # 先尝试 JSON 文件
    for json_path in KNOWLEDGE_JSON_PATHS:
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                windows = data.get("windows", {})
                result = []
                for wid, wdata in windows.items():
                    if isinstance(wdata, dict):
                        result.append({
                            "window_id": wid,
                            "summary": wdata.get("summary", ""),
                            "mood": wdata.get("mood", ""),
                            "active_users": wdata.get("active_users", []),
                            "last_update": wdata.get("last_update", ""),
                            "message_count": wdata.get("message_count", 0),
                        })
                return {"windows": result, "user_profiles": data.get("user_profiles", {}), "source": "json", "path": str(json_path)}
            except Exception as e:
                return {"windows": [], "error": str(e)}

    # 再尝试 DB
    try:
        import aiosqlite
        for db_candidate in [
            KNOWLEDGE_DB,
            KNOWLEDGE_DIR / "knowledge.db",
            KNOWLEDGE_DIR / "cache" / "knowledge.db",
        ]:
            if db_candidate.exists():
                async with aiosqlite.connect(str(db_candidate)) as db:
                    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = [r[0] for r in await cursor.fetchall()]
                    windows = []
                    for t in tables:
                        cursor = await db.execute(f"SELECT COUNT(*) FROM [{t}]")
                        count = (await cursor.fetchone())[0]
                        # 尝试获取样本
                        cols_cursor = await db.execute(f"PRAGMA table_info([{t}])")
                        cols = [c[1] for c in await cols_cursor.fetchall()]
                        windows.append({
                            "window_id": t,
                            "record_count": count,
                            "columns": cols,
                        })
                    return {"windows": windows, "source": "db", "path": str(db_candidate)}
    except Exception as e:
        return {"windows": [], "error": str(e)}

    return {"windows": [], "error": "Knowledge 数据源未找到（JSON 和 DB 均不存在）"}

