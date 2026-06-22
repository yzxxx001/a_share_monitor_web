from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from stock_monitor.config import load_settings
from stock_monitor.models import ProviderStatus, SectorSnapshot
from stock_monitor.providers.base import ProviderCallError
from stock_monitor.services.hot_sector_candidates import HotSectorCandidateService, score_short_term_resonance


class FakeCandidateProvider:
    def __init__(
        self,
        *,
        constituents: list[dict],
        snapshots: dict[str, dict],
        bars: dict[str, list[dict]],
        fail_snapshot: set[str] | None = None,
    ) -> None:
        self.constituents = constituents
        self.snapshots = snapshots
        self.bars = bars
        self.fail_snapshot = fail_snapshot or set()

    def get_industry_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        status = ProviderStatus("fake", "mock", "rank", True)
        return [
            SectorSnapshot(
                sector_code="BK001",
                sector_name="电网设备",
                trade_date=trade_date,
                change_pct=2.0,
                turnover=3.0,
                amount=100.0,
                net_inflow=5.0,
                up_count=80,
                down_count=20,
                source="mock",
                data_time=datetime(2026, 5, 27, 15, 0),
                provider_status=status,
                raw={
                    "pct_change": 2.0,
                    "up_stock_ratio": 0.8,
                    "turnover_rate": 3.0,
                    "main_net_inflow_ratio": 0.05,
                    "relative_return_vs_benchmark": 1.0,
                },
            )
        ]

    def get_sector_constituents(self, sector_code: str) -> list[dict]:
        return self.constituents

    def get_sector_history(self, sector_code: str, days: int) -> list[dict]:
        return []

    def get_stock_snapshot(self, stock_code: str, trade_date: str) -> dict:
        if stock_code in self.fail_snapshot:
            raise RuntimeError("quote failed")
        return self.snapshots[stock_code]

    def get_daily_bars(self, stock_code: str, days: int) -> list[dict]:
        return self.bars[stock_code][-days:]


class FakeRiskProvider:
    def __init__(self, events: dict[str, dict] | None = None) -> None:
        self.events = events or {}

    def get_announcements(self, stock_code: str, start_date: str, end_date: str) -> dict:
        return self.events.get(stock_code, {"status": "complete", "items": []})


def make_settings(tmp_path: Path):
    source = Path(__file__).parents[1] / "config" / "config.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["paths"]["sqlite_file"] = str(tmp_path / "monitor.db")
    raw["paths"]["log_file"] = str(tmp_path / "monitor.log")
    raw["hot_sector_monitor"]["cache"]["directory"] = str(tmp_path / "cache")
    raw["hot_sector_monitor"]["report"]["directory"] = str(tmp_path / "reports")
    raw["app"]["dry_run"] = False
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # 复制真实策略目录，使加载到的 strategy_configs 与生产一致（含中线稳健等变体）。
    real_strategies = Path(__file__).parents[1] / "config" / "strategies"
    if real_strategies.is_dir():
        shutil.copytree(real_strategies, tmp_path / "strategies")
    return load_settings(config)


def bars(count: int = 130, *, base_amount: float = 100.0, last_amount: float = 240.0) -> list[dict]:
    start = datetime(2026, 1, 1)
    rows: list[dict] = []
    price = 10.0
    for i in range(count):
        price += 0.03 if i % 5 else -0.01
        amount = last_amount if i == count - 1 else base_amount
        rows.append(
            {
                "time": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
                "open": price - 0.03,
                "high": price + 0.08,
                "low": price - 0.08,
                "close": price,
                "amount": amount,
            }
        )
    return rows


def quote(code: str, name: str, *, pct: float = 3.0, amount: float = 240.0, **extra) -> dict:
    payload = {
        "stock_code": code,
        "stock_name": name,
        "close": 13.0,
        "high": 13.1,
        "pct_change": pct,
        "amount": amount,
        "turnover_rate": 5.0,
        "main_net_inflow": amount * 0.03,
    }
    payload.update(extra)
    return payload


