from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

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


def make_config(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "config" / "config.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["paths"]["sqlite_file"] = str(tmp_path / "monitor.db")
    raw["paths"]["log_file"] = str(tmp_path / "monitor.log")
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
