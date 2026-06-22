"""
CHECKPOINT 压缩模块 v2.0 — 三系统分立架构
==================================================

核心变更：引入 TFileManager，为每个对话窗口独立维护 T 文件（智能压缩上下文），
取代直接操作 messages.db 的旧方案。

系统分立：
  A: req.contexts  — AstrBot 框架管理（不触碰）
  B: messages.db   — FlashLite QQ 消息持久化（不触碰）
  C: T 文件        — 本模块管理，实际发送给 LLM 的请求体上下文

规划文档：QQBotPlan/Plan_2_CP*.md
"""

import asyncio
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiosqlite
from astrbot.api import logger

# ========================
# Token 估算参数
# ========================
CHARS_PER_TOKEN_CN = 1.5    # 中文约 1.5 字/token
CHARS_PER_TOKEN_EN = 4.0    # 英文约 4 字符/token
IMAGE_TOKEN_ESTIMATE = 258  # 每张图片约 258 token（Gemini 低分辨率）

# ========================
# 路径常量
# ========================
DB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "QQ_data")
)
DB_PATH = os.path.join(DB_DIR, "messages.db")
CHECKPOINTS_DIR = os.path.join(DB_DIR, "checkpoints")

# T1 摘要注入时的固定标记
T1_SUMMARY_PREFIX = "[对话历史压缩摘要]"
T1_ACK_CONTENT = "好的，我已了解之前的对话历史。"


# ========================
# Token 估算工具（保留自 v1）
# ========================

def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（中英文混合）"""
    if not text:
        return 0

    cn_chars = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', text))
    en_chars = len(text) - cn_chars

    cn_tokens = cn_chars / CHARS_PER_TOKEN_CN
    en_tokens = en_chars / CHARS_PER_TOKEN_EN

    return max(int(cn_tokens + en_tokens), 1)


def estimate_context_msg_tokens(msg: dict) -> int:
    """估算 OpenAI 格式消息的 token 数

    适配 v2 的 contexts 格式（{role, content, tool_calls, ...}）
    """
    tokens = 4  # 消息开头/结尾固定开销

    content = msg.get("content")
    if content:
        if isinstance(content, str):
            tokens += estimate_tokens(content)
        elif isinstance(content, list):
            # 多模态内容（text + image 混合）
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        tokens += estimate_tokens(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        tokens += IMAGE_TOKEN_ESTIMATE
                    elif "inline_data" in part:
                        tokens += IMAGE_TOKEN_ESTIMATE

    # tool_calls
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            tokens += estimate_tokens(fn.get("name", ""))
            tokens += estimate_tokens(fn.get("arguments", ""))

    return max(tokens, 1)


# ========================
# T 文件数据结构工厂
# ========================

def _create_empty_t_file(window_key: str) -> dict:
    """创建空的 T 文件数据结构"""
    parts = window_key.split(":", 1)
    window_type = "group" if parts[0] == "GroupMessage" else "private"
    window_id = parts[1] if len(parts) > 1 else window_key

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "version": 1,
        "window_key": window_key,
        "window_type": window_type,
        "window_id": window_id,
        "T1": {
            "compressed_summary": "",
            "token_count": 0,
            "compression_ratio": 0.0,
            "original_msg_count": 0,
            "compression_count": 0,
            "last_compress_time": "",
            "compress_history": [],
        },
        "messages": [],
        "metadata": {
            "created_at": now_iso,
            "updated_at": now_iso,
            "total_messages_ever": 0,
            "total_compressions": 0,
            "avg_compression_ratio": 0.0,
        },
    }


# ========================
# 压缩 Prompt 构建
# ========================

def build_compress_prompt(
    messages_text: str,
    original_tokens: int,
    target_min_ratio: float,
    target_max_ratio: float,
    has_previous_summary: bool,
) -> str:
    """构建 v2 CHECKPOINT 压缩 Prompt

    v2 改进：不在 Prompt 中限制字数/token，由 API maxOutputTokens 硬保证上限。
    Prompt 鼓励「尽可能详细」，使模型自然趋向可用空间的上限，最大化信息保留。
    """
    summary_note = ""
    if has_previous_summary:
        summary_note = (
            "\n注意：输入内容开头有一段 [对话历史压缩摘要]，"
            "这是之前轮次的压缩结果。"
            "请将其与后续新消息融合为一份统一的新摘要，"
            "旧摘要中的信息可以适当精简但不要完全丢弃。\n"
        )

    msg_char_count = len(messages_text)

    return f"""你是一个对话压缩引擎。将以下对话内容压缩为结构化摘要。
{summary_note}
## 输出要求
- 尽可能详细地保留所有有价值的信息
- 越详细越好，不要省略重要细节
- 系统会自动控制输出长度上限，你无需担心过长
- 不要刻意缩减内容，宁可多写也不要遗漏