def row(**overrides) -> dict:
    payload = {
        "stock_code": "000001.SZ",
        "stock_name": "样本",
        "pct_change": 3.0,
        "sector_change_pct": 2.0,
        "relative_return": 1.0,
        "amount_ratio": 2.5,
        "turnover_rate": 5.0,
        "close": 12.0,
        "ma5": 11.8,
        "ma20": 11.5,
        "ma60": 10.5,
        "ma20_slope_up": True,
        "rsi14": 55.0,
        "atr_percentile": 50.0,
        "fund_flow_ratio": 0.03,
        "risk_tags": [],
        "data_complete": True,
    }
    payload.update(overrides)
    return payload


def test_common_risk_filter_excludes_st_suspended_and_delisting(tmp_path):
    settings = make_settings(tmp_path)
    items = [
        {"stock_code": "000001", "stock_name": "ST测试"},
        {"stock_code": "000002", "stock_name": "停牌测试"},
        {"stock_code": "000003", "stock_name": "退市测试"},
        {"stock_code": "000004", "stock_name": "正常测试"},
    ]
    snapshots = {
        "000001.SZ": quote("000001", "ST测试"),
        "000002.SZ": quote("000002", "停牌测试", suspended=True),
        "000003.SZ": quote("000003", "退市测试", delisting_risk=True),
        "000004.SZ": quote("000004", "正常测试"),
    }
    daily = {code: bars() for code in snapshots}
    result = HotSectorCandidateService(settings, FakeCandidateProvider(constituents=items, snapshots=snapshots, bars=daily), FakeCandidateProvider(constituents=items, snapshots=snapshots, bars=daily), FakeRiskProvider()).generate(
        "20260527", sector="电网设备", strategy="short_term_resonance", notify=False
    )

    reasons = {item["stock_code"]: "、".join(item["reasons"]) for item in result.excluded}
    assert "ST 或 *ST" in reasons["000001.SZ"]
    assert "停牌" in reasons["000002.SZ"]
    assert "退市" in reasons["000003.SZ"]
    assert len(result.candidates) == 1


def test_sealed_limit_up_goes_to_observe_only(tmp_path):
    settings = make_settings(tmp_path)
    items = [{"stock_code": "000005", "stock_name": "封板测试"}]
    snapshots = {"000005.SZ": quote("000005", "封板测试", pct=10.0, close=13.0, high=13.0, sealed_limit_up=True)}
    daily = {"000005.SZ": bars()}
    provider = FakeCandidateProvider(constituents=items, snapshots=snapshots, bars=daily)

    result = HotSectorCandidateService(settings, provider, provider, FakeRiskProvider()).generate("20260527", sector="电网设备")

    assert result.candidates == []
    assert result.observe_only[0]["stock_code"] == "000005.SZ"
    assert "封死涨停" in result.observe_only[0]["risk_tags"]


def test_medium_term_strategy_runs_through(tmp_path):
    settings = make_settings(tmp_path)
    assert "medium_term_quality_value" in settings.hot_sector_monitor.strategy_configs
    items = [{"stock_code": "000004", "stock_name": "正常测试"}]
    snapshots = {"000004.SZ": quote("000004", "正常测试")}
    daily = {"000004.SZ": bars()}
    provider = FakeCandidateProvider(constituents=items, snapshots=snapshots, bars=daily)

    result = HotSectorCandidateService(settings, provider, provider, FakeRiskProvider()).generate(
        "20260527", sector="电网设备", strategy="medium_term_quality_value"
    )

    assert result.strategy == "medium_term_quality_value"
    assert result.strategy_name == "中线稳健共振"
    assert len(result.candidates) == 1
    # 防守配方权重应已生效（trend_structure 权重高于短线配方）。
    assert result.score_weights["trend_structure"] > result.score_weights["amount_expansion"]


