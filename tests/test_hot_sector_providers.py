from __future__ import annotations

import os
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
        def _fetch_industry_sector_rank_em_direct(self):
            return [{"板块代码": "BK1036", "板块名称": "半导体", "涨跌幅": 2.5, "成交额": 12345}]

    sector_provider = FakeSectorProvider(http_config(), cache_config(tmp_path))
    rows = sector_provider.get_industry_sector_rank("2026-05-27")

    assert rows[0].sector_code == "BK1036"
    assert rows[0].sector_name == "半导体"
    assert rows[0].change_pct == 2.5
    assert rows[0].provider_status is not None
    assert rows[0].provider_status.provider == "akshare_hot_sector"


def test_sector_provider_maps_ths_summary_fields(tmp_path):
    class FakeSectorProvider(AKShareHotSectorProvider):
        def _fetch_industry_sector_rank_em_direct(self):
            return [{"板块": "贵金属", "涨跌幅": 4.11, "总成交额": 341.83, "净流入": 34.07}]

    sector_provider = FakeSectorProvider(http_config(), cache_config(tmp_path))
    rows = sector_provider.get_industry_sector_rank("20260527")

    assert rows[0].sector_name == "贵金属"
    assert rows[0].sector_code == "贵金属"
    assert rows[0].amount == 341.83
    assert rows[0].net_inflow == 34.07


def test_hot_sector_provider_appends_market_no_proxy_hosts(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    AKShareHotSectorProvider(http_config(), cache_config(tmp_path))

    assert "17.push2.eastmoney.com" in os.environ["NO_PROXY"]


def test_industry_rank_falls_back_to_ths_when_eastmoney_fails(tmp_path):
    class FallbackProvider(AKShareHotSectorProvider):
        def _fetch_industry_sector_rank_em_direct(self):
            raise requests.exceptions.ConnectionError("eastmoney direct disconnected")

        def _fetch_industry_sector_rank_em(self):
            raise requests.exceptions.ConnectionError("eastmoney disconnected")

        def _fetch_industry_sector_rank_ths_summary(self):
            return [{"行业": "半导体", "涨幅": 2.1, "换手": 3.2}]

    provider = FallbackProvider(http_config(retry_count=1), cache_config(tmp_path))
    rows = provider.get_industry_sector_rank("20260527")

    assert rows[0].sector_name == "半导体"
    assert rows[0].change_pct == 2.1
    assert rows[0].provider_status is not None
    assert rows[0].provider_status.source == "akshare_ths_summary"
    assert any("备用" in warning for warning in rows[0].warnings)


def test_industry_rank_rejects_name_only_rows(tmp_path):
    class NameOnlyProvider(AKShareHotSectorProvider):
        def _fetch_industry_sector_rank_em_direct(self):
            return [{"name": "有色金属", "code": "BK0478"}]

        def _fetch_industry_sector_rank_em(self):
            return [{"name": "半导体", "code": "881121"}]

        def _fetch_industry_sector_rank_ths_summary(self):
            return [{"name": "白酒", "code": "881273"}]

    provider = NameOnlyProvider(http_config(retry_count=1), cache_config(tmp_path))

    with pytest.raises(ProviderCallError) as exc_info:
        provider.get_industry_sector_rank("20260527")

    assert "缺少涨跌幅" in str(exc_info.value.status.error)


def test_eastmoney_direct_source_maps_api_fields(tmp_path):
    class DirectProvider(AKShareHotSectorProvider):
        def http_get_json_direct(self, url, *, params=None, headers=None):  # noqa: ANN001
            return {
                "data": {
                    "diff": [
                        {
                            "f12": "BK0478",
                            "f14": "有色金属",
                            "f3": 1.88,
                            "f6": 64865000000,
                            "f8": 2.42,
                            "f62": 4988000000,
                            "f104": 39,
                            "f105": 17,
                        }
                    ]
                }
            }

    provider = DirectProvider(http_config(retry_count=1), cache_config(tmp_path))
    rows = provider.get_industry_sector_rank("20260527")

    assert rows[0].sector_code == "BK0478"
    assert rows[0].sector_name == "有色金属"
    assert rows[0].change_pct == 1.88
    assert rows[0].amount == 64865000000
    assert rows[0].net_inflow == 4988000000
    assert rows[0].up_count == 39
    assert rows[0].down_count == 17
    assert rows[0].provider_status is not None
    assert rows[0].provider_status.source == "eastmoney_direct"


def test_industry_rank_prefers_source_with_same_day_cache(tmp_path):
    class CacheFirstProvider(AKShareHotSectorProvider):
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            super().__init__(*args, **kwargs)
            self.calls: list[str] = []

        def _fetch_industry_sector_rank_em_direct(self):
            self.calls.append("eastmoney_direct")
            raise requests.exceptions.ConnectionError("direct disconnected")

        def _fetch_industry_sector_rank_em(self):
            self.calls.append("akshare_eastmoney")
            raise requests.exceptions.ConnectionError("eastmoney disconnected")

        def _fetch_industry_sector_rank_ths_summary(self):
            self.calls.append("akshare_ths_summary")
            return [{"行业": "半导体", "涨幅": 2.1, "上涨家数": 70, "下跌家数": 20}]

    provider = CacheFirstProvider(http_config(retry_count=1), cache_config(tmp_path))
    provider.cache.save("industry_sector_rank:akshare_ths_summary:20260527", [{"行业": "缓存行业"}])

    rows = provider.get_industry_sector_rank("20260527")

    assert provider.calls == ["akshare_ths_summary"]
    assert rows[0].provider_status is not None
    assert rows[0].provider_status.source == "akshare_ths_summary"
