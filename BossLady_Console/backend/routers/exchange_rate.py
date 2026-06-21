"""
ExchangeRateService — 实时汇率获取服务

双数据源：open.er-api.com（主）→ frankfurter.dev（备）
带缓存（1 小时 TTL），启动时立即拉取一次。
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("bosslady.exchange_rate")

# 免费 API，无需 Key
_PRIMARY_URL = "https://open.er-api.com/v6/latest/USD"
_FALLBACK_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY"

# 硬编码兜底值（仅在两个 API 都不可用时使用）
_FALLBACK_RATE = 7.2


class ExchangeRateService:
    """USD → CNY 实时汇率服务

    用法:
        svc = ExchangeRateService()
        rate = await svc.get_rate()  # → 7.25 (实时) 或 7.2 (兜底)
    """

    def __init__(self, cache_ttl: float = 3600.0):
        self._cache_ttl = cache_ttl
        self._cached_rate: Optional[float] = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        """同步获取（可能是缓存值或兜底值）"""
        return self._cached_rate or _FALLBACK_RATE

    @property
    def is_live(self) -> bool:
        """当前汇率是否来自实时 API（而非兜底值）"""
        return self._cached_rate is not None

    async def get_rate(self) -> float:
        """异步获取汇率，带缓存"""
        now = time.monotonic()
        if self._cached_rate and (now - self._cached_at) < self._cache_ttl:
            return self._cached_rate

        async with self._lock:
            # double check
            if self._cached_rate and (time.monotonic() - self._cached_at) < self._cache_ttl:
                return self._cached_rate

            rate = await self._fetch_rate()
            if rate:
                self._cached_rate = rate
                self._cached_at = time.monotonic()
                return rate

        return self._cached_rate or _FALLBACK_RATE

    async def _fetch_rate(self) -> Optional[float]:
        """从双数据源拉取汇率"""
        import aiohttp

        # 主数据源
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(_PRIMARY_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = data.get("rates", {}).get("CNY")
                        if rate and isinstance(rate, (int, float)) and 5.0 < rate < 10.0:
                            logger.info(f"汇率更新 (open.er-api): 1 USD = {rate} CNY")
                            return float(rate)
        except Exception as e:
            logger.warning(f"主汇率 API 请求失败: {e}")

        # 备用数据源
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(_FALLBACK_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = data.get("rates", {}).get("CNY")
                        if rate and isinstance(rate, (int, float)) and 5.0 < rate < 10.0:
                            logger.info(f"汇率更新 (frankfurter): 1 USD = {rate} CNY")
                            return float(rate)
        except Exception as e:
            logger.warning(f"备用汇率 API 请求失败: {e}")

        logger.error("所有汇率 API 均失败，使用兜底值")
        return None


# 全局单例
_service: Optional[ExchangeRateService] = None


def get_exchange_rate_service() -> ExchangeRateService:
    """获取全局汇率服务实例"""
    global _service
    if _service is None:
        _service = ExchangeRateService()
    return _service