def test_unknown_strategy_raises(tmp_path):
    settings = make_settings(tmp_path)
    provider = FakeCandidateProvider(constituents=[], snapshots={}, bars={})
    service = HotSectorCandidateService(settings, provider, provider, FakeRiskProvider())
    try:
        service.generate("20260527", sector="电网设备", strategy="no_such_strategy")
    except ValueError as exc:
        assert "no_such_strategy" in str(exc)
    else:
        raise AssertionError("未注册策略应抛出 ValueError")


def test_amount_expansion_score_caps_extreme_volume():
    config = {
        "weights": {"relative_strength": 0.2, "amount_expansion": 0.2, "trend_structure": 0.15, "turnover_activity": 0.1, "rsi_atr_risk": 0.1, "fund_flow": 0.1, "risk_control": 0.15},
        "thresholds": {"amount_ratio_active": 1.0, "amount_ratio_strong": 2.0, "amount_ratio_extreme": 4.0},
    }
    scored, _, _ = score_short_term_resonance(
        [
            row(stock_code="000001.SZ", amount_ratio=0.8),
            row(stock_code="000002.SZ", amount_ratio=1.5),
            row(stock_code="000003.SZ", amount_ratio=3.0),
            row(stock_code="000004.SZ", amount_ratio=5.0),
        ],
        config,
    )
    by_code = {item["stock_code"]: item["score_components"]["amount_expansion"] for item in scored}
    assert by_code["000003.SZ"] > by_code["000002.SZ"] > by_code["000001.SZ"]
    assert by_code["000004.SZ"] < by_code["000003.SZ"]


def test_rsi_extreme_hot_adds_tag_and_penalty():
    config = {
        "weights": {"relative_strength": 0.2, "amount_expansion": 0.2, "trend_structure": 0.15, "turnover_activity": 0.1, "rsi_atr_risk": 0.1, "fund_flow": 0.1, "risk_control": 0.15},
        "thresholds": {"rsi_hot": 70.0, "rsi_extreme_hot": 80.0},
        "risk_penalties": {"rsi_extreme_hot": 12.0},
    }
    scored, _, _ = score_short_term_resonance([row(stock_code="000001.SZ", rsi14=55), row(stock_code="000002.SZ", rsi14=85)], config)
    by_code = {item["stock_code"]: item for item in scored}
    assert "短线过热" in by_code["000002.SZ"]["risk_tags"]
    assert by_code["000002.SZ"]["score_components"]["rsi_atr_risk"] < by_code["000001.SZ"]["score_components"]["rsi_atr_risk"]


def test_cumulative_return_penalty_demotes_overextended_stock():
    config = {
        "weights": {"relative_strength": 0.2, "amount_expansion": 0.2, "trend_structure": 0.15, "turnover_activity": 0.1, "rsi_atr_risk": 0.1, "fund_flow": 0.1, "risk_control": 0.15},
        "thresholds": {
            "cum_return_lookback": 10,
            "cum_return_mild_threshold": 8.0,
            "cum_return_severe_threshold": 20.0,
            "cum_return_penalty_rate": 0.5,
            "cum_return_max_penalty": 6.0,
        },
    }
    scored, _, _ = score_short_term_resonance(
        [
            row(stock_code="000001.SZ", cum_return_nd=5.0, cum_return_lookback=10),
            row(stock_code="000002.SZ", cum_return_nd=14.0, cum_return_lookback=10),
            row(stock_code="000003.SZ", cum_return_nd=25.0, cum_return_lookback=10),
        ],
        config,
    )
    by_code = {item["stock_code"]: item for item in scored}
    # 5% ≤ 8% 不扣分；14% 扣 0.5×(14−8)=3 分；25% ≥ 20% 扣满 6 分
    assert by_code["000001.SZ"]["cum_return_penalty"] == 0.0
    assert by_code["000002.SZ"]["cum_return_penalty"] == 3.0
    assert by_code["000003.SZ"]["cum_return_penalty"] == 6.0
    assert "累计涨幅过大" in by_code["000003.SZ"]["risk_tags"]
    assert by_code["000001.SZ"]["short_term_score"] > by_code["000003.SZ"]["short_term_score"]


