import asyncio
import json
import os
import re
import sys
import random
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

# 确保插件目录在 sys.path 中（AstrBot 插件加载器不自动处理）
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.message_event_result import MessageChain

# CHECKPOINT 压缩模块
try:
    from .checkpoint import CheckpointManager, TFileManager
except ImportError:
    from checkpoint import CheckpointManager, TFileManager

# Knowledge + Memory 双系统
try:
    from .knowledge import KnowledgeCache
    from .memory import MemoryStore
except ImportError:
    from knowledge import KnowledgeCache
    from memory import MemoryStore

# KV Cache 管理
try:
    from .kv_cache import KVCacheManager
except ImportError:
    from kv_cache import KVCacheManager

# Agent 集成
try:
    from .agent import AgentRequestBuilder
except ImportError:
    from agent import AgentRequestBuilder

# Sandbox 管理
try:
    from .sandbox import SandboxManager
except ImportError:
    from sandbox import SandboxManager

# ToolRegistry 动态工具注册表
try:
    from .tool_registry import ToolRegistry, SANDBOX_ROOT
except ImportError:
    from tool_registry import ToolRegistry, SANDBOX_ROOT

# Web Fetch 引擎
try:
    from .web_engine import WebFetchEngine
except ImportError:
    from web_engine import WebFetchEngine

# 成本追踪
try:
    from .cost_tracker import CostTracker
except ImportError:
    from cost_tracker import CostTracker

# 上下文/提示构建 Mixin（S2.5 拆分）
try:
    from .context_mixin import ContextMixin
except ImportError:
    from context_mixin import ContextMixin

# ============================================================
# Flash Lite 中断引擎
# 作用：CPU 中断处理器——维护上下文 + 决定是否唤醒主模型
# 文档：Plan_1_models.md / Test_Stage5_flashlite.md
# ============================================================

# Gemini REST API 端点
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
FLASH_LITE_MODEL = "gemini-3.1-flash-lite-preview"
TOOL_MODEL_DEFAULT = "gemini-3-flash-preview"  # 工具模型默认

# 默认关键词
DEFAULT_WAKE_KEYWORDS = ["老板娘", "boss", "老板"]


