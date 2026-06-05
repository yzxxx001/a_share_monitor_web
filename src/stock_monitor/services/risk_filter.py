from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import HotSectorRiskFilterConfig


@dataclass(frozen=True)
class RiskFilterDecision:
    status: str
    reasons: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_complete: bool = True

    @property
    def is_candidate(self) -> bool:
        return self.status == "candidate"

    @property
    def is_observe_only(self) -> bool:
        return self.status == "observe_only"

    @property
    def is_excluded(self) -> bool:
        return self.status == "excluded"


class CommonRiskFilter:
    """Reusable risk gate for hot-sector short-term and future mid-term strategies."""

    def __init__(self, config: HotSectorRiskFilterConfig) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        stock_code: str,
        stock_name: str,
        quote: dict[str, Any] | None,
        daily_bars: list[dict[str, Any]] | None,
        risk_events: dict[str, Any] | None = None,
        limit_up_pct: float = 9.8,
    ) -> RiskFilterDecision:
        reasons: list[str] = []
        tags: list[str] = []
        warnings: list[str] = []
        data_complete = True

        raw = quote or {}
        if not raw:
            return RiskFilterDecision("excluded", ["行情数据缺失"], ["行情数据缺失"], data_complete=False)

        latest_price = _first_float(raw, "latest_price", "price", "close", "最新价", "现价", "收盘")
        pct_change = _first_float(raw, "pct_change", "change_pct", "涨跌幅", "涨幅")
        if latest_price is None or latest_price <= 0 or pct_change is None:
            return RiskFilterDecision("excluded", ["行情数据缺失"], ["行情数据缺失"], data_complete=False)

        if self.config.exclude_st and _is_st(stock_name, raw):
            return RiskFilterDecision("excluded", ["ST 或 *ST"], ["ST"], data_complete=data_complete)

        if self.config.exclude_delisting_risk and _has_delisting_risk(stock_name, raw):
            return RiskFilterDecision("excluded", ["退市整理或明确退市风险"], ["退市风险"], data_complete=data_complete)

        if self.config.exclude_suspended and _is_suspended(raw):
            return RiskFilterDecision("excluded", ["停牌"], ["停牌"], data_complete=data_complete)

        bars_count = len(daily_bars or [])
        if bars_count < self.config.exclude_recent_ipo_trading_days:
            return RiskFilterDecision(
                "excluded",
                [f"上市不足 {self.config.exclude_recent_ipo_trading_days} 个交易日"],
                ["上市时间不足"],
                data_complete=False,
            )

        if risk_events is None or str(risk_events.get("status", "")).lower() in {"", "not_implemented", "unavailable"}:
            data_complete = False
            tags.extend(["风险校验数据不完整", "公告数据未完整获取"])
            warnings.append("风险校验数据不完整：公告、立案调查、减持、解禁、龙虎榜等数据源未完整接入。")
        else:
            if _truthy(risk_events.get("severe_risk")):
                return RiskFilterDecision("excluded", ["最近存在立案调查或重大风险提示"], ["重大风险提示"], data_complete=False)
            tags.extend(_event_tags(risk_events))

        if self.config.sealed_limit_up_as_observe_only and _is_sealed_limit_up(raw, pct_change, limit_up_pct):
            return RiskFilterDecision(
                "observe_only",
                ["封死涨停或无法合理成交"],
                _unique([*tags, "封死涨停"]),
                warnings,
                data_complete,
            )

        return RiskFilterDecision("candidate", reasons, _unique(tags), warnings, data_complete)


def _event_tags(events: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    mapping = [
        ("abnormal_volatility", "异常波动公告"),
        ("lhb", "龙虎榜"),
        ("dragon_tiger", "龙虎榜"),
        ("major_reduction", "近期重大减持"),
        ("unlock", "近期解禁"),
    ]
    for key, label in mapping:
        if _truthy(events.get(key)):
            tags.append(label)
    for item in events.get("items") or []:
        text = str(item)
        if "异常波动" in text:
            tags.append("异常波动公告")
        if "龙虎榜" in text:
            tags.append("龙虎榜")
        if "减持" in text:
            tags.append("近期重大减持")
        if "解禁" in text:
            tags.append("近期解禁")
    return _unique(tags)


def _is_st(name: str, raw: dict[str, Any]) -> bool:
    if _truthy(raw.get("is_st")):
        return True
    normalized = str(name or raw.get("name") or raw.get("名称") or "").upper().replace(" ", "")
    return normalized.startswith("ST") or normalized.startswith("*ST") or "*ST" in normalized


def _has_delisting_risk(name: str, raw: dict[str, Any]) -> bool:
    if _truthy(raw.get("delisting_risk")) or _truthy(raw.get("is_delisting")):
        return True
    text = " ".join(str(raw.get(key, "")) for key in ("status", "risk_status", "special_treatment", "备注"))
    text = f"{name} {text}"
    return any(token in text for token in ("退市", "退市整理", "终止上市", "delisting"))


def _is_suspended(raw: dict[str, Any]) -> bool:
    if _truthy(raw.get("suspended")) or _truthy(raw.get("is_suspended")):
        return True
    text = " ".join(str(raw.get(key, "")) for key in ("status", "交易状态", "停牌状态", "备注"))
    return "停牌" in text or "suspended" in text.lower()


def _is_sealed_limit_up(raw: dict[str, Any], pct_change: float, limit_up_pct: float) -> bool:
    if _truthy(raw.get("sealed_limit_up")) or _truthy(raw.get("is_sealed_limit_up")):
        return True
    if pct_change < limit_up_pct:
        return False
    latest_price = _first_float(raw, "latest_price", "price", "close", "最新价", "现价", "收盘")
    high = _first_float(raw, "high", "最高")
    if latest_price is not None and high is not None and abs(latest_price - high) <= max(high * 0.001, 0.01):
        return True
    return False


def _first_float(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        try:
            if value is not None and str(value).strip() != "":
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "有"}


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))
