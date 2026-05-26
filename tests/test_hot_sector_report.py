from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import yaml

from stock_monitor.config import load_settings
from stock_monitor.models import ProviderStatus, SectorSnapshot
from stock_monitor.notifiers import SendResult
from stock_monitor.providers.base import ProviderCallError
from stock_monitor.services.hot_sector_report import HotSectorReportService, score_sector_snapshots


class FakeSectorProvider:
    def __init__(self, snapshots: list[SectorSnapshot]) -> None:
        self.snapshots = snapshots

    def get_industry_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        return [replace(snapshot, trade_date=trade_date) for snapshot in self.snapshots]


class FakeWeCom:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, markdown: str) -> SendResult:
        self.messages.append(markdown)
        return SendResult("wecom", True, "fake sent")


class FailingSectorProvider:
    def get_industry_sector_rank(self, trade_date: str):
        raise ProviderCallError(
            ProviderStatus(
                provider="fake",
                source="mock",
                target=f"industry_sector_rank:{trade_date}",
                ok=False,
                error="请求失败且无可用缓存：RemoteDisconnected",
                warnings=["请求失败且无可用缓存。"],
            )
        )


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
    return load_settings(config)


def snapshot(name: str, pct: float, up_ratio: float, turnover: float, fund: float | None, relative: float, stale: bool = False) -> SectorSnapshot:
    status = ProviderStatus(
        provider="fake",
        source="mock",
        target="rank",
        ok=True,
        is_cached=stale,
        is_stale=stale,
        cached_at=datetime(2026, 5, 27, 15, 1) if stale else None,
    )
    raw = {
        "pct_change": pct,
        "up_stock_ratio": up_ratio,
        "turnover_rate": turnover,
        "main_net_inflow_ratio": fund,
        "relative_return_vs_benchmark": relative,
        "up_stock_count": int(up_ratio * 100),
        "down_stock_count": 100 - int(up_ratio * 100),
        "total_stock_count": 100,
    }
    return SectorSnapshot(
        sector_code=f"BK{name}",
        sector_name=name,
        trade_date="20260527",
        change_pct=pct,
        turnover=turnover,
        source="mock",
        data_time=datetime(2026, 5, 27, 15, 0),
        provider_status=status,
        raw=raw,
    )


def test_scores_full_fields_and_sorts_top_10(tmp_path):
    settings = make_settings(tmp_path)
    rows = [
        snapshot(f"行业{i}", pct=i / 10, up_ratio=0.55 + i / 100, turnover=i, fund=i / 100, relative=i / 20)
        for i in range(1, 13)
    ]
    warnings: list[str] = []
    scored = score_sector_snapshots(rows, settings, warnings)

    assert len(scored) == 12
    assert scored[0]["sector_name"] == "行业12"
    assert scored[0]["rank"] == 1
    assert scored[0]["sector_score"] > scored[-1]["sector_score"]
    assert all("fund_flow" in item["score_components"] for item in scored)


def test_degrades_when_fund_flow_missing(tmp_path):
    settings = make_settings(tmp_path)
    rows = [
        snapshot("半导体", 2.0, 0.8, 3.2, None, 1.2),
        snapshot("软件", 1.0, 0.7, 2.1, None, 0.8),
    ]
    warnings: list[str] = []
    scored = score_sector_snapshots(rows, settings, warnings)

    assert "fund_flow" not in scored[0]["score_components"]
    assert any("资金流数据未完整获取" in warning for warning in warnings)
    assert "资金流缺失" in scored[0]["data_tags"]


def test_computes_fund_flow_ratio_and_amount_activity(tmp_path):
    settings = make_settings(tmp_path)
    row = SectorSnapshot(
        sector_code="贵金属",
        sector_name="贵金属",
        trade_date="20260527",
        change_pct=4.11,
        source="mock",
        raw={
            "板块": "贵金属",
            "涨跌幅": 4.11,
            "总成交额": 341.83,
            "净流入": 34.07,
            "上涨家数": 13,
            "下跌家数": 0,
        },
    )
    warnings: list[str] = []
    scored = score_sector_snapshots([row], settings, warnings)

    assert scored[0]["sector_name"] == "贵金属"
    assert scored[0]["amount"] == 341.83
    assert round(scored[0]["main_net_inflow_ratio"], 4) == round(34.07 / 341.83, 4)
    assert scored[0]["main_net_inflow_ratio_estimated"] is True
    assert scored[0]["activity_label"] == "当日成交额"
    assert scored[0]["activity_value"] == 341.83
    assert scored[0]["score_components"]["relative_return"] == 50.0
    assert "资金流占比估算" in scored[0]["data_tags"]
    assert "活跃度降级:当日成交额" in scored[0]["data_tags"]
    assert "近5日相对缺失:中性分" in scored[0]["data_tags"]


def test_no_qualified_sector_generates_clear_report(tmp_path):
    settings = make_settings(tmp_path)
    service = HotSectorReportService(settings, FakeSectorProvider([snapshot("弱势行业", -0.2, 0.4, 1.0, 0.01, -0.5)]))

    result = service.generate("20260527", notify=False)

    assert result.top_sectors == []
    assert result.data_status == "no_qualified_sector"
    assert any("今日未识别到满足条件" in warning for warning in result.warnings)
    assert "今日未识别到满足条件" in result.markdown_path.read_text(encoding="utf-8")


def test_report_files_and_wecom_top3_and_duplicate_guard(tmp_path):
    settings = make_settings(tmp_path)
    rows = [
        snapshot(f"行业{i}", pct=i, up_ratio=0.6 + i / 100, turnover=i, fund=i / 100, relative=i / 10)
        for i in range(1, 6)
    ]
    wecom = FakeWeCom()
    service = HotSectorReportService(settings, FakeSectorProvider(rows), wecom)  # type: ignore[arg-type]

    first = service.generate("20260527", notify=True)
    second = service.generate("20260527", notify=True)
    third = service.generate("20260527", notify=True, force_send=True)

    assert first.markdown_path.exists()
    assert first.json_path.exists()
    assert first.html_path.exists()
    assert len(first.top_sectors) == 5
    assert len(wecom.messages) == 2
    assert wecom.messages[0].count(". 行业") == 3
    assert "已拦截重复推送" in (second.notification_skipped_reason or "")
    assert third.notification is not None and third.notification.sent is True


def test_cached_status_is_visible_in_report(tmp_path):
    settings = make_settings(tmp_path)
    service = HotSectorReportService(settings, FakeSectorProvider([snapshot("缓存行业", 1.2, 0.7, 2.0, 0.02, 0.3, stale=True)]))

    result = service.generate("20260527", notify=False)

    assert result.provider_status["is_cached"] is True
    assert result.provider_status["is_stale"] is True
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "是否使用缓存：是" in markdown


def test_provider_failure_still_writes_data_unavailable_report(tmp_path):
    settings = make_settings(tmp_path)
    service = HotSectorReportService(settings, FailingSectorProvider())

    result = service.generate("20260527", notify=False)

    assert result.data_status == "data_unavailable"
    assert result.top_sectors == []
    assert result.markdown_path.exists()
    assert result.json_path.exists()
    assert result.html_path.exists()
    markdown = result.markdown_path.read_text(encoding="utf-8")
    html = result.html_path.read_text(encoding="utf-8")
    assert "数据源当前不可用" in markdown
    assert "数据源当前不可用" in html
    assert any("行业板块数据源请求失败" in warning for warning in result.warnings)
