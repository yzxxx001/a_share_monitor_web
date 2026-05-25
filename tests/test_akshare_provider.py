from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd

from stock_monitor.market import AKShareMultiSourceProvider, internal_ts_code


def test_internal_ts_code_maps_exchanges():
    assert internal_ts_code("600000") == "600000.SH"
    assert internal_ts_code("000001") == "000001.SZ"
    assert internal_ts_code("830799") == "830799.BJ"


def test_fetch_latest_bars_prefers_sina(monkeypatch):
    captured = {}

    def fake_sina(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame(
            {
                "day": ["2026-05-25 09:35:00", "2026-05-25 09:40:00"],
                "open": [10.0, 10.1], "close": [10.1, 10.2], "high": [10.2, 10.3], "low": [9.9, 10.0],
                "volume": [1000, 1200], "amount": [10100.0, 12240.0],
            }
        )

    def should_not_call_eastmoney(**kwargs):
        raise AssertionError("Sina succeeded, EastMoney should not be called")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_zh_a_minute=fake_sina, stock_zh_a_hist_min_em=should_not_call_eastmoney))
    provider = AKShareMultiSourceProvider(period="5", adjust="", max_bars=80, retry_times=1)
    bars = provider.fetch_latest_bars("600000.SH")

    assert captured == {"symbol": "sh600000", "period": "5", "adjust": ""}
    assert bars.columns.tolist() == ["ts_code", "time", "open", "close", "high", "low", "vol", "amount"]
    assert bars.iloc[-1]["close"] == 10.2
    assert bars.iloc[-1]["ts_code"] == "600000.SH"


def test_fetch_latest_bars_falls_back_to_eastmoney(monkeypatch):
    captured = {}

    def fail_sina(**kwargs):
        raise ConnectionError("sina unavailable")

    def fake_eastmoney(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame(
            {
                "时间": ["2026-05-25 09:35:00"], "开盘": [10.0], "收盘": [10.1], "最高": [10.2], "最低": [9.9],
                "成交量": [1000], "成交额": [10100.0],
            }
        )

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_zh_a_minute=fail_sina, stock_zh_a_hist_min_em=fake_eastmoney))
    provider = AKShareMultiSourceProvider(period="5", adjust="", max_bars=80, retry_times=1)
    bars = provider.fetch_latest_bars("600000.SH")

    assert captured == {"symbol": "600000", "period": "5", "adjust": ""}
    assert bars.iloc[-1]["close"] == 10.1


def test_fetch_stock_master_adds_internal_code(monkeypatch):
    fake = SimpleNamespace(stock_info_a_code_name=lambda: pd.DataFrame({"code": ["600000", "000001", "830799"], "name": ["浦发银行", "平安银行", "艾融软件"]}))
    monkeypatch.setitem(sys.modules, "akshare", fake)
    master = AKShareMultiSourceProvider().fetch_stock_master()
    assert master["ts_code"].tolist() == ["600000.SH", "000001.SZ", "830799.BJ"]
    assert master["name"].tolist() == ["浦发银行", "平安银行", "艾融软件"]
