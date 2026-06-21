"""模型配置路由——三模型选型/API Key/参数管理"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CMD_CONFIG = PROJECT_ROOT / "AstrBot" / "data" / "cmd_config.json"
FLASHLITE_CONFIG = PROJECT_ROOT / "AstrBot" / "data" / "plugins" / "astrbot_plugin_flashlite" / "config.json"


def _load_cmd_config() -> Dict[str, Any]:
    if CMD_CONFIG.exists():
        with open(CMD_CONFIG, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {}


def _save_cmd_config(config: Dict[str, Any]):
    with open(CMD_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _load_flashlite_config() -> Dict[str, Any]:
    if FLASHLITE_CONFIG.exists():
        with open(FLASHLITE_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_flashlite_config(config: Dict[str, Any]):
    with open(FLASHLITE_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# ============================================================
# API Key 管理
# ============================================================

@router.get("/api-key")
async def get_api_key_status():
    """获取 API Key 状态（脱敏）"""
    config = _load_cmd_config()
    providers = config.get("provider", [])

    keys_info = []
    for p in providers:
        keys = p.get("key", [])
        masked = [f"***{k[-6:]}" if len(k) > 6 else "***" for k in keys]
        keys_info.append({
            "id": p.get("id", ""),
            "type": p.get("type", ""),
            "enabled": p.get("enable", False),
            "keys": masked,
            "key_count": len(keys),
        })

    return {"providers": keys_info}


class UpdateApiKeyRequest(BaseModel):
    provider_id: str
    keys: List[str]


@router.post("/api-key")
async def update_api_key(req: UpdateApiKeyRequest):
    """更新 API Key"""
    config = _load_cmd_config()
    providers = config.get("provider", [])

    for p in providers:
        if p.get("id") == req.provider_id:
            p["key"] = req.keys
            _save_cmd_config(config)
            return {"success": True, "message": f"已更新 {req.provider_id} 的 API Key"}

    raise HTTPException(404, f"未找到 provider: {req.provider_id}")


# ============================================================
# 主模型配置
# ============================================================

@router.get("/main-model")
async def get_main_model():
    """获取主模型配置"""
    config = _load_cmd_config()
    providers = config.get("provider", [])

    for p in providers:
        if p.get("enable"):
            model_config = p.get("model_config", {})
            result = {
                "id": p.get("id", ""),
                "type": p.get("type", ""),
                "model": model_config.get("model", ""),
                "max_tokens": model_config.get("max_tokens", 4096),
                "temperature": model_config.get("temperature", 0.7),
            }
            # 思考参数
            if "thinking_level" in model_config:
                result["thinking_level"] = model_config["thinking_level"]
            if "thinking_budget" in model_config:
                result["thinking_budget"] = model_config["thinking_budget"]
            return result

    return {"error": "无已启用的 provider"}


class UpdateMainModelRequest(BaseModel):
    provider_id: str
    model: str
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    thinking_level: Optional[str] = None
    thinking_budget: Optional[int] = None


@router.post("/main-model")
async def update_main_model(req: UpdateMainModelRequest):
    """更新主模型"""
    config = _load_cmd_config()
    providers = config.get("provider", [])

    for p in providers:
        if p.get("id") == req.provider_id:
            mc = p.setdefault("model_config", {})
            mc["model"] = req.model
            if req.max_tokens is not None:
                mc["max_tokens"] = req.max_tokens
            if req.temperature is not None:
                mc["temperature"] = req.temperature
            if req.thinking_level is not None:
                mc["thinking_level"] = req.thinking_level
            if req.thinking_budget is not None:
                mc["thinking_budget"] = req.thinking_budget
            _save_cmd_config(config)
            return {"success": True}

    raise HTTPException(404, f"未找到 provider: {req.provider_id}")


# ============================================================
# Flash Lite 配置
# ============================================================

@router.get("/flashlite")
async def get_flashlite_config():
    """获取 Flash Lite 配置"""
    config = _load_flashlite_config()
    result = {
        "model": config.get("model", "gemini-3.1-flash-lite-preview"),
        "sync_interval": config.get("sync_interval", 5),
        "checkpoint_limit": config.get("checkpoint_limit", config.get("checkpoint_token_limit", 50000)),
        "checkpoint_keep_recent": config.get("checkpoint_keep_recent", 10),
        "checkpoint_compress_front_ratio": config.get("checkpoint_compress_front_ratio", 0.7),
        "checkpoint_cooldown_seconds": config.get("checkpoint_cooldown_seconds", 300),
        "checkpoint_target_min": config.get("checkpoint_target_min", 0.20),
        "checkpoint_target_max": config.get("checkpoint_target_max", 0.40),
        "thinking_config": config.get("thinking_config", {"thinkingBudget": 1024}),
        "wake_keywords": config.get("wake_keywords", []),
        "review_interval_hours": config.get("review_interval_hours", 24),
        # 采样策略
        "sampling_mode": config.get("sampling_mode", "dynamic"),
        "sync_time_min_msgs": config.get("sync_time_min_msgs", 3),
        "sync_time_interval": config.get("sync_time_interval", 60),
        "dynamic_sampling": config.get("dynamic_sampling", {
            "window_minutes": 10,
            "thresholds": [5, 15, 30],
            "intervals": [3, 5, 10, 15],
        }),
        # 群聊独立配置
        "group_overrides": config.get("group_overrides", {}),
    }
    if "thinking_level" in config:
        result["thinking_level"] = config["thinking_level"]
    return result


class UpdateFlashLiteRequest(BaseModel):
    model: Optional[str] = None
    sync_interval: Optional[int] = None
    checkpoint_limit: Optional[int] = None
    checkpoint_keep_recent: Optional[int] = None
    checkpoint_compress_front_ratio: Optional[float] = None
    checkpoint_cooldown_seconds: Optional[int] = None
    checkpoint_target_min: Optional[float] = None
    checkpoint_target_max: Optional[float] = None
    thinking_budget: Optional[int] = None
    thinking_level: Optional[str] = None
    wake_keywords: Optional[List[str]] = None
    review_interval_hours: Optional[int] = None
    # 采样策略
    sampling_mode: Optional[str] = None
    sync_time_min_msgs: Optional[int] = None
    sync_time_interval: Optional[int] = None  # 时间兜底触发间隔（秒）
    dynamic_sampling: Optional[Dict[str, Any]] = None
    # 群聊独立配置
    group_overrides: Optional[Dict[str, Any]] = None


@router.post("/flashlite")
async def update_flashlite_config(req: UpdateFlashLiteRequest):
    """更新 Flash Lite 配置"""
    config = _load_flashlite_config()

    if req.model:
        config["model"] = req.model
    if req.sync_interval is not None:
        config["sync_interval"] = req.sync_interval
    if req.checkpoint_limit is not None:
        config["checkpoint_limit"] = max(1000, min(500000, req.checkpoint_limit))
    if req.checkpoint_keep_recent is not None:
        config["checkpoint_keep_recent"] = max(3, min(50, req.checkpoint_keep_recent))
    if req.checkpoint_compress_front_ratio is not None:
        config["checkpoint_compress_front_ratio"] = max(0.3, min(0.9, req.checkpoint_compress_front_ratio))
    if req.checkpoint_cooldown_seconds is not None:
        config["checkpoint_cooldown_seconds"] = max(60, min(3600, req.checkpoint_cooldown_seconds))
    if req.checkpoint_target_min is not None:
        config["checkpoint_target_min"] = max(0.05, min(0.50, req.checkpoint_target_min))
    if req.checkpoint_target_max is not None:
        config["checkpoint_target_max"] = max(0.10, min(0.70, req.checkpoint_target_max))

    # 约束: target_min ≤ target_max
    _tmin = config.get("checkpoint_target_min")
    _tmax = config.get("checkpoint_target_max")
    if _tmin is not None and _tmax is not None and _tmin > _tmax:
        config["checkpoint_target_min"], config["checkpoint_target_max"] = _tmax, _tmin

    if req.thinking_budget is not None:
        config.setdefault("thinking_config", {})["thinkingBudget"] = req.thinking_budget
    if req.thinking_level is not None:
        config["thinking_level"] = req.thinking_level
    if req.wake_keywords is not None:
        config["wake_keywords"] = req.wake_keywords
    if req.review_interval_hours is not None:
        config["review_interval_hours"] = max(1, min(168, req.review_interval_hours))
    # 采样策略
    if req.sampling_mode is not None:
        config["sampling_mode"] = req.sampling_mode if req.sampling_mode in ("fixed", "dynamic") else "fixed"
    if req.sync_time_min_msgs is not None:
        config["sync_time_min_msgs"] = max(0, min(10, req.sync_time_min_msgs))
    if req.sync_time_interval is not None:
        config["sync_time_interval"] = max(30, min(600, req.sync_time_interval))
    if req.dynamic_sampling is not None:
        ds = req.dynamic_sampling
        config["dynamic_sampling"] = {
            "window_minutes": max(1, min(60, ds.get("window_minutes", 10))),
            "thresholds": ds.get("thresholds", [5, 15, 30]),
            "intervals": ds.get("intervals", [3, 5, 10, 15]),
        }
    # 群聊独立配置
    if req.group_overrides is not None:
        # 校验：key 必须是纯数字群号，value 必须是 dict
        validated = {}
        for gid, cfg in req.group_overrides.items():
            if not isinstance(gid, str) or not gid.isdigit():
                continue
            if not isinstance(cfg, dict):
                continue
            entry = {
                "sync_interval": max(1, min(60, int(cfg.get("sync_interval", 5)))),
                "enabled": bool(cfg.get("enabled", True)),
            }
            # 可选扩展字段
            if cfg.get("reply_length_limit") is not None:
                entry["reply_length_limit"] = min(65536, max(0, int(cfg["reply_length_limit"]))) or None
            if cfg.get("tool_permission") in ("full", "search_only", "none"):
                entry["tool_permission"] = cfg["tool_permission"]
            if cfg.get("main_thinking_budget") is not None:
                entry["main_thinking_budget"] = min(32768, max(0, int(cfg["main_thinking_budget"]))) or None
            validated[gid] = entry
        config["group_overrides"] = validated

    _save_flashlite_config(config)
    return {"success": True}


# ============================================================
# 消息持久化策略
# ============================================================

@router.get("/storage-policy")
async def get_storage_policy():
    """获取消息持久化分级策略"""
    config = _load_flashlite_config()
    policy = config.get("storage_policy", {})
    return {
        "hot_days": policy.get("hot_days", 7),
        "cold_days": policy.get("cold_days", 30),
        "archive_days": policy.get("archive_days", 90),
        "enable_auto_cleanup": policy.get("enable_auto_cleanup", True),
    }


class UpdateStoragePolicyRequest(BaseModel):
    hot_days: Optional[int] = None
    cold_days: Optional[int] = None
    archive_days: Optional[int] = None
    enable_auto_cleanup: Optional[bool] = None


@router.post("/storage-policy")
async def update_storage_policy(req: UpdateStoragePolicyRequest):
    """更新消息持久化分级策略"""
    config = _load_flashlite_config()
    policy = config.setdefault("storage_policy", {})

    if req.hot_days is not None:
        policy["hot_days"] = max(1, min(30, req.hot_days))
    if req.cold_days is not None:
        policy["cold_days"] = max(7, min(180, req.cold_days))
    if req.archive_days is not None:
        policy["archive_days"] = max(30, min(365, req.archive_days))
    if req.enable_auto_cleanup is not None:
        policy["enable_auto_cleanup"] = req.enable_auto_cleanup

    # 约束: hot < cold < archive
    hot = policy.get("hot_days", 7)
    cold = policy.get("cold_days", 30)
    archive = policy.get("archive_days", 90)
    if cold <= hot:
        policy["cold_days"] = hot + 7
    if archive <= policy.get("cold_days", 30):
        policy["archive_days"] = policy.get("cold_days", 30) + 30

    _save_flashlite_config(config)
    return {"success": True, "policy": policy}


# ============================================================
# 模型列表（从 Gemini API 获取）
# ============================================================

@router.get("/available")
async def list_available_models():
    """列出可用模型（含能力标记）"""
    config = _load_cmd_config()
    providers = config.get("provider", [])

    api_key = None
    for p in providers:
        keys = p.get("key", [])
        if keys:
            api_key = keys[0]
            break

    if not api_key:
        return {"models": [], "error": "未配置 API Key"}

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = []
                    for m in data.get("models", []):
                        name = m.get("name", "").replace("models/", "")
                        methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" not in methods and "generateImages" not in methods:
                            continue  # 跳过 embedding/TTS 等非生成模型

                        # 能力探测：从 API 元数据推断
                        name_lower = name.lower()
                        has_thinking = bool(m.get("thinking", False))
                        has_image_gen = "generateImages" in methods or "imagen" in name_lower or "image" in name_lower
                        has_generate = "generateContent" in methods

                        # 参数支持检测
                        capabilities = {
                            "generateContent": has_generate,
                            "thinking": has_thinking,
                            "imageGeneration": has_image_gen,
                            "temperature": m.get("temperature") is not None or has_generate,
                            "topP": m.get("topP") is not None,
                            "topK": m.get("topK") is not None,
                        }

                        models.append({
                            "name": name,
                            "displayName": m.get("displayName", ""),
                            "inputTokenLimit": m.get("inputTokenLimit", 0),
                            "outputTokenLimit": m.get("outputTokenLimit", 0),
                            "supportedMethods": methods,
                            "hasImageGen": has_image_gen,
                            "capabilities": capabilities,
                        })
                    return {"models": models}
                return {"models": [], "error": f"API 返回 {resp.status}"}
    except Exception as e:
        return {"models": [], "error": str(e)}


# ============================================================
# 工具模型配置
# ============================================================

@router.get("/tool-model")
async def get_tool_model():
    """获取工具模型配置"""
    config = _load_flashlite_config()
    tool = config.get("tool_model", {})
    # 脱敏显示 api_keys
    raw_keys = tool.get("api_keys", [])
    masked_keys = [f"***{k[-6:]}" if len(k) > 6 else "***" for k in raw_keys]
    return {
        "model": tool.get("model", ""),
        "thinking_level": tool.get("thinking_level", "HIGH"),
        "thinking_budget": tool.get("thinking_budget", 4096),
        "api_keys": masked_keys,
        "api_keys_raw": raw_keys,
        "api_keys_count": len(raw_keys),
    }


class UpdateToolModelRequest(BaseModel):
    model: Optional[str] = None
    thinking_level: Optional[str] = None
    thinking_budget: Optional[int] = None
    api_keys: Optional[List[str]] = None


@router.post("/tool-model")
async def update_tool_model(req: UpdateToolModelRequest):
    """更新工具模型配置"""
    config = _load_flashlite_config()
    tool = config.setdefault("tool_model", {})

    if req.model is not None:
        tool["model"] = req.model
    if req.thinking_level is not None:
        tool["thinking_level"] = req.thinking_level
    if req.thinking_budget is not None:
        tool["thinking_budget"] = req.thinking_budget
    if req.api_keys is not None:
        # 过滤空字符串
        tool["api_keys"] = [k for k in req.api_keys if k and k.strip()]

    config["tool_model"] = tool
    _save_flashlite_config(config)
    return {"success": True}


# ============================================================
# 图像模型配置
# ============================================================

@router.get("/image-model")
async def get_image_model():
    """获取图像生成模型配置"""
    config = _load_flashlite_config()
    img = config.get("image_model", {})
    return {
        "model": img.get("model", "gemini-2.5-flash-image"),
        "aspect_ratio": img.get("default_aspect_ratio", "auto"),
        "image_size": img.get("image_size", "1K"),
        "number_of_images": img.get("number_of_images", 1),
        "thinking_level": img.get("thinking_level", ""),
    }


class UpdateImageModelRequest(BaseModel):
    model: Optional[str] = None
    default_aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None
    number_of_images: Optional[int] = None
    thinking_level: Optional[str] = None


@router.post("/image-model")
async def update_image_model(req: UpdateImageModelRequest):
    """更新图像生成模型配置"""
    config = _load_flashlite_config()
    img = config.setdefault("image_model", {})

    if req.model is not None:
        img["model"] = req.model
    if req.default_aspect_ratio is not None:
        img["default_aspect_ratio"] = req.default_aspect_ratio
    if req.image_size is not None:
        img["image_size"] = req.image_size
    if req.number_of_images is not None:
        img["number_of_images"] = max(1, min(4, req.number_of_images))
    if req.thinking_level is not None:
        img["thinking_level"] = req.thinking_level

    config["image_model"] = img
    _save_flashlite_config(config)
    return {"success": True}


# ============================================================
# 调试设置（控制 AstrBot 原生 show_tool_use_status）
# ============================================================

@router.get("/debug-settings")
async def get_debug_settings():
    """获取调试设置"""
    config = _load_cmd_config()
    ps = config.get("provider_settings", {})
    return {
        "show_tool_use_status": ps.get("show_tool_use_status", False),
    }


class UpdateDebugSettingsRequest(BaseModel):
    show_tool_use_status: Optional[bool] = None


@router.post("/debug-settings")
async def update_debug_settings(req: UpdateDebugSettingsRequest):
    """更新调试设置"""
    config = _load_cmd_config()
    ps = config.setdefault("provider_settings", {})

    if req.show_tool_use_status is not None:
        ps["show_tool_use_status"] = req.show_tool_use_status

    _save_cmd_config(config)
    return {"success": True, "note": "重启 AstrBot 后生效"}


# ============================================================
# 分段回复设置
# ============================================================

@router.get("/segmented-reply")
async def get_segmented_reply():
    """获取分段回复设置"""
    config = _load_cmd_config()
    seg = config.get("platform_settings", {}).get("segmented_reply", {})
    ad = seg.get("adaptive_delays", {})
    return {
        "interval_method": seg.get("interval_method", "adaptive"),
        "interval": seg.get("interval", "0.8,4.5"),
        "merge_threshold": seg.get("merge_threshold", 80),
        "content_cleanup_rule": seg.get("content_cleanup_rule", ""),
        "delay_short": ad.get("short", "0.8,1.5"),
        "delay_medium": ad.get("medium", "1.5,3.0"),
        "delay_long": ad.get("long", "2.5,4.5"),
        "emoji_send_after_segment": seg.get("emoji_send_after_segment", 1),
        "emoji_probability": seg.get("emoji_probability", 0.7),
        "max_segments": seg.get("max_segments", 3),
    }


class UpdateSegmentedReplyRequest(BaseModel):
    interval_method: Optional[str] = None
    interval: Optional[str] = None
    merge_threshold: Optional[int] = None
    content_cleanup_rule: Optional[str] = None
    delay_short: Optional[str] = None
    delay_medium: Optional[str] = None
    delay_long: Optional[str] = None
    emoji_send_after_segment: Optional[int] = None
    emoji_probability: Optional[float] = None
    max_segments: Optional[int] = None


@router.post("/segmented-reply")
async def update_segmented_reply(req: UpdateSegmentedReplyRequest):
    """更新分段回复设置"""
    config = _load_cmd_config()
    seg = config.setdefault("platform_settings", {}).setdefault("segmented_reply", {})

    if req.interval_method is not None:
        seg["interval_method"] = req.interval_method
    if req.interval is not None:
        seg["interval"] = req.interval
    if req.merge_threshold is not None:
        seg["merge_threshold"] = max(0, min(200, req.merge_threshold))
    if req.content_cleanup_rule is not None:
        seg["content_cleanup_rule"] = req.content_cleanup_rule

    # adaptive_delays
    ad = seg.setdefault("adaptive_delays", {})
    if req.delay_short is not None:
        ad["short"] = req.delay_short
    if req.delay_medium is not None:
        ad["medium"] = req.delay_medium
    if req.delay_long is not None:
        ad["long"] = req.delay_long

    # emoji_send_after_segment
    if req.emoji_send_after_segment is not None:
        seg["emoji_send_after_segment"] = max(1, min(20, req.emoji_send_after_segment))

    # emoji_probability
    if req.emoji_probability is not None:
        seg["emoji_probability"] = max(0.0, min(1.0, req.emoji_probability))

    # max_segments
    if req.max_segments is not None:
        seg["max_segments"] = max(0, min(10, req.max_segments))

    _save_cmd_config(config)
    return {"success": True, "note": "重启 AstrBot 后生效"}


# ============================================================
# 表情包管理（内化到 FlashLite）
# ============================================================

EMOJI_DIR = PROJECT_ROOT / "表情包"

# 语气词集合——匹配到的归类为「通用」tag
TONE_PARTICLES = {
    '啊', '哦', '嗯', '呢', '吧', '嘛', '呀', '哇', '噢', '嘿',
    '哈', '呐', '哎', '喂', '唉', '嗨', '吗', '么', '了', '的',
    '呃', '哼', '噗', '嘻', '啦', '咯', '喔', '耶', '诶', '欸',
    '噫', '你', '我', '他',  # 人称代词也属于通用
}

@router.get("/emojis")
async def list_emojis():
    """列出本地表情包文件及其关键词映射（分离内容词和语气词）"""
    if not EMOJI_DIR.is_dir():
        return {"emojis": [], "total": 0}

    supported = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    emojis = []
    for f in sorted(EMOJI_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in supported:
            name_no_ext = f.stem
            all_keywords = [kw.strip() for kw in name_no_ext.split() if kw.strip()]
            content_tags = [kw for kw in all_keywords if kw not in TONE_PARTICLES]
            tone_tags = [kw for kw in all_keywords if kw in TONE_PARTICLES]
            emojis.append({
                "name": f.name,
                "keywords": all_keywords,          # 原始关键词（完整）
                "content_tags": content_tags,       # 内容关键词
                "is_universal": len(tone_tags) > 0, # 是否含语气词→通用
                "size_kb": round(f.stat().st_size / 1024, 1),
                "ext": f.suffix.lower(),
            })
    return {"emojis": emojis, "total": len(emojis)}


@router.get("/emojis/image/{filename}")
async def get_emoji_image(filename: str):
    """获取表情包图片文件"""
    from fastapi.responses import FileResponse
    import mimetypes
    fpath = (EMOJI_DIR / filename).resolve()
    # 路径穿越防护：确保解析后仍在 EMOJI_DIR 内
    if not str(fpath).startswith(str(EMOJI_DIR.resolve())):
        raise HTTPException(403, "路径越界")
    if not fpath.is_file():
        raise HTTPException(404, f"表情包不存在: {filename}")
    mime = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
    return FileResponse(str(fpath), media_type=mime)


class UpdateEmojiRequest(BaseModel):
    old_name: str
    new_keywords: List[str]


@router.post("/emojis/update-keywords")
async def update_emoji_keywords(req: UpdateEmojiRequest):
    """更新表情包关键词（通过重命名文件）"""
    old_path = (EMOJI_DIR / req.old_name).resolve()
    if not str(old_path).startswith(str(EMOJI_DIR.resolve())):
        raise HTTPException(403, "路径越界")
    if not old_path.is_file():
        raise HTTPException(404, f"表情包不存在: {req.old_name}")

    ext = old_path.suffix
    new_name = " ".join(kw.strip() for kw in req.new_keywords if kw.strip()) + ext
    new_path = EMOJI_DIR / new_name

    if new_path.exists() and new_path != old_path:
        raise HTTPException(409, f"文件名已存在: {new_name}")

    old_path.rename(new_path)
    return {"success": True, "new_name": new_name, "note": "重启 AstrBot 后 FlashLite 会重新扫描关键词"}


@router.delete("/emojis/{filename}")
async def delete_emoji(filename: str):
    """删除一个表情包"""
    fpath = (EMOJI_DIR / filename).resolve()
    if not str(fpath).startswith(str(EMOJI_DIR.resolve())):
        raise HTTPException(403, "路径越界")
    if not fpath.is_file():
        raise HTTPException(404, f"表情包不存在: {filename}")
    fpath.unlink()
    return {"success": True, "note": "重启 AstrBot 后生效"}


@router.post("/emojis/upload")
async def upload_emoji_files(files: List[UploadFile] = File(...)):
    """批量上传表情包文件"""
    if not EMOJI_DIR.is_dir():
        EMOJI_DIR.mkdir(parents=True, exist_ok=True)

    supported = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    saved = []
    for f in files:
        ext = Path(f.filename).suffix.lower() if f.filename else ""
        if ext not in supported:
            continue
        target = EMOJI_DIR / f.filename
        # 避免覆盖同名文件
        if target.exists():
            stem = Path(f.filename).stem
            idx = 1
            while target.exists():
                target = EMOJI_DIR / f"{stem}_{idx}{ext}"
                idx += 1
        content = await f.read()
        target.write_bytes(content)
        saved.append(target.name)

    return {"success": True, "saved": saved, "count": len(saved), "note": "重启 AstrBot 后 FlashLite 会重新扫描关键词"}

