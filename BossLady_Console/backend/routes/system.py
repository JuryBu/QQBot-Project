"""
系统设置 + 插件管理 + 人格编辑器 API
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, File, Query, UploadFile
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/system", tags=["system"])

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
ASTRBOT_DIR = BASE_DIR / "AstrBot"
PLUGINS_DIR = ASTRBOT_DIR / "data" / "plugins"
PERSONA_DIR = ASTRBOT_DIR / "data"
CONFIG_PATH = ASTRBOT_DIR / "data" / "cmd_config.json"


# ========================
# 插件管理
# ========================

@router.get("/plugins")
async def list_plugins():
    """列出所有插件"""
    plugins = []
    if PLUGINS_DIR.exists():
        for d in sorted(PLUGINS_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("__"):
                meta = {"name": d.name, "enabled": True}
                # 读取插件 metadata
                metadata_file = d / "metadata.yaml"
                if not metadata_file.exists():
                    metadata_file = d / "_metadata.star.json"
                if metadata_file.exists():
                    try:
                        content = metadata_file.read_text(encoding="utf-8-sig")
                        if metadata_file.suffix == ".json":
                            data = json.loads(content)
                            meta["description"] = data.get("desc", "") or data.get("description", "")
                            meta["version"] = data.get("version", "")
                            meta["author"] = data.get("author", "")
                        elif metadata_file.suffix in (".yaml", ".yml"):
                            # 简易 YAML 键值解析（无需引入 pyyaml 依赖）
                            for line in content.splitlines():
                                line = line.strip()
                                if ":" not in line or line.startswith("#"):
                                    continue
                                key, _, val = line.partition(":")
                                key = key.strip().strip('"').strip("'")
                                val = val.strip().strip('"').strip("'")
                                # 去掉 YAML 行内注释（如 "v1.0.0 # 格式说明"）
                                if " #" in val:
                                    val = val[:val.index(" #")].strip()
                                if key in ("desc", "description") and val and not meta.get("description"):
                                    meta["description"] = val
                                elif key == "version" and val:
                                    meta["version"] = val
                                elif key == "author" and val:
                                    meta["author"] = val
                    except Exception:
                        pass
                plugins.append(meta)
    return {"plugins": plugins}


# ========================
# 人格设定编辑器
# ========================

ASTRBOT_DB = ASTRBOT_DIR / "data" / "data_v4.db"


@router.get("/persona")
async def get_persona():
    """获取当前人格设定"""
    try:
        # 先从 cmd_config.json 获取默认人格名称
        default_name = "default"
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
            default_name = config.get("provider_settings", {}).get("default_personality", "default")

        # 从 AstrBot DB 读取人格数据
        if ASTRBOT_DB.exists():
            import aiosqlite
            async with aiosqlite.connect(str(ASTRBOT_DB)) as db:
                # 检查 personas 表是否存在（AstrBot v4 使用复数表名）
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='personas'"
                )
                if await cursor.fetchone():
                    # 读取所有人格
                    cursor = await db.execute(
                        "SELECT persona_id, system_prompt FROM personas"
                    )
                    personas = []
                    default_prompt = ""
                    for row in await cursor.fetchall():
                        personas.append({"name": row[0], "prompt": row[1] or ""})
                        if row[0] == default_name:
                            default_prompt = row[1] or ""

                    return {
                        "prompt": default_prompt,
                        "default_name": default_name,
                        "personas": personas,
                        "source": "AstrBot DB",
                    }

        # Fallback: cmd_config.json
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
            persona_text = config.get("provider_settings", {}).get("persona_prompt", "")
            return {
                "prompt": persona_text,
                "default_name": default_name,
                "personas": [],
                "source": "cmd_config.json (fallback)",
            }

        return {"prompt": "", "source": "未找到配置"}
    except Exception as e:
        return {"prompt": "", "error": str(e)}


@router.put("/persona")
async def update_persona(body: dict = Body(...)):
    """更新人格设定"""
    try:
        new_prompt = body.get("prompt", "")
        persona_name = body.get("name")  # 指定要编辑的人格名称

        # 优先更新 AstrBot DB
        if ASTRBOT_DB.exists():
            import aiosqlite
            async with aiosqlite.connect(str(ASTRBOT_DB)) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='personas'"
                )
                if await cursor.fetchone():
                    if persona_name:
                        # 更新指定人格
                        await db.execute(
                            "UPDATE personas SET system_prompt = ? WHERE persona_id = ?",
                            (new_prompt, persona_name),
                        )
                    else:
                        # 更新默认人格
                        default_name = "default"
                        if CONFIG_PATH.exists():
                            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                                config = json.load(f)
                            default_name = config.get("provider_settings", {}).get("default_personality", "default")
                        await db.execute(
                            "UPDATE personas SET system_prompt = ? WHERE persona_id = ?",
                            (new_prompt, default_name),
                        )
                    await db.commit()
                    return {"success": True, "source": "AstrBot DB"}

        # Fallback: 写 cmd_config.json
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
            if "provider_settings" not in config:
                config["provider_settings"] = {}
            config["provider_settings"]["persona_prompt"] = new_prompt
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return {"success": True, "source": "cmd_config.json (fallback)"}

        return {"success": False, "error": "无可用存储"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ========================
# 系统设置
# ========================

@router.get("/info")
async def system_info():
    """系统信息"""
    info = {
        "version": "1.0.0",
        "project_root": str(BASE_DIR),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 各组件目录大小
    for name, path in [
        ("QQ_data", BASE_DIR / "QQ_data"),
        ("Memory", BASE_DIR / "Memory"),
        ("Sandbox", BASE_DIR / "Sandbox"),
        ("AstrBot_data", ASTRBOT_DIR / "data"),
    ]:
        if path.exists():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            info[f"{name}_size_mb"] = round(total / 1024 / 1024, 2)
        else:
            info[f"{name}_size_mb"] = 0
    return info


@router.post("/export")
async def export_data():
    """导出系统数据为压缩包"""
    try:
        export_dir = BASE_DIR / "export"
        export_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"BossLady_export_{timestamp}"

        # 要打包的目录
        dirs_to_pack = ["QQ_data", "Memory", "Knowledge", "Sandbox/config", "Sandbox/workspace"]
        temp_dir = export_dir / archive_name
        temp_dir.mkdir(exist_ok=True)

        for d in dirs_to_pack:
            src = BASE_DIR / d
            if src.exists():
                dst = temp_dir / d
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

        # 打包
        archive_path = shutil.make_archive(str(export_dir / archive_name), "zip", str(temp_dir))
        # 清理临时目录
        shutil.rmtree(temp_dir)

        return {
            "success": True,
            "path": archive_path,
            "size_mb": round(os.path.getsize(archive_path) / 1024 / 1024, 2),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/import")
async def import_data(file: UploadFile = File(...)):
    """导入并合并数据包（不覆盖已有文件，含路径安全校验）"""
    import zipfile
    import tempfile

    if not file.filename.endswith(".zip"):
        return {"success": False, "error": "请上传 .zip 格式的导出包"}

    # 白名单：只允许解压到这些顶层目录
    ALLOWED_TOP_DIRS = {"QQ_data", "Memory", "Sandbox", "Knowledge", "export"}

    try:
        # Save uploaded file to temp
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # Extract and merge
        merged = 0
        skipped = 0
        rejected = 0
        with zipfile.ZipFile(tmp_path, "r") as zf:
            for member in zf.namelist():
                # Skip directories
                if member.endswith("/"):
                    continue

                # === 路径安全校验 (防 Zip Slip) ===
                # 拒绝绝对路径
                if os.path.isabs(member):
                    rejected += 1
                    continue
                # 拒绝路径穿越
                normalized = os.path.normpath(member)
                if normalized.startswith("..") or "\\.." in normalized or "/.." in normalized:
                    rejected += 1
                    continue
                # 拒绝 Windows 盘符路径 (如 C:\...)
                if len(normalized) >= 2 and normalized[1] == ":":
                    rejected += 1
                    continue
                # 白名单检查：顶层目录必须在允许列表中
                top_dir = normalized.split(os.sep)[0].split("/")[0]
                if top_dir not in ALLOWED_TOP_DIRS:
                    rejected += 1
                    continue

                target = (BASE_DIR / normalized).resolve()
                # 最终安全检查：确保 target 在 BASE_DIR 内
                if not str(target).startswith(str(BASE_DIR.resolve())):
                    rejected += 1
                    continue

                if target.exists():
                    skipped += 1
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    merged += 1

        # Cleanup temp
        os.unlink(tmp_path)

        result_msg = f"导入完成: {merged} 个新文件, {skipped} 个已存在(跳过)"
        if rejected > 0:
            result_msg += f", {rejected} 个路径不安全(已拒绝)"
        return {"success": True, "message": result_msg}
    except zipfile.BadZipFile:
        return {"success": False, "error": "文件不是有效的 ZIP 压缩包"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/logs")
async def get_logs(lines: int = Query(100, le=500)):
    """获取最近日志"""
    # 搜索 data/*.log 和 data/logs/*.log（AstrBot v4 日志在 data/logs/ 下）
    log_candidates = list((ASTRBOT_DIR / "data").glob("*.log"))
    logs_subdir = ASTRBOT_DIR / "data" / "logs"
    if logs_subdir.exists():
        log_candidates.extend(logs_subdir.glob("*.log"))
    log_files = sorted(
        log_candidates,
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not log_files:
        return {"logs": [], "file": None}

    log_file = log_files[0]
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return {"logs": all_lines[-lines:], "file": log_file.name}
    except Exception as e:
        return {"logs": [], "error": str(e)}


# ========================
# 安全设置
# ========================

CONSOLE_CONFIG = BASE_DIR / "BossLady_Console" / "config.json"


@router.post("/password")
async def set_console_password(body: dict = Body(...)):
    """设置/清除控制台密码"""
    import hashlib
    password = body.get("password", "")
    try:
        config = {}
        if CONSOLE_CONFIG.exists():
            with open(CONSOLE_CONFIG, "r", encoding="utf-8") as f:
                config = json.load(f)

        if password:
            # 设置密码（SHA-256 哈希存储）
            config["password_hash"] = hashlib.sha256(password.encode()).hexdigest()
        else:
            # 清除密码
            config.pop("password_hash", None)

        with open(CONSOLE_CONFIG, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