def test_fund_flow_missing_uses_degraded_weights():
    config = {
        "weights": {"relative_strength": 0.2, "amount_expansion": 0.2, "trend_structure": 0.15, "turnover_activity": 0.1, "rsi_atr_risk": 0.1, "fund_flow": 0.1, "risk_control": 0.15},
        "degraded_weights_no_fund_flow": {"relative_strength": 0.25, "amount_expansion": 0.25, "trend_structure": 0.2, "turnover_activity": 0.1, "rsi_atr_risk": 0.1, "risk_control": 0.1},
    }
    scored, warnings, weights = score_short_term_resonance([row(fund_flow_ratio=None)], config)

    assert "fund_flow" not in weights
    assert "fund_flow" not in scored[0]["score_components"]
    assert any("资金流字段缺失" in warning for warning in warnings)
    assert "资金流降级" in scored[0]["data_status"]


def test_candidates_sorted_and_limited_to_10(tmp_path):
    settings = make_settings(tmp_path)
    items = [{"stock_code": f"000{i:03d}", "stock_name": f"测试{i}"} for i in range(1, 13)]
    snapshots = {
        f"000{i:03d}.SZ": quote(f"000{i:03d}", f"测试{i}", pct=2.0 + i / 10, amount=200 + i * 20)
        for i in range(1, 13)
    }
    daily = {code: bars(last_amount=snap["amount"]) for code, snap in snapshots.items()}
    provider = FakeCandidateProvider(constituents=items, snapshots=snapshots, bars=daily)

    result = HotSectorCandidateService(settings, provider, provider, FakeRiskProvider()).generate("20260527", sector="电网设备")

    assert len(result.candidates) == 10
    assert [item["rank"] for item in result.candidates] == list(range(1, 11))
    assert result.candidates[0]["short_term_score"] >= result.candidates[-1]["short_term_score"]


def test_single_stock_quote_failure_does_not_fail_sector(tmp_path):
    settings = make_settings(tmp_path)
    items = [{"stock_code": "000001", "stock_name": "失败"}, {"stock_code": "000002", "stock_name": "正常"}]
    snapshots = {"000002.SZ": quote("000002", "正常")}
    daily = {"000001.SZ": bars(), "000002.SZ": bars()}
    provider = FakeCandidateProvider(constituents=items, snapshots=snapshots, bars=daily, fail_snapshot={"000001.SZ"})

    result = HotSectorCandidateService(settings, provider, provider, FakeRiskProvider()).generate("20260527", sector="电网设备")

    assert len(result.candidates) == 1
    assert any(item["stock_code"] == "000001.SZ" for item in result.excluded)
    assert any("单只股票行情失败" in "、".join(item["reasons"]) for item in result.excluded)


def test_report_shows_excluded_and_risk_tags(tmp_path):
    settings = make_settings(tmp_path)
    items = [{"stock_code": "000001", "stock_name": "风险"}, {"stock_code": "000002", "stock_name": "ST排除"}]
    snapshots = {
        "000001.SZ": quote("000001", "风险"),
        "000002.SZ": quote("000002", "ST排除", is_st=True),
    }
    daily = {code: bars() for code in snapshots}
    provider = FakeCandidateProvider(constituents=items, snapshots=snapshots, bars=daily)
    risk = FakeRiskProvider({"000001.SZ": {"status": "complete", "abnormal_volatility": True, "lhb": True, "items": []}})

    result = HotSectorCandidateService(settings, provider, provider, risk).generate("20260527", sector="电网设备")
    markdown = result.markdown_path.read_text(encoding="utf-8")
    html = result.html_path.read_text(encoding="utf-8")

    assert result.html_path.exists()
    assert "<html" in html
    assert "短线热点共振候选报告" in html
    assert "被排除标的" in markdown
    assert "风险标签" in markdown
    assert "异常波动公告" in markdown
    assert "龙虎榜" in markdown
    assert "ST 或 *ST" in markdown