## 压缩原则
1. 按话题/时间段分块，用简洁的标题标注每个话题段
2. 保留所有参与者名字和 QQ 号
3. 保留关键事实：人名、地名、数字、日期、结论、决定
4. 保留情感倾向和关系动态
5. 用「」包围重要原文引用
6. 去除：重复内容、纯表情、日常闲聊（你好/再见）、无信息量的应答
7. 如涉及图片/文件/工具调用，注明 [图片] [文件] [工具:名称→结果摘要]

## 输出格式
直接输出摘要，不要输出其他说明文字。格式参考：

【话题：xxx（时间段）】
参与者A 和 B 讨论了...关键信息:「原文引用」

## 原始内容（{msg_char_count} 字）
{messages_text}"""


def serialize_messages_for_compress(messages: List[dict]) -> str:
    """将 OpenAI 格式消息列表序列化为压缩 prompt 用的文本"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        ts = msg.get("timestamp", "")

        # 多模态分段 list → 拼接 text part, 图片/媒体段丢弃占位; None → ""
        # 不归一化的话 L205 `list + " "` 会抛 TypeError，整个 T-FILE 压缩静默熄火
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif content is None:
            content = ""

        # 处理 tool_calls
        if msg.get("tool_calls"):
            tool_descs = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                tool_descs.append(f"[工具调用: {fn.get('name', '?')}]")
            content = (content or "") + " " + " ".join(tool_descs)

        # 处理 tool 结果
        if role == "tool":
            tool_name = msg.get("tool_call_id", "tool")
            content = f"[工具结果 {tool_name}]: {content}"

        # 时间戳简化
        time_str = ""
        if ts:
            if "T" in ts:
                time_str = ts.split("T")[1][:8]
            else:
                time_str = ts

        # 角色映射
        if role == "assistant":
            sender = "老板娘 [BOT]"
        elif role == "user":
            meta = msg.get("meta", {})
            sender = meta.get("sender_name", "用户")
            qq = meta.get("sender_qq", "")
            if qq:
                sender = f"{sender}({qq})"
        else:
            sender = role

        prefix = f"[{time_str}] " if time_str else ""
        lines.append(f"{prefix}{sender}: {content}")

    return "\n".join(lines)


# ========================
# TFileManager —— 核心 T 文件管理器
# ========================

