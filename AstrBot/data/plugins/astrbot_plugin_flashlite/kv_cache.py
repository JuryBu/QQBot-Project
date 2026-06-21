"""
KV Cache 管理模块
管理 Gemini API 的 createCachedContent 缓存

核心思路：
- 固定区（knowledge + 系统说明 + 角色设定 + 工具 resource）→ 创建 cachedContent
- 增量区（CHECKPOINT 历史 + 最近消息 + 工具调用）→ 每次请求时发送
- Knowledge 更新 → 重建缓存
- TTL 到期 → 自动重建

文档: Plan_1_gaps.md GAP 3 | Plan_1_data.md KV Cache 部分
"""

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from astrbot.api import logger

# Gemini API
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TTL = "3600s"  # 默认 1 小时


class KVCacheManager:
    """KV Cache 管理器

    管理 Gemini API 的 cachedContent 生命周期：
    1. 创建缓存（固定区内容）
    2. 返回 cachedContent name 给调用方
    3. Knowledge 更新时重建
    4. TTL 到期时自动重建
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        ttl: str = DEFAULT_TTL,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self._api_key = api_key
        self._model = model
        self._ttl = ttl
        self._session = session

        # 缓存状态
        self._cached_content_name: Optional[str] = None
        self._cached_content_hash: Optional[str] = None  # 内容指纹
        self._cached_at: float = 0
        self._ttl_seconds = self._parse_ttl(ttl)

        # 统计
        self._stats = {
            "creates": 0,
            "rebuilds": 0,
            "hits": 0,
            "expires": 0,
            "errors": 0,
        }

    @staticmethod
    def _parse_ttl(ttl_str: str) -> int:
        """解析 TTL 字符串为秒数"""
        ttl_str = ttl_str.strip().lower()
        if ttl_str.endswith("s"):
            return int(ttl_str[:-1])
        elif ttl_str.endswith("m"):
            return int(ttl_str[:-1]) * 60
        elif ttl_str.endswith("h"):
            return int(ttl_str[:-1]) * 3600
        return int(ttl_str)

    # ========================
    # 缓存创建
    # ========================

    async def ensure_cache(
        self,
        fixed_contents: List[Dict[str, Any]],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[str], bool, int]:
        """确保缓存存在且有效

        Args:
            fixed_contents: 固定区内容列表（Gemini Content 格式）
            system_instruction: 系统指令（可选，也会被缓存）
            tools: 工具声明列表（可选，也会被缓存）
                   注意：使用缓存时 generateContent 不能再传 tools

        Returns:
            (cachedContent_name, is_new, cached_token_count)
            - is_new: 是否本次新建/重建了缓存（True时应记录存储费）
            - cached_token_count: 缓存的token总数（仅is_new=True时有效）
        """
        # 计算内容指纹（含 tools）
        content_hash = self._compute_hash(fixed_contents, system_instruction, tools)

        # 检查现有缓存是否有效
        if self._is_cache_valid(content_hash):
            self._stats["hits"] += 1
            return self._cached_content_name, False, 0

        # 需要创建/重建缓存
        if self._cached_content_name:
            # 旧缓存存在但失效了（内容变化或 TTL 到期）
            await self._delete_cache()
            self._stats["rebuilds"] += 1
        else:
            self._stats["creates"] += 1

        name, token_count = await self._create_cache(fixed_contents, system_instruction, content_hash, tools)
        return name, (name is not None), token_count

    async def _create_cache(
        self,
        fixed_contents: List[Dict[str, Any]],
        system_instruction: Optional[str],
        content_hash: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[str], int]:
        """创建新的 cachedContent
        
        Returns:
            (cachedContent_name, cached_token_count)
        """
        url = f"{GEMINI_API_BASE}/cachedContents?key={self._api_key}"

        payload = {
            "model": f"models/{self._model}",
            "displayName": f"bosslady-fixed-{int(time.time())}",
            "contents": fixed_contents,
            "ttl": self._ttl,
        }

        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        # 工具声明也缓存（使用缓存时 generateContent 不再传 tools）
        if tools:
            payload["tools"] = tools

        try:
            session = self._session or aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            close_session = self._session is None

            try:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    data = await resp.json()

                    if resp.status == 200:
                        self._cached_content_name = data.get("name")
                        self._cached_content_hash = content_hash
                        self._cached_at = time.monotonic()
                        # 从响应中提取缓存 token 总数
                        usage = data.get("usageMetadata", {})
                        cached_token_count = usage.get("totalTokenCount", 0)

                        logger.info(
                            f"KV Cache 创建成功: {self._cached_content_name}, "
                            f"TTL={self._ttl}, tokens={cached_token_count}"
                        )
                        return self._cached_content_name, cached_token_count
                    else:
                        logger.error(
                            f"KV Cache 创建失败 {resp.status}: "
                            f"{json.dumps(data, ensure_ascii=False)[:200]}"
                        )
                        self._stats["errors"] += 1
                        return None, 0
            finally:
                if close_session:
                    await session.close()

        except Exception as e:
            logger.error(f"KV Cache 创建异常: {e}")
            self._stats["errors"] += 1
            return None, 0

    # ========================
    # 缓存删除
    # ========================

    async def _delete_cache(self):
        """删除旧缓存"""
        if not self._cached_content_name:
            return

        url = f"{GEMINI_API_BASE}/{self._cached_content_name}?key={self._api_key}"

        try:
            session = self._session or aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
            close_session = self._session is None

            try:
                async with session.delete(url) as resp:
                    if resp.status in (200, 204, 404):
                        logger.debug(f"旧缓存已删除: {self._cached_content_name}")
                    else:
                        logger.warning(f"删除缓存失败 {resp.status}")
            finally:
                if close_session:
                    await session.close()

        except Exception as e:
            logger.debug(f"删除缓存异常（可忽略）: {e}")

        self._cached_content_name = None
        self._cached_content_hash = None

    # ========================
    # 缓存有效性
    # ========================

    def _is_cache_valid(self, content_hash: str) -> bool:
        """检查缓存是否仍然有效"""
        if not self._cached_content_name:
            return False

        # 内容变化
        if self._cached_content_hash != content_hash:
            logger.debug("KV Cache 失效: 内容已变化")
            self._stats["expires"] += 1
            return False

        # TTL 检查（本地估算，提前 60s 重建避免竞态）
        elapsed = time.monotonic() - self._cached_at
        if elapsed > (self._ttl_seconds - 60):
            logger.debug(f"KV Cache 即将过期: elapsed={elapsed:.0f}s")
            self._stats["expires"] += 1
            return False

        return True

    # ========================
    # 内容指纹
    # ========================

    @staticmethod
    def _compute_hash(
        fixed_contents: List[Dict[str, Any]],
        system_instruction: Optional[str],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """计算内容的指纹（含 tools，用于检测变化）"""
        import hashlib
        content_str = json.dumps(fixed_contents, sort_keys=True, ensure_ascii=False)
        if system_instruction:
            content_str += system_instruction
        if tools:
            content_str += json.dumps(tools, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(content_str.encode()).hexdigest()

    # ========================
    # 固定区构建辅助
    # ========================

    @staticmethod
    def build_fixed_contents(
        knowledge: str,
        env_info: str,
        tool_resource: str,
    ) -> List[Dict[str, Any]]:
        """构建固定区 contents 格式

        固定区包含：
        1. knowledge 缓存体（全局）
        2. 系统环境说明
        3. 工具系统 resource 说明
        """
        parts_text = f"""## Knowledge 缓存
{knowledge}

## 系统环境说明
{env_info}

## 工具系统资源
{tool_resource}"""

        return [
            {
                "role": "user",
                "parts": [{"text": parts_text}],
            }
        ]

    # ========================
    # Knowledge 更新通知
    # ========================

    def invalidate(self):
        """标记缓存失效（Knowledge 更新后调用）"""
        self._cached_content_hash = None
        logger.debug("KV Cache 已标记失效（等待下次请求重建）")

    # ========================
    # 统计
    # ========================

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        return {
            **self._stats,
            "cache_active": self._cached_content_name is not None,
            "cache_name": self._cached_content_name,
            "cache_age_s": int(time.monotonic() - self._cached_at) if self._cached_at else 0,
            "ttl_s": self._ttl_seconds,
        }

    # ========================
    # 清理
    # ========================

    async def cleanup(self):
        """清理缓存（关闭时调用）"""
        if self._cached_content_name:
            await self._delete_cache()
            logger.info("KV Cache 已清理")
