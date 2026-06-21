"""Bot管理路由——NapCat QQ登录/AstrBot进程管理"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

def _find_napcat_dir() -> Optional[Path]:
    """Find NapCat directory. Prefer dir with config/webui.json (actual config)."""
    napcat_dirs = [d for d in PROJECT_ROOT.iterdir()
                   if d.is_dir() and d.name.startswith("NapCat")]
    if not napcat_dirs:
        return None
    # Prefer the one with config/webui.json
    for d in napcat_dirs:
        if (d / "config" / "webui.json").exists():
            return d
    # Fallback: one with bootmain
    for d in napcat_dirs:
        if (d / "bootmain").is_dir():
            return d
    # Last resort: first match
    return napcat_dirs[0]



@router.get("/napcat/status")
async def napcat_status():
    """NapCat 状态"""
    napcat_dir = _find_napcat_dir()
    if not napcat_dir:
        return {"running": False, "error": "未找到 NapCat 目录"}

    # 读取配置
    webui_config = {}
    webui_json = napcat_dir / "config" / "webui.json"
    if webui_json.exists():
        with open(webui_json, "r", encoding="utf-8") as f:
            webui_config = json.load(f)

    # 检查是否在运行
    running = False
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://localhost:6099",
                timeout=aiohttp.ClientTimeout(total=2)
            ) as resp:
                running = resp.status == 200
    except Exception:
        pass

    return {
        "running": running,
        "port": webui_config.get("port", 6099),
        "auto_login_account": webui_config.get("autoLoginAccount", ""),
        "token": "***" + webui_config.get("token", "")[-4:] if webui_config.get("token") else "",
        "napcat_dir": str(napcat_dir),
    }


@router.get("/napcat/qrcode")
async def get_qrcode():
    """获取 NapCat 登录二维码"""
    napcat_dir = _find_napcat_dir()
    if not napcat_dir:
        raise HTTPException(404, "未找到 NapCat 目录")

    qr_path = napcat_dir / "cache" / "qrcode.png"
    if qr_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(qr_path), media_type="image/png")

    raise HTTPException(404, "二维码尚未生成，请先触发登录")


@router.get("/napcat/webui-url")
async def get_napcat_webui_url():
    """获取 NapCat WebUI URL（用于 iframe 内嵌）"""
    napcat_dir = _find_napcat_dir()
    if not napcat_dir:
        raise HTTPException(404, "未找到 NapCat 目录")

    webui_json = napcat_dir / "config" / "webui.json"
    if webui_json.exists():
        with open(webui_json, "r", encoding="utf-8") as f:
            config = json.load(f)
        token = config.get("token", "")
        port = config.get("port", 6099)
        return {
            "url": f"http://localhost:{port}/webui",
            "token_hint": "***" + token[-4:] if len(token) > 4 else "***",
        }

    raise HTTPException(404, "NapCat WebUI 配置不存在")


@router.post("/napcat/switch-account")
async def switch_account(qq_number: str):
    """切换 QQ 账号"""
    napcat_dir = _find_napcat_dir()
    if not napcat_dir:
        raise HTTPException(404, "未找到 NapCat 目录")

    webui_json = napcat_dir / "config" / "webui.json"
    if not webui_json.exists():
        raise HTTPException(404, "NapCat 配置不存在")

    with open(webui_json, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["autoLoginAccount"] = qq_number
    with open(webui_json, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    return {"success": True, "message": f"已切换到 QQ {qq_number}，重启 NapCat 后生效"}


@router.get("/astrbot/status")
async def astrbot_status():
    """AstrBot 状态——TCP 端口探测"""
    import asyncio
    running = False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 6185), timeout=1.5
        )
        writer.close()
        await writer.wait_closed()
        running = True
    except Exception:
        pass

    return {"running": running}


@router.post("/napcat/restart")
async def restart_napcat():
    """重启 NapCat"""
    napcat_dir = _find_napcat_dir()
    if not napcat_dir:
        return {"success": False, "error": "未找到 NapCat 目录"}

    try:
        # 尝试终止现有 NapCat 进程
        subprocess.run(
            ["taskkill", "/IM", "NapCat.Shell.exe", "/F"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass

    # 查找 .bat 启动脚本
    shell_dir = napcat_dir
    bat_candidates = list(shell_dir.glob("*.bat"))
    if not bat_candidates:
        return {"success": False, "error": "未找到 NapCat 启动脚本 (.bat)"}

    try:
        subprocess.Popen(
            [str(bat_candidates[0])],
            cwd=str(shell_dir),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return {"success": True, "message": "NapCat 重启命令已发送"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/astrbot/restart")
async def restart_astrbot():
    """重启 AstrBot"""
    try:
        # 终止现有 AstrBot 进程
        subprocess.run(
            ["taskkill", "/IM", "python.exe", "/FI", "WINDOWTITLE eq AstrBot*", "/F"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass

    # 启动 AstrBot
    astrbot_dir = Path(__file__).resolve().parent.parent.parent.parent / "AstrBot"
    venv_python = astrbot_dir / ".venv" / "Scripts" / "python.exe"
    main_py = astrbot_dir / "main.py"

    if not venv_python.exists() or not main_py.exists():
        return {"success": False, "error": "AstrBot 环境不完整"}

    try:
        subprocess.Popen(
            [str(venv_python), str(main_py)],
            cwd=str(astrbot_dir),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return {"success": True, "message": "AstrBot 重启命令已发送"}
    except Exception as e:
        return {"success": False, "error": str(e)}
