from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from stock_monitor.models import ProviderStatus, SectorSnapshot
from stock_monitor.webapp import create_app


class FakeProvider:
    def fetch_stock_master(self):
        return pd.DataFrame(
            [{"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "market": "沪深京A股", "exchange": "SZ", "industry": "", "list_date": ""}]
        )

    def fetch_latest_bars(self, ts_code: str):
        closes = [10.2] * 19 + [9.4]
        times = pd.date_range("2026-05-25 09:35:00", periods=20, freq="5min")
        return pd.DataFrame({
            "ts_code": [ts_code] * 20, "time": times, "open": closes, "close": closes,
            "high": [v + 0.02 for v in closes], "low": [v - 0.02 for v in closes],
            "vol": [1000] * 20, "amount": [v * 1000 for v in closes],
        })


class FakeHotSectorProvider:
    def get_industry_sector_rank(self, trade_date: str):
        status = ProviderStatus(provider="fake", source="mock", target="rank", ok=True)
        return [
            SectorSnapshot(
                sector_code="BK001",
                sector_name="半导体",
                trade_date=trade_date,
                change_pct=2.0,
                turnover=3.0,
                provider_status=status,
                raw={
                    "pct_change": 2.0,
                    "up_stock_ratio": 0.8,
                    "turnover_rate": 3.0,
                    "main_net_inflow_ratio": 0.03,
                    "relative_return_vs_benchmark": 1.0,
                },
            ),
            SectorSnapshot(
                sector_code="BK002",
                sector_name="软件",
                trade_date=trade_date,
                change_pct=1.2,
                turnover=2.0,
                provider_status=status,
                raw={
                    "pct_change": 1.2,
                    "up_stock_ratio": 0.7,
                    "turnover_rate": 2.0,
                    "main_net_inflow_ratio": 0.01,
                    "relative_return_vs_benchmark": 0.5,
                },
            ),
        ]

    def get_sector_constituents(self, sector_code: str):
        return [{"stock_code": "000001", "stock_name": "候选A"}]

    def get_sector_history(self, sector_code: str, days: int):
        return []

    def get_stock_snapshot(self, stock_code: str, trade_date: str):
        return {
            "stock_code": stock_code,
            "stock_name": "候选A",
            "close": 13.0,
            "high": 13.1,
            "pct_change": 3.2,
            "amount": 260.0,
            "turnover_rate": 5.0,
            "main_net_inflow": 8.0,
        }

    def get_daily_bars(self, stock_code: str, days: int):
        start = datetime(2026, 1, 1)
        rows = []
        price = 10.0
        for index in range(130):
            price += 0.03 if index % 5 else -0.01
            rows.append(
                {
                    "time": (start + timedelta(days=index)).strftime("%Y-%m-%d"),
                    "open": price - 0.03,
                    "high": price + 0.08,
                    "low": price - 0.08,
                    "close": price,
                    "amount": 260.0 if index == 129 else 100.0,
                    "turnover_rate": 5.0,
                }
            )
        return rows


def make_config(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "config" / "config.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["paths"]["sqlite_file"] = str(tmp_path / "monitor.db")
    raw["paths"]["log_file"] = str(tmp_path / "monitor.log")
    raw["hot_sector_monitor"]["cache"]["directory"] = str(tmp_path / "cache")
    raw["hot_sector_monitor"]["report"]["directory"] = str(tmp_path / "reports")
    raw["web"]["auto_start_scheduler"] = False
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config


def test_add_by_name_run_and_view_api(tmp_path):
    app = create_app(make_config(tmp_path), scheduler_enabled=False)
    service = app.config["MONITOR_SERVICE"]
    service.provider = FakeProvider()
    client = app.test_client()

    response = client.post("/actions/sync-master", follow_redirects=True)
    assert "已同步 1 条".encode("utf-8") in response.data

    response = client.post(
        "/watchlist/add",
        data={"query": "平安银行", "enabled": "1", "shares": "1000", "available_to_sell": "1000", "average_cost": "10", "account_total_value": "100000"},
        follow_redirects=True,
    )
    assert "000001.SZ".encode("utf-8") in response.data

    client.post("/actions/run-once", data={"force_session": "1"}, follow_redirects=True)
    payload = client.get("/api/stocks").get_json()
    assert payload[0]["latest_price"] == 9.4
    assert "STOP_LOSS" in payload[0]["signals"]
    bars = client.get("/api/stocks/000001.SZ/bars").get_json()
    assert len(bars) == 20


def test_dashboard_can_generate_and_show_hot_sector_report(tmp_path):
    app = create_app(make_config(tmp_path), scheduler_enabled=False)
    app.config["HOT_SECTOR_PROVIDER"] = FakeHotSectorProvider()
    client = app.test_client()

    response = client.post("/actions/hot-sector-report", follow_redirects=True)

    assert "已生成热门行业板块报告".encode("utf-8") in response.data
    assert "半导体".encode("utf-8") in response.data
    assert list((tmp_path / "reports").glob("*_sector_summary.json"))


def test_dashboard_can_generate_candidates_from_hot_sector_row(tmp_path):
    app = create_app(make_config(tmp_path), scheduler_enabled=False)
    app.config["HOT_SECTOR_PROVIDER"] = FakeHotSectorProvider()
    client = app.test_client()

    response = client.post("/actions/hot-sector-report", follow_redirects=True)
    assert "筛选候选股".encode("utf-8") in response.data

    response = client.post(
        "/actions/hot-sector-candidates",
        data={"sector_name": "BK001", "sector_code": "BK001", "strategy": "short_term_resonance"},
        follow_redirects=True,
    )

    assert "短线热点共振候选报告".encode("utf-8") in response.data
    assert "候选A".encode("utf-8") in response.data
    assert list((tmp_path / "reports").glob("*_short_term_resonance.json"))
    assert list((tmp_path / "reports").glob("*_short_term_resonance.html"))
