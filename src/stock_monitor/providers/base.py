from __future__ import annotations

import hashlib
import http.client
import json
import logging
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
                    time.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)] if backoffs else 0)
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
