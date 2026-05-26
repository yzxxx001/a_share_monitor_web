from __future__ import annotations

from pathlib import Path

import pytest
import requests

from stock_monitor.config import HotSectorCacheConfig, HotSectorHttpConfig
from stock_monitor.providers.base import ProviderCallError, ResilientProvider
from stock_monitor.providers.hot_sector import AKShareHotSectorProvider, RiskEventProvider


def http_config(retry_count: int = 3) -> HotSectorHttpConfig:
    return HotSectorHttpConfig(timeout_seconds=0.1, retry_count=retry_count, retry_backoff_seconds=(0, 0, 0))


def cache_config(tmp_path: Path, enabled: bool = True) -> HotSectorCacheConfig:
    return HotSectorCacheConfig(enabled=enabled, directory=tmp_path / "cache")


def provider(tmp_path: Path, retry_count: int = 3) -> ResilientProvider:
    return ResilientProvider("fake_provider", http_config(retry_count), cache_config(tmp_path))


def test_provider_returns_payload_and_status(tmp_path):
    result = provider(tmp_path).call(source="mock", target="rank", cache_key="rank", fetcher=lambda: [{"name": "半导体"}])

    assert result.payload == [{"name": "半导体"}]
    assert result.status.ok is True
    assert result.status.is_cached is False
    assert result.status.retry_count == 0


def test_provider_retries_then_succeeds(tmp_path):
    calls = {"count": 0}

    def fetcher():
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.exceptions.Timeout("slow")
        return [{"ok": True}]

    result = provider(tmp_path, retry_count=3).call(source="mock", target="rank", cache_key="retry-rank", fetcher=fetcher)

    assert calls["count"] == 3
    assert result.payload == [{"ok": True}]
    assert result.status.retry_count == 2


def test_provider_uses_stale_cache_after_retries_fail(tmp_path):
    resilient = provider(tmp_path, retry_count=2)
    resilient.call(source="mock", target="rank", cache_key="cached-rank", fetcher=lambda: [{"cached": "fresh"}])

    def fail():
        raise requests.exceptions.ProxyError("proxy broken")

    result = resilient.call(source="mock", target="rank", cache_key="cached-rank", fetcher=fail)

    assert result.payload == [{"cached": "fresh"}]
    assert result.status.ok is True
    assert result.status.is_cached is True
    assert result.status.is_stale is True
    assert "缓存" in result.status.warnings[0]


def test_provider_raises_diagnostic_error_when_no_cache(tmp_path):
    def fail():
        raise requests.exceptions.ConnectionError("remote disconnected")

    with pytest.raises(ProviderCallError) as exc_info:
        provider(tmp_path, retry_count=2).call(source="mock", target="rank", cache_key="missing", fetcher=fail)

    status = exc_info.value.status
    assert status.ok is False
    assert status.provider == "fake_provider"
    assert status.target == "rank"
    assert status.retry_count == 2
    assert "无可用缓存" in str(status.error)


def test_batch_processing_can_continue_after_single_object_failure(tmp_path):
    resilient = provider(tmp_path, retry_count=1)
    targets = ["ok-1", "bad", "ok-2"]
    results = []
    failures = []

    for target in targets:
        try:
            result = resilient.call(
                source="mock",
                target=target,
                cache_key=target,
                fetcher=(lambda value=target: (_ for _ in ()).throw(requests.exceptions.Timeout("slow")) if value == "bad" else {"target": value}),
            )
            results.append(result.payload)
        except ProviderCallError as exc:
            failures.append(exc.status.target)

    assert results == [{"target": "ok-1"}, {"target": "ok-2"}]
    assert failures == ["bad"]


def test_risk_provider_is_explicitly_not_implemented():
    provider = RiskEventProvider()

    result = provider.get_announcements("000001.SZ", "2026-05-01", "2026-05-27")

    assert result["status"] == "not_implemented"
    assert result["items"] == []
    assert "不可视为无风险" in result["warning"]


def test_sector_provider_normalizes_rank_rows(tmp_path):
    class FakeSectorProvider(AKShareHotSectorProvider):
        def _fetch_industry_sector_rank(self):
            return [{"板块代码": "BK1036", "板块名称": "半导体", "涨跌幅": 2.5, "成交额": 12345}]

    sector_provider = FakeSectorProvider(http_config(), cache_config(tmp_path))
    rows = sector_provider.get_industry_sector_rank("2026-05-27")

    assert rows[0].sector_code == "BK1036"
    assert rows[0].sector_name == "半导体"
    assert rows[0].change_pct == 2.5
    assert rows[0].provider_status is not None
    assert rows[0].provider_status.provider == "akshare_hot_sector"
