from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .indicators import IndicatorSnapshot, MarketStateSnapshot


class Severity(IntEnum):
    """Signal severity; larger value means more urgent."""

    INFO = 10
    LOW = 20
    MEDIUM = 30
    HIGH = 40


@dataclass(frozen=True)
class Position:
    enabled: bool
    ts_code: str
    name: str
    shares: int
    available_to_sell: int
    average_cost: float
    account_total_value: float
    cash_available: float
    max_position_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    max_single_buy_pct: float
    peak_price_since_entry: float | None = None
    note: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.ts_code}）" if self.name else self.ts_code

    @property
    def has_position(self) -> bool:
        return self.shares > 0 and self.average_cost > 0

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.ts_code:
            errors.append("股票代码为空")
        if self.shares < 0 or self.available_to_sell < 0:
            errors.append("持仓股数/可卖股数不能小于 0")
        if self.available_to_sell > self.shares:
            errors.append("可卖股数不能大于持仓股数")
        if self.shares > 0 and self.average_cost <= 0:
            errors.append("有持仓时持仓成本必须大于 0")
        if self.account_total_value <= 0:
            errors.append("账户总资产必须大于 0")
        for name, value in (
            ("最大仓位比例", self.max_position_pct),
            ("固定止损比例", self.stop_loss_pct),
            ("止盈触发比例", self.take_profit_pct),
            ("移动止盈回撤比例", self.trailing_stop_pct),
            ("单次最大新增仓位比例", self.max_single_buy_pct),
        ):
            if not 0 <= value <= 1:
                errors.append(f"{name}必须在 0 到 1 之间")
        return errors


@dataclass(frozen=True)
class Signal:
    rule_id: str
    severity: Severity
    action: str
    title: str
    reason: str
    current_price: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    position: Position
    trade_time: datetime
    latest_price: float
    market_value: float
    position_pct: float
    pnl_pct: float | None
    observed_peak: float | None
    ma5: float | None
    ma20: float | None
    signals: list[Signal]
    indicators: IndicatorSnapshot | None = None
    market_state: MarketStateSnapshot | None = None


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    source: str
    target: str
    ok: bool
    is_cached: bool = False
    is_stale: bool = False
    cached_at: datetime | None = None
    requested_at: datetime | None = None
    retry_count: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RiskTag:
    code: str
    label: str
    severity: Severity
    source: str
    reason: str
    data_time: datetime | None = None


@dataclass(frozen=True)
class SectorSnapshot:
    sector_code: str
    sector_name: str
    trade_date: str
    change_pct: float | None
    turnover: float | None = None
    amount: float | None = None
    net_inflow: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    source: str = ""
    data_time: datetime | None = None
    provider_status: ProviderStatus | None = None
    is_complete: bool = True
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SectorScoreResult:
    sector_code: str
    sector_name: str
    score: float
    components: dict[str, float]
    data_time: datetime | None = None
    source: str = ""
    is_complete: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CandidateStock:
    ts_code: str
    name: str
    sector_code: str
    sector_name: str
    score: float
    score_components: dict[str, float]
    reasons: list[str]
    risk_tags: list[RiskTag] = field(default_factory=list)
    data_time: datetime | None = None
    source: str = ""
    is_complete: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportMetadata:
    report_id: str
    report_type: str
    trade_date: str
    generated_at: datetime
    source: str
    is_cached: bool = False
    is_complete: bool = True
    warnings: list[str] = field(default_factory=list)