@register(
    "astrbot_plugin_flashlite",
    "BossLady",
    "Flash Lite 中断引擎。作为 CPU 中断处理器，负责上下文维护（Knowledge 更新 + CHECKPOINT 压缩）和主模型触发决策。",
    "1.0.0",
)
class FlashLiteEngine(ContextMixin, Star):
    """Flash Lite 中断引擎

    触发方式:
    1. 同步: 每隔 N 条群消息触发
    2. 异步: @/唤醒词/CHECKPOINT 超限/工具反馈

    执行任务:
    1. Knowledge 更新（摘要当前群聊话题）
    2. 响应判断（是否需要主模型回复）
    3. CHECKPOINT 检查（token 是否超限）
    """

    _task_counter = 0  # 类级 Task ID 计数器
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._raw_config = config or {}

        # 配置
        # 兼容旧配置键 sync_interval → 新键 sync_trigger_interval
        self._sync_interval = self._cfg("sync_trigger_interval", None) or self._cfg("sync_interval", 5)
        _kw_raw = self._cfg("wake_keywords", DEFAULT_WAKE_KEYWORDS)
        # 兼容字符串配置（逗号切分）和列表配置
        if isinstance(_kw_raw, str):
            self._wake_keywords = [kw.strip() for kw in _kw_raw.split(",") if kw.strip()]
        elif isinstance(_kw_raw, list):
            self._wake_keywords = _kw_raw
        else:
            self._wake_keywords = list(DEFAULT_WAKE_KEYWORDS)
        self._api_key = self._cfg("gemini_api_key", "")
        # FlashLite 主模型名（面板可切；缺省回退模块常量）
        self._model = self._cfg("model", FLASH_LITE_MODEL)
        self._thinking_level = self._cfg("thinking_level", "MEDIUM")
        self._max_context_for_judgment = self._cfg("max_context_messages", 15)
        self._config = self._raw_config  # 供工具方法读取子配置
        # 工具模型名（优先于 Flash Lite 用于 search(scope=web) 等场景）
        _tool_cfg = self._cfg("tool_model", {})
        self._tool_model = _tool_cfg.get("model", "") if isinstance(_tool_cfg, dict) else ""
        self._tool_thinking_budget = _tool_cfg.get("thinking_budget", 2048) if isinstance(_tool_cfg, dict) else 2048
        # 工具模型 API Key 池 — Round-robin 轮转 + 429 自动切换
        _api_keys_raw = _tool_cfg.get("api_keys", []) if isinstance(_tool_cfg, dict) else []
        self._tool_api_keys: List[str] = [k for k in _api_keys_raw if k and isinstance(k, str)]
        self._tool_key_index = 0  # 当前轮转索引
        self._tool_key_cooldown: Dict[int, float] = {}  # key索引 → 冷却截止时间

        # 状态
        self._msg_counters: Dict[str, int] = defaultdict(int)  # window_id → 计数
        self._last_sync_times: Dict[str, float] = {}  # window_id → 上次同步时间戳
        self._sync_time_interval = self._cfg("sync_time_interval", 60)  # 秒
        self._sync_time_min_msgs = self._cfg("sync_time_min_msgs", 3)  # 时间兜底最低消息数
        self._sampling_mode = self._cfg("sampling_mode", "dynamic")  # dynamic / fixed
        
        # 智能动态采样配置（4 级活跃度阈值 + 对应间隔）
        _dyn_cfg = self._cfg_json("dynamic_sampling", {})
        self._dyn_window_minutes = _dyn_cfg.get("window_minutes", 10) if isinstance(_dyn_cfg, dict) else 10
        # 防御性校验：确保 window_minutes 为正整数
        try:
            self._dyn_window_minutes = max(1, int(self._dyn_window_minutes))
        except (TypeError, ValueError):
            self._dyn_window_minutes = 10
        self._dyn_thresholds = _dyn_cfg.get("thresholds", [5, 15, 30]) if isinstance(_dyn_cfg, dict) else [5, 15, 30]
        self._dyn_intervals = _dyn_cfg.get("intervals", [3, 5, 10, 15]) if isinstance(_dyn_cfg, dict) else [3, 5, 10, 15]
        # 防御性校验：确保阈值升序、间隔全为正整数、长度匹配
        self._dyn_thresholds = sorted([max(1, int(x)) for x in self._dyn_thresholds if isinstance(x, (int, float))])
        self._dyn_intervals = [max(1, int(x)) for x in self._dyn_intervals if isinstance(x, (int, float))]
        if not self._dyn_intervals:
            self._dyn_intervals = [3, 5, 10, 15]  # 回退默认
        # 确保 intervals 长度 = thresholds + 1
        while len(self._dyn_intervals) <= len(self._dyn_thresholds):
            self._dyn_intervals.append(self._dyn_intervals[-1])
        # 滑动窗口：group_id → deque of timestamps
        from collections import deque as _deque
        self._recent_msg_timestamps: Dict[str, _deque] = defaultdict(lambda: _deque(maxlen=200))
        self._knowledge_cache: Dict[str, str] = {}  # window_id → 话题摘要
        self._session: Optional[aiohttp.ClientSession] = None
        self._pending_lock = asyncio.Lock()
        self._pending_task_wakes: list = []  # 后台 Task 完成后的待唤醒队列
        # [R1重构] _current_window_key 已废弃，窗口标识改为参数传递
        # 保留字段仅做向后兼容（不再用于记账）
        self._current_window_key: str = "unknown"
        self._pending_user_messages: Dict[str, list] = {}  # Gap3: UMO → 新消息队列（agent run 期间暂存）
        self._task_pool: Dict[str, Dict] = {}  # 后台 Task 池（task_set 工具使用）
        self._last_review_time: float = time.time()  # FIX-5: 上次 Sandbox Review 时间
        self._review_interval_hours: int = int(self._cfg("review_interval_hours", "24"))  # Review 间隔
        self._review_active: bool = False  # 定期 Review 进程状态（仅 _run_review 控制）

        # @quoted 快捷语法变量表
        self._quoted_vars: Dict[str, str] = {}

        # 统计
        self._stats = {
            "total_calls": 0,
            "sync_triggers": 0,
            "async_triggers": 0,
            "main_model_notified": 0,
            "errors": 0,
            "avg_latency_ms": 0.0,
        }

        # CHECKPOINT 管理器（v1 兼容层 + v2 T 文件管理器）
        self._checkpoint_mgr = CheckpointManager(
            token_limit=self._cfg("checkpoint_limit", self._cfg("checkpoint_token_limit", 50000)),
            keep_recent=self._cfg("checkpoint_keep_recent", 10),
        )
        self._t_file_mgr = TFileManager()
        self._stats["checkpoints"] = 0

        # Knowledge + Memory 双系统
        self._knowledge = KnowledgeCache()
        # 设置 bot QQ 号黑名单，防止为 bot 自身创建画像
        try:
            config_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..", "cmd_config.json")
            )
            with open(config_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            bot_ids = set()
            for plat in cfg.get("platform", []):
                if plat.get("id", "").startswith("aiocqhttp"):
                    # aiocqhttp 的 session_id 通常就是 bot QQ 号（或从连接中获取）
                    pass  # QQ号在运行时才知道，这里用 hardcode 兜底
            # 从已有画像中找 nickname 为 "老板娘" 的也排除
            # bot QQ 运行时从 OneBot 连接 self_id 获取（Plan_5 S1 完善）；此处不硬编码（脱敏）
            self._knowledge._bot_qq_ids = bot_ids
            logger.debug(f"Bot QQ 号黑名单: {bot_ids}")
        except Exception:
            self._knowledge._bot_qq_ids = set()
        self._memory = MemoryStore()

        # KVCacheManager 延迟到 on_loaded 中创建（此时 API Key 尚未就绪）
        self._kv_cache = None
        self._tool_kv_cache = None
        try:
            self._sandbox = SandboxManager()
        except Exception as e:
            logger.warning(f"SandboxManager 初始化失败: {e}")
            self._sandbox = None

        # ToolRegistry 动态工具注册表
        try:
            self._tool_registry = ToolRegistry(sandbox_mgr=self._sandbox)
        except Exception as e:
            logger.warning(f"ToolRegistry 初始化失败: {e}")
            self._tool_registry = None

        self._agent_builder = AgentRequestBuilder(
            knowledge_cache=self._knowledge,
            memory_store=self._memory,
            checkpoint_mgr=self._checkpoint_mgr,
            sandbox_mgr=self._sandbox,
            tool_registry=self._tool_registry,
        )

        # CostTracker 成本追踪
        try:
            _cost_dir = os.path.join(SANDBOX_ROOT, "cost_logs")
            _cost_usd_to_cny = self._cfg("cost_usd_to_cny", 7.2)
            _cost_custom_pricing = self._cfg_json("cost_custom_pricing", {})
            self._cost_tracker = CostTracker(
                data_dir=_cost_dir,
                usd_to_cny=_cost_usd_to_cny if isinstance(_cost_usd_to_cny, (int, float)) else 7.2,
                custom_pricing=_cost_custom_pricing if isinstance(_cost_custom_pricing, dict) else None,
            )
            logger.info(f"CostTracker 初始化完成 (data_dir={_cost_dir})")
        except Exception as e:
            logger.warning(f"CostTracker 初始化失败: {e}")
            self._cost_tracker = None

        # ========================
        # 表情包管理器（内化自 letai_sendemojis）
        # ========================
        self._emoji_map: Dict[str, List[str]] = {}  # keyword → [file_path, ...]
        self._emoji_files: List[Dict[str, Any]] = []  # [{path, name, keywords}, ...]
        self._emoji_recent: List[str] = []  # 最近使用的文件路径，避免重复
        self._emoji_recent_max = 10
        self._init_emoji_manager()

        logger.info(
            f"FlashLiteEngine 初始化 (interval={self._sync_interval}, "
            f"keywords={self._wake_keywords}, "
            f"cp_limit={self._checkpoint_mgr.token_limit}, "
            f"emoji={len(self._emoji_files)}个)"
        )

    def _cfg(self, key: str, default=None):
        if self._raw_config is None:
            return default
        if hasattr(self._raw_config, "get"):
            return self._raw_config.get(key, default)
        return getattr(self._raw_config, key, default)

    def _cfg_json(self, key: str, default=None):
        """读取可能为 JSON 字符串的配置键（schema type:string + 运行时 json.loads）。
        若取到的值是 str 则尝试 json.loads；解析失败时，若 default 本身是 JSON 字符串
        则回退解析 default，否则原样返回 default。非 str 值直接原样返回。"""
        v = self._cfg(key, default)
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                if isinstance(default, str):
                    try:
                        return json.loads(default)
                    except Exception:
                        return default
                return default
        return v

    def _get_group_overrides(self):
        """统一读取 group_overrides（动态键，可能为 JSON 字符串）。"""
        return self._cfg_json("group_overrides", {})

    def _resolve_quoted(self, value: str) -> str:
        """解析 @quoted 快捷语法，将 @quoted_file / @quoted_msg 等替换为实际值"""
        if isinstance(value, str) and value.startswith("@quoted"):
            return self._quoted_vars.get(value, value)
        return value

    def _extract_forward_id_from_event(self, event: AstrMessageEvent) -> str:
        """从事件消息组件/message_str 中提取转发消息 ID（兜底方案）"""
        try:
            if hasattr(event, 'message_obj') and event.message_obj:
                # 方式1：遍历消息组件找 Forward
                from astrbot.api.message_components import Forward
                for comp in event.message_obj.message:
                    if isinstance(comp, Forward) and hasattr(comp, 'id') and comp.id:
                        return str(comp.id)
                    # Reply 链中也可能有 Forward
                    chain = getattr(comp, 'chain', []) or getattr(comp, 'message', []) or []
                    for c in chain:
                        if isinstance(c, Forward) and hasattr(c, 'id') and c.id:
                            return str(c.id)
            # 方式2：从 message_str 中用正则提取 forward id
            msg_str = getattr(event, 'message_str', '') or ''
            import re
            m = re.search(r'(?:转发|forward)[^0-9]*(\d{10,})', msg_str, re.IGNORECASE)
            if m:
                return m.group(1)
            # 方式3：从 AGENT_BUILD extra_user_content 中提取（Forward Message: id=xxx）
            m = re.search(r'Forward Message:\s*id=(\d+)', msg_str, re.IGNORECASE)
            if m:
                return m.group(1)
        except Exception as e:
            logger.debug(f"[_extract_forward_id_from_event] error: {e}")
        return ""

    async def _fetch_forward_content(self, event: AstrMessageEvent, forward_id: str, depth: int = 0, max_depth: int = 5) -> str:
        """通过 NapCat/OneBot API 拉取合并转发消息的实际文本内容（支持递归嵌套，最深 max_depth 层）"""
        if depth >= max_depth:
            return f"[嵌套转发: 已达最大递归深度{max_depth}层，跳过]"
        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            call_action = getattr(api, "call_action", None)
            if not callable(call_action):
                logger.warning("[_fetch_forward_content] 无法获取 bot.api.call_action")
                return ""
            # NapCat/go-cqhttp: get_forward_msg — 兼容 id / message_id
            fwd_data = None
            for params in [{"id": forward_id}, {"message_id": forward_id}]:
                try:
                    fwd_data = await call_action("get_forward_msg", **params)
                    if fwd_data:
                        break
                except Exception:
                    continue
            if not fwd_data:
                return ""
            # 解析 nodes — 兼容多种 NapCat 返回格式
            nodes = fwd_data.get("messages", fwd_data.get("message", []))
            if not isinstance(nodes, list):
                if isinstance(fwd_data, list):
                    nodes = fwd_data
                else:
                    logger.warning(f"[_fetch_forward_content] 未知返回格式: type={type(fwd_data)}, keys={list(fwd_data.keys()) if isinstance(fwd_data, dict) else 'N/A'}")
                    return ""
            # 调试（仅顶层打印）
            if depth == 0 and nodes:
                first_node = nodes[0]
                logger.info(f"[_fetch_forward_content] 第一个 node keys={list(first_node.keys()) if isinstance(first_node, dict) else type(first_node)}, "
                           f"content_type={type(first_node.get('content', first_node.get('message', 'MISSING')))}")
                # 顶层初始化media收集列表
                self._pending_media_files = []
            text_parts = []
            indent = "  " * depth  # 嵌套缩进
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                sender = node.get("sender", {}).get("nickname", "") if isinstance(node.get("sender"), dict) else ""
                node_content = node.get("content") or node.get("message") or node.get("raw_message") or ""
                node_texts = []
                if isinstance(node_content, list):
                    for seg in node_content:
                        if not isinstance(seg, dict):
                            continue
                        seg_type = seg.get("type", "")
                        seg_data = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
                        if seg_type == "text":
                            txt = seg_data.get("text", "").strip()
                            if txt:
                                node_texts.append(txt)
                        elif seg_type == "image":
                            img_url = seg_data.get("url", "") or seg_data.get("file", "")
                            if img_url:
                                local = await self._download_media_to_sandbox(img_url, "image", max_size_mb=5)
                                if local:
                                    node_texts.append(f"[图片: {local}]")
                                    if hasattr(self, '_pending_media_files'):
                                        self._pending_media_files.append(("image", local))
                                else:
                                    node_texts.append(f"[图片: {img_url[:80]}]")
                            else:
                                node_texts.append("[图片]")
                        elif seg_type == "forward":
                            nested_id = seg_data.get("id", "") or seg_data.get("content", "")
                            if nested_id:
                                nested_content = await self._fetch_forward_content(event, str(nested_id), depth + 1, max_depth)
                                if nested_content:
                                    node_texts.append(f"\n{'  ' * (depth+1)}--- 嵌套转发(层{depth+1}) ---\n{nested_content}\n{'  ' * (depth+1)}--- 嵌套转发结束 ---")
                                else:
                                    node_texts.append("[嵌套转发: 拉取失败]")
                            else:
                                node_texts.append("[嵌套转发]")
                        elif seg_type == "video":
                            video_url = seg_data.get("url", "") or seg_data.get("file", "")
                            if video_url:
                                # 尝试下载视频到 Sandbox（≤20MB）并加入多模态分析管道
                                local = await self._download_media_to_sandbox(video_url, "video", max_size_mb=20)
                                if local:
                                    node_texts.append(f"[视频: {local}]")
                                    if hasattr(self, '_pending_media_files'):
                                        self._pending_media_files.append(("video", local))
                                else:
                                    node_texts.append(f"[视频: {video_url[:80]}（过大或下载失败）]")
                            else:
                                node_texts.append("[视频]")
                        elif seg_type == "file":
                            file_name = seg_data.get("file_name", "") or seg_data.get("name", "") or seg_data.get("file", "")
                            file_url = seg_data.get("url", "")
                            if file_url:
                                file_ext = os.path.splitext(file_name)[1].lower()
                                # 以文件形式发送的图片 → 当作图片处理
                                image_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff")
                                video_exts = (".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm")
                                audio_exts = (".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a", ".wma")
                                if file_ext in image_exts:
                                    # 图片文件 → 下载到 Sandbox 并标记为图片
                                    local = await self._download_media_to_sandbox(file_url, "image", max_size_mb=10)
                                    if local:
                                        node_texts.append(f"[图片文件: {file_name}, 本地={local}]")
                                        if hasattr(self, '_pending_media_files'):
                                            self._pending_media_files.append(("image", local))
                                    else:
                                        node_texts.append(f"[图片文件: {file_name}, url={file_url[:80]}]")
                                elif file_ext in video_exts:
                                    # 视频文件 → 标记为视频
                                    local = await self._download_media_to_sandbox(file_url, "video", max_size_mb=50)
                                    if local:
                                        node_texts.append(f"[视频文件: {file_name}, 本地={local}]")
                                        if hasattr(self, '_pending_media_files'):
                                            self._pending_media_files.append(("video", local))
                                    else:
                                        node_texts.append(f"[视频文件: {file_name}, url={file_url[:80]}]")
                                elif file_ext in audio_exts:
                                    # 音频文件 → 标记为语音
                                    node_texts.append(f"[语音文件: {file_name}, url={file_url[:80]}]")
                                else:
                                    # 文档类文件 → 下载
                                    downloadable = file_ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".json", ".md")
                                    if downloadable:
                                        local = await self._download_media_to_sandbox(file_url, "file", max_size_mb=10)
                                        if local:
                                            node_texts.append(f"[文件: {file_name}, 本地={local}]")
                                            if hasattr(self, '_pending_media_files') and file_ext == ".pdf":
                                                self._pending_media_files.append(("pdf", local))
                                        else:
                                            node_texts.append(f"[文件: {file_name}, url={file_url[:80]}]")
                                    else:
                                        node_texts.append(f"[文件: {file_name}, url={file_url[:80]}]")
                            else:
                                node_texts.append(f"[文件: {file_name}]")
                        elif seg_type == "face":
                            face_desc = seg_data.get("text", "") or seg_data.get("summary", "") or "[表情]"
                            node_texts.append(face_desc)
                        elif seg_type == "mface":
                            face_desc = seg_data.get("summary", "") or "[表情]"
                            node_texts.append(face_desc)
                        elif seg_type == "record":
                            node_texts.append("[语音消息]")
                        elif seg_type == "share":
                            title = seg_data.get("title", "")
                            url = seg_data.get("url", "")
                            node_texts.append(f"[分享: {title}, {url[:60]}]" if title else "[分享链接]")
                        elif seg_type == "json":
                            raw = seg_data.get("data", "")
                            card_title = ""
                            if isinstance(raw, str):
                                try:
                                    import json as _json
                                    jd = _json.loads(raw)
                                    card_title = jd.get("meta", {}).get("detail_1", {}).get("title", "") or jd.get("prompt", "")
                                except Exception:
                                    pass
                            node_texts.append(f"[卡片: {card_title}]" if card_title else "[卡片消息]")
                        elif seg_type in ("at",):
                            at_qq = seg_data.get("qq", "")
                            node_texts.append(f"@{at_qq}")
                        elif seg_type == "reply":
                            node_texts.append("[回复消息]")
                        else:
                            node_texts.append(f"[{seg_type}]")
                elif isinstance(node_content, str) and node_content.strip():
                    import re
                    clean_text = re.sub(r'\[CQ:[^\]]*\]', lambda m: (
                        "[图片]" if "image" in m.group() else
                        "[表情]" if "face" in m.group() else
                        "[视频]" if "video" in m.group() else
                        "[转发]" if "forward" in m.group() else ""
                    ), node_content).strip()
                    if clean_text:
                        node_texts.append(clean_text)
                line_text = "".join(node_texts)
                if sender:
                    text_parts.append(f"{indent}{sender}: {line_text}")
                elif line_text:
                    text_parts.append(f"{indent}{line_text}")
            result = "\n".join(text_parts)
            logger.info(f"[_fetch_forward_content] 层{depth}: 拉取{len(nodes)}条，{len(result)}字")
            return result
        except Exception as e:
            logger.warning(f"[_fetch_forward_content] 获取转发消息失败(层{depth}): {e}")
            return ""

    def _extract_window_key(self, event) -> str:
        """[R1重构] 从 AstrMessageEvent 中提取窗口标识，替代全局 _current_window_key

        Returns:
            窗口标识字符串，如 'GroupMessage:123456' 或 'FriendMessage:789012'
            提取失败返回 'unknown'
        """
        try:
            _msg = getattr(event, 'message_obj', None)
            if _msg and hasattr(_msg, 'raw_message'):
                _rm = _msg.raw_message
                if isinstance(_rm, dict):
                    if _rm.get('message_type') == 'group':
                        gid = _rm.get('group_id', '')
                        if gid:
                            return f"GroupMessage:{gid}"
                    else:
                        uid = _rm.get('user_id', '')
                        if uid:
                            return f"FriendMessage:{uid}"
            # 兜底：尝试 session_id
            sid = getattr(event, 'session_id', '')
            if sid:
                return f"FriendMessage:{sid}"
        except Exception:
            pass
        return 'unknown'

    def _register_quoted_vars(self, event: AstrMessageEvent):
        """从当前消息的 Reply 组件提取引用资源，注册到 _quoted_vars"""
        self._quoted_vars = {}  # 每次请求重置
        try:
            if not hasattr(event, 'message_obj') or not event.message_obj:
                return
            from astrbot.api.message_components import Reply, File, Image, Forward
            for comp in event.message_obj.message:
                if isinstance(comp, Reply):
                    self._quoted_vars["@quoted_msg"] = str(comp.id) if hasattr(comp, 'id') else ""
                    # 遍历被引用消息的链条（如果有）
                    chain = getattr(comp, 'chain', []) or getattr(comp, 'message', []) or []
                    for c in chain:
                        if isinstance(c, File) and hasattr(c, 'url'):
                            self._quoted_vars["@quoted_file"] = c.url or ""
                        elif isinstance(c, Image) and hasattr(c, 'url'):
                            self._quoted_vars["@quoted_image"] = c.url or ""
                        elif isinstance(c, Forward) and hasattr(c, 'id'):
                            self._quoted_vars["@quoted_forward"] = c.id or ""
            if self._quoted_vars:
                logger.debug(f"@quoted vars registered: {list(self._quoted_vars.keys())}")
        except Exception as e:
            logger.debug(f"_register_quoted_vars error: {e}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 加载完成后初始化 HTTP 会话"""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        # 从 AstrBot 配置中获取 API key（如果插件配置没有）
        if not self._api_key:
            self._api_key = self._load_api_key_from_astrbot()
        if self._api_key:
            logger.info("FlashLiteEngine API key 已就绪")
            # API Key 就绪后创建 KVCacheManager（避免传入空 key）
            self._kv_cache = KVCacheManager(
                api_key=self._api_key,
                model=FLASH_LITE_MODEL,  # 与 FlashLite 实际使用的模型一致
            )
            _tool_key_for_cache = self._tool_api_keys[0] if self._tool_api_keys else self._api_key
            self._tool_kv_cache = KVCacheManager(
                api_key=_tool_key_for_cache,
                model=self._tool_model or TOOL_MODEL_DEFAULT,  # 与工具模型实际使用的模型一致
            )
            logger.info("KVCacheManager 已创建（FlashLite + 工具模型）")
        else:
            logger.warning("FlashLiteEngine 未找到 API key！无法调用 Gemini API")

    def _load_api_key_from_astrbot(self) -> str:
        """从 AstrBot 的 cmd_config.json 中提取 Gemini API key"""
        try:
            config_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..", "cmd_config.json")
            )
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                # 兼容多种配置结构：provider / provider_sources
                for field in ("provider", "provider_sources"):
                    sources = data.get(field, [])
                    if not isinstance(sources, list):
                        continue
                    for src in sources:
                        src_id = src.get("id", "").lower()
                        src_base = src.get("api_base", "").lower()
                        if "gemini" in src_id or "gemini" in src_base or "google" in src_id:
                            keys = src.get("key", [])
                            if isinstance(keys, list) and keys:
                                return keys[0]
                            elif isinstance(keys, str) and keys:
                                return keys
        except Exception as e:
            logger.error(f"读取 API key 失败: {e}")
        return ""

    # ========================
    # 消息路由（核心）
    # ========================

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=998)
    async def route_message(self, event: AstrMessageEvent):
        """消息路由器：决定是否/如何触发 Flash Lite

        priority=998: 在持久化(9999)之后，在 context_enhancer 等常规插件之前
        不调用 stop_event()，让消息继续传播
        """
        raw = event.message_obj.raw_message
        if raw is None:
            return

        # ======= 后台 Task 完成检查 =======
        # 如果有 Task 刚完成，后台静默唤醒主模型
        if self._pending_task_wakes:
            wake_info = self._pending_task_wakes.pop(0)
            try:
                if hasattr(event, "is_at_or_wake_command"):
                    event.is_at_or_wake_command = True
                if hasattr(event, "set_extra"):
                    event.set_extra("flashlite_context_summary",
                        f"[工具/Task 唤醒] {wake_info.get('reason', '')}")
                    event.set_extra("flashlite_trigger_reason", "task_completed")
                    event.set_extra("flashlite_task_result_pointer",
                        wake_info.get("report_path", ""))
                logger.info(f"后台 Task 唤醒主模型: {wake_info.get('task_id', '')}")
            except Exception as e:
                logger.warning(f"Task 唤醒设置失败: {e}")

        def _get(obj, key, default=None):
            try:
                if hasattr(obj, "__getitem__"):
                    return obj[key]
            except (KeyError, TypeError):
                pass
            return getattr(obj, key, default)

        post_type = _get(raw, "post_type")
        if post_type != "message":
            return

        message_type = _get(raw, "message_type", "group")

        if message_type == "group":
            # === 群聊路径 ===
            group_id = str(_get(raw, "group_id", ""))
            if not group_id:
                return
            self._current_window_key = f"GroupMessage:{group_id}"  # [R1] 仅向后兼容，不再用于记账

            # ===== 群聊 FlashLite 禁用拦截（在所有触发路径之前）=====
            # 当群级配置 enabled=false 时，完全跳过 FlashLite 所有处理
            # （包括 @/关键词异步触发、消息计数同步触发、时间兜底触发）
            _group_overrides = self._get_group_overrides()
            if isinstance(_group_overrides, dict):
                _grp_override = _group_overrides.get(group_id, {})
                if isinstance(_grp_override, dict) and not _grp_override.get("enabled", True):
                    return  # 该群已完全禁用 FlashLite

            # 提取文本
            content = self._extract_text(raw)
            sender = _get(raw, "sender", {})
            sender_name = _get(sender, "card") or _get(sender, "nickname") or ""
            sender_qq = str(_get(sender, "user_id", "") or _get(raw, "user_id", ""))

            # === 每条消息缓冲到内存（确保 Knowledge 有上下文可用）===
            try:
                if content and self._t_file_mgr:
                    _window_key = f"GroupMessage:{group_id}"
                    _user_msg = {
                        "role": "user",
                        "content": f"[{sender_name}] {content}" if sender_name else content,
                        "meta": {
                            "sender_qq": sender_qq,
                            "sender_name": sender_name,
                            "is_bot": False,
                        },
                    }
                    self._t_file_mgr.buffer_message(_window_key, _user_msg)
            except Exception as _tfe:
                logger.debug(f"[T-FILE] 群消息缓冲异常: {_tfe}")

            # 检测是否 @ 或包含唤醒词
            # 修复 Codex 问题4: 使用 AstrBot 框架的 is_at_or_wake_command而非不存在的 message_obj.is_at
            is_at = getattr(event, "is_at_or_wake_command", False)
            has_keyword = any(kw in content for kw in self._wake_keywords) if content else False

            # === 异步触发（修复 Codex 问题1: 改为 await 同步等待） ===
            if is_at or has_keyword:
                await self._async_trigger(
                    group_id=group_id,
                    trigger_type="at" if is_at else "keyword",
                    trigger_content=content,
                    sender_name=sender_name,
                    event=event,
                )
                # 重置同步计数器
                self._msg_counters[group_id] = 0
                return

            # === 同步触发（消息计数 + 定时双条件） ===
            self._msg_counters[group_id] += 1
            now = time.monotonic()
            # 记录消息时间戳到滑动窗口（用于动态采样频率计算）
            self._recent_msg_timestamps[group_id].append(now)
            last_sync = self._last_sync_times.get(group_id, 0)
            time_elapsed = now - last_sync if last_sync else float('inf')
            effective_interval = self._get_effective_interval(group_id)
            count_trigger = self._msg_counters[group_id] >= effective_interval
            time_trigger = time_elapsed >= self._sync_time_interval and self._msg_counters[group_id] >= self._sync_time_min_msgs

            if count_trigger or time_trigger:
                trigger_reason = "count" if count_trigger else "time"
                self._msg_counters[group_id] = 0
                self._last_sync_times[group_id] = now
                await self._sync_trigger(
                    group_id=group_id,
                    event=event,
                )

        elif message_type == "private":
            # === 私聊路径（每条消息都经过 FlashLite 判断） ===
            user_id = str(_get(raw, "user_id", ""))
            if not user_id:
                return
            self._current_window_key = f"FriendMessage:{user_id}"  # [R1+R2] 统一命名为 FriendMessage
            content = self._extract_text(raw)
            sender = _get(raw, "sender", {})
            sender_name = _get(sender, "nickname") or _get(sender, "card") or ""

            # === 每条消息实时追加到 T 文件 ===
            try:
                if content and self._t_file_mgr:
                    _window_key = f"FriendMessage:{user_id}"
                    _user_msg = {
                        "role": "user",
                        "content": f"[{sender_name}] {content}" if sender_name else content,
                        "meta": {
                            "sender_qq": user_id,
                            "sender_name": sender_name,
                            "is_bot": False,
                        },
                    }
                    self._t_file_mgr.buffer_message(_window_key, _user_msg)
            except Exception as _tfe:
                logger.debug(f"[T-FILE] 私聊消息缓冲异常: {_tfe}")

            await self._private_trigger(
                user_id=user_id,
                content=content,
                sender_name=sender_name,
                event=event,
            )
        else:
            return  # 其他类型（如 notice 等）不处理

    # ========================
    # 同步触发
    # ========================

    def _calc_dynamic_interval(self, group_id: str) -> int:
        """根据滑动窗口内消息频率计算动态采样间隔
        
        4 级活跃度（默认配置）：
        - 静默期（0-4 msg/10min）→ interval=3（少量消息每条都重要）
        - 正常期（5-14 msg/10min）→ interval=5
        - 活跃期（15-29 msg/10min）→ interval=10
        - 爆发期（30+ msg/10min）→ interval=15
        """
        now = time.monotonic()
        window = self._dyn_window_minutes * 60  # 转为秒
        
        # 清理窗口外的过期时间戳
        timestamps = self._recent_msg_timestamps[group_id]
        cutoff = now - window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        
        msg_count = len(timestamps)
        
        # 匹配活跃度级别
        thresholds = self._dyn_thresholds  # e.g. [5, 15, 30]
        intervals = self._dyn_intervals    # e.g. [3, 5, 10, 15]
        
        level = 0
        for t in thresholds:
            if msg_count >= t:
                level += 1
            else:
                break
        
        # level: 0=静默, 1=正常, 2=活跃, 3=爆发
        return intervals[min(level, len(intervals) - 1)]
    
    def _get_effective_interval(self, group_id: str) -> int:
        """获取当前群的有效采样间隔（群覆盖 > 动态 > 全局固定）"""
        # Stage 9: 群独立配置覆盖（预留接口）
        group_overrides = self._get_group_overrides()
        if isinstance(group_overrides, dict) and group_id in group_overrides:
            override = group_overrides[group_id]
            if isinstance(override, dict):
                # enabled 开关：禁用时返回极大值，等效跳过采样
                if not override.get("enabled", True):
                    return 999999
                if "sync_interval" in override:
                    try:
                        val = int(override["sync_interval"])
                        if val > 0:
                            return val
                    except (TypeError, ValueError):
                        pass  # 回退到动态/全局配置
        
        # 动态模式
        if self._sampling_mode == "dynamic":
            return self._calc_dynamic_interval(group_id)
        
        # 固定模式
        return self._sync_interval

    async def _sync_trigger(self, group_id: str, event: AstrMessageEvent):
        """同步触发：每 N 条消息，更新 Knowledge + 判断是否需要回复"""
        self._stats["sync_triggers"] += 1

        # FIX-5: 定期 Sandbox Review
        try:
            now_ts = time.time()
            review_due = (now_ts - self._last_review_time) >= (self._review_interval_hours * 3600)
            if review_due and hasattr(self, '_sandbox') and self._sandbox:
                self._last_review_time = now_ts
                FlashLiteEngine._task_counter += 1
                review_tid = f"task-{FlashLiteEngine._task_counter:04d}"
                review_meta = {
                    "source_pointer": "system:periodic_review",
                    "steps": [],
                    "wake_condition": "notify_main",
                    "description": "Sandbox 定期清理与维护",
                    "step_progress": "",
                    "results": [],
                }
                review_desc = (
                    "执行 Sandbox 定期维护：\n"
                    "1. 列出 workspace/ 下所有文件和目录\n"
                    "2. 清理超过 7 天的临时文件(drafts/中非重要文件)\n"
                    "3. 检查 task_reports/ 中已完成的报告\n"
                    "4. 统计磁盘使用量和异常文件\n"
                    "5. 完成后调用 system_report 写入维护日志（会自动写入受保护区域）"
                )
                import asyncio
                async def _run_review():
                    try:
                        self._review_active = True
                        self._sandbox._review_mode = True
                        self._sandbox._security._review_mode = True  # 问题4: 同步到 Security
                        result = await self._call_tool_model(f"执行以下任务并返回结果:\n{review_desc}", window_key=f"GroupMessage:{group_id}")
                        # 兜底：检查工具模型是否已自行写入日志
                        import os as _os
                        report_dir = _os.path.join(self._sandbox._root, "base_tools", "system_report", "review")
                        today = datetime.now().strftime("%Y%m%d")
                        wrote_today = any(today in f for f in _os.listdir(report_dir)) if _os.path.isdir(report_dir) else False
                        if not wrote_today:
                            logger.warning(f"Review {review_tid}: 工具模型未写入日志，主进程兜底写入")
                            await self.tool_system_report(
                                event=None,
                                content=f"## 定期维护 ({review_tid})\n\n{result}",
                                report_type="review",
                            )
                        self._knowledge.add_operation(f"Sandbox 定期维护完成 ({review_tid})")
                    except Exception as e:
                        logger.warning(f"定期 Review 失败: {e}")
                    finally:
                        self._review_active = False
                        self._sandbox._review_mode = False
                        self._sandbox._security._review_mode = False  # 问题4: 同步到 Security
                task = asyncio.create_task(_run_review())
                self._task_pool[review_tid] = {"task": task, "meta": review_meta}
                logger.info(f"定期 Sandbox Review 已启动: {review_tid}")

                # === 画像语义 Review（同一周期触发） ===
                async def _run_profile_review():
                    try:
                        candidates = self._knowledge.get_review_candidates(min_facts=5)
                        if not candidates:
                            return
                        # 每次最多 review 3 个用户
                        for qq_id in candidates[:3]:
                            prompt = self._knowledge.prepare_review_prompt(qq_id)
                            if not prompt:
                                continue
                            try:
                                async with self._session.post(
                                    f"{GEMINI_API_BASE}/{self._model}:generateContent?key={self._api_key}",
                                    json={"contents": [{"parts": [{"text": prompt}]}]},
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as resp:
                                    if resp.status == 200:
                                        rj = await resp.json()
                                        text = rj.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                                        if text:
                                            count = self._knowledge.apply_review_result(qq_id, text)
                                            logger.info(f"画像 Review: {qq_id} → {count} 条")
                                    else:
                                        logger.warning(f"画像 Review API 失败: {resp.status}")
                            except Exception as e:
                                logger.warning(f"画像 Review {qq_id} 失败: {e}")
                        self._knowledge.add_operation("画像语义 Review 完成")
                    except Exception as e:
                        logger.warning(f"画像 Review 整体失败，降级快速去重: {e}")
                        self._knowledge.review_profiles_quick()
                asyncio.create_task(_run_profile_review())
        except Exception as e:
            logger.warning(f"Review 定时检查异常: {e}")

        try:
            # 先刷盘缓冲区中的消息
            _wk = f"GroupMessage:{group_id}"
            await self._t_file_mgr.flush_buffer(_wk)
            # 收集最近消息（从 T 文件系统获取）
            _t_file = await self._t_file_mgr.load(_wk)
            recent_context = self._t_file_mgr.build_flashlite_context(_t_file)

            # 构建 prompt
            prompt = self._build_judgment_prompt(
                group_id=group_id,
                context=recent_context,
                trigger_type="sync",
                trigger_content=None,
            )

            # 调用 Flash Lite
            t0 = time.monotonic()
            result = await self._call_flash_lite(prompt, window_key=f"GroupMessage:{group_id}")
            latency = (time.monotonic() - t0) * 1000

            # 解析结果
            parsed = self._parse_judgment(result)
            self._update_latency_stats(latency)

            # 更新 Knowledge
            if parsed.get("knowledge_update"):
                self._knowledge_cache[group_id] = parsed["knowledge_update"]
                # 同步更新 KnowledgeCache 模块
                self._knowledge.update_window(
                    window_key=f"GroupMessage:{group_id}",
                    summary=parsed["knowledge_update"],
                    active_users=parsed.get("active_users", []),
                    mood=parsed.get("knowledge_mood", ""),
                    recent_topics=parsed.get("recent_topics", []),
                )

            # FIX-2: Memory 被动召回（思路 C 序号精确模式）
            if parsed.get("memory_hint") and self._memory:
                try:
                    hint_str = parsed["memory_hint"].strip()
                    # 尝试解析序号（如 "1,3,7"）
                    indices = [int(x.strip()) for x in hint_str.split(",") if x.strip().isdigit()]
                    
                    if indices:
                        # 序号模式：通过迷你索引序号精确召回
                        all_entries = await self._memory._get_workspace_entries(None)
                        all_entries.sort(key=lambda e: (not e.get('pinned', False), e.get('title', '')))
                        
                        recalled = []
                        for idx in indices:
                            if 1 <= idx <= len(all_entries):
                                entry = all_entries[idx - 1]
                                full = await self._memory.read(entry["id"])
                                if full:
                                    recalled.append(full)
                        
                        if recalled and hasattr(event, "set_extra"):
                            hint_text = "\n---\n".join(
                                f"**{r.get('title', '')}** ({r.get('category', 'general')})\n{r.get('content', '')[:500]}"
                                for r in recalled
                            )
                            event.set_extra("memory_recall", hint_text)
                            logger.info(f"Memory 序号召回: {indices} → {len(recalled)} 条")
                    else:
                        # 降级：旧模式关键词模糊搜索
                        hints_raw = await self._memory.query(
                            query=hint_str, limit=3
                        )
                        hints = hints_raw.get("results", []) if isinstance(hints_raw, dict) else []
                        if hints and hasattr(event, "set_extra"):
                            hint_text = "\n".join(
                                f"- {h.get('title', '')}: {h.get('summary', '')[:80]}"
                                for h in hints if isinstance(h, dict)
                            )
                            event.set_extra("memory_recall", hint_text)
                            logger.info(f"Memory 关键词召回: {hint_str} → {len(hints)} 条")
                except Exception as e:
                    logger.warning(f"Memory 召回失败: {e}")

            # 昵称自动同步：从 ACTIVE_USERS 提取最新昵称更新到卡片
            if parsed.get("active_users") and self._knowledge:
                try:
                    self._knowledge.sync_nicknames(parsed["active_users"])
                except Exception as e:
                    logger.debug(f"昵称同步失败: {e}")

            # FIX-4: 用户画像更新
            if parsed.get("profile_update") and self._knowledge:
                try:
                    pu = parsed["profile_update"]
                    # 新格式: QQ号:category:summary|content
                    parts_pu = pu.split(":", 2)
                    if len(parts_pu) >= 3:
                        qq_id = parts_pu[0].strip()
                        cat = parts_pu[1].strip()
                        rest = parts_pu[2]
                        if not qq_id.isdigit():
                            logger.warning(f"⚠️ PROFILE_UPDATE qq_id 不是纯数字: '{qq_id}'，已跳过。FlashLite 应使用纯数字QQ号")
                        else:
                            if "|" in rest:
                                summ, cont = rest.split("|", 1)
                            else:
                                summ, cont = rest, ""
                            self._knowledge.update_user_profile(
                                qq_id=qq_id, summary=summ.strip(),
                                content=cont.strip(), category=cat
                            )
                            logger.info(f"用户画像更新: {qq_id} [{cat}] {summ[:40]}")
                    else:
                        # 仅接受新格式 QQ号:category:summary|content（带 isdigit 校验）；
                        # 旧格式 QQ号:info 已废弃，避免产生脏 key
                        logger.warning(f"⚠️ PROFILE_UPDATE 格式不符（非 QQ号:category:summary 新格式），已跳过: '{pu[:60]}'")
                except Exception as e:
                    logger.warning(f"用户画像更新失败: {e}")

            # FIX-4+: 卡片注入指定传递到 event extra
            if parsed.get("inject_cards") and hasattr(event, "set_extra"):
                event.set_extra("inject_cards", parsed["inject_cards"])

            # 判断是否触发主模型
            if parsed.get("should_trigger"):
                self._stats["main_model_notified"] += 1
                await self._notify_main_model(event, parsed)

            logger.debug(
                f"[同步] 群{group_id}: trigger={parsed.get('should_trigger', False)}, "
                f"knowledge='{parsed.get('knowledge_update', '')[:50]}...', "
                f"latency={latency:.0f}ms"
            )

        except Exception as e:
            logger.error(f"同步触发异常: {e}")
            self._stats["errors"] += 1

    # ========================
    # 异步触发
    # ========================

    async def _async_trigger(
        self,
        group_id: str,
        trigger_type: str,
        trigger_content: str,
        sender_name: str,
        event: AstrMessageEvent,
    ):
        """异步触发：@ 或关键词，立即响应"""
        self._stats["async_triggers"] += 1
        try:
            _wk = f"GroupMessage:{group_id}"
            await self._t_file_mgr.flush_buffer(_wk)
            _t_file = await self._t_file_mgr.load(_wk)
            recent_context = self._t_file_mgr.build_flashlite_context(_t_file)

            prompt = self._build_judgment_prompt(
                group_id=group_id,
                context=recent_context,
                trigger_type=trigger_type,
                trigger_content=trigger_content,
                sender_name=sender_name,
            )

            t0 = time.monotonic()
            result = await self._call_flash_lite(prompt, window_key=f"GroupMessage:{group_id}")
            latency = (time.monotonic() - t0) * 1000

            parsed = self._parse_judgment(result)
            self._update_latency_stats(latency)

            if parsed.get("knowledge_update"):
                self._knowledge_cache[group_id] = parsed["knowledge_update"]
                self._knowledge.update_window(
                    window_key=f"GroupMessage:{group_id}",
                    summary=parsed["knowledge_update"],
                    mood=parsed.get("knowledge_mood", ""),
                )

            # FIX-2: Memory 被动召回（思路 C 序号精确模式）
            if parsed.get("memory_hint") and self._memory:
                try:
                    hint_str = parsed["memory_hint"].strip()
                    indices = [int(x.strip()) for x in hint_str.split(",") if x.strip().isdigit()]
                    
                    if indices:
                        all_entries = await self._memory._get_workspace_entries(None)
                        all_entries.sort(key=lambda e: (not e.get('pinned', False), e.get('title', '')))
                        recalled = []
                        for idx in indices:
                            if 1 <= idx <= len(all_entries):
                                entry = all_entries[idx - 1]
                                full = await self._memory.read(entry["id"])
                                if full:
                                    recalled.append(full)
                        if recalled and hasattr(event, "set_extra"):
                            hint_text = "\n---\n".join(
                                f"**{r.get('title', '')}** ({r.get('category', 'general')})\n{r.get('content', '')[:500]}"
                                for r in recalled
                            )
                            event.set_extra("memory_recall", hint_text)
                            logger.info(f"Memory 序号召回: {indices} → {len(recalled)} 条")
                    else:
                        hints_raw = await self._memory.query(query=hint_str, limit=3)
                        hints = hints_raw.get("results", []) if isinstance(hints_raw, dict) else []
                        if hints and hasattr(event, "set_extra"):
                            hint_text = "\n".join(
                                f"- {h.get('title', '')}: {h.get('summary', '')[:80]}"
                                for h in hints if isinstance(h, dict)
                            )
                            event.set_extra("memory_recall", hint_text)
                except Exception as e:
                    logger.warning(f"Memory 召回失败: {e}")

            # 昵称自动同步
            if parsed.get("active_users") and self._knowledge:
                try:
                    self._knowledge.sync_nicknames(parsed["active_users"])
                except Exception as e:
                    logger.debug(f"昵称同步失败: {e}")

            # FIX-4: 用户画像更新
            if parsed.get("profile_update") and self._knowledge:
                try:
                    pu = parsed["profile_update"]
                    parts_pu = pu.split(":", 2)
                    if len(parts_pu) >= 3:
                        qq_id = parts_pu[0].strip()
                        cat = parts_pu[1].strip()
                        rest = parts_pu[2]
                        if not qq_id.isdigit():
                            logger.warning(f"⚠️ PROFILE_UPDATE qq_id 不是纯数字: '{qq_id}'，已跳过")
                        else:
                            if "|" in rest:
                                summ, cont = rest.split("|", 1)
                            else:
                                summ, cont = rest, ""
                            self._knowledge.update_user_profile(
                                qq_id=qq_id, summary=summ.strip(),
                                content=cont.strip(), category=cat
                            )
                    else:
                        # 仅接受新格式 QQ号:category:summary|content（带 isdigit 校验）；
                        # 旧格式 QQ号:info 已废弃，避免产生脏 key
                        logger.warning(f"⚠️ PROFILE_UPDATE 格式不符（非 QQ号:category:summary 新格式），已跳过: '{pu[:60]}'")
                except Exception as e:
                    logger.warning(f"用户画像更新失败: {e}")

            # FIX-4+: 卡片注入指定传递到 event extra
            if parsed.get("inject_cards") and hasattr(event, "set_extra"):
                event.set_extra("inject_cards", parsed["inject_cards"])

            # 对于 @ 触发，如果 Flash Lite 判断不需要回复但确实被 @ 了，强制触发
            if trigger_type == "at" and not parsed.get("should_trigger"):
                parsed["should_trigger"] = True
                parsed["reason"] = "强制触发：用户明确 @ 了老板娘"

            if parsed.get("should_trigger"):
                self._stats["main_model_notified"] += 1
                await self._notify_main_model(event, parsed)

            logger.info(
                f"[{trigger_type}] 群{group_id}: trigger={parsed.get('should_trigger')}, "
                f"latency={latency:.0f}ms"
            )

        except Exception as e:
            logger.error(f"异步触发异常: {e}")
            self._stats["errors"] += 1

    # ========================
    # 私聊触发
    # ========================

    async def _private_trigger(
        self,
        user_id: str,
        content: str,
        sender_name: str,
        event: AstrMessageEvent,
    ):
        """私聊触发：每条私聊消息都经过 FlashLite 判断

        与群聊的区别：
        - 没有消息计数/间隔逻辑（每条消息都判断）
        - window_key 使用 FriendMessage:{user_id}
        - TRIGGER_MAIN=false 时需要 stop_event() 阻止 AstrBot 自动响应
        - 私聊判断标准更宽松（几乎总是触发）
        """
        self._stats.setdefault("private_triggers", 0)
        self._stats["private_triggers"] += 1
        try:
            window_key = f"FriendMessage:{user_id}"

            # 先刷盘缓冲区
            await self._t_file_mgr.flush_buffer(window_key)
            # 收集最近私聊上下文（从 T 文件系统获取）
            _t_file = await self._t_file_mgr.load(window_key)
            recent_context = self._t_file_mgr.build_flashlite_context(_t_file)

            # 构建 prompt（私聊模式）
            prompt = self._build_judgment_prompt(
                group_id=user_id,
                context=recent_context,
                trigger_type="private",
                trigger_content=content,
                sender_name=sender_name,
                window_type="private",
            )

            # 调用 Flash Lite
            t0 = time.monotonic()
            result = await self._call_flash_lite(prompt, window_key=f"FriendMessage:{user_id}")
            latency = (time.monotonic() - t0) * 1000

            # 解析结果
            parsed = self._parse_judgment(result)
            self._update_latency_stats(latency)

            # Knowledge 更新（使用 FriendMessage:uid 作为窗口标识）
            if parsed.get("knowledge_update"):
                self._knowledge_cache[user_id] = parsed["knowledge_update"]
                self._knowledge.update_window(
                    window_key=window_key,
                    summary=parsed["knowledge_update"],
                    active_users=parsed.get("active_users", []),
                    mood=parsed.get("knowledge_mood", ""),
                    recent_topics=parsed.get("recent_topics", []),
                )

            # Memory 被动召回（思路 C 序号精确模式）
            if parsed.get("memory_hint") and self._memory:
                try:
                    hint_str = parsed["memory_hint"].strip()
                    indices = [int(x.strip()) for x in hint_str.split(",") if x.strip().isdigit()]

                    if indices:
                        all_entries = await self._memory._get_workspace_entries(None)
                        all_entries.sort(key=lambda e: (not e.get('pinned', False), e.get('title', '')))
                        recalled = []
                        for idx in indices:
                            if 1 <= idx <= len(all_entries):
                                entry = all_entries[idx - 1]
                                full = await self._memory.read(entry["id"])
                                if full:
                                    recalled.append(full)
                        if recalled and hasattr(event, "set_extra"):
                            hint_text = "\n---\n".join(
                                f"**{r.get('title', '')}** ({r.get('category', 'general')})\n{r.get('content', '')[:500]}"
                                for r in recalled
                            )
                            event.set_extra("memory_recall", hint_text)
                            logger.info(f"[私聊] Memory 序号召回: {indices} → {len(recalled)} 条")
                    else:
                        hints_raw = await self._memory.query(query=hint_str, limit=3)
                        hints = hints_raw.get("results", []) if isinstance(hints_raw, dict) else []
                        if hints and hasattr(event, "set_extra"):
                            hint_text = "\n".join(
                                f"- {h.get('title', '')}: {h.get('summary', '')[:80]}"
                                for h in hints if isinstance(h, dict)
                            )
                            event.set_extra("memory_recall", hint_text)
                            logger.info(f"[私聊] Memory 关键词召回: {hint_str} → {len(hints)} 条")
                except Exception as e:
                    logger.warning(f"[私聊] Memory 召回失败: {e}")

            # 昵称自动同步
            if parsed.get("active_users") and self._knowledge:
                try:
                    self._knowledge.sync_nicknames(parsed["active_users"])
                except Exception as e:
                    logger.debug(f"[私聊] 昵称同步失败: {e}")

            # 用户画像更新
            if parsed.get("profile_update") and self._knowledge:
                try:
                    pu = parsed["profile_update"]
                    parts_pu = pu.split(":", 2)
                    if len(parts_pu) >= 3:
                        qq_id = parts_pu[0].strip()
                        cat = parts_pu[1].strip()
                        rest = parts_pu[2]
                        if not qq_id.isdigit():
                            logger.warning(f"⚠️ [私聊] PROFILE_UPDATE qq_id 不是纯数字: '{qq_id}'，已跳过")
                        else:
                            if "|" in rest:
                                summ, cont = rest.split("|", 1)
                            else:
                                summ, cont = rest, ""
                            self._knowledge.update_user_profile(
                                qq_id=qq_id, summary=summ.strip(),
                                content=cont.strip(), category=cat
                            )
                            logger.info(f"[私聊] 用户画像更新: {qq_id} [{cat}] {summ[:40]}")
                    else:
                        # 仅接受新格式 QQ号:category:summary|content（带 isdigit 校验）；
                        # 旧格式 QQ号:info 已废弃，避免产生脏 key
                        logger.warning(f"⚠️ [私聊] PROFILE_UPDATE 格式不符（非 QQ号:category:summary 新格式），已跳过: '{pu[:60]}'")
                except Exception as e:
                    logger.warning(f"[私聊] 用户画像更新失败: {e}")

            # 卡片注入指定
            if parsed.get("inject_cards") and hasattr(event, "set_extra"):
                event.set_extra("inject_cards", parsed["inject_cards"])

            # === 核心判断：触发 or 阻止 ===
            if parsed.get("should_trigger"):
                self._stats["main_model_notified"] += 1
                await self._notify_main_model(event, parsed)
                logger.info(
                    f"[私聊] 用户{user_id}: TRIGGER=true, "
                    f"reason='{parsed.get('reason', '')[:50]}', latency={latency:.0f}ms"
                )
                # 不调用 stop_event()：让 AstrBot pipeline 的 waking_check 正常唤醒私聊消息
            else:
                # FlashLite 判定不需要回复 → 阻止 AstrBot 自动响应
                event.stop_event()
                logger.info(
                    f"[私聊] 用户{user_id}: TRIGGER=false (stopped), "
                    f"reason='{parsed.get('reason', '')[:50]}', latency={latency:.0f}ms"
                )

        except Exception as e:
            logger.error(f"[私聊] 触发异常: {e}")
            self._stats["errors"] += 1

    # ========================
    # 系统提示词构建（FlashLite / 工具模型）
    # ========================

    # ========================
    # Gemini REST API 调用
    # ========================

    async def _call_flash_lite(self, prompt: str, max_output_tokens: int = 4096, window_key: str = "unknown") -> str:
        """直接调用 Gemini REST API（不走 AstrBot 的 OpenAI 兼容层）
        
        KVCache 优化：system prompt 纯静态（100% 缓存命中），
        动态内容（Knowledge/时间/Memory索引）拼到 user prompt 前缀。
        
        Args:
            prompt: 用户 prompt
            max_output_tokens: 最大输出 token 数，压缩调用时动态计算
            window_key: 窗口标识（R1重构：显式传递，不再依赖全局状态）
        """
        import datetime
        if not self._api_key:
            raise RuntimeError("API key 未配置")

        # === 纯静态 system prompt（用于隐式/显式缓存命中）===
        _fl_system = self._build_flash_lite_system()

        # === 动态前缀（拼到 user prompt 前面）===
        _dynamic_prefix_parts = []
        
        # Knowledge 快照
        knowledge_snapshot = ""
        if self._knowledge:
            knowledge_snapshot = self._knowledge.get_prompt_text() or "暂无 Knowledge 数据"
        _dynamic_prefix_parts.append(f"# 当前 Knowledge 快照\n{knowledge_snapshot}")
        
        # 系统时间
        _dynamic_prefix_parts.append(f"# 系统时间\n{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Memory 迷你索引
        _mem_index = await self._build_memory_mini_index()
        if _mem_index:
            _dynamic_prefix_parts.append(_mem_index)
        
        _dynamic_prefix = "\n\n".join(_dynamic_prefix_parts) + "\n\n---\n\n"
        
        # 最终 user prompt = 动态前缀 + 原始 prompt
        _effective_prompt = _dynamic_prefix + prompt

        url = f"{GEMINI_API_BASE}/{self._model}:generateContent?key={self._api_key}"

        # KVCache 尝试：缓存纯静态 system prompt
        _cached_name = None
        if self._kv_cache:
            try:
                _cached_name, _is_new, _cache_tokens = await self._kv_cache.ensure_cache(
                    fixed_contents=[{"role": "user", "parts": [{"text": "KV Cache 锚点"}]}],
                    system_instruction=_fl_system,
                )
                # 缓存新建/重建时记录存储费
                if _is_new and _cache_tokens > 0 and self._cost_tracker:
                    await self._cost_tracker.record_storage(
                        model=self._model,
                        cached_token_count=_cache_tokens,
                        ttl_seconds=self._kv_cache._ttl_seconds,
                    )
            except Exception as _e:
                logger.debug(f"KVCache 降级: {_e}")
                _cached_name = None

        # 公共配置
        _gen_config = {
            "temperature": 0.3,
            "maxOutputTokens": max_output_tokens,  # 动态：默认 4096，压缩时按需计算
            "thinkingConfig": {
                "thinkingLevel": self._thinking_level,
            },
        }
        _safety = [
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
        ]

        # 根据是否有缓存构建 payload
        if _cached_name:
            # 使用显式缓存：所有动态内容在 user message 中
            payload = {
                "cachedContent": _cached_name,
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": _effective_prompt}],
                    }
                ],
                "generationConfig": _gen_config,
                "safetySettings": _safety,
            }
        else:
            # 降级/隐式缓存：system prompt 纯静态 → Gemini 自动隐式缓存
            payload = {
                "systemInstruction": {
                    "parts": [{"text": _fl_system}]
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": _effective_prompt}],
                    }
                ],
                "generationConfig": _gen_config,
                "safetySettings": _safety,
            }

        self._stats["total_calls"] += 1

        async with self._session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"Gemini API 错误 {resp.status}: {error_text[:200]}")

            data = await resp.json()

        # === 成本追踪：提取 usageMetadata ===
        _usage = data.get("usageMetadata", {})
        if self._cost_tracker and _usage:
            try:
                await self._cost_tracker.record(
                    model=self._model,
                    call_type="flashlite",
                    window_key=window_key,
                    prompt_tokens=_usage.get("promptTokenCount", 0),
                    cached_tokens=_usage.get("cachedContentTokenCount", 0),
                    output_tokens=_usage.get("candidatesTokenCount", 0),
                )
            except Exception as _ce:
                logger.debug(f"CostTracker 记录失败: {_ce}")

        # 提取文本响应
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini API 返回空 candidates")

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [p["text"] for p in parts if "text" in p and "thought" not in p.get("_type", "")]

        # 过滤掉 thinking 部分（3.x 模型返回思考内容在单独的 part 中）
        result_parts = []
        for p in parts:
            if p.get("thought"):
                continue  # 跳过思考部分
            if "text" in p:
                result_parts.append(p["text"])

        return "\n".join(result_parts) if result_parts else "\n".join(text_parts)

    def _get_tool_api_key(self) -> str:
        """获取下一个可用的工具模型 API Key（Round-robin 轮转）"""
        if not self._tool_api_keys:
            return self._api_key  # 没有独立 key 池则回退到主 key

        now = time.time()
        pool_size = len(self._tool_api_keys)
        # 尝试找到一个不在冷却期的 key
        for attempt in range(pool_size):
            idx = (self._tool_key_index + attempt) % pool_size
            cooldown_until = self._tool_key_cooldown.get(idx, 0)
            if now >= cooldown_until:
                self._tool_key_index = (idx + 1) % pool_size  # 下次从下一个开始
                return self._tool_api_keys[idx]
        # 全部冷却中，找最快解冻的
        earliest_idx = min(self._tool_key_cooldown, key=self._tool_key_cooldown.get)
        self._tool_key_index = (earliest_idx + 1) % pool_size
        return self._tool_api_keys[earliest_idx]

    async def _call_tool_model(self, prompt: str, max_tokens: int = 4096, task_id: str = "", max_steps: int = 20, context_text: str = "", window_key: str = "unknown") -> str:

        """调用工具模型 mini agent——支持工具调用循环 + API Key 池轮转

        Gap4: 升级为 mini agent loop，子代理可以调用 sandbox 工具（view_file, modify_file, sandbox_exec）。
        每轮检查 finish_reason，如果模型返回 function_call 则执行工具并注入结果继续循环。
        """
        max_agent_steps = max_steps
        
        # === 动态前缀：Knowledge + 系统时间（从 system prompt 移到 user prompt）===
        import datetime as _dt
        _dynamic_prefix_parts = []
        knowledge_snapshot = ""
        if self._knowledge:
            knowledge_snapshot = self._knowledge.get_prompt_text() or "暂无"
        _dynamic_prefix_parts.append(f"# 当前 Knowledge 概况\n{knowledge_snapshot}")
        _dynamic_prefix_parts.append(f"# 系统时间: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        _dynamic_prefix = "\n\n".join(_dynamic_prefix_parts) + "\n\n---\n\n"
        
        _effective_prompt = _dynamic_prefix + prompt
        if context_text:
            _effective_prompt = f"## 当前对话上下文\n{context_text}\n\n---\n\n{_dynamic_prefix}{prompt}"
        messages = [{"role": "user", "parts": [{"text": _effective_prompt}]}]
        # 子代理独立草稿空间
        agent_draft_dir = f"workspace/agent_drafts/{task_id}/" if task_id else "workspace/agent_drafts/_default/"

        # 定义子代理可用的工具
        tool_declarations = [
            {
                "name": "agent_view_file",
                "description": "读取 Sandbox 内的文件内容",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对于 Sandbox 根的文件路径"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "agent_modify_file",
                "description": "在 Sandbox 内创建或修改文件",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对于 Sandbox 根的文件路径"},
                        "content": {"type": "string", "description": "文件内容"},
                        "mode": {"type": "string", "description": "write(覆盖)/append(追加)", "enum": ["write", "append"]}
                    },
                    "required": ["path", "content"]
                }
            },
            {
                "name": "agent_draft",
                "description": "读写子代理专属草稿纸（自动在 agent_drafts 目录下操作）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "description": "草稿文件名"},
                        "content": {"type": "string", "description": "写入内容（留空则为读取）"}
                    },
                    "required": ["filename"]
                }
            },
        ]


        # 4.1: 动态加载 base_tools 工具定义到子代理
        try:
            # 指向项目根真目录 Sandbox/base_tools（统一走 tool_registry.SANDBOX_ROOT）
            base_tools_dir = os.path.join(SANDBOX_ROOT, "base_tools")
            excluded_tools = {"task_set", "knowledge_update", "browser_agent", "run_custom_tool"}  # H-3: 防递归委托
            if os.path.isdir(base_tools_dir):
                import json as _json
                for tool_file in sorted(os.listdir(base_tools_dir)):
                    if not tool_file.endswith(".tool.json"):
                        continue
                    try:
                        with open(os.path.join(base_tools_dir, tool_file), encoding="utf-8") as _f:
                            tool_def = _json.load(_f)
                        tname = tool_def.get("name", "")
                        if tname in excluded_tools or not tname:
                            continue
                        if hasattr(self, f"tool_{tname}"):
                            tool_declarations.append({
                                "name": f"agent_{tname}",
                                "description": tool_def.get("description", f"执行 {tname}"),
                                "parameters": tool_def.get("parameters", {
                                    "type": "object", "properties": {}
                                })
                            })
                    except Exception:
                        continue
                logger.debug(f"工具模型加载了 {len(tool_declarations)} 个工具声明")
        except Exception as e:
            logger.warning(f"base_tools 加载失败: {e}")

        # 工具模型 KVCache：缓存 system prompt + tools 声明
        _tool_cached_name = None
        _tool_declarations = [{"functionDeclarations": tool_declarations}]
        if self._tool_kv_cache:
            try:
                _tool_system = self._build_tool_model_system()
                _tool_cached_name, _t_is_new, _t_cache_tokens = await self._tool_kv_cache.ensure_cache(
                    fixed_contents=[{"role": "user", "parts": [{"text": "工具模型 KV Cache 锚点"}]}],
                    system_instruction=_tool_system,
                    tools=_tool_declarations,  # tools 也放入缓存
                )
                if _t_is_new and _t_cache_tokens > 0 and self._cost_tracker:
                    await self._cost_tracker.record_storage(
                        model=self._tool_model or "gemini-2.5-flash",
                        cached_token_count=_t_cache_tokens,
                        ttl_seconds=self._tool_kv_cache._ttl_seconds,
                    )
            except Exception as _e:
                logger.debug(f"工具模型 KVCache 降级: {_e}")
                _tool_cached_name = None

        for step in range(max_agent_steps):
            api_key = self._get_tool_api_key()
            if not api_key:
                raise RuntimeError("API key 未配置")
            model = self._tool_model or TOOL_MODEL_DEFAULT
            max_retries = min(len(self._tool_api_keys), 3) if self._tool_api_keys else 1

            # 公共配置
            _tool_gen_config = {
                "temperature": 0.3,
                "maxOutputTokens": max_tokens,
                "thinkingConfig": {
                    "thinkingBudget": self._tool_thinking_budget,
                },
            }
            _tool_safety = [
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
            ]

            if _tool_cached_name:
                # 使用缓存：不传 tools/systemInstruction（已在缓存中）
                payload = {
                    "cachedContent": _tool_cached_name,
                    "contents": messages,
                    "generationConfig": _tool_gen_config,
                    "safetySettings": _tool_safety,
                }
            else:
                # 无缓存：完整 payload
                payload = {
                    "systemInstruction": {
                        "parts": [{"text": self._build_tool_model_system()}]
                    },
                    "contents": messages,
                    "generationConfig": _tool_gen_config,
                    "tools": _tool_declarations,
                    "safetySettings": _tool_safety,
                }

            data = None
            for retry in range(max_retries):
                url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
                async with self._session.post(
                    url, json=payload, headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status == 429 and self._tool_api_keys and retry < max_retries - 1:
                        if self._tool_api_keys:
                            current_idx = (self._tool_key_index - 1) % len(self._tool_api_keys)
                            self._tool_key_cooldown[current_idx] = time.time() + 60
                        api_key = self._get_tool_api_key()
                        logger.warning(f"[ToolAgent] 429 rate limit, 切换 key (retry {retry+1})")
                        continue
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"Tool Agent API 错误 {resp.status}: {error_text[:200]}")
                    data = await resp.json()
                    break

            if data is None:
                raise RuntimeError("Tool Agent API: 所有 Key 都被 rate limit")

            # === 成本追踪：提取 usageMetadata ===
            _usage = data.get("usageMetadata", {})
            if self._cost_tracker and _usage:
                try:
                    await self._cost_tracker.record(
                        model=model,
                        call_type="tool_model",
                        window_key=window_key,
                        prompt_tokens=_usage.get("promptTokenCount", 0),
                        cached_tokens=_usage.get("cachedContentTokenCount", 0),
                        output_tokens=_usage.get("candidatesTokenCount", 0),
                    )
                except Exception as _ce:
                    logger.debug(f"CostTracker(tool) 记录失败: {_ce}")

            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError("Tool Agent API 返回空 candidates")

            resp_parts = candidates[0].get("content", {}).get("parts", [])

            # 检查是否有 function_call
            function_calls = [p for p in resp_parts if "functionCall" in p]

            if not function_calls:
                # 无工具调用 = 最终回复
                result_parts = []
                for p in resp_parts:
                    if p.get("thought"):
                        continue
                    if "text" in p:
                        result_parts.append(p["text"])
                return "\n".join(result_parts) if result_parts else ""

            # 有工具调用——执行并注入结果
            # 先把 model 的完整回复加到 messages
            messages.append({"role": "model", "parts": resp_parts})

            # 执行每个 function_call
            func_response_parts = []
            for fc_part in function_calls:
                fc = fc_part["functionCall"]
                fn_name = fc.get("name", "")
                fn_args = fc.get("args", {})
                tool_result = await self._execute_agent_tool(fn_name, fn_args, agent_draft_dir)
                func_response_parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"result": str(tool_result)[:2000]}  # 截断防止上下文爆炸
                    }
                })

            messages.append({"role": "user", "parts": func_response_parts})
            logger.debug(f"[ToolAgent] Step {step+1}: executed {len(function_calls)} tool(s)")

        # 达到 max_agent_steps
        return "[子代理] 达到最大步数限制，已返回中间结果"

    async def _execute_agent_tool(self, name: str, args: dict, draft_dir: str) -> str:
        """执行子代理的工具调用（含通用 agent_ 路由 + 超时保护）"""
        try:
            if name == "agent_view_file":
                path = args.get("path", "")
                if self._sandbox:
                    return await self._sandbox.view_file(path)
                return f"错误: Sandbox 未初始化"

            elif name == "agent_modify_file":
                path = args.get("path", "")
                content = args.get("content", "")
                mode = args.get("mode", "write")
                if self._sandbox:
                    return await self._sandbox.modify_file(path, content, mode=mode)
                return f"错误: Sandbox 未初始化"

            elif name == "agent_draft":
                filename = args.get("filename", "scratch.md")
                content = args.get("content", "")
                full_path = f"{draft_dir}{filename}"
                if self._sandbox:
                    if content:
                        return await self._sandbox.modify_file(full_path, content, mode="write")
                    else:
                        try:
                            return await self._sandbox.view_file(full_path)
                        except Exception:
                            return f"草稿 {filename} 不存在"
                return f"错误: Sandbox 未初始化"

            elif name.startswith("agent_"):
                # 4.2: 通用路由 agent_xxx → tool_xxx
                real_name = name[6:]  # agent_search → search
                tool_method = getattr(self, f"tool_{real_name}", None)
                if not tool_method:
                    return f"未知工具: {name}"
                # 4.3: 读取超时配置 + asyncio.wait_for 保护
                timeout_s = self._get_tool_timeout_s(real_name)
                result = await asyncio.wait_for(
                    tool_method(event=None, **args),
                    timeout=timeout_s
                )
                return str(result)[:2000]
            else:
                return f"未知工具: {name}"
        except asyncio.TimeoutError:
            return f"工具 {name} 执行超时"
        except Exception as e:
            return f"工具执行错误: {e}"

    def _get_tool_timeout_s(self, tool_name: str) -> float:
        """读取 base_tools/*.tool.json 中的 timeout_ms，返回秒数，默认 30s"""
        try:
            tool_file = os.path.join(
                SANDBOX_ROOT, "base_tools", f"{tool_name}.tool.json"
            )
            with open(tool_file, encoding="utf-8") as f:
                return json.loads(f.read()).get("timeout_ms", 30000) / 1000
        except Exception:
            return 30.0

    # ========================
    # Prompt 构建
    # ========================


    # ========================
    # 结果解析
    # ========================

    # ========================
    # 主模型通知
    # ========================

    async def _notify_main_model(self, event: AstrMessageEvent, parsed: Dict):
        """通知 AstrBot 触发主模型回复

        修复 Codex 问题9: 使用 set_extra 注入上下文摘要，而非私有属性
        1.3 增强: 同时传递最近 N 条关键消息原文，解决主模型看不到对话内容的问题
        """
        try:
            # AstrBot 的核心机制：设置 is_at_or_wake_command 为 True
            if hasattr(event, "is_at_or_wake_command"):
                event.is_at_or_wake_command = True

            # 通过 set_extra 传递上下文摘要给主模型（修复 Codex 问题9）
            if parsed.get("context_summary") and hasattr(event, "set_extra"):
                event.set_extra("flashlite_context_summary", parsed["context_summary"])
                event.set_extra("flashlite_trigger_reason", parsed.get("reason", ""))

            # 1.3: 注入最近 N 条消息原文（含回复内容和附件信息）
            if hasattr(event, "set_extra"):
                try:
                    raw = getattr(event, "message_obj", None)
                    window_id = ""
                    window_type = "group"
                    if raw and hasattr(raw, "raw_message"):
                        rm = raw.raw_message
                        if isinstance(rm, dict):
                            msg_type = rm.get("message_type", "group")
                            if msg_type == "group":
                                window_id = str(rm.get("group_id", ""))
                                window_type = "group"
                            elif msg_type == "private":
                                window_id = str(rm.get("user_id", ""))
                                window_type = "private"
                    if window_id:
                        _wk = f"GroupMessage:{window_id}" if window_type == "group" else f"FriendMessage:{window_id}"
                        _tf = await self._t_file_mgr.load(_wk)
                        recent_ctx = self._t_file_mgr.build_flashlite_context(_tf)
                        if recent_ctx:
                            event.set_extra("flashlite_recent_messages", recent_ctx)
                except Exception as ctx_err:
                    logger.debug(f"注入最近消息原文失败: {ctx_err}")

            logger.info(
                f"已通知主模型响应: reason='{parsed.get('reason', '')[:60]}'"
            )
        except Exception as e:
            logger.error(f"通知主模型失败: {e}")

    async def _wake_main_for_task(
        self,
        task_event: "AstrMessageEvent",
        task_id: str,
        desc: str,
        summary: str,
        report_path: str = "",
    ):
        """Task 完成后主动以老板娘人格调用主模型生成回复

        直接调 Gemini API，以老板娘人格+task结果上下文生成回复，
        通过 task_event.send() 发送给用户。不依赖下一条消息。
        """
        try:
            api_key = self._load_api_key_from_astrbot()
            if not api_key:
                logger.warning(f"Task {task_id} 唤醒主模型失败: 无 API key")
                return

            # 读取老板娘 persona（从 AstrBot cmd_config.json）
            persona_text = ""
            try:
                cfg_path = os.path.normpath(
                    os.path.join(os.path.dirname(__file__), "..", "..", "cmd_config.json"))
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8-sig") as f:
                        cfg = json.load(f)
                    persona_text = cfg.get("provider_settings", {}).get("prompt_prefix", "")
            except Exception:
                pass

            # 获取相关上下文
            window_context = ""
            try:
                raw = getattr(task_event, "message_obj", None)
                window_id = ""
                window_type = "group"
                if raw and hasattr(raw, "raw_message"):
                    rm = raw.raw_message
                    if isinstance(rm, dict):
                        msg_type = rm.get("message_type", "group")
                        if msg_type == "group":
                            window_id = str(rm.get("group_id", ""))
                        elif msg_type == "private":
                            window_id = str(rm.get("user_id", ""))
                            window_type = "private"
                if window_id:
                    _wk2 = f"GroupMessage:{window_id}" if window_type == "group" else f"FriendMessage:{window_id}"
                    _tf2 = await self._t_file_mgr.load(_wk2)
                    window_context = self._t_file_mgr.build_flashlite_context(_tf2)
            except Exception:
                pass

            # 构造 system prompt
            import datetime as _dt
            _now = _dt.datetime.now()
            system_text = (
                f"{persona_text}\n\n"
                "## 当前情境\n"
                f"当前时间: {_now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "你之前给子代理派遣了一个后台任务，现在任务已经完成了。\n"
                "请审阅任务结果，用你的人格风格简短回复用户（1-3句话）。\n"
                "不需要重复任务ID等技术信息，用自然的方式告知结果。\n"
            )

            # 构造 user message
            task_msg = (
                f"你之前派遣的后台任务已完成：\n"
                f"任务描述: {desc}\n"
                f"执行结果摘要: {summary[:500]}\n"
            )
            if report_path:
                task_msg += f"详细报告位置: {report_path}\n"
            if window_context:
                task_msg += f"\n最近对话上下文:\n{window_context[:800]}\n"

            # 调用 Gemini API
            model = self._tool_model or TOOL_MODEL_DEFAULT
            url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
            payload = {
                "systemInstruction": {"parts": [{"text": system_text}]},
                "contents": [{"role": "user", "parts": [{"text": task_msg}]}],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 300,
                },
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
                ],
            }

            async with self._session.post(
                url, json=payload, headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning(f"Task {task_id} 主模型 API 错误 {resp.status}: {err_text[:200]}")
                    return
                data = await resp.json()

            # === CostTracker: 主模型直连记账 ===
            try:
                usage_meta = data.get("usageMetadata", {})
                if usage_meta and hasattr(self, '_cost_tracker') and self._cost_tracker:
                    await self._cost_tracker.record(
                        model=model,
                        call_type="main_model_task_wake",
                        window_key=_wk2 if window_id else 'task_wake',
                        prompt_tokens=usage_meta.get("promptTokenCount", 0),
                        cached_tokens=usage_meta.get("cachedContentTokenCount", 0),
                        output_tokens=usage_meta.get("candidatesTokenCount", 0),
                    )
            except Exception:
                pass

            # 提取回复文本
            reply_text = ""
            try:
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for p in parts:
                        if "text" in p:
                            reply_text += p["text"]
            except Exception:
                pass

            if reply_text.strip():
                from astrbot.core.message.message_event_result import MessageChain as _MC
                await task_event.send(_MC().message(reply_text.strip()))
                logger.info(f"Task {task_id} 主模型回复已发送")
            else:
                logger.warning(f"Task {task_id} 主模型未生成有效回复")

        except Exception as e:
            logger.error(f"Task {task_id} 唤醒主模型异常: {e}")

    async def _checkpoint_review(
        self,
        task_event: "AstrMessageEvent",
        task_id: str,
        desc: str,
        current_step: int,
        total_steps: int,
        recent_results: list,
        remaining_steps: list,
    ) -> dict:
        """Checkpoint 步：暂停 task 循环，调主模型审阅并返回决策

        Returns:
            {
                "message": "可选，有值则发给用户",
                "remaining_steps": [可选，有值则替换剩余步骤]
            }
            返回空 dict 表示静默继续
        """
        try:
            api_key = self._load_api_key_from_astrbot()
            if not api_key:
                logger.warning(f"Checkpoint {task_id}: 无 API key，静默继续")
                return {}

            # 读取老板娘 persona
            persona_text = ""
            try:
                cfg_path = os.path.normpath(
                    os.path.join(os.path.dirname(__file__), "..", "..", "cmd_config.json"))
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8-sig") as f:
                        persona_text = json.load(f).get(
                            "provider_settings", {}).get("prompt_prefix", "")
            except Exception:
                pass

            # 构造 system prompt
            import datetime as _dt
            system_text = (
                f"{persona_text}\n\n"
                "## Checkpoint 审阅模式\n"
                f"当前时间: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "你之前派遣了一个后台任务，现在执行到了你设置的检查点。\n"
                "请审阅当前进度和结果，做出决策。\n\n"
                "**回复必须是严格的 JSON 对象**（不要 markdown 代码块），字段说明：\n"
                '- "message": 字符串，如果你想通知用户当前进度，写在这里（用你的人格风格）。不通知的话不要包含此字段。\n'
                '- "remaining_steps": 数组，如果你想修改后续步骤，提供新的步骤列表。'
                '每个步骤格式: {"desc":"步骤描述","tool":"工具名","args":{参数}}。不修改的话不要包含此字段。\n\n'
                "决策示例：\n"
                '静默继续: {}\n'
                '通知用户: {"message":"进度正常～继续等会儿就好了"}\n'
                '修改步骤: {"remaining_steps":[{"desc":"新步骤","tool":"sandbox_exec","args":{"code":"..."}}]}\n'
                '通知+修改: {"message":"发现问题，我调整一下计划","remaining_steps":[...]}\n'
            )

            # 构造审阅内容
            import json as _json
            results_str = _json.dumps(recent_results[-5:], ensure_ascii=False, indent=2)[:1000]
            remaining_str = _json.dumps(remaining_steps, ensure_ascii=False, indent=2)[:1000]

            review_msg = (
                f"## 任务信息\n"
                f"任务ID: {task_id}\n"
                f"任务描述: {desc}\n"
                f"当前进度: 第 {current_step}/{total_steps} 步完成\n\n"
                f"## 最近执行结果\n```json\n{results_str}\n```\n\n"
                f"## 剩余步骤\n```json\n{remaining_str}\n```\n\n"
                "请审阅以上内容，回复 JSON 决策。"
            )

            # 调用 Gemini API
            model = self._tool_model or TOOL_MODEL_DEFAULT
            url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
            payload = {
                "systemInstruction": {"parts": [{"text": system_text}]},
                "contents": [{"role": "user", "parts": [{"text": review_msg}]}],
                "generationConfig": {
                    "temperature": 0.4,
                    "maxOutputTokens": 1024,
                    "responseMimeType": "application/json",
                },
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
                ],
            }

            async with self._session.post(
                url, json=payload, headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning(f"Checkpoint {task_id} API 错误 {resp.status}: {err_text[:200]}")
                    return {}
                data = await resp.json()

            # === CostTracker: checkpoint review 记账 ===
            try:
                usage_meta = data.get("usageMetadata", {})
                if usage_meta and hasattr(self, '_cost_tracker') and self._cost_tracker:
                    await self._cost_tracker.record(
                        model=model,
                        call_type="main_model_checkpoint",
                        window_key='checkpoint',
                        prompt_tokens=usage_meta.get("promptTokenCount", 0),
                        cached_tokens=usage_meta.get("cachedContentTokenCount", 0),
                        output_tokens=usage_meta.get("candidatesTokenCount", 0),
                    )
            except Exception:
                pass

            # 提取回复文本
            reply_text = ""
            try:
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for p in parts:
                        if "text" in p:
                            reply_text += p["text"]
            except Exception:
                pass

            if not reply_text.strip():
                logger.info(f"Checkpoint {task_id}: 主模型无输出，静默继续")
                return {}

            # 解析 JSON 决策
            try:
                decision = json.loads(reply_text.strip())
                if not isinstance(decision, dict):
                    decision = {}
            except json.JSONDecodeError:
                logger.warning(f"Checkpoint {task_id}: 主模型输出非 JSON，静默继续")
                return {}

            logger.info(
                f"Checkpoint {task_id} 主模型决策: "
                f"message={'有' if decision.get('message') else '无'}, "
                f"modify={'有' if decision.get('remaining_steps') else '无'}"
            )

            # 执行 message（如果有）
            if decision.get("message"):
                try:
                    from astrbot.core.message.message_event_result import MessageChain as _MC
                    await task_event.send(_MC().message(str(decision["message"])))
                except Exception as send_err:
                    logger.warning(f"Checkpoint {task_id} 通知发送失败: {send_err}")

            return decision

        except Exception as e:
            logger.error(f"Checkpoint {task_id} 审阅异常: {e}")
            return {}

    # ========================
    # 上下文收集
    # ========================

    async def _persist_bot_reply(self, group_id: str, reply_text: str, 
                                  tool_summary: str = "", window_type: str = "group"):
        """将 bot 回复写入 persistence 的 qq_messages 表

        使 _get_recent_context 能读到模型的回复和工具调用结果。
        包含去重检查，避免同一条回复被多次写入。
        """
        try:
            import aiosqlite
            db_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "QQ_data", "messages.db")
            )
            if not os.path.exists(db_path):
                return

            # 组装内容
            content = reply_text
            if tool_summary:
                content = f"{reply_text}\n[工具调用] {tool_summary}" if reply_text else f"[工具调用] {tool_summary}"

            if not content or not content.strip():
                return

            # 截断
            content = content[:2000]

            from datetime import datetime
            now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            
            # 从配置读取 bot 名称
            bot_name = '老板娘'
            try:
                from astrbot.core import sp
                persona = sp.get("persona_name", None)
                if persona:
                    bot_name = persona
            except Exception:
                pass

            async with aiosqlite.connect(db_path) as db:
                # **去重检查**：查最近一条 bot 回复，内容相同则跳过
                cursor = await db.execute(
                    """SELECT content_text FROM qq_messages 
                       WHERE window_id = ? AND window_type = ? AND sender_id = 'bot'
                       ORDER BY created_at DESC LIMIT 1""",
                    (group_id, window_type)
                )
                last_row = await cursor.fetchone()
                if last_row and last_row[0] and last_row[0].strip() == content.strip():
                    logger.debug(f"Bot 回复去重跳过: {group_id}")
                    return

                await db.execute(
                    """INSERT INTO qq_messages 
                       (window_type, window_id, message_id, sender_id, sender_name, 
                        content_text, content_raw, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (window_type, group_id, f"bot_{now_str}", 'bot', bot_name,
                     content, '', now_str)
                )
                await db.commit()
            logger.debug(f"Bot 回复已写入 persistence: {group_id} ({len(content)}字)")
        except Exception as e:
            logger.debug(f"写入 bot 回复到 persistence 失败: {e}")

    # ========================
    # 工具方法
    # ========================

    @staticmethod
    def _extract_text(raw: Any) -> str:
        """从 OneBot 消息段中提取纯文本"""
        parts = []
        message = raw.get("message", []) if isinstance(raw, dict) else getattr(raw, "message", [])

        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        parts.append(seg.get("data", {}).get("text", ""))
                    elif seg.get("type") == "at":
                        parts.append(f"@{seg.get('data', {}).get('qq', '')}")
        elif isinstance(message, str):
            parts.append(message)

        return " ".join(parts).strip()

    def _update_latency_stats(self, latency_ms: float):
        """更新平均延迟统计"""
        n = self._stats["total_calls"]
        if n <= 1:
            self._stats["avg_latency_ms"] = latency_ms
        else:
            old_avg = self._stats["avg_latency_ms"]
            self._stats["avg_latency_ms"] = old_avg + (latency_ms - old_avg) / n

    # ========================
    # 调试命令
    # ========================

    @filter.command("flashlite_status", alias={"中断状态"})
    async def show_status(self, event: AstrMessageEvent):
        """显示 Flash Lite 中断引擎状态"""
        msg = (
            f"⚡ Flash Lite 中断引擎状态\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📊 总调用次数: {self._stats['total_calls']}\n"
            f"🔄 同步触发: {self._stats['sync_triggers']}\n"
            f"⚡ 异步触发: {self._stats['async_triggers']}\n"
            f"🔴 主模型通知: {self._stats['main_model_notified']}\n"
            f"❌ 错误: {self._stats['errors']}\n"
            f"⏱ 平均延迟: {self._stats['avg_latency_ms']:.0f}ms\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📚 Knowledge 缓存: {len(self._knowledge_cache)} 个窗口\n"
            f"🔢 消息计数器: {dict(self._msg_counters)}\n"
        )

        # 显示各群 Knowledge
        if self._knowledge_cache:
            msg += "\n📝 Knowledge 摘要:\n"
            for wid, summary in list(self._knowledge_cache.items())[:5]:
                msg += f"  • {wid}: {summary[:40]}...\n"

        yield event.plain_result(msg)

    @filter.command("flashlite_knowledge", alias={"群知识"})
    async def show_knowledge(self, event: AstrMessageEvent):
        """查看某群的 Knowledge 缓存"""
        # 获取当前群号
        raw = event.message_obj.raw_message
        if raw:
            group_id = str(raw.get("group_id", "") if isinstance(raw, dict) else getattr(raw, "group_id", ""))
        else:
            group_id = ""

        summary = self._knowledge_cache.get(group_id, "（暂无知识缓存）")
        yield event.plain_result(f"📚 群 {group_id} Knowledge:\n{summary}")

    # ========================
    # LLM 请求钩子（修复 Codex 问题2/9：将 Flash Lite 上下文+Knowledge 注入主模型请求）
    # ========================

    @filter.on_llm_request(priority=9000)
    async def inject_flashlite_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        return await self._inject_flashlite_context_impl(event, req)

    @filter.on_llm_response()
    async def track_main_model_cost(self, event: AstrMessageEvent, response) -> None:
        """on_llm_response 钩子：捕获主模型 provider 调用的 usage 数据并记录成本

        Critical Fix: 补全主模型调用记账，确保 CostTracker 覆盖所有 API 调用路径。
        """
        try:
            if not hasattr(self, '_cost_tracker') or self._cost_tracker is None:
                return

            # 从 LLMResponse.usage (TokenUsage) 获取 token 用量
            usage = getattr(response, 'usage', None)
            if usage is None:
                return

            prompt_tokens = getattr(usage, 'input', 0) or 0
            cached_tokens = getattr(usage, 'input_cached', 0) or 0
            output_tokens = getattr(usage, 'output', 0) or 0

            # 至少有一些 token 才值得记录
            if prompt_tokens + output_tokens == 0:
                return

            # 尝试从 raw_completion 获取模型名
            model_name = "unknown_main_model"
            raw = getattr(response, 'raw_completion', None)
            if raw is not None:
                # Gemini: GenerateContentResponse
                if hasattr(raw, 'model'):
                    model_name = raw.model or model_name
                # OpenAI: ChatCompletion
                elif hasattr(raw, 'model'):
                    model_name = raw.model or model_name

            # [R1重构] 从 event 提取窗口标识，不再依赖全局状态
            window_key = self._extract_window_key(event)

            await self._cost_tracker.record(
                model=model_name,
                call_type="main_model",
                window_key=window_key,
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
            )
            logger.debug(
                f"[CostTracker] 主模型记账: model={model_name} "
                f"prompt={prompt_tokens} cached={cached_tokens} output={output_tokens}"
            )
        except Exception as e:
            logger.debug(f"[CostTracker] 主模型记账跳过: {e}")

    # ========================
    # T 文件辅助方法
    # ========================


    def _extract_new_messages(
        self, contexts: list, t_file: dict
    ) -> list:
        """从 req.contexts 中提取 T 文件尚未记录的新消息

        策略：T1 已压缩的消息数 + T 文件 messages 中现有数 = 已处理总数
        req.contexts 中超出这个数量的部分就是新消息。
        
        H-2 修复：当 AstrBot 截断 contexts 导致 len < processed_count 时，
        使用指纹对齐降级策略恢复增量提取。
        """
        existing_count = len(t_file.get("messages", []))
        compressed_count = t_file.get("T1", {}).get("original_msg_count", 0)
        processed_count = compressed_count + existing_count

        if not contexts:
            return []

        if len(contexts) > processed_count:
            # 正常情况：contexts 比 T 文件多 → 增量提取
            return contexts[processed_count:]
        
        elif len(contexts) < processed_count:
            # ⚠️ H-2: AstrBot 截断了 contexts
            # 降级策略：用 T 文件最后一条消息的内容指纹在 contexts 中反向查找对齐点
            t_msgs = t_file.get("messages", [])
            if t_msgs:
                last_t_fp = self._msg_fingerprint(t_msgs[-1])
                for i in range(len(contexts) - 1, -1, -1):
                    if self._msg_fingerprint(contexts[i]) == last_t_fp:
                        new_msgs = contexts[i + 1:]
                        if new_msgs:
                            logger.info(
                                f"[T-FILE] 降级对齐: contexts 被截断 "
                                f"({len(contexts)} < {processed_count}), "
                                f"找到 {len(new_msgs)} 条新消息"
                            )
                        return new_msgs
            
            # 完全无法对齐 → 安全降级，不追加
            logger.warning(
                f"[T-FILE] contexts 截断且无法对齐 "
                f"({len(contexts)} < {processed_count})"
            )
            return []
        
        return []  # len == processed_count，无新消息

    @staticmethod
    def _msg_fingerprint(msg: dict) -> str:
        """消息指纹：role + content 前50字 + tool_call_id
        
        用于在 contexts 截断场景下进行消息对齐匹配。
        """
        role = msg.get("role", "")
        content = str(msg.get("content", ""))[:50]
        tcid = msg.get("tool_call_id", "")
        return f"{role}|{content}|{tcid}"

    # ========================
    # LLM 工具注册（AstrBot 原生 function-calling 集成）
    # ========================

    @filter.llm_tool(name="view_file")
    async def tool_view_file(self, event: AstrMessageEvent, path: str, paths: str = "", start_line: int = 1, end_line: int = None):
        '''查看 Sandbox 内的文件内容。支持纯文本和图片文件。

        - 纯文本文件(.txt/.md/.py/.json等): 返回指定行范围的文本（大文件自动分页，每页200行）
        - 图片文件(.png/.jpg等): 自动优化处理后返回图片数据
        - 批量模式: 传入 paths(JSON数组) 一次读取多个文件
        - PDF/Office文档: 自动提取文本内容

        Args:
            path(string): Sandbox 内相对路径，如 workspace/notes/memo.txt。不要加 Sandbox/ 前缀(单文件模式)，如 workspace/files/doc.txt。不要加 Sandbox/ 前缀
            paths(string): JSON数组字符串，批量读取多个文件路径(可选)，路径格式同 path
            start_line(number): 起始行号(1-indexed，仅文本文件，默认1)
            end_line(number): 结束行号(1-indexed，仅文本文件，不传则自动分页)
        '''
        try:
            # === 重复调用检测 ===
            call_sig = f"view_file:{path}:{start_line}:{end_line}"
            if not hasattr(self, '_tool_call_dedup'):
                self._tool_call_dedup = {}

            umo = getattr(event, "unified_msg_origin", "default")
            if umo not in self._tool_call_dedup:
                self._tool_call_dedup[umo] = []

            history = self._tool_call_dedup[umo]
            # 计算连续相同调用次数
            consecutive = 0
            for prev in reversed(history):
                if prev == call_sig:
                    consecutive += 1
                else:
                    break

            if consecutive >= 2:
                # 超过2次连续重复，拒绝执行
                logger.warning(f"[view_file] 重复调用检测: {call_sig} 已连续{consecutive}次，中断循环")
                history.clear()  # 重置避免永久阻塞
                return (
                    f"⚠️ 你已经连续 {consecutive} 次用相同参数调用 view_file(\"{path}\")。"
                    f"文件内容已在上次调用中完整返回，请直接基于已有内容回复用户，"
                    f"不要再重复读取同一个文件。"
                )

            history.append(call_sig)
            # 保留最近20条记录
            if len(history) > 20:
                self._tool_call_dedup[umo] = history[-20:]

            # === 正常执行 ===
            # 批量模式
            if paths:
                import json as _json
                try:
                    path_list = _json.loads(paths) if isinstance(paths, str) else paths
                except (ValueError, TypeError):
                    path_list = [p.strip() for p in paths.split(',') if p.strip()]
                # 路径清理：批量模式
                path_list = [p[len("Sandbox")+1:] if p.startswith(("Sandbox/","Sandbox\\")) else p for p in path_list]
                content = await self._sandbox.view_files_batch(path_list)
                return content[:16000]
            # 单文件模式
            # 路径清理：兼容模型可能传入的 Sandbox/ 前缀
            if path.startswith("Sandbox/") or path.startswith("Sandbox\\"):
                path = path[len("Sandbox") + 1:]
            content = await self._sandbox.view_file(path, start_line, end_line)
            return content[:8000]
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="modify_file")
    async def tool_modify_file(self, event: AstrMessageEvent, path: str, content: str, mode: str = "write"):
        '''在 Sandbox/workspace/ 内创建或修改文件。

        Args:
            path(string): Sandbox 内相对路径
            content(string): 文件内容
            mode(string): write(覆盖) 或 append(追加)
        '''
        try:
            # 路径清理：兼容模型可能传入的 Sandbox/ 前缀
            if path.startswith("Sandbox/") or path.startswith("Sandbox\\"):
                path = path[len("Sandbox") + 1:]
            await self._sandbox.modify_file(path, content, mode)
            return f"文件已{'追加' if mode == 'append' else '写入'}: {path}"
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="sandbox_exec")
    async def tool_sandbox_exec(self, event: AstrMessageEvent, code: str = "", language: str = "python", timeout_ms: int = 30000, command: str = ""):
        '''在 Sandbox 安全沙盒内执行代码或系统命令。

        code 模式：执行代码片段（写临时脚本运行）
        command 模式：执行系统命令（如 pip install、git 等）

        Args:
            code(string): 要执行的代码（和 command 二选一）
            command(string): 系统命令（和 code 二选一，如 'pip install pdfplumber'）
            language(string): python/node/bash/shell/cmd（默认python）
            timeout_ms(number): 超时毫秒数(默认30000，上限300000)
        '''
        try:
            timeout_ms = min(timeout_ms, 300000)
            result = await self._sandbox.exec_code(
                code=code, language=language,
                timeout_ms=timeout_ms, command=command,
            )
            if result.get("success"):
                output = result.get("stdout", "")
                if result.get("stderr"):
                    output += f"\n[stderr] {result['stderr']}"
                return output[:8000] if output else "(执行成功，无输出)"
            else:
                # 透传具体错误信息而非仅"未知错误"
                error_msg = result.get("error", "未知错误")
                extra = ""
                if result.get("killed"):
                    extra = f" [进程被杀: {result.get('kill_reason', 'unknown')}]"
                if result.get("stderr") and result.get("stderr") != error_msg:
                    extra += f"\n[stderr] {result['stderr'][:2000]}"
                return f"执行失败: {error_msg}{extra}"
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="memory_write")
    async def tool_memory_write(self, event: AstrMessageEvent, title: str, content: str, tags: str = "[]", workspace: str = "general", category: str = "general", search_summary: str = "", pinned: bool = False):
        '''将重要信息写入长期记忆。支持去重检测和自动摘要生成。

        Args:
            title(string): 记忆标题
            content(string): 记忆内容(上限15KB)
            tags(string): 标签数组JSON字符串
            workspace(string): 工作区(群号/QQ号/general)
            category(string): 分类(general/problem-solution/technical-note/conversation)
            search_summary(string): 搜索优化摘要(可选,系统自动生成)
            pinned(boolean): 是否置顶
        '''
        try:
            import json as _json
            # tags 容错: 优先 JSON 解析，降级逗号分割
            if isinstance(tags, str):
                try:
                    tag_list = _json.loads(tags)
                except _json.JSONDecodeError:
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            else:
                tag_list = tags
            result = await self._memory.write(
                title=title, content=content, tags=tag_list,
                workspace=workspace, category=category,
                search_summary=search_summary, pinned=pinned,
            )
            msg = f"✅ 记忆已写入: {result['id']}"
            if result.get('duplicates'):
                dup_info = ', '.join(d['title'] for d in result['duplicates'][:3])
                msg += f"\n⚠️ 疑似重复: {dup_info}"
            return msg
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="memory_query")
    async def tool_memory_query(self, event: AstrMessageEvent, query: str = "", workspace: str = "", grep: str = "", depth: str = "index", scope: str = "workspace", tags: str = "[]", after: str = "", before: str = "", limit: int = 10):
        '''搜索记忆库。支持混合搜索+FTS5全文检索+三级深度。

        Args:
            query(string): 搜索关键词(模糊搜索)
            workspace(string): 工作区过滤(群号/QQ号/general)
            grep(string): 全文精确搜索(FTS5)
            depth(string): 返回深度(index/summary/full)
            scope(string): 搜索范围(workspace/global)
            tags(string): 标签过滤JSON数组
            after(string): 时间过滤(只返回此时间之后)
            before(string): 时间过滤(只返回此时间之前)
            limit(number): 返回条数上限
        '''
        try:
            import json as _json
            # tags 容错: 优先 JSON 解析，降级逗号分割
            if isinstance(tags, str) and tags != "[]":
                try:
                    tag_list = _json.loads(tags)
                except _json.JSONDecodeError:
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
            else:
                tag_list = None
            result = await self._memory.query(
                query=query, workspace=workspace or None,
                grep=grep, depth=depth, scope=scope,
                tags=tag_list, after=after or None,
                before=before or None, limit=limit,
            )
            # 智能降级: 如果返回概览但用户指定了 workspace，追加该工作区的记忆列表
            if result.get('mode') == 'overview' and workspace:
                list_result = await self._memory.query(
                    query=workspace, workspace=workspace,
                    depth=depth, scope="workspace", limit=limit,
                )
                entries = list_result.get('results', [])
                if entries:
                    lines = [f"📋 工作区 '{workspace}' 的记忆 ({len(entries)} 条):"]
                    for r in entries:
                        pin = "📌" if r.get('pinned') else ""
                        lines.append(f"- {pin}[{r['id']}] {r['title']} (tags: {','.join(r.get('tags', []))})")
                        if depth in ('summary', 'full'):
                            lines.append(f"  {r.get('category','')}, {r.get('size_bytes',0)}B, {r['updated_at']}")
                    return '\n'.join(lines)
            if result.get('mode') == 'overview':
                lines = [f"📊 记忆概览 (共 {result['total']} 条)"]
                for ws, cnt in result.get('by_workspace', {}).items():
                    lines.append(f"  - {ws}: {cnt} 条")
                if result.get('pinned'):
                    lines.append("📌 置顶:")
                    for p in result['pinned']:
                        lines.append(f"  - [{p['id']}] {p['title']}")
                if result.get('top_tags'):
                    tags_str = ', '.join(f"{t['tag']}({t['count']})" for t in result['top_tags'][:10])
                    lines.append(f"🏷️ 热门标签: {tags_str}")
                return '\n'.join(lines)
            entries = result.get('results', [])
            if not entries:
                return "未找到匹配的记忆"
            lines = []
            for r in entries:
                pin = "📌" if r.get('pinned') else ""
                lines.append(f"- {pin}[{r['id']}] {r['title']} (tags: {','.join(r.get('tags', []))})")
                if depth == 'summary':
                    lines.append(f"  {r.get('category','')}, {r.get('size_bytes',0)}B, {r['updated_at']}")
            return '\n'.join(lines)
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="memory_read")
    async def tool_memory_read(self, event: AstrMessageEvent, id: str):
        '''读取某条记忆的完整内容（含完整元信息）。

        Args:
            id(string): 记忆 ID
        '''
        try:
            mem = await self._memory.read(id)
            if not mem:
                return f"记忆 {id} 不存在"
            # 返回完整元信息
            import json as _json
            meta = {
                "id": mem.get("id", id),
                "title": mem.get("title", ""),
                "workspace": mem.get("workspace", ""),
                "category": mem.get("category", "general"),
                "tags": mem.get("tags", []),
                "pinned": mem.get("pinned", False),
                "created_at": mem.get("created_at", ""),
                "updated_at": mem.get("updated_at", ""),
            }
            content = mem.get("content", "")
            return f"# {meta['title']}\n\n{content}\n\n---\n📋 元信息: {_json.dumps(meta, ensure_ascii=False)}"
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="memory_update")
    async def tool_memory_update(self, event: AstrMessageEvent, id: str, content: str = "", append: str = "", title: str = "", tags: str = "", category: str = "", search_summary: str = "", pinned: str = ""):
        '''更新已有记忆。支持更新所有字段。

        Args:
            id(string): 记忆 ID
            content(string): 替换全部内容
            append(string): 追加到末尾
            title(string): 更新标题
            tags(string): 新增标签(JSON数组，合并到已有)
            category(string): 更新分类(general/problem-solution/technical-note/conversation)
            search_summary(string): 更新搜索摘要
            pinned(string): 是否置顶(true/false)
        '''
        try:
            import json as _json
            kwargs = {}
            if content: kwargs["content"] = content
            if append: kwargs["append"] = append
            if title: kwargs["title"] = title
            if tags:
                try:
                    kwargs["tags"] = _json.loads(tags) if isinstance(tags, str) else tags
                except _json.JSONDecodeError:
                    kwargs["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
            if category: kwargs["category"] = category
            if search_summary: kwargs["search_summary"] = search_summary
            if pinned:
                kwargs["pinned"] = pinned.lower() in ("true", "1", "yes")
            if not kwargs:
                return "错误: 未提供任何更新内容"
            ok = await self._memory.update(id, **kwargs)
            return f"记忆 {id} 已更新 ({', '.join(kwargs.keys())})" if ok else f"记忆 {id} 不存在"
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="knowledge_update")
    async def tool_knowledge_update(self, event: AstrMessageEvent, window_key: str, summary: str, active_users: str = "[]", mood: str = "", recent_topics: str = "[]", operation: str = ""):
        '''更新 Knowledge 知识缓存。

        Args:
            window_key(string): 窗口标识（GroupMessage:群号 或 FriendMessage:QQ号）
            summary(string): 话题摘要（200-500字）
            active_users(string): 活跃用户列表JSON
            mood(string): 群氛围
            recent_topics(string): 最近话题JSON
            operation(string): 操作记录
        '''
        try:
            import json as _json
            users = _json.loads(active_users) if isinstance(active_users, str) and active_users else []
            topics = _json.loads(recent_topics) if isinstance(recent_topics, str) and recent_topics else []
            self._knowledge.update_window(
                window_key=window_key,
                summary=summary,
                active_users=users,
                mood=mood,
                recent_topics=topics,
            )
            if operation:
                self._knowledge.add_operation(operation)
            return f"Knowledge 已更新: {window_key}"
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="knowledge_card")
    async def tool_knowledge_card(self, event: AstrMessageEvent, qq_ids: str, max_facts: int = 10, detail: bool = False, include_archived: bool = False):
        '''查询指定用户的卡片画像信息。支持 QQ号 和昵称混合查询。

        Args:
            qq_ids(string): QQ号或昵称列表，多个用逗号分隔。优先用QQ号精确匹配，找不到时自动用昵称降级搜索。
            max_facts(number): 每人最多返回的事实条数(默认10)
            detail(boolean): 是否返回完整内容(默认false只返回摘要)
            include_archived(boolean): 是否包含归档的冷事实(默认false)
        '''
        try:
            ids = [x.strip() for x in qq_ids.split(",") if x.strip()]
            if not ids:
                return "错误: 需要提供 QQ 号或昵称"
            # 昵称降级: 如果 QQ 号找不到，尝试用昵称在所有卡片中搜索
            profiles = self._knowledge._cache.get("user_profiles", {})
            resolved_ids = []
            not_found = []
            for q in ids:
                if q in profiles:
                    resolved_ids.append(q)
                else:
                    # 昵称降级搜索: 在所有卡片的 nickname 中查找
                    found = False
                    for qq_id, pf in profiles.items():
                        nick = pf.get("nickname", "")
                        if nick and (q.lower() == nick.lower() or q in nick or nick in q):
                            resolved_ids.append(qq_id)
                            found = True
                            break
                    if not found:
                        not_found.append(q)
            cards = ""
            if resolved_ids:
                cards = self._knowledge.get_user_cards(
                    resolved_ids, max_facts, detail=detail, include_archived=include_archived
                )
            result_parts = []
            if cards:
                result_parts.append(f"## 用户卡片\n{cards}")
            if not_found:
                result_parts.append(f"未找到以下用户的卡片: {', '.join(not_found)}")
            return '\n'.join(result_parts) if result_parts else f"未找到以下用户的卡片: {', '.join(ids)}"
        except Exception as e:
            return f"错误: {e}"

    @filter.llm_tool(name="search")
    async def tool_search(self, event: AstrMessageEvent, query: str, scope: str = "auto", num_results: int = 5, deep: bool = False):
        '''统一搜索工具——联网搜索/记忆搜索/文件搜索。

        scope 模式说明：
        - auto: 自动判断（默认）——天气/新闻/实时信息→web，其他→all
        - web: 联网搜索（Google Grounding），获取互联网最新信息
        - memory: 搜索本地 Memory 记忆库
        - files: 搜索 Sandbox 工作区文件内容
        - all: memory + files 同时搜索

        Args:
            query(string): 搜索关键词
            scope(string): 搜索范围 auto/web/memory/files/all
            num_results(int): 返回结果数量(默认5)
            deep(boolean): web模式深度抓取来源页面并概括(默认false)
        '''
        try:
            results = []

            # === auto 模式：自动判断 scope ===
            if scope == "auto":
                web_keywords = ['天气', '新闻', '最新', '今天', '价格', '股票', '比分',
                                '热搜', '实时', '现在', '几点', '汇率', '怎么了', '发生',
                                'weather', 'news', 'latest', 'price', 'today', 'score']
                if any(kw in query.lower() for kw in web_keywords):
                    scope = "web"
                else:
                    scope = "all"

            # === Memory 搜索 ===
            if scope in ("memory", "all"):
                try:
                    mem_result = await self._memory.query(query=query, limit=5)
                    mems = mem_result.get("results", []) if isinstance(mem_result, dict) else []
                    for m in mems:
                        results.append(f"[记忆] {m.get('title','')}: {m.get('summary', '')[:80]}")
                except Exception:
                    pass

            # === 文件搜索 ===
            if scope in ("files", "all"):
                try:
                    files = await self._sandbox.list_files("workspace")
                    for f in files:
                        if query.lower() in f["name"].lower():
                            results.append(f"[文件] {f['path']} ({f.get('type', 'file')})")
                    # 文件内容 grep
                    if not results or scope == "files":
                        try:
                            # C-2 修复: 使用 json.dumps 转义 query，防止代码注入
                            import json as _json
                            safe_query = _json.dumps(query.lower())  # 自动转义引号等特殊字符
                            grep_script = (
                                'import os, glob, json\n'
                                f'QUERY = json.loads({_json.dumps(safe_query)})\n'
                                'matches = []\n'
                                'for f in glob.glob("workspace/**/*", recursive=True):\n'
                                '    if os.path.isfile(f) and os.path.getsize(f) < 50000:\n'
                                '        try:\n'
                                '            with open(f, "r", encoding="utf-8", errors="replace") as fh:\n'
                                '                for i, line in enumerate(fh, 1):\n'
                                '                    if QUERY in line.lower():\n'
                                '                        matches.append(f"{f}:{i}: {line.strip()[:80]}")\n'
                                '                        if len(matches) >= 10: break\n'
                                '        except: pass\n'
                                '    if len(matches) >= 10: break\n'
                                'for m in matches: print(m)\n'
                            )
                            grep_result = await self._sandbox.exec_code(
                                grep_script, "python", 10000,
                            )
                            if grep_result.get("success") and grep_result.get("stdout", "").strip():
                                for line in grep_result["stdout"].strip().split("\n")[:5]:
                                    results.append(f"[内容匹配] {line}")
                        except Exception:
                            pass
                except Exception:
                    pass

            # === 联网搜索 (Grounding with Google Search) ===
            if scope == "web":
                try:
                    if not self._api_key:
                        return "错误: API key 未配置，无法联网搜索"

                    grounding_model = self._tool_model or FLASH_LITE_MODEL
                    url = f"{GEMINI_API_BASE}/{grounding_model}:generateContent?key={self._api_key}"
                    payload = {
                        "contents": [{"role": "user", "parts": [{"text": f"搜索并总结以下内容的最新信息，返回{num_results}条结果摘要：{query}"}]}],
                        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
                        "tools": [{"googleSearch": {}}],
                    }
                    async with self._session.post(url, json=payload, headers={"Content-Type": "application/json"}) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            return f"联网搜索失败 ({resp.status}): {err[:200]}"
                        data = await resp.json()

                    candidates = data.get("candidates", [])
                    if not candidates:
                        return "联网搜索无结果"

                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "\n".join(p["text"] for p in parts if "text" in p and not p.get("thought"))

                    # 提取 grounding 来源
                    grounding = candidates[0].get("groundingMetadata", {})
                    sources = grounding.get("groundingChunks", [])
                    source_urls = []
                    if sources:
                        text += "\n\n📌 来源:\n"
                        for i, src in enumerate(sources[:num_results], 1):
                            web = src.get("web", {})
                            src_url = web.get('uri', '')
                            text += f"  {i}. [{web.get('title', '未知')}]({src_url})\n"
                            source_urls.append(src_url)

                    # 深度模式：抓取来源页面
                    if deep and source_urls:
                        if not hasattr(self, '_web_engine') or self._web_engine is None:
                            sandbox_path = self._sandbox._root if self._sandbox else ""
                            self._web_engine = WebFetchEngine(sandbox_path=sandbox_path)
                        fetch_tasks = [
                            self._web_engine.fetch_page(u, mode="compact") for u in source_urls[:3]
                        ]
                        pages = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                        for i, page in enumerate(pages):
                            if isinstance(page, dict) and "content" in page:
                                chunk = page["content"][:2000]
                                # 工具模型 chunk 概括
                                summary = await self._call_tool_model(
                                    f"请用100-200字概括以下网页内容的核心信息：\n\n{chunk}",
                                    window_key=self._extract_window_key(event)
                                )
                                text += f"\n📄 来源{i+1}详情: {summary}"

                    return text or "联网搜索完成但无文本结果"
                except Exception as e:
                    return f"联网搜索错误: {e}"

            return "\n".join(results) if results else "未找到匹配结果"
        except Exception as e:
            return f"搜索错误: {e}"

    @filter.llm_tool(name="web_fetch")
    async def tool_web_fetch(self, event: AstrMessageEvent, url: str = "", mode: str = "text", session_id: str = "", action: str = "", selector: str = "", value: str = "", scroll_count: int = 0, urls: str = ""):
        '''获取网页内容/交互操作（Playwright无头浏览器，降级aiohttp）。

        支持多种模式:
        - 内容获取: mode=text(默认)/full/compact/minimal
        - 原始HTML: mode=html → 返回原始HTML
        - 截图+文本: mode=rich → 同时返回截图和文本
        - 链接提取: mode=links → 提取页面所有链接
        - 截图: mode=screenshot → 截图保存到Sandbox
        - 表格提取: mode=tables → 提取页面HTML表格为Markdown
        - 批量截图: mode=batch_screenshot + urls(JSON数组) → 批量截图
        - 文件下载: mode=download → 下载文件到Sandbox
        - 本地文件: url=file:// → 直接打开本地PDF/Office/图片等
        - 交互操作: action=click/type/scroll/wait/screenshot/content/visible/find/close
        - 多步流水线: mode=pipeline + value(JSON步骤数组)

        Args:
            url(string): 网页URL或file://路径(新页面必需)
            mode(string): text/full/compact/minimal/html/rich/links/screenshot/tables/batch_screenshot/download/pipeline
            session_id(string): 复用已有会话(交互模式)
            action(string): 交互操作(click/type/scroll/wait/screenshot/content/visible/find/close)
            selector(string): CSS选择器(交互/表格用)
            value(string): 输入值(type/find/pipeline步骤JSON)
            scroll_count(int): 滚动次数
            urls(string): JSON数组-批量截图的URL列表
        '''
        try:
            # @quoted 快捷语法解析
            url = self._resolve_quoted(url)
            if not hasattr(self, '_web_engine') or self._web_engine is None:
                sandbox_path = ""
                if self._sandbox:
                    sandbox_path = self._sandbox._root
                self._web_engine = WebFetchEngine(sandbox_path=sandbox_path)
            # 交互模式
            if action:
                result = await self._web_engine.interact(
                    action=action, url=url, session_id=session_id,
                    selector=selector, value=value, scroll_count=scroll_count,
                )
                if "error" in result:
                    return f"交互错误: {result['error']}"
                import json as _json
                return _json.dumps(result, ensure_ascii=False, indent=2)
            if not url and mode not in ("batch_screenshot",):
                return "错误: 需要提供 URL"

            # 原始 HTML 模式
            if mode == "html":
                result = await self._web_engine.fetch_html(url, selector=selector, scroll_count=scroll_count)
                if "error" in result:
                    return f"获取失败: {result['error']}"
                return result.get("html", "")[:16000]

            # 截图+文本一体模式
            if mode == "rich":
                result = await self._web_engine.fetch_rich(url, scroll_count=scroll_count)
                if "error" in result:
                    return f"获取失败: {result['error']}"
                ss = result.get("screenshot", "")
                text = result.get("content", "")
                return f"📸 截图: {ss}\n\n{text}"

            # 表格提取模式
            if mode == "tables":
                result = await self._web_engine.extract_tables(url, selector=selector)
                if "error" in result:
                    return f"获取失败: {result['error']}"
                tables = result.get("tables", [])
                if not tables:
                    return "未找到表格"
                parts = []
                for t in tables:
                    parts.append(f"### 表格 {t['index']+1} ({t['rows']}行×{t['cols']}列)\n{t['markdown']}")
                return "\n\n".join(parts)

            # 批量截图模式
            if mode == "batch_screenshot":
                import json as _json
                url_list = []
                if urls:
                    try:
                        url_list = _json.loads(urls) if isinstance(urls, str) else urls
                    except:
                        url_list = [u.strip() for u in urls.split(',') if u.strip()]
                elif url:
                    url_list = [url]
                if not url_list:
                    return "错误: 需要提供 urls(JSON数组)"
                result = await self._web_engine.batch_screenshot(url_list)
                if "error" in result:
                    return f"批量截图失败: {result['error']}"
                parts = []
                for ss in result.get("screenshots", []):
                    if "error" in ss:
                        parts.append(f"❌ {ss['url']}: {ss['error']}")
                    else:
                        parts.append(f"📸 {ss['url']} → {ss.get('screenshot', '')}")
                return "\n".join(parts)

            # 文件下载模式
            if mode == "download":
                result = await self._web_engine.download(url)
                if "error" in result:
                    return f"下载失败: {result['error']}"
                return f"✅ 已下载: {result.get('path', '')} ({result.get('size', 0)}B)"

            # 多步流水线模式
            if mode == "pipeline":
                import json as _json
                steps = []
                if value:
                    try:
                        steps = _json.loads(value) if isinstance(value, str) else value
                    except:
                        return "错误: pipeline 模式需要 value 参数为 JSON 步骤数组"
                if not steps:
                    return "错误: 需要提供 steps"
                result = await self._web_engine.pipeline(url, steps)
                return _json.dumps(result, ensure_ascii=False, indent=2)

            # 标准抓取模式
            result = await self._web_engine.fetch_page(
                url=url, mode=mode, scroll_count=scroll_count,
            )
            if "error" in result:
                return f"获取失败: {result['error']}"
            if mode == "links":
                links = result.get("links", [])
                return "\n".join(f"- [{l.get('text','')}]({l['url']})" for l in links) or "未找到链接"
            if mode == "screenshot":
                return f"截图已保存: {result.get('screenshot', '')}"
            return result.get("content", "页面为空")
        except Exception as e:
            return f"获取错误: {e}"


    @filter.llm_tool(name="QQ_data_original")
    async def tool_qq_data_original(self, event: AstrMessageEvent, window_key: str = "", start_seq: int = 0, count: int = 20, keyword: str = "", around_msg_id: str = ""):
        '''查阅 QQ 聊天原文记录——用于回溯被压缩的历史消息或查看引用消息上下文。

        Args:
            window_key(string): 窗口标识 (GroupMessage:群号 或 FriendMessage:QQ号)，留空自动使用当前会话
            start_seq(int): 起始消息序号 (0=最新)
            count(int): 获取消息条数
            keyword(string): 关键词过滤
            around_msg_id(string): 围绕某条消息ID取上下文（支持 @quoted_msg 快捷语法）。填写后忽略 start_seq，取该消息前后各 count/2 条
        '''
        import sqlite3
        try:
            count = min(count, 50)  # 安全上限

            # 解析 @quoted 快捷语法
            if around_msg_id:
                around_msg_id = self._resolve_quoted(around_msg_id)

            # 自动推断 window_key
            if not window_key:
                if hasattr(event, 'message_obj') and event.message_obj:
                    msg = event.message_obj
                    if msg.type and 'group' in str(msg.type).lower():
                        window_key = f"GroupMessage:{msg.group_id or msg.session_id}"
                    else:
                        window_key = f"FriendMessage:{msg.session_id}"

            gid = window_key.split(":")[-1] if ":" in window_key else window_key

            # 数据库搜索路径（优先 qq_messages，兼容 message_log）
            db_candidates = [
                os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "QQ_data", "messages.db")),
                os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "data_v4.db")),
            ]
            results = []
            for db_path in db_candidates:
                if not os.path.exists(db_path):
                    continue
                try:
                    conn = sqlite3.connect(db_path, timeout=5)
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

                    # === qq_messages 表（persistence 插件格式）===
                    if "qq_messages" in tables:
                        if around_msg_id:
                            # 指针回溯模式：围绕 message_id 取上下文
                            cur.execute("SELECT id FROM qq_messages WHERE message_id = ?", (str(around_msg_id),))
                            anchor = cur.fetchone()
                            if not anchor:
                                conn.close()
                                continue
                            anchor_id = anchor[0]
                            half = count // 2
                            sql = """SELECT * FROM qq_messages 
                                     WHERE window_id = ? AND id BETWEEN ? AND ? 
                                     ORDER BY id ASC"""
                            rows = cur.execute(sql, (gid, anchor_id - half, anchor_id + half)).fetchall()
                        else:
                            sql = """SELECT * FROM qq_messages 
                                     WHERE window_id = ? 
                                     ORDER BY id DESC LIMIT ? OFFSET ?"""
                            rows = cur.execute(sql, (gid, count, start_seq)).fetchall()
                        for r in rows:
                            msg = dict(r)
                            text = msg.get("content_text", "")
                            if keyword and keyword.lower() not in str(text).lower():
                                continue
                            sender = msg.get("sender_name", "?")
                            ts = msg.get("created_at", "")
                            mid = msg.get("message_id", "")
                            marker = " 📌" if str(mid) == str(around_msg_id) else ""
                            results.append(f"[{ts}] {sender} (msg_id={mid}){marker}: {text}")

                    # === message_log 表（AstrBot 内置格式）===
                    elif "message_log" in tables:
                        sql = "SELECT * FROM message_log WHERE session_id LIKE ? ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                        rows = cur.execute(sql, (f"%{gid}%", count, start_seq)).fetchall()
                        for r in rows:
                            msg = dict(r)
                            text = msg.get("content", msg.get("message", ""))
                            if keyword and keyword.lower() not in str(text).lower():
                                continue
                            sender = msg.get("sender_name", msg.get("user_id", "?"))
                            ts = msg.get("timestamp", "")
                            results.append(f"[{ts}] {sender}: {text}")

                    conn.close()
                    if results:
                        break
                except Exception as db_err:
                    logger.debug(f"数据库 {db_path} 查询失败: {db_err}")
                    continue
            if not results:
                return f"未找到 {window_key} 的聊天记录（数据库可能尚未初始化或格式不兼容）"
            mode = "指针回溯" if around_msg_id else "顺序查询"
            header = f"📜 {window_key} 原文记录 [{mode}] (共{len(results)}条):\n"
            return header + "\n".join(results)
        except Exception as e:
            return f"查询错误: {e}"

    @filter.llm_tool(name="task_set")
    async def tool_task_set(self, event: AstrMessageEvent, action: str, task_description: str = "", task_id: str = "", name: str = "", source_pointer: str = "", steps: str = "[]", wake_condition: str = "", inject_context: str = ""):
        '''管理并发后台任务——创建、查询状态、终止。支持多步骤编排和唤醒条件。

        Args:
            action(string): create/check/kill/list
            task_description(string): 任务描述 (create 时)
            task_id(string): 任务 ID (check/kill 时)
            name(string): 任务名称(create 时可选 用于 list 展示 如不填自动取描述前 30 字)
            source_pointer(string): 源头指针——此任务的来源引用(如消息ID/文件路径/上下文标记)
            steps(string): 步骤列表JSON(create时可选)——按顺序执行的子任务数组，每项含 {"desc":"描述"} (tool/args字段预留供将来反射调用)
            wake_condition(string): 唤醒条件——任务完成后如何通知主模型(如 "notify_main"/"write_report"/"silent")
            inject_context(string): 是否注入当前对话上下文给工具模型("true"注入 其他值或留空不注入)
        '''
        try:
            import json as _json
            if action == "list":
                if not self._task_pool:
                    return "当前无活跃任务"
                lines = []
                for tid, info in self._task_pool.items():
                    task = info["task"]
                    status = "✅完成" if task.done() else "⏳运行中"
                    meta = info.get("meta", {})
                    task_name = meta.get("name", "未命名")
                    src = meta.get("source_pointer", "")
                    step_progress = meta.get("step_progress", "")
                    created = meta.get("created_at", "")
                    line = f"  {tid} [{task_name}]: {status}"
                    if created:
                        line += f" ({created})"
                    if step_progress:
                        line += f" 进度:{step_progress}"
                    if src:
                        line += f" 来源:{src}"
                    lines.append(line)
                return f"活跃任务 ({len(self._task_pool)}):\n" + "\n".join(lines)
            elif action == "create":
                if not task_description:
                    return "错误: 创建任务需要 task_description"
                FlashLiteEngine._task_counter += 1
                tid = f"task-{FlashLiteEngine._task_counter:04d}"
                # 解析步骤列表
                step_list = []
                if steps and steps != "[]":
                    try:
                        step_list = _json.loads(steps) if isinstance(steps, str) else steps
                    except _json.JSONDecodeError:
                        step_list = []
                wake = wake_condition or "notify_main"
                task_name = name or task_description[:30]
                meta = {
                    "name": task_name,
                    "source_pointer": source_pointer,
                    "steps": step_list,
                    "wake_condition": wake,
                    "description": task_description,
                    "step_progress": f"0/{len(step_list)}" if step_list else "",
                    "results": [],
                    "window_id": getattr(event, 'message_id', '')[:20] if event else "",
                    "created_at": __import__('datetime').datetime.now().strftime('%H:%M:%S'),
                }
                # inject_context: 在主模型发出调用的此刻读取当前窗口 T 文件上下文快照
                _ctx_text = ""
                if inject_context and inject_context.lower() == "true" and self._t_file_mgr:
                    try:
                        _wk = None
                        if hasattr(event, "message_obj") and event.message_obj:
                            _raw = getattr(event.message_obj, "raw_message", None)
                            if _raw and isinstance(_raw, dict):
                                if _raw.get("message_type", "group") == "group":
                                    _wk = f"GroupMessage:{_raw.get('group_id', '')}"
                                else:
                                    _wk = f"FriendMessage:{_raw.get('user_id', '')}"
                        if _wk:
                            _tf = await self._t_file_mgr.load(_wk)
                            _ctx_text = self._t_file_mgr.build_flashlite_context(_tf)
                            logger.debug(f"[task_set] inject_context: 获取 {_wk} 上下文 {len(_ctx_text)} 字")
                    except Exception as _e:
                        logger.warning(f"[task_set] inject_context 获取上下文失败: {_e}")
                meta["context_text"] = _ctx_text
                async def _run_task(desc, task_steps, task_meta, task_event, task_id):
                    """用工具模型执行多步骤任务，支持步骤间引用和并行执行"""
                    import datetime as _dt
                    result = None
                    worklog_entries = []  # FIX-3: 工作日志
                    worklog_entries.append(f"# Task {task_id} 工作日志\n")
                    worklog_entries.append(f"**任务:** {desc}\n")
                    worklog_entries.append(f"**开始时间:** {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                    try:
                        if not task_steps:
                            # 单一任务模式——用工具模型（更强推理）
                            result = await self._call_tool_model(f"执行以下任务并返回结果:\n{desc}", task_id=task_id, context_text=task_meta.get("context_text", ""), window_key=self._extract_window_key(task_event))
                        else:
                            # 多步骤编排模式——支持并行批次
                            all_results = []
                            batches = {}
                            for i, step in enumerate(task_steps):
                                batch_id = step.get("batch", i) if isinstance(step, dict) else i
                                batches.setdefault(batch_id, []).append((i, step))
                            
                            for batch_id in sorted(batches.keys()):
                                batch_items = batches[batch_id]
                                async def _exec_step(idx, step_info):
                                    step_desc = step_info.get("desc", "") if isinstance(step_info, dict) else str(step_info)
                                    tool_name = step_info.get("tool", "") if isinstance(step_info, dict) else ""
                                    tool_args = step_info.get("args", {}) if isinstance(step_info, dict) else {}
                                    
                                    # 如果指定了 tool 字段，尝试直接调用对应工具
                                    if tool_name and hasattr(self, f"tool_{tool_name}"):
                                        try:
                                            tool_func = getattr(self, f"tool_{tool_name}")
                                            result = await tool_func(task_event, **tool_args)
                                            return {"step": idx+1, "desc": step_desc, "tool": tool_name, "result": str(result)}
                                        except Exception as e:
                                            return {"step": idx+1, "desc": step_desc, "tool": tool_name, "error": str(e)}
                                    else:
                                        # 用工具模型文本执行
                                        context = _json.dumps(all_results[-3:], ensure_ascii=False) if all_results else '无'
                                        step_max_steps = step_info.get("max_steps", 20) if isinstance(step_info, dict) else 20
                                        step_result = await self._call_tool_model(
                                            f"任务: {desc}\n当前步骤 {idx+1}/{len(task_steps)}: {step_desc}\n"
                                            f"之前步骤结果: {context}\n请执行当前步骤并返回结果。",
                                            task_id=task_id,
                                            max_steps=step_max_steps,
                                            context_text=task_meta.get("context_text", ""),
                                            window_key=self._extract_window_key(task_event)
                                        )
                                        return {"step": idx+1, "desc": step_desc, "result": step_result}
                                
                                if len(batch_items) > 1:
                                    coros = [_exec_step(idx, step) for idx, step in batch_items]
                                    batch_results = await asyncio.gather(*coros, return_exceptions=True)
                                    for br in batch_results:
                                        if isinstance(br, Exception):
                                            all_results.append({"error": str(br)})
                                        else:
                                            all_results.append(br)
                                else:
                                    idx, step = batch_items[0]
                                    r = await _exec_step(idx, step)
                                    all_results.append(r)
                                task_meta["step_progress"] = f"{len(all_results)}/{len(task_steps)}"

                                # FIX-3: 追加工作日志
                                for br in (batch_results if len(batch_items) > 1 else [r]):
                                    if isinstance(br, dict):
                                        step_n = br.get('step', '?')
                                        step_d = br.get('desc', '')[:60]
                                        tool_n = br.get('tool', '-')
                                        if br.get('error'):
                                            worklog_entries.append(f"- **Step {step_n}** [{tool_n}] {step_d} ❌ {br['error'][:100]}\n")
                                        else:
                                            result_preview = str(br.get('result', ''))[:80]
                                            worklog_entries.append(f"- **Step {step_n}** [{tool_n}] {step_d} ✅ {result_preview}\n")
                                
                                # === wake_at_step: checkpoint 暂停 → 主模型审阅 ===
                                _steps_modified = False
                                for idx, step in batch_items:
                                    if isinstance(step, dict) and step.get("wake_at_step"):
                                        try:
                                            # 计算剩余步骤（当前 batch 之后的所有步骤）
                                            executed_indices = {i for i, _ in batch_items}
                                            _remaining = [
                                                s for j, s in enumerate(task_steps)
                                                if j > max(executed_indices)
                                            ]
                                            # 写中间报告
                                            if self._sandbox:
                                                try:
                                                    progress_summary = _json.dumps(all_results[-3:], ensure_ascii=False)[:400]
                                                    await self._sandbox.modify_file(
                                                        f"workspace/task_reports/{task_id}_checkpoint_{idx+1}.md",
                                                        f"# Task {task_id} Checkpoint (Step {idx+1})\n\n"
                                                        f"**任务:** {desc}\n"
                                                        f"**进度:** {len(all_results)}/{len(task_steps)}\n\n"
                                                        f"**最近结果:**\n```json\n{progress_summary}\n```\n",
                                                        mode="write"
                                                    )
                                                except Exception:
                                                    pass
                                            # 暂停 task 循环，等主模型审阅
                                            decision = await self._checkpoint_review(
                                                task_event, task_id, desc,
                                                current_step=idx + 1,
                                                total_steps=len(task_steps),
                                                recent_results=all_results,
                                                remaining_steps=_remaining,
                                            )
                                            # 处理步骤替换
                                            if decision.get("remaining_steps"):
                                                new_steps = decision["remaining_steps"]
                                                worklog_entries.append(
                                                    f"\n**[Checkpoint {idx+1}] 主模型修改了后续步骤 "
                                                    f"({len(_remaining)}→{len(new_steps)}步)**\n"
                                                )
                                                # 执行新步骤
                                                for ns_idx, ns in enumerate(new_steps):
                                                    ns_r = await _exec_step(len(all_results) + ns_idx, ns)
                                                    all_results.append(ns_r)
                                                    # 追加工作日志
                                                    if isinstance(ns_r, dict):
                                                        ns_desc = ns_r.get('desc', '')[:60]
                                                        ns_tool = ns_r.get('tool', '-')
                                                        if ns_r.get('error'):
                                                            worklog_entries.append(f"- **Step(new) {ns_idx+1}** [{ns_tool}] {ns_desc} ❌ {ns_r['error'][:100]}\n")
                                                        else:
                                                            worklog_entries.append(f"- **Step(new) {ns_idx+1}** [{ns_tool}] {ns_desc} ✅\n")
                                                _steps_modified = True
                                                break
                                        except Exception as ckpt_err:
                                            logger.warning(f"Checkpoint {task_id} 异常: {ckpt_err}")
                                if _steps_modified:
                                    break  # 退出 batch 循环（新步骤已执行完）
                            
                            task_meta["step_progress"] = f"{len(task_steps)}/{len(task_steps)} ✅"
                            task_meta["results"] = all_results
                            result = _json.dumps(all_results, ensure_ascii=False, indent=2)
                    except Exception as e:
                        result = f"任务执行异常: {e}"
                    
                    # === notify_main: Task 完成后主动通知 ===
                    wake = task_meta.get("wake_condition", "notify_main")
                    if wake == "notify_main" and task_event:
                        try:
                            summary = result[:500] if result else "无输出"
                            # 写入 Sandbox 报告文件（指针机制）
                            report_path = f"workspace/task_reports/{task_id}.md"
                            worklog_path = f"workspace/task_reports/{task_id}_worklog.md"
                            if self._sandbox:
                                # FIX-3: 写工作日志
                                try:
                                    import datetime as _dt
                                    worklog_entries.append(f"\n**结束时间:** {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                                    await self._sandbox.modify_file(
                                        worklog_path,
                                        "\n".join(worklog_entries),
                                        mode="write"
                                    )
                                except Exception:
                                    worklog_path = ""
                                # 写结果报告（含工作日志指针）
                                try:
                                    await self._sandbox.modify_file(
                                        report_path,
                                        f"# Task {task_id} 完成报告\n\n"
                                        f"**任务:** {desc}\n\n"
                                        f"**工作日志:** [{task_id}_worklog.md]({worklog_path})\n\n"
                                        f"**结果:**\n\n{result}\n",
                                        mode="write"
                                    )
                                except Exception:
                                    report_path = ""  # 写入失败
                            # Gap2: 调试模式下先发详细技术通知
                            _show_task_notify = False
                            try:
                                _cfg_path = os.path.normpath(
                                    os.path.join(os.path.dirname(__file__), "..", "..", "cmd_config.json"))
                                if os.path.exists(_cfg_path):
                                    with open(_cfg_path, "r", encoding="utf-8-sig") as _f:
                                        _show_task_notify = json.load(_f).get(
                                            "provider_settings", {}).get("show_tool_use_status", False)
                            except Exception:
                                pass
                            if _show_task_notify and task_event:
                                try:
                                    from astrbot.core.message.message_event_result import MessageChain as _MC
                                    notify_text = (
                                        f"📋 后台任务 {task_id} 已完成\n"
                                        f"任务: {desc[:80]}\n"
                                        f"结果摘要: {summary[:200]}"
                                    )
                                    if report_path:
                                        notify_text += f"\n详细报告: {report_path}"
                                    await task_event.send(_MC().message(notify_text))
                                    logger.info(f"Task {task_id} 完成，已主动通知用户（调试模式）")
                                except Exception as send_err:
                                    logger.warning(f"Task {task_id} 主动通知失败: {send_err}")
                            # 主动唤醒主模型——以老板娘人格审阅 task 结果并回复用户
                            if task_event:
                                try:
                                    await self._wake_main_for_task(
                                        task_event, task_id, desc, summary, report_path)
                                except Exception as wake_err:
                                    logger.warning(f"Task {task_id} 唤醒主模型失败: {wake_err}")
                            # Knowledge 记录操作
                            try:
                                self._knowledge.add_operation(
                                    f"Task {task_id} 完成: {desc[:80]}")
                            except Exception:
                                pass
                        except Exception:
                            pass  # 通知失败不影响任务结果
                    elif wake == "write_report" and self._sandbox:
                        try:
                            report_path = f"workspace/task_reports/{task_id}.md"
                            await self._sandbox.modify_file(
                                report_path,
                                f"# {desc}\n\n{result}",
                                mode="write"
                            )
                        except Exception:
                            pass
                    
                    return result
                task = asyncio.create_task(_run_task(task_description, step_list, meta, event, tid))
                self._task_pool[tid] = {"task": task, "meta": meta}
                info_lines = [f"任务已创建: {tid}", f"名称: {task_name}", f"描述: {task_description}"]
                if source_pointer:
                    info_lines.append(f"来源: {source_pointer}")
                if step_list:
                    info_lines.append(f"步骤: {len(step_list)}步")
                info_lines.append(f"唤醒: {wake}")
                info_lines.append(f"使用 task_set(action='check', task_id='{tid}') 查看进度")
                return "\n".join(info_lines)
            elif action == "check":
                if not task_id or task_id not in self._task_pool:
                    return f"任务 {task_id} 不存在。使用 action='list' 查看所有任务"
                info = self._task_pool[task_id]
                task = info["task"]
                meta = info.get("meta", {})
                if not task.done():
                    progress = meta.get("step_progress", "")
                    return f"任务 {task_id} 运行中... {progress}"
                try:
                    result = task.result()
                    src = meta.get("source_pointer", "")
                    wake = meta.get("wake_condition", "notify_main")
                    del self._task_pool[task_id]
                    header = f"任务 {task_id} 已完成"
                    if src:
                        header += f" [来源: {src}]"
                    # 通知已在 _run_task 内部通过 event.send / write_report 完成
                    # check 只需返回结果给主模型
                    if wake == "silent":
                        return f"{header} (静默完成)"
                    return f"{header}:\n{result}"
                except Exception as e:
                    del self._task_pool[task_id]
                    return f"任务 {task_id} 执行失败: {e}"
            elif action == "kill":
                if not task_id or task_id not in self._task_pool:
                    return f"任务 {task_id} 不存在"
                self._task_pool[task_id]["task"].cancel()
                del self._task_pool[task_id]
                return f"任务 {task_id} 已终止"
            else:
                return f"不支持的操作: {action}。支持: create/check/kill/list"
        except Exception as e:
            return f"任务管理错误: {e}"

    # ========================
    # 发送工具（原始设计核心："发送本身也是工具调用"）
    # ========================

    @filter.llm_tool(name="send_image")
    async def tool_send_image(self, event: AstrMessageEvent, image_path: str):
        '''发送 Sandbox 中的图片到当前 QQ 对话。

        Args:
            image_path(string): 图片在 Sandbox 中的相对路径，如 workspace/images/xxx.png。不要加 Sandbox/ 前缀
        '''
        try:
            from astrbot.core.message.components import Image as AstrImage
            from astrbot.core.message.message_event_result import MessageChain

            # 安全检查：路径必须在 Sandbox 内
            # 兼容处理：自动剥离 "Sandbox/" 前缀（避免双重 Sandbox/Sandbox/）
            clean_path = image_path
            if clean_path.startswith("Sandbox/") or clean_path.startswith("Sandbox\\"):
                clean_path = clean_path[len("Sandbox") + 1:]
            if self._sandbox:
                real_path = self._sandbox._security.resolve_path(clean_path)
            else:
                sandbox_base = SANDBOX_ROOT
                real_path = os.path.normpath(os.path.join(sandbox_base, image_path))
                if not real_path.startswith(os.path.normpath(sandbox_base)):
                    return "❌ 安全错误：路径超出 Sandbox 范围"

            if not os.path.exists(real_path):
                return f"❌ 文件不存在: {image_path}"

            # 使用 MessageChain.file_image 发送本地图片
            chain = MessageChain().file_image(real_path)
            await event.send(chain)

            size_kb = os.path.getsize(real_path) // 1024
            ext = os.path.splitext(real_path)[1].lower()
            return f"✅ 图片已发送到对话 ({size_kb}KB, {ext})"
        except Exception as e:
            return f"❌ 发送图片失败: {e}"

    @filter.llm_tool(name="send_file")
    async def tool_send_file(self, event: AstrMessageEvent, file_path: str, filename: str = ""):
        '''发送 Sandbox 中的文件到当前 QQ 对话。

        Args:
            file_path(string): 文件在 Sandbox 中的相对路径，如 workspace/files/report.pdf。不要加 Sandbox/ 前缀
            filename(string): 显示给用户的文件名（可选，默认用原文件名）
        '''
        try:
            from astrbot.core.message.components import File as AstrFile
            from astrbot.core.message.message_event_result import MessageChain

            # 安全检查
            # 兼容处理：剥离 Sandbox/ 前缀
            clean_file = file_path
            if clean_file.startswith("Sandbox/") or clean_file.startswith("Sandbox\\"):
                clean_file = clean_file[len("Sandbox") + 1:]
            if self._sandbox:
                real_path = self._sandbox._security.resolve_path(clean_file)
            else:
                sandbox_base = SANDBOX_ROOT
                real_path = os.path.normpath(os.path.join(sandbox_base, file_path))
                if not real_path.startswith(os.path.normpath(sandbox_base)):
                    return "❌ 安全错误：路径超出 Sandbox 范围"

            if not os.path.exists(real_path):
                return f"❌ 文件不存在: {file_path}"

            display_name = filename or os.path.basename(real_path)
            file_comp = AstrFile(name=display_name, file=real_path)
            chain = MessageChain()
            chain.chain.append(file_comp)
            await event.send(chain)

            size_kb = os.path.getsize(real_path) // 1024
            return f"✅ 文件已发送: {display_name} ({size_kb}KB)"
        except Exception as e:
            return f"❌ 发送文件失败: {e}"

    @filter.llm_tool(name="generate_image")
    async def tool_generate_image(self, event: AstrMessageEvent, prompt: str, aspect_ratio: str = "auto", reference_image: str = "", number_of_images: int = 1):
        '''使用 Gemini 图像模型生成图片。支持纯文本描述生成，也支持传入参考图做 image-to-image 编辑。

        Args:
            prompt(string): 图片描述提示词（英文效果更好），描述你想生成或修改的图片内容
            aspect_ratio(string): 图片宽高比。可选: auto(模型默认), 1:1(方形), 16:9(横屏), 9:16(竖屏), 4:3, 3:4
            reference_image(string): 可选，Sandbox中的参考图片路径（如 workspace/images/ref.png），传入后可基于此图进行编辑/风格转换
            number_of_images(int): 生成图片数量，1-4张，默认1
        '''
        try:
            if not self._api_key:
                return "错误: API key 未配置"

            # 读取图像模型配置
            img_cfg = self._config.get("image_model", {}) if hasattr(self, '_config') and self._config else {}
            imagen_model = img_cfg.get("model", "gemini-2.5-flash-image")

            # 构建 contents parts
            parts = [{"text": prompt}]

            # 图片上下文（reference_image）
            if reference_image and self._sandbox:
                try:
                    import base64
                    real_path = self._sandbox._security.resolve_path(reference_image)
                    if os.path.exists(real_path):
                        with open(real_path, "rb") as f:
                            img_bytes = f.read()
                        # 检测 MIME 类型
                        ext = os.path.splitext(real_path)[1].lower()
                        mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                                    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
                        mime_type = mime_map.get(ext, "image/png")
                        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                        parts.insert(0, {
                            "inlineData": {"mimeType": mime_type, "data": img_b64}
                        })
                        logger.info(f"generate_image: 加载参考图 {reference_image} ({len(img_bytes)//1024}KB)")
                    else:
                        logger.warning(f"generate_image: 参考图不存在 {real_path}")
                except Exception as ref_err:
                    logger.warning(f"generate_image: 加载参考图失败: {ref_err}")

            # 构建 generationConfig（参考 Kaleidoscope 万花筒经验）
            gen_config = {
                "responseModalities": ["IMAGE", "TEXT"],
            }

            # imageConfig: aspectRatio + imageSize
            # 优先用工具调用参数，回退到配置默认值
            effective_aspect = aspect_ratio if aspect_ratio and aspect_ratio != "auto" else img_cfg.get("default_aspect_ratio", "auto")
            image_config = {}
            if effective_aspect and effective_aspect != "auto":
                image_config["aspectRatio"] = effective_aspect
            # imageSize: 从配置读取 (0.5K/1K/2K/4K)
            cfg_image_size = img_cfg.get("image_size", "")
            if cfg_image_size and cfg_image_size != "1K":  # 1K 是 API 默认值，不必显式传
                image_config["imageSize"] = cfg_image_size
            if image_config:
                gen_config["imageConfig"] = image_config

            # thinkingConfig: 仅特定模型支持特定级别（Kaleidoscope 实测）
            # gemini-3.1-flash-image-preview 支持 MINIMAL/HIGH
            # gemini-2.5-flash-image / gemini-3-pro-image-preview 不支持
            cfg_thinking_level = img_cfg.get("thinking_level", "")
            if cfg_thinking_level:
                gen_config["thinkingConfig"] = {"thinkingLevel": cfg_thinking_level}

            # number_of_images: generateContent API 不支持 numberOfImages 参数
            # 需要通过客户端并行请求实现，在下方处理
            n_images = max(1, min(4, number_of_images))

            # 构建 payload
            url = f"{GEMINI_API_BASE}/{imagen_model}:generateContent?key={self._api_key}"
            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": gen_config,
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
                ],
            }

            # 发送请求（多图时并行发送多次独立请求）
            import asyncio
            import base64

            async def _do_one_request():
                async with self._session.post(url, json=payload, headers={"Content-Type": "application/json"}) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        return None, f"({resp.status}): {err[:200]}"
                    return await resp.json(), None

            if n_images <= 1:
                data, err = await _do_one_request()
                results = [(data, err)]
            else:
                results = await asyncio.gather(*[_do_one_request() for _ in range(n_images)])

            # 提取图片
            saved_paths = []
            text_reply = ""
            errors = []
            for data, err in results:
                if err:
                    errors.append(err)
                    continue
                candidates = (data or {}).get("candidates", [])
                if not candidates:
                    continue
                parts_out = candidates[0].get("content", {}).get("parts", [])
                for p in parts_out:
                    if "inlineData" in p:
                        img_data = base64.b64decode(p["inlineData"]["data"])
                        mime = p["inlineData"].get("mimeType", "image/png")
                        ext = ".png" if "png" in mime else ".jpg"
                        filename = f"generated_{int(time.time())}_{len(saved_paths)}{ext}"
                        if self._sandbox:
                            save_path = os.path.join("workspace", "images", filename)
                            real = self._sandbox._security.resolve_path(save_path)
                            os.makedirs(os.path.dirname(real), exist_ok=True)
                            with open(real, "wb") as f:
                                f.write(img_data)
                            saved_paths.append(save_path)
                    elif "text" in p:
                        text_reply += p["text"]

            if saved_paths:
                result = f"✅ 生成了 {len(saved_paths)} 张图片:\n"
                for sp in saved_paths:
                    result += f"  - {sp}\n"
                if errors:
                    result += f"（{len(errors)} 次请求失败）\n"
                if text_reply:
                    result += f"模型备注: {text_reply[:200]}"
                return result
            if errors:
                return f"生图失败: {'; '.join(errors)}"
            return f"生图完成但未获取到图片数据。模型回复: {text_reply[:300]}"
        except Exception as e:
            return f"生图错误: {e}"

    @filter.llm_tool(name="media_summary")
    async def tool_media_summary(self, event: AstrMessageEvent, content: str, media_type: str = "forward", media_count: int = 0, duration: int = 0, extract_raw: bool = False):
        '''统一媒体内容摘要工具——合并转发消息+视频+图文混合的三级分片处理。
        转发消息：自动通过NapCat API拉取，递归嵌套转发最深5层；小图片(≤5MB)和小文件(≤10MB，如PDF)自动下载到Sandbox并用Gemini多模态分析内容（非仅标注占位符）；所有forward类型摘要均存档原文供view_file查看。
        视频：MIME类型自动检测（mp4/webm/mkv/mov等），时长分级（短≤5min/中≤15min/长≤60min/超长拒绝），长视频重点关注前15分钟。

        Args:
            content(string): 合并转发消息用@quoted_forward（自动解析）或纯数字转发ID；视频用URL或Sandbox路径（自动MIME检测）；图文混合用描述文本
            media_type(string): forward(合并转发,默认)/video(视频,支持mp4/webm/mkv/mov等)/mixed(图文混合)
            media_count(int): 消息/媒体条数
            duration(int): 视频时长(秒)，仅video使用。超60分钟(3600秒)会被拒绝
            extract_raw(bool): 原文提取模式——true时跳过AI总结,存原文到workspace/media_logs/,返回文件路径。适合完整阅读、逐条分析、搜索特定内容。默认false(AI总结)
        '''
        try:
            if not self._api_key:
                return "错误: API key 未配置"

            # ========== @quoted 解析：支持引用转发消息 ==========
            content = self._resolve_quoted(content)
            # 若 @quoted_forward 未被解析（仍为字面值），尝试从事件中提取转发 ID
            if content == "@quoted_forward":
                fwd_id = self._extract_forward_id_from_event(event)
                if fwd_id:
                    content = fwd_id
                    logger.info(f"[media_summary] @quoted_forward 未注册，从事件中提取到 forward_id={fwd_id}")
                else:
                    return "当前消息中未找到可解析的合并转发消息。请直接发送（非引用）转发消息后再调用此工具。"
            # 若解析后是纯转发 ID（长数字串），自动拉取转发内容
            if content.strip().isdigit() and len(content.strip()) > 8:
                fwd_content = await self._fetch_forward_content(event, content.strip())
                if fwd_content:
                    content = fwd_content
                else:
                    return "无法获取转发消息内容——可能转发消息已过期或 NapCat 未连接。请手动复制转发内容后重试。"

            # ========== 原文提取模式：存档 + 返回指针 ==========
            if extract_raw:
                archive_path = self._archive_content(content, media_type)
                if archive_path:
                    return f"📄 原文已提取（{len(content)} 字，{media_count} 条消息）\n[文件: {archive_path}]\n使用 view_file 工具查看完整内容。"
                else:
                    return f"📄 原文提取完成（{len(content)} 字），但存档失败。以下是原文前 3000 字：\n\n{content[:3000]}"

            # ========== 多模态内容分析（前5个图片/PDF/视频 喂 Gemini）==========
            if hasattr(self, '_pending_media_files') and self._pending_media_files and not extract_raw:
                files_to_clean = [path for _, path in self._pending_media_files[:5]]
                media_descriptions = await self._analyze_media_files(self._pending_media_files[:5])
                if media_descriptions:
                    content += "\n\n--- 媒体内容分析 ---\n" + "\n".join(media_descriptions)
                    logger.info(f"[media_summary] 多模态分析完成，{len(media_descriptions)} 个文件")
                # 概括模式：分析完毕后清理临时下载的媒体文件
                for rel_path in files_to_clean:
                    try:
                        if self._sandbox:
                            real = self._sandbox._security.resolve_path(rel_path)
                            if os.path.isfile(real):
                                os.remove(real)
                    except Exception:
                        pass
                self._pending_media_files = []

            # ========== 视频处理分支 ==========
            if media_type == "video":
                return await self._summarize_video(content, duration)

            # ========== 转发/混合消息处理分支（三级分片） ==========
            content_len = len(content)
            # 所有 forward 类型都存档原文（即使是小型）
            archive_path = ""
            if media_type == "forward" and self._sandbox:
                archive_path = self._archive_content(content, media_type)

            _wk = self._extract_window_key(event)

            if content_len <= 2000 and media_count <= 3:
                # 小型：一次性喂
                result = await self._call_flash_lite(
                    f"请总结以下{'合并转发' if media_type == 'forward' else ''}消息的核心内容（200-400字），"
                    f"提取关键信息和讨论结论：\n\n{content}",
                    window_key=_wk
                )
                pointer = f"\n\n[文件: {archive_path}]" if archive_path else ""
                return f"📋 摘要:\n{result}{pointer}"
            elif content_len <= 8000 and media_count <= 10:
                # 中型：分 chunk（每3图一组并行处理）
                chunk_size = 2000
                chunks = [content[i:i+chunk_size] for i in range(0, content_len, chunk_size)]
                # 并发处理分片
                tasks = []
                for i, chunk in enumerate(chunks):
                    tasks.append(self._call_flash_lite(
                        f"这是消息的第{i+1}/{len(chunks)}部分，请简要概括核心内容（50-100字）：\n\n{chunk}",
                        window_key=_wk
                    ))
                chunk_summaries = await asyncio.gather(*tasks, return_exceptions=True)
                valid_summaries = [
                    f"[Part {i+1}] {s}" for i, s in enumerate(chunk_summaries)
                    if isinstance(s, str)
                ]
                combined = "\n".join(valid_summaries)
                final = await self._call_flash_lite(
                    f"以下是消息的分段摘要，请合并成一份完整摘要（200-400字）：\n\n{combined}",
                    window_key=_wk
                )
                pointer = f"\n\n[文件: {archive_path}]" if archive_path else ""
                return f"📋 摘要 (分{len(chunks)}片):\n{final}{pointer}"
            else:
                # 大型：采样 + 分片（首2+尾1+均匀2=最多5段）
                samples = [content[:2000]]
                step = max(1, (content_len - 3000) // 2)
                for i in range(2):
                    start = 2000 + i * step
                    samples.append(content[start:start+1000])
                samples.append(content[-1000:])
                sampled_text = "\n---分片分隔---\n".join(samples)
                result = await self._call_flash_lite(
                    f"以下是一段长消息的采样片段（原文{content_len}字，{media_count}个媒体），"
                    f"请根据采样推断并总结全文核心（300-500字）：\n\n{sampled_text}",
                    window_key=_wk
                )
                pointer = f"\n\n[文件: {archive_path}]" if archive_path else ""
                return f"📋 摘要 (大型，{content_len}字采样):\n{result}{pointer}"
        except Exception as e:
            return f"摘要错误: {e}"

    def _archive_content(self, content: str, media_type: str) -> str:
        """将内容存档到 Sandbox 的 media_logs 目录，返回相对路径"""
        try:
            if not self._sandbox:
                return ""
            ts = int(time.time())
            fname = f"{media_type}_{ts}"
            archive_path = f"workspace/media_logs/{fname}.txt"
            real_check = self._sandbox._security.resolve_path(archive_path)
            if os.path.exists(real_check):
                archive_path = f"workspace/media_logs/{fname}_dup.txt"
            os.makedirs(os.path.dirname(self._sandbox._security.resolve_path(archive_path)), exist_ok=True)
            real_path = self._sandbox._security.resolve_path(archive_path)
            with open(real_path, "w", encoding="utf-8") as f:
                f.write(content)
            return archive_path
        except Exception as e:
            logger.warning(f"[_archive_content] 存档失败: {e}")
            return ""

    async def _download_media_to_sandbox(self, url: str, media_type: str = "image", max_size_mb: int = 5) -> str:
        """下载小图片/文件到 Sandbox media_logs 目录，返回相对路径。超限或失败返回空字符串。"""
        if not url or not self._sandbox or not url.startswith(("http://", "https://")):
            return ""
        try:
            # HEAD 检查大小
            try:
                async with self._session.head(url, timeout=aiohttp.ClientTimeout(total=10),
                                              allow_redirects=True) as head_resp:
                    content_length = int(head_resp.headers.get("Content-Length", 0))
                    if content_length > max_size_mb * 1024 * 1024:
                        return ""
            except Exception:
                pass  # HEAD 失败不阻塞，继续尝试下载

            # 下载文件
            content_type = ""
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30),
                                         allow_redirects=True) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.read()
                if len(data) > max_size_mb * 1024 * 1024:
                    return ""
                content_type = resp.headers.get("Content-Type", "")

            # 从 URL 推断扩展名
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            path_part = parsed.path.split("/")[-1] if parsed.path else ""
            ext = os.path.splitext(path_part)[1].lower()
            if not ext:
                # 根据 Content-Type 猜测
                if "png" in content_type:
                    ext = ".png"
                elif "jpeg" in content_type or "jpg" in content_type:
                    ext = ".jpg"
                elif "gif" in content_type:
                    ext = ".gif"
                elif "webp" in content_type:
                    ext = ".webp"
                elif "pdf" in content_type:
                    ext = ".pdf"
                elif "mp4" in content_type or "video" in content_type:
                    ext = ".mp4"
                elif "webm" in content_type:
                    ext = ".webm"
                elif "matroska" in content_type or "mkv" in content_type:
                    ext = ".mkv"
                elif "quicktime" in content_type:
                    ext = ".mov"
                else:
                    ext = ".bin"

            # 保存到 Sandbox
            ts = int(time.time())
            import hashlib
            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            subdir = "images" if media_type == "image" else ("videos" if media_type == "video" else "files")
            fname = f"{media_type}_{ts}_{url_hash}{ext}"
            rel_path = f"workspace/media_logs/{subdir}/{fname}"
            real_path = self._sandbox._security.resolve_path(rel_path)
            os.makedirs(os.path.dirname(real_path), exist_ok=True)
            with open(real_path, "wb") as f:
                f.write(data)
            logger.info(f"[_download_media] 已下载 {media_type} ({len(data)//1024}KB) → {rel_path}")
            return rel_path
        except Exception as e:
            logger.debug(f"[_download_media] 下载失败: {e}")
            return ""

    async def _analyze_media_files(self, media_files: list) -> list:
        """对图片/PDF 文件调用 Gemini 多模态 API 做内容描述，返回描述列表"""
        if not media_files or not self._api_key:
            return []

        _IMAGE_MIME = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }

        async def _analyze_one(media_type: str, rel_path: str) -> str:
            try:
                if not self._sandbox:
                    return ""
                real_path = self._sandbox._security.resolve_path(rel_path)
                if not os.path.exists(real_path):
                    return ""
                file_size = os.path.getsize(real_path)
                size_limit = 20 * 1024 * 1024 if media_type == "video" else 10 * 1024 * 1024
                if file_size > size_limit:
                    return f"[{rel_path}: 文件过大({file_size//1024//1024}MB)，跳过分析]"

                import base64
                with open(real_path, "rb") as f:
                    data_b64 = base64.b64encode(f.read()).decode()

                ext = os.path.splitext(real_path)[1].lower()
                if media_type == "image":
                    mime = _IMAGE_MIME.get(ext, "image/jpeg")
                    prompt = "请简要描述这张图片的内容（50-100字），包括主要内容、文字信息（如有）。"
                elif media_type == "pdf":
                    mime = "application/pdf"
                    prompt = "请简要概括这个 PDF 文件的内容（100-200字），提取关键信息。"
                elif media_type == "video":
                    mime = self._guess_video_mime(real_path)
                    # 视频大小分级：≤10MB 详细，10-20MB 概要，>20MB 跳过
                    if file_size > 20 * 1024 * 1024:
                        return f"[{os.path.basename(rel_path)}: 视频过大({file_size//1024//1024}MB)，跳过]"
                    elif file_size > 10 * 1024 * 1024:
                        prompt = f"这是转发消息中的一段视频（{file_size//1024//1024}MB），请简要概括视频内容（50-100字）。"
                    else:
                        prompt = f"这是转发消息中的一段视频（{file_size//1024}KB），请详细描述视频内容（100-200字），包括画面、对话、文字信息。"
                else:
                    return ""

                url = f"{GEMINI_API_BASE}/gemini-2.5-flash:generateContent?key={self._api_key}"
                payload = {
                    "contents": [{"role": "user", "parts": [
                        {"inlineData": {"mimeType": mime, "data": data_b64}},
                        {"text": prompt}
                    ]}],
                    "generationConfig": {
                        "temperature": 0.2, "maxOutputTokens": 512,
                        "mediaResolution": "MEDIA_RESOLUTION_LOW",
                    },
                }
                # 视频分析超时放宽到 120s（视频处理比图片慢得多）
                api_timeout = 120 if media_type == "video" else 30
                async with self._session.post(url, json=payload,
                                              headers={"Content-Type": "application/json"},
                                              timeout=aiohttp.ClientTimeout(total=api_timeout)) as resp:
                    if resp.status != 200:
                        return f"[{os.path.basename(rel_path)}: 分析失败]"
                    result = await resp.json()
                candidates = result.get("candidates", [])
                if not candidates:
                    return f"[{os.path.basename(rel_path)}: 无分析结果]"
                parts = candidates[0].get("content", {}).get("parts", [])
                text = " ".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
                return f"📎 {os.path.basename(rel_path)}: {text}" if text else ""
            except Exception as e:
                logger.debug(f"[_analyze_media] 分析 {rel_path} 失败: {e}")
                return ""

        tasks = [_analyze_one(mt, path) for mt, path in media_files]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, str) and r]

    # 视频 MIME 类型映射
    _VIDEO_MIME_MAP = {
        ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".flv": "video/x-flv",
        ".wmv": "video/x-ms-wmv", ".m4v": "video/mp4", ".3gp": "video/3gpp",
        ".ts": "video/mp2t", ".m3u8": "video/mp2t",
    }

    @staticmethod
    def _guess_video_mime(path_or_url: str) -> str:
        """根据路径或 URL 推断视频 MIME 类型"""
        import urllib.parse
        # 从 URL 取路径部分，再取扩展名
        parsed = urllib.parse.urlparse(path_or_url)
        ext = os.path.splitext(parsed.path)[1].lower()
        return FlashLiteEngine._VIDEO_MIME_MAP.get(ext, "video/mp4")

    async def _summarize_video(self, video_info: str, duration: int) -> str:
        """视频摘要——MIME 自动检测 + 时长/大小分级处理"""
        # ===== 时长/大小分级预检 =====
        # 超长视频（>60min）直接拒绝
        if duration > 3600:
            return f"⚠️ 视频过长（{duration//60}分钟），超过 60 分钟上限，无法处理。请截取关键片段后重试。"

        # 构建 prompt（根据时长调整指令）
        if duration <= 300:  # ≤5min 短视频
            prompt_text = f"请详细总结这个视频的内容（300-500字），包括主要画面、对话内容、关键信息。视频时长约{duration}秒。"
        elif duration <= 900:  # ≤15min 中视频
            prompt_text = (f"这是一个{duration//60}分钟的视频。请详细总结全部内容（400-600字），"
                          f"包括主要画面、对话内容、关键信息和结论。")
        else:  # ≤60min 长视频
            prompt_text = (f"这是一个较长的视频（{duration//60}分钟）。请重点关注前15分钟的核心内容，"
                          f"概述整体主题和关键结论（400-600字）。视频后半段如能捕捉到也请简要提及。")

        url = f"{GEMINI_API_BASE}/gemini-2.5-flash:generateContent?key={self._api_key}"
        parts = [{"text": prompt_text}]

        if video_info.startswith(("http://", "https://")):
            mime = self._guess_video_mime(video_info)
            parts.insert(0, {"fileData": {"mimeType": mime, "fileUri": video_info}})
        else:
            video_path = video_info
            if self._sandbox and not os.path.isabs(video_info):
                clean_video = video_info
                if clean_video.startswith("Sandbox/") or clean_video.startswith("Sandbox\\"):
                    clean_video = clean_video[len("Sandbox") + 1:]
                video_path = self._sandbox._security.resolve_path(clean_video)
            if os.path.exists(video_path):
                import base64
                file_size = os.path.getsize(video_path)
                # 大小分级限制
                max_mb = 50 if duration <= 900 else 20  # 中短视频放宽到50MB，长视频限20MB
                if file_size > max_mb * 1024 * 1024:
                    return f"⚠️ 视频文件过大 ({file_size // 1024 // 1024}MB)，超过 {max_mb}MB 限制"
                mime = self._guess_video_mime(video_path)
                with open(video_path, "rb") as f:
                    video_b64 = base64.b64encode(f.read()).decode()
                parts.insert(0, {"inlineData": {"mimeType": mime, "data": video_b64}})
            else:
                return f"视频文件不存在: {video_info}"

        # 长视频超时放宽
        api_timeout = 180 if duration > 900 else 120
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.3, "maxOutputTokens": 2048,
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
            },
        }
        async with self._session.post(url, json=payload, headers={"Content-Type": "application/json"},
                                      timeout=aiohttp.ClientTimeout(total=api_timeout)) as resp:
            if resp.status != 200:
                err = await resp.text()
                return f"视频分析失败 ({resp.status}): {err[:300]}"
            data = await resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return "视频分析无结果"
        result_parts = candidates[0].get("content", {}).get("parts", [])
        text = "\n".join(p["text"] for p in result_parts if "text" in p and not p.get("thought"))
        tier_label = "短" if duration <= 300 else ("中" if duration <= 900 else "长")
        return f"🎬 视频摘要({tier_label}片，{duration//60}分{duration%60}秒):\n{text}" if text else "视频分析完成但无文本输出"
    @filter.llm_tool(name="browser_agent")  # L-2: 移除重复装饰器

    async def tool_browser_agent(self, event: AstrMessageEvent, task: str, url: str = "", inject_context: str = ""):

        '''启动工具模型子代理执行复杂任务 子代理自主使用工具完成任务并返回结果



        Args:

            task(string): 任务描述

            url(string): 起始 URL（如果是网页任务）

            inject_context(string): 是否注入当前对话上下文给工具模型("true"注入 其他值或留空不注入)

        '''

        try:

            if not self._sandbox:

                return "错误: Sandbox 不可用"

            

            # 检测 Playwright 可用性

            pw_available = False

            try:

                import playwright

                pw_available = True

            except ImportError:

                logger.info("[browser_agent] Playwright 未安装 尝试自动安装...")

                try:

                    install_result = await self._sandbox.exec_code(

                        code="import subprocess, sys; subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'playwright']); subprocess.check_call([sys.executable, '-m', 'playwright', 'install', 'chromium']); print('OK')",

                        language="python", timeout_ms=120000

                    )

                    if install_result.get("success") and "OK" in install_result.get("stdout", ""):

                        pw_available = True

                        logger.info("[browser_agent] Playwright 自动安装成功")

                    else:

                        logger.warning(f"[browser_agent] Playwright 安装失败: {install_result.get('stderr', '')[:200]}")

                except Exception as e:

                    logger.warning(f"[browser_agent] Playwright 安装异常: {e}")

            

            # 构建委托 prompt——让工具模型子代理自主使用工具完成任务

            env_hint = "Playwright 可用 可以用 agent_sandbox_exec 执行 Playwright 代码来操作浏览器" if pw_available else "Playwright 不可用 请用 requests+BeautifulSoup 方案"

            

            agent_prompt = (

                f"你是一个工具模型子代理 负责自主完成以下任务\n"

                f"任务: {task}\n"

                f"{'起始URL: ' + url if url else ''}\n\n"

                f"环境: {env_hint}\n\n"

                f"你可以使用以下工具来完成任务:\n"

                f"- agent_web_fetch: 抓取网页文本内容\n"

                f"- agent_search: 搜索信息\n"

                f"- agent_sandbox_exec: 在 Sandbox 执行 Python 代码\n"

                f"- agent_view_file / agent_modify_file: 读写文件\n\n"

                f"请自主规划步骤 使用工具完成任务\n"

                f"最终以文本形式输出任务结果 如果生成了文件请说明路径"

            )

            

            # inject_context: 在主模型发出调用的此刻读取当前窗口 T 文件上下文快照
            _ctx_text = ""
            if inject_context and inject_context.lower() == "true" and self._t_file_mgr:
                try:
                    _wk = None
                    if hasattr(event, "message_obj") and event.message_obj:
                        _raw = getattr(event.message_obj, "raw_message", None)
                        if _raw and isinstance(_raw, dict):
                            if _raw.get("message_type", "group") == "group":
                                _wk = f"GroupMessage:{_raw.get('group_id', '')}"
                            else:
                                _wk = f"FriendMessage:{_raw.get('user_id', '')}"
                    if _wk:
                        _tf = await self._t_file_mgr.load(_wk)
                        _ctx_text = self._t_file_mgr.build_flashlite_context(_tf)
                        logger.debug(f"[browser_agent] inject_context: 获取 {_wk} 上下文 {len(_ctx_text)} 字")
                except Exception as _e:
                    logger.warning(f"[browser_agent] inject_context 获取上下文失败: {_e}")

            # 直接委托给工具模型子代理——它会自主调用工具完成任务

            result = await self._call_tool_model(agent_prompt, max_tokens=4096, context_text=_ctx_text, window_key=self._extract_window_key(event))

            

            if result and result.strip():

                return f"\U0001f916 子代理完成:\n{result}"

            else:

                return "子代理执行完毕但无有效输出"

        except Exception as e:

            import traceback

            logger.error(f"[browser_agent] 异常 traceback:\n{traceback.format_exc()}")

            return f"子代理错误: {e}"



    @filter.llm_tool(name="upload_data")
    async def tool_upload_data(self, event: AstrMessageEvent, source: str, filename: str = "", send_to_qq: bool = True):
        '''从 Sandbox 读取文件并发送到 QQ 对话——从 Sandbox 往外发送文件。

        Args:
            source(string): Sandbox 中的文件相对路径，如 workspace/data/output.csv。不要加 Sandbox/ 前缀
            filename(string): 发送时的显示文件名（默认同源文件名）
            send_to_qq(boolean): 是否直接发送到当前QQ对话(默认true)
        '''
        try:
            if not self._sandbox:
                return "错误: Sandbox 不可用"
            # 兼容处理：剥离 Sandbox/ 前缀
            clean_source = source
            if clean_source.startswith("Sandbox/") or clean_source.startswith("Sandbox\\"):
                clean_source = clean_source[len("Sandbox") + 1:]
            real_path = self._sandbox._security.resolve_path(clean_source)
            if not os.path.isfile(real_path):
                return f"文件不存在: {source}"
            file_size = os.path.getsize(real_path)
            if file_size > 30 * 1024 * 1024:
                return f"文件过大 ({file_size // 1024 // 1024}MB)，超过 30MB 发送限制"
            send_name = filename or os.path.basename(real_path)
            _, ext = os.path.splitext(real_path)
            text_exts = {".txt", ".md", ".json", ".py", ".js", ".csv", ".log", ".yaml", ".yml", ".xml", ".html", ".css"}

            # 小文本文件 + 不发送：直接返回内容预览
            if ext.lower() in text_exts and file_size < 4096 and not send_to_qq:
                with open(real_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                return f"📄 文件: {send_name} ({file_size}B)\n```\n{content}\n```"

            # 发送到 QQ 对话
            if send_to_qq:
                try:
                    from astrbot.core.message.components import File as AstrFile, Image as AstrImage
                    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
                    result = event.make_result()
                    if ext.lower() in image_exts:
                        # 图片：用 file_image 链式方法
                        result.file_image(real_path)
                        event.set_result(result)
                        return f"📸 图片已发送: {send_name} ({file_size // 1024}KB)"
                    else:
                        # 文件：构造 File 组件并 append 到 chain
                        file_comp = AstrFile(name=send_name, file=real_path)
                        result.chain.append(file_comp)
                        event.set_result(result)
                        return f"📦 文件已发送: {send_name} ({file_size // 1024}KB)"
                except Exception as send_err:
                    logger.warning(f"QQ文件发送失败，降级为路径返回: {send_err}")
                    return (
                        f"📦 文件就绪(发送失败，需手动处理): {send_name}\n"
                        f"  路径: Sandbox/{source}\n"
                        f"  大小: {file_size // 1024}KB\n"
                        f"  绝对路径: {real_path}\n"
                        f"  发送错误: {send_err}"
                    )
            else:
                return (
                    f"📦 文件就绪: {send_name}\n"
                    f"  路径: Sandbox/{source}\n"
                    f"  大小: {file_size // 1024}KB\n"
                    f"  类型: {ext}\n"
                    f"  绝对路径: {real_path}"
                )
        except PermissionError as e:
            return f"权限错误: {e}"
        except Exception as e:
            return f"文件读取错误: {e}"

    @filter.llm_tool(name="save_data")
    async def tool_save_data(self, event: AstrMessageEvent, data: str = "", path: str = "", url: str = "", local_path: str = "", encoding: str = "utf-8"):
        '''保存数据/下载文件到 Sandbox 工作区。支持三种来源模式：

        1. 文本写入: data + path → 直接写入文本内容
        2. URL下载: url + path → 下载网络文件到Sandbox
        3. 本地复制: local_path + path → 复制QQ消息附件到Sandbox

        Args:
            data(string): 模式1-要保存的文本数据内容
            path(string): Sandbox 内的保存路径 (如 workspace/files/doc.pdf)。不要加 Sandbox/ 前缀
            url(string): 模式2-要下载的文件URL(http/https)
            local_path(string): 模式3-本地文件路径(仅限QQ消息附件缓存目录)
            encoding(string): 编码格式(仅文本模式)
        '''
        try:
            if not self._sandbox:
                return "错误: Sandbox 不可用"
            if not path:
                return "错误: 必须指定保存路径 path"
            # 兼容处理：剥离 Sandbox/ 前缀
            if path.startswith("Sandbox/") or path.startswith("Sandbox\\"):
                path = path[len("Sandbox") + 1:]
            if not path.startswith("workspace"):
                path = os.path.join("workspace", path)

            real_path = self._sandbox._security.resolve_path(path)
            os.makedirs(os.path.dirname(real_path), exist_ok=True)

            # @quoted 快捷语法解析
            url = self._resolve_quoted(url)
            local_path = self._resolve_quoted(local_path)

            # 模式2: URL下载
            if url:
                return await self._save_data_from_url(url, real_path, path)

            # 模式3: 本地文件复制
            if local_path:
                return await self._save_data_from_local(local_path, real_path, path)

            # 模式1: 文本写入(原有逻辑)
            if not data:
                return "错误: data/url/local_path 至少提供一个"
            await self._sandbox.modify_file(path, data, mode="write")
            size = os.path.getsize(real_path)
            return f"✅ 已保存: {path} ({size}B)"
        except PermissionError as e:
            return f"权限错误: {e}"
        except Exception as e:
            return f"保存错误: {e}"

    async def _save_data_from_url(self, url: str, real_path: str, display_path: str) -> str:
        """从 URL 下载文件到 Sandbox"""
        import aiohttp
        MAX_SIZE = 50 * 1024 * 1024  # 50MB 限制
        HEADERS = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        return f"下载失败: HTTP {resp.status}"
                    content_length = resp.headers.get('Content-Length')
                    if content_length and int(content_length) > MAX_SIZE:
                        return f"文件过大: {int(content_length) // 1024 // 1024}MB (上限50MB)"
                    # content-type 校验
                    content_type = resp.headers.get('Content-Type', '')
                    # 检测是否覆盖同名文件
                    pre_exists = os.path.exists(real_path)
                    old_size = os.path.getsize(real_path) if pre_exists else 0
                    total = 0
                    # 问题5修复: 先写临时文件，完成后原子替换，避免下载失败破坏旧文件
                    tmp_path = real_path + ".downloading"
                    try:
                        with open(tmp_path, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(8192):
                                total += len(chunk)
                                if total > MAX_SIZE:
                                    f.close()
                                    os.remove(tmp_path)
                                    return f"下载中断: 超过50MB限制"
                                f.write(chunk)
                        # 下载成功，原子替换
                        os.replace(tmp_path, real_path)
                    except Exception:
                        # 下载失败，清理临时文件，保留旧文件
                        if os.path.exists(tmp_path):
                            try: os.remove(tmp_path)
                            except: pass
                        raise
            size = os.path.getsize(real_path)
            ext = os.path.splitext(real_path)[1].lower()
            warnings = []

            # Content-Type 与扩展名一致性警告
            ct_check = {
                '.pdf': 'application/pdf', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif', '.html': 'text/html',
                '.json': 'application/json', '.zip': 'application/zip',
            }
            if ext in ct_check and content_type and ct_check[ext] not in content_type:
                warnings.append(f"Content-Type({content_type})与扩展名({ext})不匹配")

            # 魔数校验：文件头与扩展名是否匹配
            magic_check = {
                '.pdf': (b'%PDF-', 'PDF'),
                '.png': (b'\x89PNG', 'PNG图片'),
                '.jpg': (b'\xff\xd8\xff', 'JPEG图片'),
                '.jpeg': (b'\xff\xd8\xff', 'JPEG图片'),
                '.zip': (b'PK\x03\x04', 'ZIP压缩包'),
                '.docx': (b'PK\x03\x04', 'DOCX文档'),
                '.xlsx': (b'PK\x03\x04', 'XLSX表格'),
                '.pptx': (b'PK\x03\x04', 'PPTX演示'),
            }
            if ext in magic_check:
                expected_magic, expected_type = magic_check[ext]
                try:
                    with open(real_path, 'rb') as f:
                        header = f.read(max(len(expected_magic), 20))
                    if not header.startswith(expected_magic):
                        # 检查是否是文本内容
                        is_text = all(b < 128 or b > 160 for b in header[:50])
                        if is_text:
                            text_preview = header[:100].decode('utf-8', errors='replace').strip()
                            warnings.append(
                                f"⚠️ 文件内容不是{expected_type}！实际是文本内容: \"{text_preview[:60]}...\"\n"
                                f"   可能是下载链接已失效或服务器返回了错误页面。建议用户重新发送文件。"
                            )
                        else:
                            warnings.append(f"⚠️ 文件头不匹配{expected_type}格式，文件可能已损坏")
                except Exception:
                    pass

            warn_str = ""
            if warnings:
                warn_str = "\n" + "\n".join(f"⚠️ {w}" if not w.startswith("⚠️") else f"\n{w}" for w in warnings)

            # 检测实际文件类型
            file_type = self._detect_file_type(real_path)
            # 检查是否覆盖了同名文件
            overwrite_note = ""
            if pre_exists:
                overwrite_note = f" | ⚠️ 已覆盖同名文件(旧{old_size}B)"
            return f"✅ 已下载: {display_path} ({size}B, 实际类型: {file_type}, 来源: {url[:80]}){overwrite_note}{warn_str}"
        except Exception as e:
            # 清理失败的下载文件
            if os.path.exists(real_path):
                try: os.remove(real_path)
                except: pass
            return f"下载错误: {e}"

    @staticmethod
    def _detect_file_type(file_path: str) -> str:
        """通过魔数+扩展名检测文件实际类型"""
        MAGIC_MAP = [
            (b'%PDF-', 'PDF文档'),
            (b'\x89PNG', 'PNG图片'),
            (b'\xff\xd8\xff', 'JPEG图片'),
            (b'GIF8', 'GIF图片'),
            (b'PK\x03\x04', None),  # ZIP 系列，需要进一步判断
            (b'RIFF', 'WebP/AVI'),
            (b'\x1f\x8b', 'GZip压缩'),
            (b'BM', 'BMP图片'),
        ]
        ext = os.path.splitext(file_path)[1].lower()
        ext_map = {
            '.pdf': 'PDF文档', '.md': 'Markdown', '.html': 'HTML页面', '.htm': 'HTML页面',
            '.txt': '纯文本', '.csv': 'CSV表格', '.json': 'JSON数据',
            '.py': 'Python脚本', '.js': 'JavaScript', '.css': 'CSS样式',
            '.docx': 'Word文档', '.xlsx': 'Excel表格', '.pptx': 'PPT演示',
            '.zip': 'ZIP压缩包', '.rar': 'RAR压缩包', '.7z': '7z压缩包',
            '.mp4': 'MP4视频', '.mp3': 'MP3音频', '.wav': 'WAV音频',
            '.png': 'PNG图片', '.jpg': 'JPEG图片', '.jpeg': 'JPEG图片', '.gif': 'GIF图片',
            '.webp': 'WebP图片', '.svg': 'SVG矢量图',
        }
        try:
            with open(file_path, 'rb') as f:
                header = f.read(16)
            for magic, ftype in MAGIC_MAP:
                if header.startswith(magic):
                    if ftype is None:  # PK 系列
                        if ext in ('.docx',): return 'Word文档(.docx)'
                        elif ext in ('.xlsx',): return 'Excel表格(.xlsx)'
                        elif ext in ('.pptx',): return 'PPT演示(.pptx)'
                        else: return f'ZIP系压缩包({ext or ".zip"})'
                    return ftype
            # 魔数未匹配，用扩展名
            if ext in ext_map:
                return ext_map[ext]
            # 检测是否纯文本
            if all(b < 128 or b > 160 for b in header[:50]):
                return f'文本文件({ext or "无扩展名"})'
            return f'二进制文件({ext or "未知"})'
        except Exception:
            return ext_map.get(ext, f'未知({ext})')

    async def _save_data_from_local(self, local_path: str, real_path: str, display_path: str) -> str:
        """从本地路径复制文件到 Sandbox（仅限安全白名单目录）"""
        import shutil
        local_path = os.path.normpath(local_path)
        # 安全白名单：QQ/NapCat/QQNT 各种消息缓存目录 + AstrBot 自身 data
        ALLOWED_PREFIXES = [
            os.path.normpath(os.path.expandvars(p)) for p in [
                # QQ 官方缓存
                r"%USERPROFILE%\Documents\Tencent Files",
                r"%LOCALAPPDATA%\Tencent",
                r"%APPDATA%\Tencent",
                r"%APPDATA%\Tencent\QQNT",
                # QQ 独立目录
                r"%APPDATA%\QQ",
                # NapCat 各种可能位置
                r"%APPDATA%\NapCat",
                r"%LOCALAPPDATA%\NapCat",
                r"%USERPROFILE%\.config\NapCat",
                # NapCat 上传缓存（TEMP 下）
                r"%LOCALAPPDATA%\Temp\napcat-plugin-uploads",
                r"%TEMP%\napcat-plugin-uploads",
                # 通用临时目录
                r"%TEMP%",
                r"%LOCALAPPDATA%\Temp",
                # AstrBot 自身 data 目录（NapCat 转发文件可能缓存在这里）
                os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "")),
            ] if os.path.expandvars(p) != p or os.path.exists(p)  # 过滤掉未展开的变量
        ]
        # H-5 修复: 使用 Path.resolve() + is_relative_to() 严格路径边界校验
        from pathlib import Path as _Path
        resolved_local = _Path(local_path).resolve()
        allowed = False
        for prefix in ALLOWED_PREFIXES:
            try:
                resolved_local.relative_to(_Path(prefix).resolve())
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            allowed_str = ', '.join(os.path.basename(p) for p in ALLOWED_PREFIXES[:4])
            return f"安全限制: 不允许复制该路径({local_path})。仅限 {allowed_str} 等缓存目录。"
        if not os.path.isfile(local_path):
            return f"文件不存在: {local_path}"
        file_size = os.path.getsize(local_path)
        if file_size > 50 * 1024 * 1024:
            return f"文件过大: {file_size // 1024 // 1024}MB (上限50MB)"
        # 检查是否覆盖同名文件
        overwrite_note = ""
        if os.path.exists(real_path):
            old_size = os.path.getsize(real_path)
            overwrite_note = f" | ⚠️ 已覆盖同名文件(旧{old_size}B)"
        shutil.copy2(local_path, real_path)
        # 检测实际文件类型
        file_type = self._detect_file_type(real_path)
        return f"✅ 已复制: {display_path} ({file_size}B, 实际类型: {file_type}, 来源: {os.path.basename(local_path)}){overwrite_note}"

    @filter.llm_tool(name="tool_help")
    async def tool_help(self, event: AstrMessageEvent, name: str = ""):
        """查看工具详细用法。传入工具名获取参数、示例和注意事项。不传则列出全部工具。

        Args:
            name(string): 工具名（如 search, web_fetch, memory_write）留空列出全部
        """
        import json as _json
        base_tools_dir = os.path.join(self._sandbox._root, "base_tools") if self._sandbox else os.path.join(SANDBOX_ROOT, "base_tools")
        if not os.path.isdir(base_tools_dir):
            return "错误: base_tools 目录不存在"

        # 读取所有工具定义
        tools = {}
        for fn in sorted(os.listdir(base_tools_dir)):
            if not fn.endswith(".tool.json"):
                continue
            try:
                with open(os.path.join(base_tools_dir, fn), encoding="utf-8") as f:
                    d = _json.load(f)
                tools[d.get("name", fn)] = d
            except Exception:
                continue

        if not name:
            # 列出全部工具
            lines = [f"可用工具共 {len(tools)} 个："]
            by_cat = {}
            for tname, tdef in tools.items():
                cat = tdef.get("category", "其他")
                by_cat.setdefault(cat, []).append(tname)
            for cat, names in sorted(by_cat.items()):
                lines.append(f"\n【{cat}】{', '.join(names)}")
            lines.append("\n用 tool_help(name=\"工具名\") 查看详细参数和用法")
            return "\n".join(lines)

        # 查找指定工具
        tdef = tools.get(name)
        if not tdef:
            # 模糊搜索
            matches = [k for k in tools if name.lower() in k.lower()]
            if matches:
                return f"未找到 '{name}'，你要找的是: {', '.join(matches)} ？"
            return f"未找到工具 '{name}'，用 tool_help() 查看全部"

        # 详细输出
        lines = [f"### {tdef['name']}"]
        lines.append(f"说明: {tdef.get('description', '无')}")
        lines.append(f"分类: {tdef.get('category', '未分类')}")
        if tdef.get('timeout_ms'):
            lines.append(f"超时: {tdef['timeout_ms']}ms")

        params = tdef.get("parameters", {}).get("properties", {})
        required = tdef.get("parameters", {}).get("required", [])
        if params:
            lines.append("\n参数:")
            for pname, pdef in params.items():
                req = " [必填]" if pname in required else ""
                lines.append(f"  - {pname} ({pdef.get('type', '?')}){req}: {pdef.get('description', '')}")
        else:
            lines.append("\n参数: 无")

        return "\n".join(lines)

    @filter.llm_tool(name="wait")
    async def tool_wait(self, event: AstrMessageEvent, seconds: int = 10):
        """等待指定时间后返回。用于定时提醒、延迟操作。

        Args:
            seconds(number): 等待秒数（1-300，默认10）
        """
        from datetime import datetime as _dt
        seconds = max(1, min(int(seconds), 300))
        await asyncio.sleep(seconds)
        return f"已等待 {seconds} 秒，当前时间: {_dt.now().strftime('%H:%M:%S')}"

    @filter.llm_tool(name="grep")
    async def tool_grep(self, event: AstrMessageEvent, pattern: str = "",
                        path: str = "workspace/", max_results: int = 20):
        """在 Sandbox 内搜索文件内容。

        Args:
            pattern(string): 搜索文本（不区分大小写）
            path(string): 搜索路径（相对 Sandbox 根，默认 workspace/）
            max_results(number): 最大结果数（默认20）
        """
        if not pattern:
            return "错误: pattern 不能为空"
        if not self._sandbox:
            return "错误: Sandbox 未初始化"

        search_root = os.path.join(self._sandbox._root, path)
        if not os.path.isdir(search_root):
            return f"错误: 路径不存在 {path}"

        results = []
        skip_ext = {'.pyc', '.exe', '.dll', '.png', '.jpg', '.jpeg', '.gif',
                    '.webp', '.mp3', '.mp4', '.zip', '.tar', '.gz', '.db', '.sqlite'}
        pattern_lower = pattern.lower()

        for root, dirs, files in os.walk(search_root):
            for fname in files:
                if os.path.splitext(fname)[1].lower() in skip_ext:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern_lower in line.lower():
                                rel = os.path.relpath(fpath, self._sandbox._root)
                                results.append(f"{rel}:{line_num}: {line.strip()[:100]}")
                                if len(results) >= max_results:
                                    break
                except Exception:
                    pass
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

        if results:
            return f"找到 {len(results)} 处匹配:\n" + "\n".join(results)
        return f"未找到匹配 '{pattern}' 的内容"

    @filter.llm_tool(name="system_report")
    async def tool_system_report(self, event: AstrMessageEvent, content: str, report_type: str = "daily"):
        '''写入系统维护日志到受保护区域 base_tools/system_report/。
        仅在定期 Review 进程中可调用，外部调用一律拒绝。

        Args:
            content(string): 报告内容 (markdown)
            report_type(string): daily/review/alert
        '''
        try:
            if not self._sandbox:
                return "错误: Sandbox 不可用"
            # 守卫1: 仅 Review 进程内可调用
            if not getattr(self, '_review_active', False):
                return "错误: system_report 仅在定期 Review 进程中可调用"
            # 守卫2: 拒绝主模型调用（主模型调用时 event 非 None）
            if event is not None:
                return "错误: system_report 是系统内部维护接口"
            # _review_mode 已由 _run_review 统一管理，此处直接写入
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_dir = f"base_tools/system_report/{report_type}"
            log_file = f"{log_dir}/report_{ts}.md"
            header = f"# 系统报告 [{report_type.upper()}]\n\n"
            header += f"**时间**: {date_str}\n\n---\n\n"
            full_content = header + content
            await self._sandbox.modify_file(log_file, full_content, mode="write")
            # 追加到索引文件
            index_line = f"- [{date_str}] [{report_type}] {content[:60]}... → `{log_file}`\n"
            try:
                await self._sandbox.modify_file(f"base_tools/system_report/index.md", index_line, mode="append")
            except Exception:
                await self._sandbox.modify_file(f"base_tools/system_report/index.md", f"# 系统报告索引\n\n{index_line}", mode="write")
            return f"📝 系统报告已写入: Sandbox/{log_file}"
        except Exception as e:
            return f"报告写入错误: {e}"

    # ========================
    # 自定义工具入口
    # ========================

    @filter.llm_tool(name="run_custom_tool")
    async def tool_run_custom_tool(
        self, event: AstrMessageEvent,
        name: str,
        args: str = "{}",
    ):
        """执行自定义工具（AI 在 workspace/ 中自己创建的工具）。

        Args:
            name(string): 工具名称（对应 workspace/ 下的 .tool.json 中的 name 字段）
            args(string): JSON 格式的参数字典
        """
        if not self._tool_registry:
            return "❌ ToolRegistry 未初始化"

        # 解析 args
        try:
            args_dict = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError:
            return f"❌ args 解析失败: 请传入合法 JSON"

        # 检查工具是否存在
        tool = self._tool_registry.get_tool(name)
        if not tool:
            # 尝试重新扫描（可能是刚创建的）
            self._tool_registry.scan()
            tool = self._tool_registry.get_tool(name)
            if not tool:
                available = list(self._tool_registry.get_custom_tools().keys())
                return (
                    f"❌ 找不到自定义工具: {name}\n"
                    f"可用的自定义工具: {available if available else '(暂无)'}\n"
                    f"提示: 在 workspace/ 下创建 {name}.tool.json 和 {name}.py 来添加新工具"
                )

        if tool["builtin"]:
            return f"⚠️ {name} 是内建工具，请直接调用，无需通过 run_custom_tool"

        # 调度执行
        result = await self._tool_registry.dispatch(name, args_dict)
        if result["success"]:
            return f"✅ 工具 {name} 执行成功:\n{result['result']}"
        else:
            return f"❌ 工具 {name} 执行失败:\n{result['error']}"

    # ========================
    # 表情包管理器（内化自 letai_sendemojis）
    # ========================

    def _init_emoji_manager(self):
        """扫描本地表情包目录，建立 keyword → file_path 映射表"""
        emoji_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))),
            "表情包"
        )
        if not os.path.isdir(emoji_dir):
            logger.warning(f"表情包目录不存在: {emoji_dir}")
            return

        # "通用" tag 展开为语气词集合（与 BossLady Console 的 TONE_PARTICLES 保持一致）
        TONE_PARTICLES = {
            '啊', '哦', '嗯', '呢', '吧', '嘛', '呀', '哇', '噢', '嘿',
            '哈', '呐', '哎', '喂', '唉', '嗨', '吗', '么', '了', '的',
            '呃', '哼', '噗', '嘻', '啦', '咯', '喔', '耶', '诶', '欸',
            '噫', '你', '我', '他',
        }

        supported_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        for fname in os.listdir(emoji_dir):
            fpath = os.path.join(emoji_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in supported_ext:
                continue

            # 文件名去掉扩展名后按空格切分为关键词
            name_no_ext = os.path.splitext(fname)[0]
            keywords = [kw.strip() for kw in name_no_ext.split() if kw.strip()]

            entry = {"path": fpath, "name": fname, "keywords": keywords}
            self._emoji_files.append(entry)

            # 构建匹配索引：遇到"通用"则展开为语气词集合
            for kw in keywords:
                if kw == "通用":
                    for particle in TONE_PARTICLES:
                        self._emoji_map.setdefault(particle, []).append(fpath)
                else:
                    self._emoji_map.setdefault(kw, []).append(fpath)

        logger.info(f"表情包管理器: 加载 {len(self._emoji_files)} 个文件, {len(self._emoji_map)} 个关键词")

    def _match_emoji(self, text: str) -> Optional[str]:
        """对文本进行关键词匹配，返回一个表情包文件路径（或 None）"""
        if not self._emoji_map:
            return None

        # 收集所有匹配的候选路径
        candidates: List[str] = []
        for kw, paths in self._emoji_map.items():
            if kw in text:
                candidates.extend(paths)

        if not candidates:
            return None

        # 去重
        candidates = list(set(candidates))

        # 排除最近用过的
        fresh = [p for p in candidates if p not in self._emoji_recent]
        pool = fresh if fresh else candidates

        selected = random.choice(pool)

        # 更新最近使用列表
        self._emoji_recent.append(selected)
        if len(self._emoji_recent) > self._emoji_recent_max:
            self._emoji_recent.pop(0)

        return selected

    @filter.on_decorating_result(priority=9000)
    async def _emoji_on_decorating_result(self, event: AstrMessageEvent):
        """在 AI 回复装饰后，将匹配的表情包 Image 插入 result.chain，
        由 AstrBot 核心分段循环自然排序发送，避免异步竞态。"""
        if not self._emoji_files:
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 提取 AI 回复文本
        ai_text = ""
        for comp in result.chain:
            if hasattr(comp, 'text') and comp.text:
                ai_text += comp.text
        if not ai_text.strip():
            return

        # 匹配表情包
        emoji_path = self._match_emoji(ai_text)
        if not emoji_path:
            return

        # 概率过滤：从面板配置读取 emoji_probability（默认 0.7）
        emoji_prob = 0.7
        try:
            seg_cfg = self.context.get_config().get("platform_settings", {}).get("segmented_reply", {})
            emoji_prob = float(seg_cfg.get("emoji_probability", 0.7))
        except Exception:
            pass
        if random.random() >= emoji_prob:
            logger.debug(f"[Emoji] 概率过滤: {emoji_prob:.0%}，本次跳过")
            return

        if not os.path.exists(emoji_path):
            logger.warning(f"[Emoji] 文件不存在: {emoji_path}")
            return

        # 从面板配置读取"第几条消息后发送"（默认 1）
        send_after = 1
        try:
            seg_cfg = self.context.get_config().get("platform_settings", {}).get("segmented_reply", {})
            send_after = int(seg_cfg.get("emoji_send_after_segment", 1))
        except Exception:
            pass

        # 标记到 event extras，由 respond/stage.py 在文本分段完成后执行实际插入
        # （on_decorating_result 在分段切分之前执行，此时 chain 尚未拆分，直接 insert 会失效）
        event.set_extra("_emoji_path", emoji_path)
        event.set_extra("_emoji_send_after", send_after)
        logger.info(f"[Emoji] 已标记: {os.path.basename(emoji_path)} (send_after={send_after})")

    def get_emoji_list(self) -> List[Dict[str, Any]]:
        """返回表情包列表（供 Console API 使用）"""
        return [
            {"name": e["name"], "path": e["path"], "keywords": e["keywords"]}
            for e in self._emoji_files
        ]

    # ========================
    # 生命周期
    # ========================

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
        # 关闭 WebFetchEngine
        if hasattr(self, '_web_engine') and self._web_engine:
            await self._web_engine.shutdown()
        # 关闭 CostTracker（同步刷盘残余数据）
        if hasattr(self, '_cost_tracker') and self._cost_tracker:
            try:
                await self._cost_tracker.shutdown()
                logger.info("CostTracker 已安全关闭并同步刷盘")
            except Exception as e:
                logger.warning(f"CostTracker 关闭异常: {e}")
        logger.info(
            f"FlashLiteEngine 已关闭 | "
            f"总调用 {self._stats['total_calls']} 次, "
            f"通知主模型 {self._stats['main_model_notified']} 次"
        )


Main = FlashLiteEngine
