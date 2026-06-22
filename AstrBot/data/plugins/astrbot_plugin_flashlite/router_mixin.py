"""RouterMixin —— FlashLiteEngine 消息路由/触发相关方法（S2.5 薄壳法拆分）

由 main.py 的 FlashLiteEngine 多继承（class FlashLiteEngine(RouterMixin, ContextMixin, Star)）。
本文件方法全部通过共享 self 访问 FlashLiteEngine 在 __init__ 建立的状态/方法，
不在此重复声明字段、不写 __init__。

⚠️ 薄壳法约定：带 @filter 装饰器的 handler（route_message）装饰器+壳留 main.py，
方法体搬到此处改名 _route_message_impl（无装饰器，普通方法，靠继承被 self 调用）。
无装饰器的纯逻辑方法（_sync_trigger / _async_trigger / _private_trigger /
_calc_dynamic_interval / _get_effective_interval / _extract_text /
_extract_forward_id_from_event / _fetch_forward_content）直接整体搬入本文件。

⚠️ _task_counter 适配：_sync_trigger 内原 `FlashLiteEngine._task_counter` 硬引用改为
`type(self)._task_counter`（类变量仍定义在 main 的 FlashLiteEngine 上，type(self) 即
FlashLiteEngine，语义等价）。这是本切片唯一一处代码改动，其余纯移动。
"""

import asyncio
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

# Gemini REST API 端点（与 main.py 模块级常量保持一致：带 /models 后缀，
# 供 _sync_trigger 的画像语义 Review 拼接 generateContent URL 使用）
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

if TYPE_CHECKING:
    # 依赖契约（运行期由 FlashLiteEngine 主类经 self 提供，静态检查仅作提示）：
    #   方法：self._cfg / self._cfg_json / self._get_group_overrides
    #         self._resolve_quoted / self._register_quoted_vars / self._extract_window_key
    #         self._extract_text / self._calc_dynamic_interval / self._get_effective_interval
    #         self._async_trigger / self._sync_trigger / self._private_trigger
    #         self._fetch_forward_content / self._download_media_to_sandbox
    #         self._build_judgment_prompt / self._parse_judgment
    #         self._call_flash_lite / self._call_tool_model / self._notify_main_model
    #         self._update_latency_stats / self.tool_system_report
    #   字段：self._stats / self._t_file_mgr / self._knowledge / self._knowledge_cache
    #         self._memory / self._sandbox / self._session / self._model / self._api_key
    #         self._wake_keywords / self._msg_counters / self._last_sync_times
    #         self._recent_msg_timestamps / self._pending_task_wakes / self._task_pool
    #         self._pending_media_files / self._current_window_key
    #         self._sampling_mode / self._sync_interval / self._sync_time_interval
    #         self._sync_time_min_msgs / self._dyn_window_minutes / self._dyn_thresholds
    #         self._dyn_intervals / self._review_active / self._last_review_time
    #         self._review_interval_hours
    pass


class RouterMixin:
    """FlashLiteEngine 消息路由/触发 mixin（纯移动，逻辑零改动）。"""

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

    async def _route_message_impl(self, event: AstrMessageEvent):
        """消息路由器：决定是否/如何触发 Flash Lite

        priority=998: 在持久化(9999)之后，在 context_enhancer 等常规插件之前
        不调用 stop_event()，让消息继续传播
        """
        # S3 F3.1: 每条消息到达即取全局单调纳秒戳，作为 receive_seq 去重键地基。
        # 必须在最前面、每条消息都设，time.time_ns() 保证全局单调唯一。
        event._receive_ts_ns = time.time_ns()

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
                    _mid = _get(raw, "message_id")
                    _user_msg = {
                        "role": "user",
                        "content": f"[{sender_name}] {content}" if sender_name else content,
                        # S3 F3.1: 顶层结构化字段（checkpoint v2 落盘读取）
                        "message_id": _mid if _mid is not None else None,
                        "sender": {
                            "qq": sender_qq,
                            "name": sender_name,
                            "is_bot": False,
                        },
                        "receive_seq": getattr(event, "_receive_ts_ns", None),
                        "has_multimodal": self._detect_multimodal(raw),
                        # S1 兼容：保留原 meta 结构不破坏旧消费方
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
                    _mid = _get(raw, "message_id")
                    _user_msg = {
                        "role": "user",
                        "content": f"[{sender_name}] {content}" if sender_name else content,
                        # S3 F3.1: 顶层结构化字段（checkpoint v2 落盘读取）
                        "message_id": _mid if _mid is not None else None,
                        "sender": {
                            "qq": user_id,
                            "name": sender_name,
                            "is_bot": False,
                        },
                        "receive_seq": getattr(event, "_receive_ts_ns", None),
                        "has_multimodal": self._detect_multimodal(raw),
                        # S1 兼容：保留原 meta 结构不破坏旧消费方
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
                type(self)._task_counter += 1
                review_tid = f"task-{type(self)._task_counter:04d}"
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

    # S3 F3.1: 多模态媒体类型集合（图片/语音/视频/文件）。
    _MULTIMODAL_SEG_TYPES = frozenset({"image", "record", "video", "file"})

    @classmethod
    def _detect_multimodal(cls, raw: Any) -> bool:
        """检测 OneBot 消息段中是否含图片/媒体（has_multimodal 来源）"""
        message = raw.get("message", []) if isinstance(raw, dict) else getattr(raw, "message", [])
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") in cls._MULTIMODAL_SEG_TYPES:
                    return True
        return False