class TFileManager:
    """Per-window T 文件管理器

    为每个对话窗口维护一份独立的 T 文件（JSON），
    包含 T1（压缩历史摘要）和 messages（未压缩的原文消息）。

    T 文件存放路径：QQ_data/checkpoints/{window_key}.json
    """

    def __init__(self):
        # Per-window 互斥锁
        self._locks: Dict[str, asyncio.Lock] = {}
        # Per-window 压缩互斥标记（防止同一窗口并发压缩）
        self._compressing: set = set()
        # Per-window 内存消息缓冲区（减少高频 I/O）
        self._msg_buffer: Dict[str, List[dict]] = {}
        # 确保 checkpoints 目录存在
        os.makedirs(CHECKPOINTS_DIR, exist_ok=True)

    def buffer_message(self, window_key: str, msg: dict) -> None:
        """纯内存追加消息到缓冲区（无锁无I/O，用于高频路径）
        
        消息会在下次 load() / flush_buffer() 时批量写入磁盘。
        """
        if window_key not in self._msg_buffer:
            self._msg_buffer[window_key] = []
        # 添加时间戳
        if "timestamp" not in msg:
            msg["timestamp"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._msg_buffer[window_key].append(msg)

    async def flush_buffer(self, window_key: str) -> None:
        """将缓冲区中的消息批量写入 T 文件磁盘（带锁）"""
        pending = self._msg_buffer.pop(window_key, [])
        if not pending:
            return
        async with self._get_lock(window_key):
            t_file = await self.load(window_key)
            t_file = self._append_messages_inner(t_file, pending)
            await self.save(window_key, t_file)

    def _get_lock(self, window_key: str) -> asyncio.Lock:
        """获取 per-window 的 asyncio.Lock"""
        if window_key not in self._locks:
            self._locks[window_key] = asyncio.Lock()
        return self._locks[window_key]

    def _file_path(self, window_key: str) -> str:
        """计算 T 文件路径"""
        safe_name = window_key.replace(":", "_")
        return os.path.join(CHECKPOINTS_DIR, f"{safe_name}.json")

    # ========================
    # 读/写
    # ========================

    async def load(self, window_key: str) -> dict:
        """加载 T 文件。不存在则创建空文件。

        如果文件损坏（JSON 解析失败），回退到空 T 并记录 error。
        返回数据已包含内存缓冲区中的未刷盘消息。
        """
        fp = self._file_path(window_key)

        if not os.path.exists(fp):
            t_file = _create_empty_t_file(window_key)
            await self.save(window_key, t_file)
            logger.info(f"[T-FILE] 创建新 T 文件: {fp}")
            return self._merge_buffer(window_key, t_file)

        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 版本兼容检查
            if data.get("version") != 1:
                logger.warning(f"[T-FILE] 版本不匹配({data.get('version')})，重新创建")
                data = _create_empty_t_file(window_key)
                await self.save(window_key, data)

            return self._merge_buffer(window_key, data)

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"[T-FILE] 文件损坏 {fp}: {e}")
            # 问题6修复: 保留损坏文件现场，而非直接覆盖
            try:
                import time as _time
                corrupt_path = f"{fp}.corrupt.{int(_time.time())}"
                os.rename(fp, corrupt_path)
                logger.warning(f"[T-FILE] 损坏文件已保留: {corrupt_path}")
            except OSError as rename_err:
                logger.warning(f"[T-FILE] 无法保留损坏文件: {rename_err}")
            t_file = _create_empty_t_file(window_key)
            await self.save(window_key, t_file)
            return self._merge_buffer(window_key, t_file)

    def _merge_buffer(self, window_key: str, t_file: dict) -> dict:
        """将内存缓冲区中的消息合并到 T 文件数据中（纯内存，不写盘）"""
        pending = self._msg_buffer.get(window_key, [])
        if pending:
            t_file = self._append_messages_inner(t_file, pending)
        return t_file

    async def save(self, window_key: str, t_file: dict) -> None:
        """原子保存 T 文件（先写临时文件再重命名）"""
        fp = self._file_path(window_key)
        dir_path = os.path.dirname(fp)
        os.makedirs(dir_path, exist_ok=True)

        t_file["metadata"]["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        try:
            # 写入临时文件
            fd, tmp_path = tempfile.mkstemp(
                dir=dir_path, prefix=".t_file_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(t_file, f, ensure_ascii=False, indent=2)

                # 原子重命名（Windows 上需要先删除目标）
                if os.path.exists(fp):
                    os.replace(tmp_path, fp)
                else:
                    os.rename(tmp_path, fp)
            except Exception:
                # 清理临时文件
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

        except Exception as e:
            logger.error(f"[T-FILE] 保存失败 {fp}: {e}")
            raise

    # ========================
    # 消息追加
    # ========================

    async def append_messages(
        self, window_key: str, new_messages: List[dict]
    ) -> dict:
        """追加新消息到 T 文件。返回更新后的 T 文件。

        会自动为消息添加 timestamp（如果没有）。
        公开 API，自带锁（向后兼容）。
        """
        if not new_messages:
            return await self.load(window_key)

        async with self._get_lock(window_key):
            t_file = await self.load(window_key)
            t_file = self._append_messages_inner(t_file, new_messages)
            await self.save(window_key, t_file)

        return t_file

    async def _append_messages_unlocked(
        self, window_key: str, t_file: dict, new_messages: List[dict]
    ) -> dict:
        """追加新消息到 T 文件（无锁版本）。

        供事务链内部调用（调用方已持有窗口锁）。
        注意：调用方必须自行调用 save() 持久化。
        返回更新后的 t_file（内存中）。
        """
        if not new_messages:
            return t_file
        return self._append_messages_inner(t_file, new_messages)

    def _append_messages_inner(
        self, t_file: dict, new_messages: List[dict]
    ) -> dict:
        """追加消息的核心逻辑（纯内存操作，无锁无IO）。"""
        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        for msg in new_messages:
            # 构建存储格式
            stored_msg = {
                "role": msg.get("role", "user"),
            }

            # content 可能是 str 或 None（tool_calls 消息）
            if msg.get("content") is not None:
                stored_msg["content"] = msg["content"]

            # 保存 tool_calls
            if msg.get("tool_calls"):
                stored_msg["tool_calls"] = msg["tool_calls"]

            # 保存 tool_call_id（tool role 消息）
            if msg.get("tool_call_id"):
                stored_msg["tool_call_id"] = msg["tool_call_id"]

            # 时间戳
            stored_msg["timestamp"] = msg.get("timestamp", now_iso)

            # 元数据（如果有）
            if msg.get("meta"):
                stored_msg["meta"] = msg["meta"]

            t_file["messages"].append(stored_msg)

        t_file["metadata"]["total_messages_ever"] += len(new_messages)
        return t_file

    # ========================
    # 构建 LLM Contexts
    # ========================

    def build_llm_contexts(self, t_file: dict) -> List[dict]:
        """从 T 文件构建 OpenAI 格式 contexts（发送给主模型）

        返回格式：[T1_user, T1_ack, msg1, msg2, ...]
        """
        contexts = []

        # 1. T1：压缩历史摘要
        t1 = t_file.get("T1", {})
        summary = t1.get("compressed_summary", "")
        if summary:
            contexts.append({
                "role": "user",
                "content": f"{T1_SUMMARY_PREFIX}\n{summary}",
            })
            contexts.append({
                "role": "assistant",
                "content": T1_ACK_CONTENT,
            })

        # 2. 原文消息
        for msg in t_file.get("messages", []):
            ctx_msg = {"role": msg["role"]}

            if msg.get("content") is not None:
                ctx_msg["content"] = msg["content"]

            if msg.get("tool_calls"):
                ctx_msg["tool_calls"] = msg["tool_calls"]

            if msg.get("tool_call_id"):
                ctx_msg["tool_call_id"] = msg["tool_call_id"]

            contexts.append(ctx_msg)

        return contexts

    # ========================
    # 构建 FlashLite 上下文
    # ========================

    def build_flashlite_context(
        self, t_file: dict, max_tokens: int = 8000
    ) -> str:
        """从 T 文件构建 FlashLite 触发判断用的文本上下文

        采用从尾部向前截断到 max_tokens 的策略。
        """
        parts = []

        # T1 摘要
        t1 = t_file.get("T1", {})
        summary = t1.get("compressed_summary", "")
        if summary:
            parts.append(f"[对话历史摘要]\n{summary}\n")

        # 原文消息（从最新向前构建，满 max_tokens 时停止）
        messages = t_file.get("messages", [])
        msg_lines = []
        running_tokens = estimate_tokens("\n".join(parts)) if parts else 0

        # 从尾部向前遍历
        for msg in reversed(messages):
            line = self._format_msg_for_flashlite(msg)
            line_tokens = estimate_tokens(line)

            if running_tokens + line_tokens > max_tokens:
                break

            msg_lines.insert(0, line)
            running_tokens += line_tokens

        if msg_lines:
            parts.append("\n".join(msg_lines))

        return "\n".join(parts)

    @staticmethod
    def _format_msg_for_flashlite(msg: dict) -> str:
        """将单条消息格式化为 FlashLite 可读的文本行"""
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        # 同 serialize_messages_for_compress 的归一化:
        # `or ""` 在 list (truthy) 上失效会导致 list 原样穿透到 f-string 渲染成
        # `[{'type':'text', 'text':...}]` 字面串污染 FlashLite 触发判定 (实测 210/341 条触发)
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif content is None:
            content = ""
        ts = msg.get("timestamp", "")

        # 时间简化
        time_str = ""
        if ts and "T" in str(ts):
            time_str = str(ts).split("T")[1][:8]

        # 角色映射
        if role == "assistant":
            sender = "老板娘 [BOT]"
        elif role == "user":
            meta = msg.get("meta", {})
            name = meta.get("sender_name", "用户")
            qq = meta.get("sender_qq", "")
            sender = f"{name}({qq})" if qq else name
        elif role == "tool":
            tool_id = msg.get("tool_call_id", "")
            return f"[{time_str}] [工具结果 {tool_id}]: {content[:200]}"
        else:
            sender = role

        # tool_calls 简化
        if msg.get("tool_calls"):
            tool_names = [
                tc.get("function", {}).get("name", "?")
                for tc in msg["tool_calls"]
            ]
            content = f"[调用工具: {', '.join(tool_names)}]"

        prefix = f"[{time_str}] " if time_str else ""
        return f"{prefix}{sender}: {content}"

    # ========================
    # 压缩核心逻辑
    # ========================

    async def compress_if_needed(
        self,
        window_key: str,
        t_file: dict,
        flash_lite_caller: Callable,
        token_limit: int = 50000,
        keep_recent: int = 10,
        compress_front_ratio: float = 0.7,
        cooldown_seconds: int = 300,
        target_min: float = 0.20,
        target_max: float = 0.40,
    ) -> Tuple[dict, Optional[dict]]:
        """检查并执行压缩

        三重守卫：
          ① total_tokens > token_limit
          ② len(candidate) > keep_recent
          ③ 距上次压缩 > cooldown_seconds

        Returns:
            (更新后的 t_file, 压缩结果 dict 或 None)
        """
        # 构建候选上下文
        candidate = self.build_llm_contexts(t_file)

        if not candidate:
            return t_file, None

        # ① 估算总 token
        total_tokens = sum(estimate_context_msg_tokens(m) for m in candidate)

        if total_tokens <= token_limit:
            return t_file, None

        # ② 消息数量检查（基于 T 文件原始消息数，而非含 T1 的 candidate 数）
        raw_msg_count = len(t_file.get("messages", []))
        if raw_msg_count <= keep_recent:
            logger.debug(
                f"[CHECKPOINT] {window_key}: token {total_tokens} > {token_limit} "
                f"但原始消息数 {raw_msg_count} ≤ keep_recent {keep_recent}，跳过"
            )
            return t_file, None

        # ③ 冷却期
        last_time_str = t_file.get("T1", {}).get("last_compress_time", "")
        if last_time_str:
            try:
                last_time = datetime.fromisoformat(last_time_str).timestamp()
                if (time.time() - last_time) < cooldown_seconds:
                    logger.debug(
                        f"[CHECKPOINT] {window_key}: 冷却期内 "
                        f"(上次 {last_time_str})，跳过"
                    )
                    return t_file, None
            except ValueError:
                pass  # 时间解析失败，忽略冷却期

        # ④ 压缩互斥检查：同一窗口不允许并发压缩
        if window_key in self._compressing:
            logger.info(
                f"[CHECKPOINT] {window_key}: 另一个压缩正在进行中，跳过"
            )
            return t_file, None

        # 三重守卫 + 互斥检查全部通过，标记并执行压缩
        self._compressing.add(window_key)
        try:  # M-1: finally 保证清除互斥标记
            logger.info(
                f"[CHECKPOINT] 触发压缩: {window_key}, "
                f"总 token {total_tokens} > {token_limit}, "
                f"消息 {len(candidate)} 条（原始 {raw_msg_count} 条）"
            )

            # 检测是否存在旧 T1 摘要（占 candidate 前 2 条）
            has_previous_summary = (
                len(candidate) > 0
                and candidate[0].get("role") == "user"
                and T1_SUMMARY_PREFIX in (candidate[0].get("content") or "")
            )
            t1_count_in_candidate = 2 if has_previous_summary else 0

            # 计算压缩范围（排除 T1 消息对后，对原始消息按比例压缩）
            available_for_compress = raw_msg_count - keep_recent
            if available_for_compress <= 0:
                logger.debug(
                    f"[CHECKPOINT] {window_key}: 排除保留后无可压缩消息，跳过"
                )
                return t_file, None

            original_compress_count = max(1, int(available_for_compress * compress_front_ratio))

            # 语义完整性：确保不切开 user-assistant 对话对
            # 如果分割点正好在 user 消息后（下一条是 assistant 回复），多包含 1 条
            _split_idx = t1_count_in_candidate + original_compress_count
            if _split_idx < len(candidate):
                next_msg = candidate[_split_idx]
                if next_msg.get("role") == "assistant" and original_compress_count < available_for_compress:
                    prev_msg = candidate[_split_idx - 1] if _split_idx > 0 else None
                    if prev_msg and prev_msg.get("role") == "user":
                        original_compress_count += 1

            # 总 compress_count = T1 消息对 + 原始消息压缩数
            compress_count = t1_count_in_candidate + original_compress_count

            to_compress = candidate[:compress_count]
            to_keep = candidate[compress_count:]

            # 序列化待压缩内容
            messages_text = serialize_messages_for_compress(to_compress)
            compress_tokens = estimate_tokens(messages_text)

            # 构建压缩 prompt
            prompt = build_compress_prompt(
                messages_text=messages_text,
                original_tokens=compress_tokens,
                target_min_ratio=target_min,
                target_max_ratio=target_max,
                has_previous_summary=has_previous_summary,
            )

            # 调用 Flash Lite（动态 max_output_tokens 硬保证压缩率上限）
            raw_max = max(100, int(compress_tokens * target_max))
            delta = max(50, int(raw_max * 0.15))  # 15% 余量，模型通常不会写满上限
            dynamic_max_tokens = raw_max + delta
            logger.info(
                f"[CHECKPOINT] 压缩 max_tokens: {dynamic_max_tokens} "
                f"(原文 {compress_tokens} × {target_max} + Δ{delta})"
            )

            t0 = time.monotonic()
            try:
                compressed_text = await flash_lite_caller(prompt, max_output_tokens=dynamic_max_tokens, window_key=window_key)
                latency = (time.monotonic() - t0) * 1000
            except Exception as e:
                logger.error(f"[CHECKPOINT] 压缩调用失败: {e}")
                return t_file, None

            if not compressed_text or not compressed_text.strip():
                logger.warning("[CHECKPOINT] Flash Lite 返回空结果，跳过")
                return t_file, None

            # 压缩率验证
            compressed_tokens = estimate_tokens(compressed_text)
            actual_ratio = compressed_tokens / max(compress_tokens, 1)

            if actual_ratio < target_min:
                logger.warning(
                    f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 低于目标 "
                    f"{target_min:.0%}，摘要可能过于简略 "
                    f"(原文 {compress_tokens} → {compressed_tokens} tokens)"
                )
            elif actual_ratio > target_max:
                logger.warning(
                    f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 高于目标 "
                    f"{target_max:.0%}，摘要可能保留过多细节"
                )
            else:
                logger.info(
                    f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} ✓ "
                    f"(目标 {target_min:.0%}~{target_max:.0%})"
                )

            # 计算实际被压缩的原始消息数（不含 T1 消息对）
            original_msgs_compressed_count = original_compress_count
            # 加上之前已经压缩的
            total_original_compressed = (
                t_file["T1"].get("original_msg_count", 0)
                + original_msgs_compressed_count
            )

            # 计算剩余的 messages（从原始消息数组中跳过被压缩的部分）
            remaining_messages = t_file["messages"][original_compress_count:]
            # 记录压缩前快照的消息总数（用于 Save 时合并中间到达的消息）
            pre_compress_msg_count = len(t_file["messages"])

            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

            # 更新 T 文件
            old_history = t_file["T1"].get("compress_history", [])
            new_history_entry = {
                "time": now_iso,
                "before_tokens": compress_tokens,
                "after_tokens": compressed_tokens,
                "ratio": round(actual_ratio, 4),
                "msgs_compressed": original_msgs_compressed_count,
            }

            # 限制 compress_history 最多保留最近 20 条
            compress_history = (old_history + [new_history_entry])[-20:]

            t_file["T1"] = {
                "compressed_summary": compressed_text,
                "token_count": compressed_tokens,
                "compression_ratio": round(actual_ratio, 4),
                "original_msg_count": total_original_compressed,
                "compression_count": t_file["T1"].get("compression_count", 0) + 1,
                "last_compress_time": now_iso,
                "compress_history": compress_history,
            }
            t_file["metadata"]["total_compressions"] = (
                t_file["metadata"].get("total_compressions", 0) + 1
            )

            # 更新平均压缩率
            all_ratios = [h["ratio"] for h in compress_history if "ratio" in h]
            if all_ratios:
                t_file["metadata"]["avg_compression_ratio"] = round(
                    sum(all_ratios) / len(all_ratios), 4
                )

            # 保存 T 文件（合并式 Save：锁内 load-merge-save）
            # 压缩期间可能有新消息通过 append_messages 写入磁盘，
            # 需要重新 load 最新状态，提取中间到达的消息，追加到保留部分
            async with self._get_lock(window_key):
                current_t_file = await self.load(window_key)
                current_msgs = current_t_file.get("messages", [])

                # 冲突检测：如果磁盘消息数 < 压缩前快照消息数，
                # 说明另一个并发压缩已先保存（消息被裁剪），放弃本次保存
                if len(current_msgs) < pre_compress_msg_count:
                    logger.warning(
                        f"[CHECKPOINT] {window_key}: 检测到并发压缩冲突 "
                        f"(磁盘 {len(current_msgs)} < 快照 {pre_compress_msg_count})，"
                        f"放弃本次保存，使用磁盘最新状态"
                    )
                    return current_t_file, None

                # 提取压缩期间中间到达的消息
                mid_arrival_msgs = current_msgs[pre_compress_msg_count:]

                # 合并：压缩后保留部分 + 中间到达的消息
                t_file["messages"] = remaining_messages + mid_arrival_msgs

                # 同步 metadata：取 max 防止统计回退
                cur_meta = current_t_file.get("metadata", {})
                t_file["metadata"]["total_messages_ever"] = max(
                    t_file["metadata"].get("total_messages_ever", 0),
                    cur_meta.get("total_messages_ever", 0),
                )

                if mid_arrival_msgs:
                    logger.info(
                        f"[CHECKPOINT] {window_key}: 压缩期间有 "
                        f"{len(mid_arrival_msgs)} 条新消息到达，已合并保留"
                    )

                await self.save(window_key, t_file)

        finally:
            # M-1: 无论成功/失败/异常，始终清除互斥标记
            self._compressing.discard(window_key)

        # 保存到 checkpoint_history 表（供面板统计）
        await self._save_to_db(
            window_key=window_key,
            compressed_content=compressed_text,
            original_count=original_msgs_compressed_count,
            compression_ratio=actual_ratio,
            token_estimate=compressed_tokens,
        )

        result = {
            "original_messages": original_msgs_compressed_count,
            "original_tokens": compress_tokens,
            "compressed_tokens": compressed_tokens,
            "compression_ratio": round(actual_ratio, 4),
            "kept_messages": len(remaining_messages),
            "latency_ms": round(latency),
        }

        logger.info(
            f"[CHECKPOINT] 完成: {window_key}, "
            f"{original_msgs_compressed_count} 条 → {actual_ratio:.1%}, "
            f"耗时 {latency:.0f}ms, 保留 {len(remaining_messages)} 条原文"
        )

        return t_file, result

    # ========================
    # 数据库写入（面板统计）
    # ========================

    @staticmethod
    async def _save_to_db(
        window_key: str,
        compressed_content: str,
        original_count: int,
        compression_ratio: float,
        token_estimate: int,
    ):
        """将压缩结果写入 checkpoint_history 表（供面板统计）"""
        if not os.path.exists(DB_PATH):
            return

        parts = window_key.split(":", 1)
        window_type = "group" if parts[0] == "GroupMessage" else "private"
        window_id = parts[1] if len(parts) > 1 else window_key

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """INSERT INTO checkpoint_history
                       (window_type, window_id, compressed_content,
                        original_msg_range_start, original_msg_range_end,
                        compression_ratio, token_estimate, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        window_type,
                        window_id,
                        compressed_content,
                        0,  # 不再用 msg ID 追踪范围
                        original_count,
                        compression_ratio,
                        token_estimate,
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    ),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"[CHECKPOINT] 保存统计到 DB 失败: {e}")


# ========================
# 保留的 CheckpointManager（兼容旧接口，逐步废弃）
# ========================

class CheckpointManager:
    """CHECKPOINT 压缩管理器（v1 兼容层）

    保留 get_stats() 供面板查询，其余功能已迁移到 TFileManager。
    """

    def __init__(
        self,
        token_limit: int = 50000,
        keep_recent: int = 10,
        target_compression_min: float = 0.10,
        target_compression_max: float = 0.35,
    ):
        self.token_limit = token_limit
        self.keep_recent = keep_recent
        self.target_compression_min = target_compression_min
        self.target_compression_max = target_compression_max

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算 token 数（代理到模块级函数）"""
        return estimate_tokens(text)

    @staticmethod
    def estimate_message_tokens(msg: Dict[str, Any]) -> int:
        """估算单条消息 token 数（v1 格式：messages.db 行）"""
        tokens = 0
        content = msg.get("content_text", "")
        if content:
            tokens += estimate_tokens(content)
        sender = msg.get("sender_name", "")
        if sender:
            tokens += estimate_tokens(sender) + 2
        if msg.get("has_image"):
            image_urls = msg.get("image_urls")
            if image_urls:
                try:
                    urls = json.loads(image_urls) if isinstance(image_urls, str) else image_urls
                    tokens += len(urls) * IMAGE_TOKEN_ESTIMATE
                except (json.JSONDecodeError, TypeError):
                    tokens += IMAGE_TOKEN_ESTIMATE
        return max(tokens, 1)

    async def get_stats(self, window_id: Optional[str] = None) -> Dict:
        """获取 CHECKPOINT 统计信息"""
        if not os.path.exists(DB_PATH):
            return {"error": "数据库不存在"}

        stats = {}
        async with aiosqlite.connect(DB_PATH) as db:
            if window_id:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM checkpoint_history WHERE window_id = ?",
                    (window_id,),
                )
                stats["checkpoints"] = (await cursor.fetchone())[0]

                cursor = await db.execute(
                    "SELECT AVG(compression_ratio) FROM checkpoint_history WHERE window_id = ?",
                    (window_id,),
                )
                avg = (await cursor.fetchone())[0]
                stats["avg_compression"] = round(avg, 3) if avg else 0
            else:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM checkpoint_history"
                )
                stats["total_checkpoints"] = (await cursor.fetchone())[0]

        return stats
