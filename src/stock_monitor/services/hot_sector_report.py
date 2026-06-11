from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings
from ..models import ProviderStatus, SectorSnapshot
from ..notifiers import SendResult, WeComNotifier
from ..providers.base import ProviderCallError
from ..providers.hot_sector import SectorDataProvider
from ..reports.hot_sector import build_hot_sector_html, build_hot_sector_markdown, build_hot_sector_wecom_summary

LOGGER = logging.getLogger(__name__)


NO_STRONG_SECTOR_MESSAGE = "今日未识别到满足条件的明显强势行业板块，本次不生成候选观察股清单。"


@dataclass(frozen=True)
class HotSectorReportResult:
    trade_date: str
    generated_at: datetime
    data_status: str
    provider_status: dict[str, Any]
    score_mode: str
    score_weights: dict[str, float]
    warnings: list[str]
    top_sectors: list[dict[str, Any]]
    markdown_path: Path
    json_path: Path
    html_path: Path
    board_type: str = "industry"
    notification: SendResult | None = None
    notification_skipped_reason: str | None = None


class HotSectorReportService:
    def __init__(self, settings: Settings, provider: SectorDataProvider, wecom: WeComNotifier | None = None) -> None:
        self.settings = settings
        self.provider = provider
        self.wecom = wecom or WeComNotifier(settings.notifications.wecom, settings.app.dry_run)
        self.zone = ZoneInfo(settings.app.timezone)

    def generate(self, trade_date: str, *, board_type: str = "industry", notify: bool = False, force_send: bool = False, force_refresh: bool = False) -> HotSectorReportResult:
        trade_date = normalize_trade_date(trade_date, self.zone)
        label = "概念" if board_type == "concept" else "行业"
        generated_at = datetime.now(self.zone)
        warnings: list[str] = []
        provider_failed = False
        if force_refresh:
            _clear_rank_cache(self.provider, trade_date, board_type)
        try:
            if board_type == "concept":
                snapshots = self.provider.get_concept_sector_rank(trade_date)
            else:
                snapshots = self.provider.get_industry_sector_rank(trade_date)
            provider_status = _merge_provider_status(snapshots)
        except ProviderCallError as exc:
            provider_failed = True
            snapshots = []
            provider_status = _provider_status_to_dict(exc.status)
            warnings.extend(exc.status.warnings)
            warnings.append(f"{label}板块数据源请求失败：{exc.status.error}")
        if not snapshots:
            warnings.append(f"{label}板块数据为空，本次报告不包含 Top 板块。")

        scored = score_sector_snapshots(snapshots, self.settings, warnings)
        top_sectors = scored[: self.settings.hot_sector_monitor.sector_scope.top_n_display]
        if not top_sectors:
            if provider_failed:
                warnings.append(f"{label}板块数据不可用，未生成热门{label}排名。")
            else:
                warnings.append(NO_STRONG_SECTOR_MESSAGE)
        else:
            self._enrich_five_day_relative(top_sectors, board_type, warnings)

        score_mode = _score_mode_from_warnings(warnings)
        data_status = "complete"
        if provider_failed:
            data_status = "data_unavailable"
        elif provider_status.get("is_stale"):
            data_status = "stale_cache"
        if warnings and not provider_failed:
            data_status = "degraded" if top_sectors else "no_qualified_sector"

        report_dir = self.settings.hot_sector_monitor.report.directory
        report_dir.mkdir(parents=True, exist_ok=True)
        file_stem = "concept_summary" if board_type == "concept" else "sector_summary"
        markdown_path = report_dir / f"{trade_date}_{file_stem}.md"
        json_path = report_dir / f"{trade_date}_{file_stem}.json"
        html_path = report_dir / f"{trade_date}_{file_stem}.html"

        result = HotSectorReportResult(
            trade_date=trade_date,
            generated_at=generated_at,
            data_status=data_status,
            provider_status=provider_status,
            score_mode=score_mode,
            score_weights=score_weights(score_mode),
            warnings=_unique(warnings),
            top_sectors=top_sectors,
            markdown_path=markdown_path,
            json_path=json_path,
            html_path=html_path,
            board_type=board_type,
        )
        markdown_path.write_text(build_hot_sector_markdown(result), encoding="utf-8")
        html_path.write_text(build_hot_sector_html(result), encoding="utf-8")
        json_path.write_text(json.dumps(result_to_json(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        notification = None
        skipped_reason = None
        if notify and self.settings.hot_sector_monitor.notification.enable_wecom_webhook:
            notification, skipped_reason = self._send_summary(result, force_send)
            result = HotSectorReportResult(
                trade_date=result.trade_date,
                generated_at=result.generated_at,
                data_status=result.data_status,
                provider_status=result.provider_status,
                score_mode=result.score_mode,
                score_weights=result.score_weights,
                warnings=result.warnings,
                top_sectors=result.top_sectors,
                markdown_path=result.markdown_path,
                json_path=result.json_path,
                html_path=result.html_path,
                board_type=result.board_type,
                notification=notification,
                notification_skipped_reason=skipped_reason,
            )
            json_path.write_text(json.dumps(result_to_json(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        LOGGER.info("热门%s板块报告已生成：%s；Top=%d。", label, markdown_path, len(top_sectors))
        return result

    def _send_summary(self, result: HotSectorReportResult, force_send: bool) -> tuple[SendResult | None, str | None]:
        if not result.top_sectors:
            return None, "无满足条件的行业板块，未发送摘要"
        marker = self._notification_marker(result.trade_date, result.board_type)
        if marker.exists() and not force_send and self.settings.hot_sector_monitor.notification.prevent_duplicate:
            return None, "同一交易日收盘热门板块摘要已发送，已拦截重复推送"
        summary = build_hot_sector_wecom_summary(result)
        send_result = self.wecom.send(summary)
        if send_result.sent or self.settings.app.dry_run:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(datetime.now(self.zone).isoformat(timespec="seconds"), encoding="utf-8")
        return send_result, None

    def _notification_marker(self, trade_date: str, board_type: str = "industry") -> Path:
        suffix = "concept" if board_type == "concept" else "hot_sector"
        return self.settings.hot_sector_monitor.report.directory / ".sent" / f"{trade_date}_{suffix}_close_summary.sent"

    def _enrich_five_day_relative(self, top_sectors: list[dict[str, Any]], board_type: str, warnings: list[str]) -> None:
        """用东方财富板块K线补全展示用的近5日相对表现；东方财富不可达时静默保持中性。

        仅更新展示字段，不重算综合评分（仅对 Top-N 取数，并入评分会扰乱百分位）。
        """
        getter = getattr(self.provider, "get_sector_five_day_metrics", None)
        if not callable(getter):
            return
        sectors = [(row.get("sector_name"), row.get("sector_code")) for row in top_sectors if row.get("sector_name")]
        if not sectors:
            return
        try:
            metrics = getter(sectors, board_type=board_type)
        except Exception as exc:  # best-effort：任何异常都退回中性降级
            LOGGER.info("近5日相对表现补全跳过（数据源不可用）：%s", exc)
            return
        if not metrics:
            return

        filled = 0
        for row in top_sectors:
            metric = metrics.get(row.get("sector_name"))
            if not metric:
                continue
            relative = metric.get("relative_5d")
            absolute = metric.get("return_5d")
            if relative is not None:
                row["relative_return_vs_benchmark"] = relative
                row["relative_return_label"] = "近5日相对表现"
                row["relative_return_neutral"] = False
            elif absolute is not None:
                row["relative_return_vs_benchmark"] = absolute
                row["relative_return_label"] = "近5日表现替代"
                row["relative_return_neutral"] = False
            else:
                continue
            row["return_5d"] = absolute
            tags = [t for t in (row.get("data_tags") or []) if not t.startswith("近5日相对缺失") and not t.startswith("相对表现降级")]
            if row["relative_return_label"] == "近5日表现替代":
                tags.append("相对表现降级:近5日表现")
            row["data_tags"] = tags or ["正常"]
            filled += 1

        if filled:
            warnings[:] = [w for w in warnings if "近5日相对基准指数超额表现暂不可稳定获取" not in w]
            warnings.append(f"近5日相对表现已通过东方财富板块K线补全 {filled} 个板块（基准：沪深300，仅用于展示，未并入综合评分）。")


def _clear_rank_cache(provider: SectorDataProvider, trade_date: str, board_type: str) -> None:
    """删除当日板块排名缓存，使本次生成被迫走实时抓取；抓取失败时将明确报错而非回退旧缓存。"""
    cache = getattr(provider, "cache", None)
    delete = getattr(cache, "delete", None)
    if not callable(delete):
        return
    if board_type == "concept":
        sources = getattr(provider, "concept_rank_sources", ("eastmoney_direct", "akshare_eastmoney", "ths_concept"))
    else:
        sources = getattr(provider, "industry_rank_sources", ("eastmoney_direct", "akshare_eastmoney", "akshare_ths_summary"))
    for source in sources:
        delete(f"{board_type}_sector_rank:{source}:{trade_date}")


def normalize_trade_date(value: str, zone: ZoneInfo) -> str:
    token = value.strip().lower()
    if token == "today":
        return datetime.now(zone).strftime("%Y%m%d")
    compact = token.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError("日期格式应为 today、YYYYMMDD 或 YYYY-MM-DD")
    return compact


def score_sector_snapshots(snapshots: list[SectorSnapshot], settings: Settings, warnings: list[str]) -> list[dict[str, Any]]:
    rows = [_normalize_snapshot(snapshot) for snapshot in snapshots]
    if not rows:
        return []
    if not any(row["pct_change"] is not None for row in rows):
        warnings.append("行业板块数据缺少涨跌幅字段，无法计算热门板块排名。")
        return []
    up_ratio_available = any(row["up_stock_ratio"] is not None for row in rows)
    if not up_ratio_available:
        warnings.append("板块数据缺少上涨覆盖率字段（如概念资金流源仅有公司家数），已跳过上涨覆盖率过滤，仅按涨跌幅入选。")

    if not any(row["main_net_inflow_ratio"] is not None for row in rows):
        fund_flow_available = False
        warnings.append("资金流数据未完整获取，本次板块评分采用降级规则。")
    else:
        fund_flow_available = True
        if any(row["main_net_inflow_ratio"] is None for row in rows):
            warnings.append("部分板块资金流字段缺失，缺失项按最低分处理。")

    for row in rows:
        if row["activity_ratio"] is not None:
            row["activity_value"] = row["activity_ratio"]
            row["activity_label"] = "成交活跃度"
        elif row["turnover_rate"] is not None:
            row["activity_value"] = row["turnover_rate"]
            row["activity_label"] = "换手率"
        elif row["amount"] is not None:
            row["activity_value"] = row["amount"]
            row["activity_label"] = "当日成交额"
        else:
            row["activity_value"] = None
            row["activity_label"] = "缺失"
    if not all(row["activity_ratio"] is not None for row in rows):
        if any(row["amount"] is not None for row in rows):
            warnings.append("板块近20日成交额数据未稳定获取，成交活跃度降级为当日成交额百分位。")
        else:
            warnings.append("板块近20日成交额数据未稳定获取，成交活跃度降级为换手率百分位。")

    if not any(row["relative_return_vs_benchmark"] is not None for row in rows):
        if any(row["return_5d"] is not None for row in rows):
            warnings.append("近5日相对基准指数超额表现暂不可稳定获取，已降级使用板块近5日表现。")
            for row in rows:
                row["relative_return_vs_benchmark"] = row["return_5d"]
                row["relative_return_label"] = "近5日表现替代"
                row["relative_return_neutral"] = False
        else:
            warnings.append("近5日相对基准指数超额表现暂不可稳定获取，持续性评分采用中性分。")
            for row in rows:
                row["relative_return_vs_benchmark"] = 0.0
                row["relative_return_label"] = "缺失-中性分"
                row["relative_return_neutral"] = True
    else:
        for row in rows:
            row["relative_return_label"] = "近5日相对表现"
            row["relative_return_neutral"] = False

    _assign_percentile(rows, "pct_change", "pct_change_pctile")
    _assign_percentile(rows, "up_stock_ratio", "up_stock_ratio_pctile")
    _assign_percentile(rows, "activity_value", "activity_pctile")
    _assign_percentile(rows, "main_net_inflow_ratio", "fund_flow_pctile")
    _assign_percentile(rows, "relative_return_vs_benchmark", "relative_return_pctile")
    for row in rows:
        if row.get("relative_return_neutral"):
            row["relative_return_pctile"] = 50.0

    weights = score_weights("full" if fund_flow_available else "degraded_no_fund_flow")
    min_pct_change = settings.hot_sector_monitor.sector_filter.min_pct_change
    min_up_stock_ratio = settings.hot_sector_monitor.sector_filter.min_up_stock_ratio
    scored: list[dict[str, Any]] = []
    for row in rows:
        if row["pct_change"] is None:
            continue
        if up_ratio_available and row["up_stock_ratio"] is None:
            continue
        if row["pct_change"] <= min_pct_change:
            continue
        if up_ratio_available and row["up_stock_ratio"] < min_up_stock_ratio:
            continue
        components = {
            "pct_change": row["pct_change_pctile"],
            "up_stock_ratio": row["up_stock_ratio_pctile"],
            "activity": row["activity_pctile"],
            "relative_return": row["relative_return_pctile"],
        }
        if fund_flow_available:
            components["fund_flow"] = row["fund_flow_pctile"]
        score = sum(components[key] * weights[key] for key in weights)
        row.update(
            rank=0,
            sector_score=round(score, 2),
            score_components={key: round(value, 2) for key, value in components.items()},
            score_weights=weights,
            data_tags=_data_tags(row, fund_flow_available),
        )
        scored.append(row)

    scored.sort(key=lambda item: item["sector_score"], reverse=True)
    for index, row in enumerate(scored, start=1):
        row["rank"] = index
    return scored


def score_weights(score_mode: str) -> dict[str, float]:
    if score_mode == "degraded_no_fund_flow":
        return {"pct_change": 0.35, "up_stock_ratio": 0.30, "activity": 0.20, "relative_return": 0.15}
    return {"pct_change": 0.25, "up_stock_ratio": 0.25, "activity": 0.20, "fund_flow": 0.15, "relative_return": 0.15}


def result_to_json(result: HotSectorReportResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["generated_at"] = result.generated_at.isoformat(timespec="seconds")
    payload["markdown_path"] = str(result.markdown_path)
    payload["json_path"] = str(result.json_path)
    payload["html_path"] = str(result.html_path)
    if result.notification is not None:
        payload["notification"] = asdict(result.notification)
    return payload


def _normalize_snapshot(snapshot: SectorSnapshot) -> dict[str, Any]:
    raw = snapshot.raw or {}
    up_count = _first_int(raw, "up_stock_count", "上涨家数", "上涨数", default=snapshot.up_count)
    down_count = _first_int(raw, "down_stock_count", "下跌家数", "下跌数", default=snapshot.down_count)
    total_count = _first_int(raw, "total_stock_count", "成分股数量", "总家数")
    if total_count is None and up_count is not None and down_count is not None:
        total_count = up_count + down_count
    up_ratio = _first_float(raw, "up_stock_ratio", "上涨覆盖率")
    if up_ratio is None and up_count is not None and total_count:
        up_ratio = up_count / total_count
    return {
        "sector_code": snapshot.sector_code,
        "sector_name": snapshot.sector_name,
        "trade_date": snapshot.trade_date,
        "pct_change": _first_float(raw, "pct_change", "change_pct", "涨跌幅", default=snapshot.change_pct),
        "amount": _first_float(raw, "amount", "总成交额", "成交额", default=snapshot.amount),
        "turnover_rate": _first_float(raw, "turnover_rate", "换手率", default=snapshot.turnover),
        "up_stock_count": up_count,
        "down_stock_count": down_count,
        "total_stock_count": total_count,
        "up_stock_ratio": up_ratio,
        "main_net_inflow": _first_float(raw, "main_net_inflow", "主力净流入", "净流入", default=snapshot.net_inflow),
        "main_net_inflow_ratio": _computed_inflow_ratio(raw, snapshot),
        "main_net_inflow_ratio_estimated": _is_inflow_ratio_estimated(raw, snapshot),
        "return_3d": _first_float(raw, "return_3d", "近3日涨跌幅"),
        "return_5d": _first_float(raw, "return_5d", "近5日涨跌幅"),
        "relative_return_vs_benchmark": _first_float(raw, "relative_return_vs_benchmark", "近5日相对表现"),
        "activity_ratio": _first_float(raw, "activity_ratio", "成交活跃度"),
        "provider_status": _provider_status_to_dict(snapshot.provider_status),
        "data_timestamp": (snapshot.data_time.isoformat(timespec="seconds") if snapshot.data_time else None),
        "source": snapshot.source,
        "warnings": list(snapshot.warnings),
    }


def _assign_percentile(rows: list[dict[str, Any]], field: str, output: str) -> None:
    values = sorted(value for value in (row.get(field) for row in rows) if value is not None)
    if not values:
        for row in rows:
            row[output] = 50.0
        return
    if len(values) == 1:
        for row in rows:
            row[output] = 100.0 if row.get(field) is not None else 0.0
        return
    for row in rows:
        value = row.get(field)
        if value is None:
            row[output] = 0.0
            continue
        less_or_equal = sum(1 for item in values if item <= value)
        row[output] = (less_or_equal - 1) / (len(values) - 1) * 100


def _data_tags(row: dict[str, Any], fund_flow_available: bool) -> list[str]:
    tags: list[str] = []
    status = row.get("provider_status") or {}
    if status.get("is_cached"):
        tags.append("缓存数据")
    if status.get("is_stale"):
        tags.append("陈旧缓存")
    if not fund_flow_available or row.get("main_net_inflow_ratio") is None:
        tags.append("资金流缺失")
    elif row.get("main_net_inflow_ratio_estimated"):
        tags.append("资金流占比估算")
    if row.get("activity_ratio") is None:
        tags.append(f"活跃度降级:{row.get('activity_label', '缺失')}")
    if row.get("relative_return_neutral"):
        tags.append("近5日相对缺失:中性分")
    elif row.get("relative_return_label") == "近5日表现替代":
        tags.append("相对表现降级:近5日表现")
    return tags or ["正常"]


def _merge_provider_status(snapshots: list[SectorSnapshot]) -> dict[str, Any]:
    statuses = [snapshot.provider_status for snapshot in snapshots if snapshot.provider_status is not None]
    if not statuses:
        return {"ok": bool(snapshots), "items": len(snapshots)}
    return {
        "ok": all(status.ok for status in statuses),
        "items": len(snapshots),
        "provider": statuses[0].provider,
        "source": statuses[0].source,
        "is_cached": any(status.is_cached for status in statuses),
        "is_stale": any(status.is_stale for status in statuses),
        "cached_at": next((status.cached_at.isoformat(timespec="seconds") for status in statuses if status.cached_at), None),
        "warnings": _unique([warning for status in statuses for warning in status.warnings]),
    }


def _provider_status_to_dict(status: ProviderStatus | None) -> dict[str, Any]:
    if status is None:
        return {}
    payload = asdict(status)
    if status.cached_at:
        payload["cached_at"] = status.cached_at.isoformat(timespec="seconds")
    if status.requested_at:
        payload["requested_at"] = status.requested_at.isoformat(timespec="seconds")
    return payload


def _score_mode_from_warnings(warnings: list[str]) -> str:
    if any("资金流数据未完整获取" in warning for warning in warnings):
        return "degraded_no_fund_flow"
    if any("降级" in warning or "暂不可稳定获取" in warning for warning in warnings):
        return "degraded_partial"
    return "full"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _first_float(raw: dict[str, Any], *keys: str, default: float | None = None) -> float | None:
    for key in keys:
        value = raw.get(key)
        try:
            if value is not None and str(value).strip() != "":
                return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _first_int(raw: dict[str, Any], *keys: str, default: int | None = None) -> int | None:
    value = _first_float(raw, *keys, default=None if default is None else float(default))
    return None if value is None else int(value)


def _computed_inflow_ratio(raw: dict[str, Any], snapshot: SectorSnapshot) -> float | None:
    explicit = _first_float(raw, "main_net_inflow_ratio", "主力净流入占比")
    if explicit is not None:
        return explicit
    inflow = _first_float(raw, "main_net_inflow", "主力净流入", "净流入", default=snapshot.net_inflow)
    amount = _first_float(raw, "amount", "总成交额", "成交额", default=snapshot.amount)
    if inflow is None or amount is None or amount == 0:
        return None
    return inflow / amount


def _is_inflow_ratio_estimated(raw: dict[str, Any], snapshot: SectorSnapshot) -> bool:
    explicit = _first_float(raw, "main_net_inflow_ratio", "主力净流入占比")
    if explicit is not None:
        return False
    return _computed_inflow_ratio(raw, snapshot) is not None
