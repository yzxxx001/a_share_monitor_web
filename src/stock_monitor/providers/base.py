from __future__ import annotations

import hashlib
import http.client
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from ..config import HotSectorCacheConfig, HotSectorHttpConfig
from ..models import ProviderStatus

LOGGER = logging.getLogger(__name__)


RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ProxyError,
    requests.exceptions.ChunkedEncodingError,
    http.client.RemoteDisconnected,
)

# 模拟真实浏览器请求头，降低触发反爬机制的概率
_DEFAULT_DIRECT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # 仅声明本地确实能解码的压缩方式：未安装 brotli 时若声明 br，东方财富 push2 会返回
    # Brotli 压缩正文，requests 无法解压导致正文为乱码、.json() 抛 JSONDecodeError，使
    # eastmoney_direct 直连源每次必败并被静默降级到同花顺。详见 hot_sector 数据源排查。
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


class ProviderCallError(RuntimeError):
    def __init__(self, status: ProviderStatus) -> None:
        self.status = status
        super().__init__(status.error or "Provider request failed")


@dataclass(frozen=True)
class ProviderCallResult:
    payload: Any
    status: ProviderStatus


class CacheStore:
    def __init__(self, config: HotSectorCacheConfig) -> None:
        self.enabled = config.enabled
        self.directory = config.directory
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def exists(self, key: str) -> bool:
        return self.enabled and self._path(key).exists()

    def delete(self, key: str) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        if path.exists():
            path.unlink()

    def load(self, key: str) -> tuple[Any, datetime] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fp:
            cached = json.load(fp)
        return cached.get("payload"), datetime.fromisoformat(str(cached["cached_at"]))

    def save(self, key: str, payload: Any) -> datetime | None:
        if not self.enabled:
            return None
        cached_at = datetime.now()
        path = self._path(key)
        with path.open("w", encoding="utf-8") as fp:
            json.dump(
                {"cached_at": cached_at.isoformat(timespec="seconds"), "payload": payload},
                fp,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        return cached_at


class ResilientProvider:
    def __init__(
        self,
        provider_name: str,
        http_config: HotSectorHttpConfig,
        cache_config: HotSectorCacheConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.http_config = http_config
        self.cache = CacheStore(cache_config)
        self.logger = logger or LOGGER

    def http_get_json(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        response = requests.get(url, params=params, headers=headers, timeout=self.http_config.timeout_seconds)
        response.raise_for_status()
        return response.json()

    def http_get_json_direct(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        merged_headers = {**_DEFAULT_DIRECT_HEADERS, **(headers or {})}
        session = requests.Session()
        session.trust_env = False
        response = session.get(url, params=params, headers=merged_headers, timeout=self.http_config.timeout_seconds)
        response.raise_for_status()
        body = response.text
        if not body or not body.strip():
            # 空正文通常是服务端限速的表现；归为可重试的连接问题，触发退避重试，
            # 而非让后续 .json() 抛非可重试的 JSONDecodeError 导致主源被立即降级。
            raise requests.ConnectionError(f"直连返回空响应（疑似限速）：{url}")
        try:
            return response.json()
        except ValueError as exc:
            # 非 JSON 正文（限速页 / 压缩解码失败等）同样按可重试处理；重试耗尽后才降级到下一个源。
            raise requests.ConnectionError(
                f"直连返回非 JSON 正文（疑似限速或编码异常）：{self._short_error(exc)}"
            ) from exc

    def call(
        self,
        *,
        source: str,
        target: str,
        cache_key: str,
        fetcher: Callable[[], Any],
    ) -> ProviderCallResult:
        requested_at = datetime.now()
        attempts = max(self.http_config.retry_count, 1)
        backoffs = list(self.http_config.retry_backoff_seconds)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                payload = fetcher()
                cached_at = self.cache.save(cache_key, payload)
                status = ProviderStatus(
                    provider=self.provider_name,
                    source=source,
                    target=target,
                    ok=True,
                    is_cached=False,
                    is_stale=False,
                    cached_at=cached_at,
                    requested_at=requested_at,
                    retry_count=attempt - 1,
                )
                self.logger.info(
                    "provider=%s target=%s source=%s ok retry_count=%d cache_saved=%s",
                    self.provider_name,
                    target,
                    source,
                    attempt - 1,
                    cached_at is not None,
                )
                return ProviderCallResult(payload, status)
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                self.logger.warning(
                    "provider=%s target=%s source=%s attempt=%d/%d failed=%s",
                    self.provider_name,
                    target,
                    source,
                    attempt,
                    attempts,
                    self._short_error(exc),
                )
                if attempt < attempts:
                    base_delay = backoffs[min(attempt - 1, len(backoffs) - 1)] if backoffs else 0
                    jitter = random.uniform(0, max(base_delay * 0.4, 0.5))
                    time.sleep(base_delay + jitter)
            except Exception as exc:
                last_error = exc
                self.logger.exception(
                    "provider=%s target=%s source=%s non_retryable_failed=%s",
                    self.provider_name,
                    target,
                    source,
                    self._short_error(exc),
                )
                break

        cached = self.cache.load(cache_key)
        error = self._short_error(last_error) if last_error else "unknown provider failure"
        if cached is not None:
            payload, cached_at = cached
            status = ProviderStatus(
                provider=self.provider_name,
                source=source,
                target=target,
                ok=True,
                is_cached=True,
                is_stale=True,
                cached_at=cached_at,
                requested_at=requested_at,
                retry_count=attempts,
                error=error,
                warnings=["实时数据请求失败，已使用最近一次缓存。"],
            )
            self.logger.warning(
                "provider=%s target=%s source=%s using_stale_cache cached_at=%s retry_count=%d error=%s",
                self.provider_name,
                target,
                source,
                cached_at.isoformat(timespec="seconds"),
                attempts,
                error,
            )
            return ProviderCallResult(payload, status)

        status = ProviderStatus(
            provider=self.provider_name,
            source=source,
            target=target,
            ok=False,
            requested_at=requested_at,
            retry_count=attempts,
            error=f"请求失败且无可用缓存：{error}",
            warnings=["请求失败且无可用缓存。"],
        )
        self.logger.error(
            "provider=%s target=%s source=%s failed_no_cache retry_count=%d error=%s",
            self.provider_name,
            target,
            source,
            attempts,
            error,
        )
        raise ProviderCallError(status)

    @staticmethod
    def _short_error(exc: Exception | None, limit: int = 260) -> str:
        if exc is None:
            return "unknown"
        message = f"{type(exc).__name__}: {exc}"
        return message if len(message) <= limit else message[: limit - 3] + "..."
