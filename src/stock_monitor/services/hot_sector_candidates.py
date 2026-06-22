from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from ..config import Settings
from ..indicators import compute_indicators
from ..market import internal_ts_code
from ..notifiers import SendResult, WeComNotifier
from ..providers.base import ProviderCallError
from ..providers.hot_sector import RiskEventProvider, SectorDataProvider, StockDataProvider
from ..reports.hot_sector_candidates import build_candidate_html, build_candidate_markdown, build_candidate_wecom_summary
from .hot_sector_report import normalize_trade_date, score_sector_snapshots
from .risk_filter import CommonRiskFilter, RiskFilterDecision
from .strategy_registry import get_scorer

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HotSectorCandidateReportResult:
    trade_date: str
    generated_at: datetime
    sector_name: str
    sector_code: str
    strategy: str
    strategy_name: str
    sector_heat_score: float | None
    sector_change_pct: float | None
    data_status: str
    score_weights: dict[str, float]
    warnings: list[str]
    candidates: list[dict[str, Any]]
    observe_only: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    markdown_path: Path
    json_path: Path
    html_path: Path
    notification: SendResult | None = None
    notification_skipped_reason: str | None = None


class HotSectorCandidateService:
    def __init__(
        self,
        settings: Settings,
        sector_provider: SectorDataProvider,
        stock_provider: StockDataProvider,
        risk_provider: RiskEventProvider | None = None,
        wecom: WeComNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.sector_provider = sector_provider
        self.stock_provider = stock_provider
        self.risk_provider = risk_provider or RiskEventProvider()
        self.wecom = wecom or WeComNotifier(settings.notifications.wecom, settings.app.dry_run)
        self.zone = ZoneInfo(settings.app.timezone)
        self.risk_filter = CommonRiskFilter(settings.hot_sector_monitor.common_risk_filter)

    def generate(
        self,
        trade_date: str,
        *,
        sector: str,
        strategy: str = "short_term_resonance",
        notify: bool = False,
        force_refresh: bool = False,
    ) -> HotSectorCandidateReportResult:
        trade_date = normalize_trade_date(trade_date, self.zone)
        if strategy not in self.settings.hot_sector_monitor.strategy_configs:
            available = ", ".join(sorted(self.settings.hot_sector_monitor.strategy_configs)) or "无"
            raise ValueError(f"未注册的策略：{strategy}（可用：{available}）")

        generated_at = datetime.now(self.zone)
        config = _strategy_config(self.settings, strategy)
        scorer = get_scorer(str(config.get("scorer", "short_term_resonance")))
        thresholds = dict(config.get("thresholds") or {})
        history_days = int(config.get("history_days", 130))
        max_candidates = int(config.get("max_candidates") or self.settings.hot_sector_monitor.candidate.max_count_per_sector)
        max_candidates = min(max_candidates, self.settings.hot_sector_monitor.candidate.max_count_per_sector)
        warnings: list[str] = []

        if force_refresh:
            _clear_rank_cache(self.sector_provider, trade_date)

        sector_snapshot = None
        sector_heat_score = None
        snapshots = []
        try:
            snapshots = self.sector_provider.get_industry_sector_rank(trade_date)
            sector_snapshot = _find_sector_snapshot(snapshots, sector)
            scored_sectors = score_sector_snapshots(snapshots, self.settings, warnings)
            sector_heat_score = _find_sector_heat_score(scored_sectors, sector_snapshot, sector)
        except ProviderCallError as exc:
            warnings.extend(exc.status.warnings)
            warnings.append(f"板块排行数据获取失败：{exc.status.error}")

        sector_code = sector_snapshot.sector_code if sector_snapshot else sector
        sector_name = sector_snapshot.sector_name if sector_snapshot else sector
        sector_change_pct = sector_snapshot.change_pct if sector_snapshot else None
        if sector_snapshot is None:
            warnings.append(f"未在热门板块排行中精确匹配到 {sector}，将按输入名称尝试获取成分股。")

        constituents = self._get_constituents(sector_name, sector_code, force_refresh, warnings)
        metrics: list[dict[str, Any]] = []
        observe_only: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        for raw_stock in constituents:
            stock_code = _stock_code(raw_stock)
            stock_name = _stock_name(raw_stock)
            if not stock_code:
                excluded.append(
                    {
                        "stock_code": "",
                        "stock_name": stock_name or "未知",
                        "reasons": ["成分股代码缺失"],
                        "risk_tags": ["数据不完整"],
                    }
                )
                continue
            try:
                if force_refresh:
                    _clear_stock_cache(self.stock_provider, stock_code, trade_date, history_days)
                quote = self.stock_provider.get_stock_snapshot(stock_code, trade_date)
                bars = self.stock_provider.get_daily_bars(stock_code, history_days)
                merged_quote = {**raw_stock, **(quote or {})}
                if not stock_name:
                    stock_name = _stock_name(merged_quote)
                risk_events = self._risk_events(stock_code, trade_date)
                decision = self.risk_filter.evaluate(
                    stock_code=stock_code,
                    stock_name=stock_name,
                    quote=_normalize_quote(merged_quote),
                    daily_bars=bars,
                    risk_events=risk_events,
                    limit_up_pct=float(thresholds.get("limit_up_pct", 9.8)),
                )
                warnings.extend(decision.warnings)
                if decision.is_excluded:
                    excluded.append(_decision_row(stock_code, stock_name, decision))
                    continue

                metric = build_stock_metrics(
                    raw_stock=raw_stock,
                    quote=merged_quote,
                    daily_bars=bars,
                    sector_code=sector_code,
                    sector_name=sector_name,
                    sector_change_pct=sector_change_pct,
                    risk_decision=decision,
                    cum_return_lookback=int(thresholds.get("cum_return_lookback", 10)),
                )
                if decision.is_observe_only:
                    observe_only.append(_observe_row(metric, decision))
                    continue
                metrics.append(metric)
            except Exception as exc:
                excluded.append(
                    {
                        "stock_code": stock_code,
                        "stock_name": stock_name,
                        "reasons": [f"单只股票行情失败：{_short_error(exc)}"],
                        "risk_tags": ["行情获取失败"],
                    }
                )
                warnings.append(f"{stock_code} 单只股票行情失败，已跳过，不影响板块其它股票。")

        scored, score_warnings, score_weights = scorer(metrics, config)
        warnings.extend(score_warnings)
        candidates = scored[:max_candidates]
        for rank, row in enumerate(candidates, start=1):
            row["rank"] = rank

        data_status = "complete"
        if warnings:
            data_status = "degraded"
        if not constituents:
            data_status = "data_unavailable"
            warnings.append("板块成分股为空，未生成候选观察股。")

        report_dir = self.settings.hot_sector_monitor.report.directory
        report_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{trade_date}_{_safe_filename(sector_name)}_{strategy}"
        markdown_path = report_dir / f"{stem}.md"
        json_path = report_dir / f"{stem}.json"
        html_path = report_dir / f"{stem}.html"

        result = HotSectorCandidateReportResult(
            trade_date=trade_date,
            generated_at=generated_at,
            sector_name=sector_name,
            sector_code=sector_code,
            strategy=strategy,
            strategy_name=str(config.get("display_name") or "短线热点共振"),
            sector_heat_score=sector_heat_score,
            sector_change_pct=sector_change_pct,
            data_status=data_status,
            score_weights=score_weights,
            warnings=_unique(warnings),
            candidates=candidates,
            observe_only=observe_only,
            excluded=excluded,
            markdown_path=markdown_path,
            json_path=json_path,
            html_path=html_path,
        )
        markdown_path.write_text(build_candidate_markdown(result), encoding="utf-8")
        html_path.write_text(build_candidate_html(result), encoding="utf-8")
        json_path.write_text(json.dumps(candidate_result_to_json(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        if notify and self.settings.hot_sector_monitor.notification.enable_wecom_webhook:
            notification = self.wecom.send(build_candidate_wecom_summary(result))
            result = HotSectorCandidateReportResult(
                trade_date=result.trade_date,
                generated_at=result.generated_at,
                sector_name=result.sector_name,
                sector_code=result.sector_code,
                strategy=result.strategy,
                strategy_name=result.strategy_name,
                sector_heat_score=result.sector_heat_score,
                sector_change_pct=result.sector_change_pct,
                data_status=result.data_status,
                score_weights=result.score_weights,
                warnings=result.warnings,
                candidates=result.candidates,
                observe_only=result.observe_only,
                excluded=result.excluded,
                markdown_path=result.markdown_path,
                json_path=result.json_path,
                html_path=result.html_path,
                notification=notification,
            )
            json_path.write_text(json.dumps(candidate_result_to_json(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        LOGGER.info("%s候选报告已生成：%s；候选=%d。", result.strategy_name, markdown_path, len(candidates))
        return result

    def _get_constituents(self, sector_name: str, sector_code: str, force_refresh: bool, warnings: list[str]) -> list[dict[str, Any]]:
        identifiers = list(dict.fromkeys([sector_name, sector_code]))
        errors: list[str] = []
        for identifier in identifiers:
            if not identifier:
                continue
            try:
                if force_refresh:
                    _delete_provider_cache(self.sector_provider, f"sector_constituents:{identifier}")
                rows = self.sector_provider.get_sector_constituents(identifier)
                if rows:
                    return rows
            except ProviderCallError as exc:
                errors.append(exc.status.error or str(exc))
            except Exception as exc:
                errors.append(_short_error(exc))
        if errors:
            warnings.append(f"板块成分股获取失败：{'; '.join(errors)}")
        return []

    def _risk_events(self, stock_code: str, trade_date: str) -> dict[str, Any]:
        try:
            events = self.risk_provider.get_announcements(stock_code, trade_date, trade_date)
        except Exception as exc:
            return {"status": "unavailable", "items": [], "warning": _short_error(exc)}
        return events or {"status": "unavailable", "items": []}


def build_stock_metrics(
    *,
    raw_stock: dict[str, Any],
    quote: dict[str, Any],
    daily_bars: list[dict[str, Any]],
    sector_code: str,
    sector_name: str,
    sector_change_pct: float | None,
    risk_decision: RiskFilterDecision,
    cum_return_lookback: int = 10,
) -> dict[str, Any]:
    stock_code = _stock_code({**raw_stock, **quote})
    stock_name = _stock_name({**raw_stock, **quote})
    normalized_quote = _normalize_quote(quote)
    df = _daily_bars_dataframe(daily_bars)
    latest_close = normalized_quote.get("close")
    if latest_close is None and not df.empty:
        latest_close = float(df.iloc[-1]["close"])
    if latest_close is None:
        raise ValueError("行情数据缺失")

    ind = compute_indicators(df, float(latest_close)) if not df.empty else None
    close = df["close"].astype(float) if not df.empty else pd.Series(dtype=float)
    amount = df["amount"].astype(float) if not df.empty and "amount" in df.columns else pd.Series(dtype=float)
    ma5 = _ma(close, 5)
    ma20 = _ma(close, 20)
    ma60 = _ma(close, 60)
    ma20_prev = float(close.iloc[-6:-1].mean()) if len(close) >= 25 else None
    ma20_slope_up = ma20 is not None and ma20_prev is not None and ma20 >= ma20_prev

    quote_amount = normalized_quote.get("amount")
    today_amount = quote_amount if quote_amount is not None else (float(amount.iloc[-1]) if len(amount) else None)
    avg20_amount = float(amount.iloc[-21:-1].mean()) if len(amount) >= 21 else None
    amount_ratio = today_amount / avg20_amount if today_amount is not None and avg20_amount and avg20_amount > 0 else None
    turnover_rate = normalized_quote.get("turnover_rate")
    turnover_120d_percentile = None
    if turnover_rate is not None and "turnover_rate" in df.columns:
        turnover_series = pd.to_numeric(df["turnover_rate"], errors="coerce").dropna().tail(120)
        turnover_120d_percentile = _value_percentile(turnover_series, turnover_rate)

    pct_change = normalized_quote.get("pct_change")
    relative_return = pct_change - sector_change_pct if pct_change is not None and sector_change_pct is not None else None
    fund_flow_ratio = _fund_flow_ratio(normalized_quote)
    cum_return_nd = _cumulative_return(close, cum_return_lookback)

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "sector_code": sector_code,
        "sector_name": sector_name,
        "pct_change": pct_change,
        "sector_change_pct": sector_change_pct,
        "relative_return": relative_return,
        "cum_return_nd": cum_return_nd,
        "cum_return_lookback": cum_return_lookback,
        "amount": today_amount,
        "avg20_amount": avg20_amount,
        "amount_ratio": amount_ratio,
        "turnover_rate": turnover_rate,
        "turnover_120d_percentile": turnover_120d_percentile,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "ma20_slope_up": ma20_slope_up,
        "close": float(latest_close),
        "rsi14": ind.rsi14 if ind else None,
        "atr14": ind.atr14 if ind else None,
        "atr_percentile": ind.atr_percentile if ind else None,
        "fund_flow_ratio": fund_flow_ratio,
        "risk_tags": list(risk_decision.risk_tags),
        "warnings": list(risk_decision.warnings),
        "data_complete": risk_decision.data_complete and _metric_data_complete(pct_change, amount_ratio, ma20, ind.rsi14 if ind else None),
        "data_status": "完整" if risk_decision.data_complete else "风险校验数据不完整",
        "source": _provider_source(quote),
    }


def score_short_term_resonance(rows: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], dict[str, float]]:
    warnings: list[str] = []
    if not rows:
        return [], warnings, _weights(config, fund_flow_available=False)

    _assign_percentile(rows, "turnover_rate", "turnover_percentile")
    _assign_percentile(rows, "fund_flow_ratio", "fund_flow_percentile")
    fund_flow_available = any(row.get("fund_flow_ratio") is not None for row in rows)
    turnover_120d_available = any(row.get("turnover_120d_percentile") is not None for row in rows)
    weights = _weights(config, fund_flow_available=fund_flow_available)
    if not fund_flow_available:
        warnings.append("资金流字段缺失，资金流权重已按配置重分配，并在报告中标注降级。")
    if not turnover_120d_available:
        warnings.append("换手率近120日分位字段缺失，已降级使用板块内换手率分位。")

    scored: list[dict[str, Any]] = []
    thresholds = dict(config.get("thresholds") or {})
    for row in rows:
        risk_tags = list(row.get("risk_tags") or [])
        components: dict[str, float] = {}
        components["relative_strength"] = _relative_strength_score(row.get("relative_return"), thresholds, risk_tags)
        components["amount_expansion"] = _amount_score(row.get("amount_ratio"), thresholds, risk_tags)
        trend_score, trend_state = _trend_score(row)
        components["trend_structure"] = trend_score
        turnover_percentile = row.get("turnover_120d_percentile")
        if turnover_percentile is None:
            turnover_percentile = row.get("turnover_percentile")
        components["turnover_activity"] = _turnover_score(turnover_percentile, row.get("turnover_rate"))
        components["rsi_atr_risk"] = _rsi_atr_score(row.get("rsi14"), row.get("atr_percentile"), thresholds, risk_tags)
        if fund_flow_available:
            components["fund_flow"] = _fund_flow_score(row.get("fund_flow_percentile"), row.get("fund_flow_ratio"))
        components["risk_control"] = _risk_control_score(risk_tags, config)

        score = sum(components[key] * weights[key] for key in weights if key in components)
        cum_penalty = _cumulative_penalty(row.get("cum_return_nd"), thresholds, risk_tags)
        score -= cum_penalty
        row = dict(row)
        row["rank"] = 0
        row["short_term_score"] = round(score, 2)
        row["score_components"] = {key: round(value, 2) for key, value in components.items()}
        row["cum_return_penalty"] = round(cum_penalty, 2)
        row["trend_state"] = trend_state
        row["rsi_state"] = _rsi_state(row.get("rsi14"))
        row["risk_tags"] = _unique(risk_tags)
        row["selection_reason"] = _selection_reason(row)
        row["data_status"] = _data_status(row, fund_flow_available)
        scored.append(row)

    scored.sort(key=lambda item: item["short_term_score"], reverse=True)
    return scored, warnings, weights


def candidate_result_to_json(result: HotSectorCandidateReportResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["generated_at"] = result.generated_at.isoformat(timespec="seconds")
    payload["markdown_path"] = str(result.markdown_path)
    payload["json_path"] = str(result.json_path)
    payload["html_path"] = str(result.html_path)
    if result.notification is not None:
        payload["notification"] = asdict(result.notification)
    return payload


def _strategy_config(settings: Settings, strategy: str) -> dict[str, Any]:
    config = dict(settings.hot_sector_monitor.strategy_configs.get(strategy) or {})
    if not config:
        raise ValueError(f"策略配置缺失：{strategy}")
    return config


def _weights(config: dict[str, Any], *, fund_flow_available: bool) -> dict[str, float]:
    key = "weights" if fund_flow_available else "degraded_weights_no_fund_flow"
    raw = dict(config.get(key) or config.get("weights") or {})
    total = sum(float(value) for value in raw.values())
    if total <= 0:
        raise ValueError("策略权重配置无效")
    return {name: float(value) / total for name, value in raw.items()}


def _relative_strength_score(value: float | None, thresholds: dict[str, Any], tags: list[str]) -> float:
    if value is None:
        tags.append("相对表现数据缺失")
        return 50.0
    weak = float(thresholds.get("relative_return_weak_below", -2.0))
    sync_low = float(thresholds.get("relative_return_sync_low", -1.0))
    sync_high = float(thresholds.get("relative_return_sync_high", 3.0))
    extreme = float(thresholds.get("relative_return_extreme_above", 7.0))
    if value < weak:
        return 20.0
    if value <= sync_low:
        return 50.0
    if value <= sync_high:
        return 85.0 + max(value - sync_low, 0.0) / max(sync_high - sync_low, 1.0) * 10.0
    if value > extreme:
        tags.append("追高风险")
        return 70.0
    return 90.0


def _amount_score(value: float | None, thresholds: dict[str, Any], tags: list[str]) -> float:
    if value is None:
        tags.append("成交额数据不完整")
        return 50.0
    active = float(thresholds.get("amount_ratio_active", 1.0))
    strong = float(thresholds.get("amount_ratio_strong", 2.0))
    extreme = float(thresholds.get("amount_ratio_extreme", 4.0))
    if value < active:
        return 20.0
    if value < strong:
        return 55.0 + (value - active) / max(strong - active, 1.0) * 15.0
    if value <= extreme:
        return 85.0 + (value - strong) / max(extreme - strong, 1.0) * 15.0
    tags.append("成交额极端放大")
    return 75.0


def _trend_score(row: dict[str, Any]) -> tuple[float, str]:
    close = row.get("close")
    ma20 = row.get("ma20")
    ma60 = row.get("ma60")
    slope_up = bool(row.get("ma20_slope_up"))
    if close is None or ma20 is None:
        return 50.0, "趋势数据不足"
    above_ma20 = close > ma20
    mid_up = ma60 is not None and ma20 >= ma60
    if above_ma20 and mid_up:
        if row.get("ma5") is not None and row["ma5"] >= ma20:
            return 95.0, "均线多头"
        return 88.0, "中期趋势向上"
    if above_ma20 and slope_up:
        return 82.0, "站上MA20"
    if above_ma20:
        return 68.0, "趋势修复中"
    return 35.0, "趋势偏弱"


def _turnover_score(percentile: float | None, turnover_rate: float | None) -> float:
    if percentile is not None:
        return max(20.0, min(100.0, 30.0 + percentile * 0.7))
    if turnover_rate is None:
        return 50.0
    return max(20.0, min(90.0, 40.0 + turnover_rate * 5.0))


def _rsi_atr_score(rsi: float | None, atr_percentile: float | None, thresholds: dict[str, Any], tags: list[str]) -> float:
    score = 85.0
    if rsi is None:
        tags.append("RSI数据不完整")
        score = 70.0
    elif rsi < float(thresholds.get("rsi_oversold", 30.0)):
        tags.append("弱势或超跌")
        score = 60.0
    elif rsi < float(thresholds.get("rsi_hot", 70.0)):
        score = 90.0
    elif rsi < float(thresholds.get("rsi_extreme_hot", 80.0)):
        tags.append("RSI偏热")
        score = 70.0
    else:
        tags.append("短线过热")
        score = 45.0
    if atr_percentile is not None and atr_percentile >= float(thresholds.get("atr_high_percentile", 80.0)):
        tags.append("ATR或波动率高位")
        score -= 15.0
    return max(0.0, score)


def _fund_flow_score(percentile: float | None, ratio: float | None) -> float:
    if ratio is None:
        return 20.0
    base = 30.0 + (percentile or 0.0) * 0.7
    if ratio < 0:
        base -= 10.0
    return max(0.0, min(100.0, base))


def _risk_control_score(tags: list[str], config: dict[str, Any]) -> float:
    penalties = dict(config.get("risk_penalties") or {})
    mapping = {
        "风险校验数据不完整": "risk_check_incomplete",
        "公告数据未完整获取": "announcement_incomplete",
        "异常波动公告": "abnormal_volatility",
        "龙虎榜": "lhb",
        "近期重大减持": "major_reduction",
        "近期解禁": "unlock",
        "短线过热": "rsi_extreme_hot",
        "成交额极端放大": "extreme_amount",
        "ATR或波动率高位": "high_volatility",
        "追高风险": "chasing_risk",
    }
    score = 90.0
    for tag in _unique(tags):
        score -= float(penalties.get(mapping.get(tag, ""), 0.0))
    return max(0.0, min(100.0, score))


def _rsi_state(rsi: float | None) -> str:
    if rsi is None:
        return "RSI数据不足"
    if rsi < 30:
        return "弱势或超跌"
    if rsi < 70:
        return "正常"
    if rsi < 80:
        return "RSI偏热"
    return "短线过热"


def _selection_reason(row: dict[str, Any]) -> str:
    parts: list[str] = []
    relative = row.get("relative_return")
    if relative is not None:
        parts.append("板块共振" if -1 <= relative <= 3 else f"相对板块{relative:+.2f}pct")
    amount_ratio = row.get("amount_ratio")
    if amount_ratio is not None:
        parts.append(f"成交额{amount_ratio:.1f}倍放大")
    trend = row.get("trend_state")
    if trend:
        parts.append(str(trend))
    rsi = row.get("rsi_state")
    if rsi and rsi != "正常":
        parts.append(f"{rsi}，需关注追高或波动风险")
    cum_return = row.get("cum_return_nd")
    if cum_return is not None and row.get("cum_return_penalty"):
        lookback = row.get("cum_return_lookback") or 10
        parts.append(f"近{lookback}日累计涨幅{cum_return:+.1f}%，已扣{row['cum_return_penalty']:.1f}分")
    return " + ".join(parts) if parts else "数据完整度有限，仅列为观察。"


def _data_status(row: dict[str, Any], fund_flow_available: bool) -> str:
    tags: list[str] = []
    if not row.get("data_complete"):
        tags.append("数据不完整")
    if not fund_flow_available or row.get("fund_flow_ratio") is None:
        tags.append("资金流降级")
    if row.get("risk_tags"):
        if "风险校验数据不完整" in row["risk_tags"]:
            tags.append("风险校验数据不完整")
    return "、".join(_unique(tags)) if tags else "完整"


def _metric_data_complete(*values: Any) -> bool:
    return all(value is not None and not (isinstance(value, float) and math.isnan(value)) for value in values)


def _daily_bars_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "amount"])
    aliases = {
        "time": ("time", "date", "日期"),
        "open": ("open", "开盘"),
        "high": ("high", "最高"),
        "low": ("low", "最低"),
        "close": ("close", "收盘"),
        "amount": ("amount", "成交额"),
        "vol": ("vol", "volume", "成交量"),
        "turnover_rate": ("turnover_rate", "换手率", "turnover"),
    }
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        item: dict[str, Any] = {}
        for target, keys in aliases.items():
            item[target] = _first_value(raw, *keys)
        normalized.append(item)
    df = pd.DataFrame(normalized)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for column in ("open", "high", "low", "close", "amount", "vol"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("time" if "time" in df.columns else "close")
    if "amount" not in df.columns:
        df["amount"] = 0.0
    return df.reset_index(drop=True)


def _value_percentile(series: pd.Series, value: float) -> float | None:
    values = [float(item) for item in series.dropna().tolist()]
    if not values:
        return None
    if len(values) == 1:
        return 100.0
    less_or_equal = sum(1 for item in values if item <= float(value))
    return (less_or_equal - 1) / (len(values) - 1) * 100


def _normalize_quote(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    normalized["close"] = _first_float(raw, "latest_price", "price", "close", "最新价", "现价", "收盘")
    normalized["pct_change"] = _first_float(raw, "pct_change", "change_pct", "涨跌幅", "涨幅")
    normalized["amount"] = _first_float(raw, "amount", "成交额", "当日成交额")
    normalized["turnover_rate"] = _first_float(raw, "turnover_rate", "换手率", "换手")
    normalized["main_net_inflow"] = _first_float(raw, "main_net_inflow", "主力净流入", "净流入")
    normalized["fund_flow_ratio"] = _first_float(raw, "fund_flow_ratio", "main_net_inflow_ratio", "主力净流入占比")
    return normalized


def _fund_flow_ratio(quote: dict[str, Any]) -> float | None:
    explicit = quote.get("fund_flow_ratio")
    if explicit is not None:
        return explicit
    inflow = quote.get("main_net_inflow")
    amount = quote.get("amount")
    if inflow is None or amount is None or amount == 0:
        return None
    return inflow / amount


def _find_sector_snapshot(snapshots: list[Any], sector: str) -> Any | None:
    token = sector.strip().lower()
    for snapshot in snapshots:
        if token in {str(snapshot.sector_name).lower(), str(snapshot.sector_code).lower()}:
            return snapshot
    for snapshot in snapshots:
        if token and (token in str(snapshot.sector_name).lower() or token in str(snapshot.sector_code).lower()):
            return snapshot
    return None


def _find_sector_heat_score(scored: list[dict[str, Any]], snapshot: Any | None, sector: str) -> float | None:
    token = sector.strip().lower()
    for row in scored:
        if snapshot and row.get("sector_code") == snapshot.sector_code:
            return row.get("sector_score")
        if token in {str(row.get("sector_name", "")).lower(), str(row.get("sector_code", "")).lower()}:
            return row.get("sector_score")
    return None


def _stock_code(raw: dict[str, Any]) -> str:
    value = _first_value(raw, "stock_code", "ts_code", "代码", "证券代码")
    if value is None:
        return ""
    return internal_ts_code(str(value).split(".", maxsplit=1)[0].strip())


def _stock_name(raw: dict[str, Any]) -> str:
    value = _first_value(raw, "stock_name", "name", "名称", "股票名称")
    return str(value).strip() if value is not None else ""


def _decision_row(stock_code: str, stock_name: str, decision: RiskFilterDecision) -> dict[str, Any]:
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "reasons": decision.reasons,
        "risk_tags": decision.risk_tags,
        "data_status": "完整" if decision.data_complete else "数据不完整",
    }


def _observe_row(metric: dict[str, Any], decision: RiskFilterDecision) -> dict[str, Any]:
    return {
        "stock_code": metric["stock_code"],
        "stock_name": metric["stock_name"],
        "reasons": decision.reasons,
        "risk_tags": _unique([*decision.risk_tags, *metric.get("risk_tags", [])]),
        "pct_change": metric.get("pct_change"),
        "amount_ratio": metric.get("amount_ratio"),
        "data_status": "完整" if decision.data_complete else "数据不完整",
    }


def _cumulative_return(close: pd.Series, lookback: int) -> float | None:
    if lookback <= 0 or len(close) < lookback + 1:
        return None
    base = float(close.iloc[-(lookback + 1)])
    latest = float(close.iloc[-1])
    if base <= 0 or math.isnan(base) or math.isnan(latest):
        return None
    return (latest - base) / base * 100.0


def _cumulative_penalty(value: float | None, thresholds: dict[str, Any], tags: list[str]) -> float:
    if value is None:
        return 0.0
    mild = float(thresholds.get("cum_return_mild_threshold", 8.0))
    severe = float(thresholds.get("cum_return_severe_threshold", 20.0))
    rate = float(thresholds.get("cum_return_penalty_rate", 0.5))
    max_penalty = float(thresholds.get("cum_return_max_penalty", 6.0))
    if value <= mild:
        return 0.0
    if value >= severe:
        penalty = max_penalty
    else:
        penalty = min(max_penalty, rate * (value - mild))
    if penalty > 0:
        tags.append("累计涨幅过大")
    return max(0.0, penalty)


def _ma(series: pd.Series, window: int) -> float | None:
    if len(series) < window:
        return None
    value = series.tail(window).mean()
    return float(value) if pd.notna(value) else None


def _assign_percentile(rows: list[dict[str, Any]], field: str, output: str) -> None:
    values = sorted(float(value) for value in (row.get(field) for row in rows) if value is not None)
    if not values:
        for row in rows:
            row[output] = None
        return
    if len(values) == 1:
        for row in rows:
            row[output] = 100.0 if row.get(field) is not None else None
        return
    for row in rows:
        value = row.get(field)
        if value is None:
            row[output] = None
            continue
        less_or_equal = sum(1 for item in values if item <= float(value))
        row[output] = (less_or_equal - 1) / (len(values) - 1) * 100


def _clear_rank_cache(provider: SectorDataProvider, trade_date: str) -> None:
    sources = getattr(provider, "industry_rank_sources", ("eastmoney_direct", "akshare_eastmoney", "akshare_ths_summary"))
    for source in sources:
        _delete_provider_cache(provider, f"industry_sector_rank:{source}:{trade_date}")


def _clear_stock_cache(provider: StockDataProvider, stock_code: str, trade_date: str, history_days: int) -> None:
    _delete_provider_cache(provider, f"stock_snapshot:{stock_code}:{trade_date}")
    _delete_provider_cache(provider, f"daily_bars:{stock_code}:{history_days}")


def _delete_provider_cache(provider: Any, key: str) -> None:
    cache = getattr(provider, "cache", None)
    delete = getattr(cache, "delete", None)
    if callable(delete):
        delete(key)


def _first_value(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _first_float(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        try:
            if value is not None and str(value).strip() != "":
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _provider_source(raw: dict[str, Any]) -> str:
    status = raw.get("_provider_status")
    return getattr(status, "source", "") if status is not None else ""


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', "_", value.strip())
    return cleaned.strip("_") or "sector"


def _short_error(exc: Exception, limit: int = 180) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))
