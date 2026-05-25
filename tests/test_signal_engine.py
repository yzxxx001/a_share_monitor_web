from datetime import datetime, timedelta

import pandas as pd

from stock_monitor.config import RuleConfig
from stock_monitor.models import Position, Severity
from stock_monitor.signal_engine import evaluate_position


def rules() -> RuleConfig:
    return RuleConfig(20, 21, 1.5, 30, ("HIGH",))


def position(**overrides) -> Position:
    values = dict(
        enabled=True,
        ts_code="000001.SZ",
        name="测试股票",
        shares=1000,
        available_to_sell=1000,
        average_cost=10.0,
        account_total_value=100000.0,
        cash_available=50000.0,
        max_position_pct=0.30,
        stop_loss_pct=0.05,
        take_profit_pct=0.12,
        trailing_stop_pct=0.04,
        max_single_buy_pct=0.10,
        peak_price_since_entry=None,
        note="",
    )
    values.update(overrides)
    return Position(**values)


def bars(closes, highs=None, volumes=None):
    start = datetime(2026, 5, 25, 9, 35)
    highs = highs or [close + 0.02 for close in closes]
    volumes = volumes or [1000 for _ in closes]
    return pd.DataFrame(
        {
            "ts_code": ["000001.SZ"] * len(closes),
            "time": [start + timedelta(minutes=5 * index) for index in range(len(closes))],
            "open": closes,
            "close": closes,
            "high": highs,
            "low": [close - 0.02 for close in closes],
            "vol": volumes,
            "amount": [close * volume for close, volume in zip(closes, volumes)],
        }
    )


def test_stop_loss_is_high_severity():
    result = evaluate_position(position(), bars([9.40]), rules())
    stop = [signal for signal in result.signals if signal.rule_id == "STOP_LOSS"]
    assert stop and stop[0].severity == Severity.HIGH


def test_trailing_stop_after_profit_peak():
    result = evaluate_position(position(peak_price_since_entry=12.0), bars([11.40]), rules())
    assert any(signal.rule_id == "TRAILING_STOP" for signal in result.signals)


def test_trend_weak_after_sufficient_cached_bars():
    result = evaluate_position(position(), bars([10.4] * 15 + [10.0, 9.9, 9.8, 9.7, 9.6]), rules())
    assert any(signal.rule_id == "TREND_WEAK" for signal in result.signals)
