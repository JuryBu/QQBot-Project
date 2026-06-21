"""仪表盘路由——系统状态总览（增强版真实健康检查）"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
QQ_DATA_DB = PROJECT_ROOT / "QQ_data" / "messages.db"
MEMORY_DB = PROJECT_ROOT / "Memory" / "memory.db"
KNOWLEDGE_FILE = PROJECT_ROOT / "Knowledge" / "knowledge_cache.json"
ASTRBOT_DB = PROJECT_ROOT / "AstrBot" / "data" / "data_v4.db"
CHECKPOINT_DB = PROJECT_ROOT / "QQ_data" / "messages.db"  # checkpoint_history 同库


@router.get("/status")
async def get_system_status():
    """系统状态概览——多层深度探测"""
    import asyncio

    async def check_port(host: str, port: int, timeout: float = 1.5) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def check_http(url: str, timeout: float = 2.0) -> dict:
        """HTTP GET 探测，返回 {ok, status, detail}"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    return {"ok": resp.status < 500, "status": resp.status}
        except ImportError:
            # fallback to TCP if aiohttp not available
            return {"ok": await check_port("127.0.0.1", int(url.split(":")[-1].split("/")[0])), "status": 0}
        except Exception as e:
            return {"ok": False, "status": 0, "detail": str(e)}

    async def check_onebot_ws(timeout: float = 2.0) -> dict:
        """检测 AstrBot 的 OneBot WS 端口 (6199) 是否在监听
        
        注意：AstrBot 6199 端口只接受 NapCat 的反向 WS Client 认证连接，
        第三方直接 ws_connect 会被 405 拒绝，所以改用 TCP 端口探测 + HTTP 存活检测。
        如果端口在监听说明 AstrBot 的 WS 服务端已启动，NapCat 可以连上来。
        """
        port_ok = await check_port("127.0.0.1", 6199, timeout)
        if not port_ok:
            return {"ok": False, "detail": "6199 端口未监听"}
        # 端口在监听，再做 HTTP GET 验证服务确实是 AstrBot 的 WS 端点
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://127.0.0.1:6199/",
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    # 405/400/426 都说明服务存活（只是拒绝非WS请求），只有连接失败才是问题
                    return {"ok": True, "detail": f"WS 服务端就绪 (HTTP {resp.status})"}
        except Exception:
            # GET 失败但端口在监听，仍视为可用
            return {"ok": True, "detail": "WS 服务端就绪 (TCP)"}

    # 并发探测所有服务
    astrbot_port, napcat_port, napcat_http, astrbot_http, onebot_ws = await asyncio.gather(
        check_port("127.0.0.1", 6185),
        check_port("127.0.0.1", 6099),
        check_http("http://127.0.0.1:6099"),
        check_http("http://127.0.0.1:6185"),
        check_onebot_ws(),
    )

    # QQ BOT 真实在线 = NapCat HTTP 响应 + AstrBot HTTP 响应 + OneBot WS 服务端就绪
    qq_bot_online = napcat_http["ok"] and astrbot_http["ok"] and onebot_ws["ok"]

    return {
        "qq_bot": {
            "online": qq_bot_online,
            "detail": "QQ消息链路正常" if qq_bot_online else (
                "OneBot WS 未连接" if not onebot_ws["ok"] else
                "AstrBot 未运行" if not astrbot_http["ok"] else
                "NapCat 未运行"
            ),
        },
        "napcat": {
            "running": napcat_http["ok"],
            "port": 6099,
            "http_ok": napcat_http["ok"],
        },
        "astrbot": {
            "running": astrbot_http["ok"],
            "port": 6185,
            "http_ok": astrbot_http["ok"],
        },
        "onebot_ws": {
            "connected": onebot_ws["ok"],
            "port": 6199,
            "detail": onebot_ws.get("detail", ""),
        },
        "console": {"running": True, "port": 8090},
        "uptime": time.process_time(),
        "time": datetime.now().isoformat(),
    }


@router.get("/stats")
async def get_stats():
    """统计数据（增强版：含 CHECKPOINT 次数）"""
    stats = {
        "messages": {"total": 0, "today": 0, "windows": 0},
        "memory": {"total": 0},
        "knowledge": {"windows": 0},
        "sandbox": {"workspace_mb": 0},
        "checkpoint": {"total": 0},
        "db_size_mb": 0,
    }

    # 消息统计
    if QQ_DATA_DB.exists():
        try:
            import aiosqlite
            async with aiosqlite.connect(str(QQ_DATA_DB)) as db:
                # 总数
                cursor = await db.execute("SELECT COUNT(*) FROM qq_messages")
                row = await cursor.fetchone()
                stats["messages"]["total"] = row[0] if row else 0

                # 今天
                today = datetime.now().strftime("%Y-%m-%d")
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM qq_messages WHERE created_at >= ?",
                    (f"{today}T00:00:00",)
                )
                row = await cursor.fetchone()
                stats["messages"]["today"] = row[0] if row else 0

                # 窗口数
                cursor = await db.execute("SELECT COUNT(DISTINCT window_id) FROM qq_messages")
                row = await cursor.fetchone()
                stats["messages"]["windows"] = row[0] if row else 0

                # CHECKPOINT 压缩次数
                try:
                    cursor = await db.execute("SELECT COUNT(*) FROM checkpoint_history")
                    row = await cursor.fetchone()
                    stats["checkpoint"]["total"] = row[0] if row else 0
                except Exception:
                    pass  # 表可能不存在

            # 数据库文件大小
            stats["db_size_mb"] = round(QQ_DATA_DB.stat().st_size / 1024 / 1024, 2)
        except Exception:
            pass

    # 记忆统计
    if MEMORY_DB.exists():
        try:
            import aiosqlite
            async with aiosqlite.connect(str(MEMORY_DB)) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM memories")
                row = await cursor.fetchone()
                stats["memory"]["total"] = row[0] if row else 0
        except Exception:
            pass

    # Knowledge 统计 — 先尝试 JSON，再尝试 DB
    knowledge_found = False
    if KNOWLEDGE_FILE.exists():
        try:
            with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                stats["knowledge"]["windows"] = len(data.get("windows", {}))
                knowledge_found = True
        except Exception:
            pass

    if not knowledge_found:
        # 回退到 SQLite DB（knowledge.py 可能直接存 DB）
        knowledge_db_candidates = [
            PROJECT_ROOT / "AstrBot" / "data" / "plugins" / "astrbot_plugin_flashlite" / "knowledge.db",
            PROJECT_ROOT / "Knowledge" / "knowledge.db",
        ]
        for db_path in knowledge_db_candidates:
            if db_path.exists():
                try:
                    import aiosqlite
                    async with aiosqlite.connect(str(db_path)) as db:
                        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        tables = [r[0] for r in await cursor.fetchall()]
                        stats["knowledge"]["windows"] = len(tables)
                        knowledge_found = True
                        break
                except Exception:
                    pass

    # Sandbox 统计
    sandbox_workspace = PROJECT_ROOT / "Sandbox" / "workspace"
    if sandbox_workspace.exists():
        total = sum(f.stat().st_size for f in sandbox_workspace.rglob("*") if f.is_file())
        stats["sandbox"]["workspace_mb"] = round(total / 1024 / 1024, 2)

    return stats
