from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    name: str
    timezone: str
    schedule_minutes: int
    run_delay_seconds: int
    only_trading_session: bool
    dry_run: bool
    log_level: str


@dataclass(frozen=True)
class PathConfig:
    positions_file: Path
    sqlite_file: Path
    log_file: Path


@dataclass(frozen=True)
class MarketConfig:
    provider: str
    frequency: str
    period: str
    adjust: str
    cached_bars: int
    primary_source: str
    fallback_source: str
    retry_times: int
    retry_backoff_seconds: float
    bypass_proxy_for_market_hosts: bool


@dataclass(frozen=True)
class RuleConfig:
    minimum_bars_for_trend: int
    minimum_bars_for_breakout: int
    volume_spike_ratio: float
    alert_cooldown_minutes: int
    sms_levels: tuple[str, ...]
    amount_surge_ratio: float = 3.0         # 成交额放大倍数触发阈值
    rsi_overbought: float = 80.0            # RSI 超买风险提示阈值
    intraday_atr_multiplier: float = 1.0    # 盘中单根K线涨跌幅 ≥ ATR/价格×N 时触发强提醒
    daily_return_alert_pct: float = 0.05    # 日内累计涨跌幅阈值（触发收盘风险提醒）


@dataclass(frozen=True)
class WeComConfig:
    enabled: bool
    webhook_env: str
    msg_type: str
    max_markdown_chars: int


@dataclass(frozen=True)
class AliyunSmsConfig:
    enabled: bool
    access_key_id_env: str
    access_key_secret_env: str
    endpoint: str
    phone_numbers: str
    sign_name: str
    template_code: str


@dataclass(frozen=True)
class NotificationConfig:
    wecom: WeComConfig
    aliyun_sms: AliyunSmsConfig


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    auto_start_scheduler: bool
    access_token_env: str
    secret_key_env: str


@dataclass(frozen=True)
class HotSectorScopeConfig:
    types: tuple[str, ...]
    top_n_display: int
    top_n_notify: int


@dataclass(frozen=True)
class HotSectorScheduleConfig:
    close_report_time: str
    intraday_scan_enabled: bool
    intraday_interval_minutes: int


@dataclass(frozen=True)
class HotSectorCandidateConfig:
    max_count_per_sector: int
    default_strategy: str
    available_strategies: tuple[str, ...]


@dataclass(frozen=True)
class HotSectorRiskFilterConfig:
    exclude_st: bool
    exclude_suspended: bool
    exclude_delisting_risk: bool
    exclude_recent_ipo_trading_days: int
    severe_risk_announcement_days: int
    sealed_limit_up_as_observe_only: bool


@dataclass(frozen=True)
class HotSectorFilterConfig:
    min_pct_change: float
    min_up_stock_ratio: float


@dataclass(frozen=True)
class HotSectorNotificationConfig:
    enable_wecom_webhook: bool
    send_close_summary: bool
    enable_intraday_sector_alert: bool
    cooldown_minutes: int
    prevent_duplicate: bool


@dataclass(frozen=True)
class HotSectorHttpConfig:
    timeout_seconds: float
    retry_count: int
    retry_backoff_seconds: tuple[float, ...]


@dataclass(frozen=True)
class HotSectorCacheConfig:
    enabled: bool
    directory: Path


@dataclass(frozen=True)
class HotSectorReportConfig:
    directory: Path


@dataclass(frozen=True)
class HotSectorMonitorConfig:
    enabled: bool
    sector_scope: HotSectorScopeConfig
    schedule: HotSectorScheduleConfig
    candidate: HotSectorCandidateConfig
    common_risk_filter: HotSectorRiskFilterConfig
    strategy_configs: dict[str, Any]
    sector_filter: HotSectorFilterConfig
    notification: HotSectorNotificationConfig
    http: HotSectorHttpConfig
    cache: HotSectorCacheConfig
    report: HotSectorReportConfig


@dataclass(frozen=True)
class Settings:
    project_root: Path
    app: AppConfig
    paths: PathConfig
    market: MarketConfig
    rules: RuleConfig
    notifications: NotificationConfig
    web: WebConfig
    hot_sector_monitor: HotSectorMonitorConfig


def _resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _str_tuple(value: Any, default: list[str]) -> tuple[str, ...]:
    items = value if isinstance(value, list | tuple) else default
    return tuple(str(item) for item in items)


def _float_tuple(value: Any, default: list[float]) -> tuple[float, ...]:
    items = value if isinstance(value, list | tuple) else default
    return tuple(float(item) for item in items)


def _default_strategy_configs() -> dict[str, Any]:
    return {
        "short_term_resonance": {
            "display_name": "短线热点共振",
            "history_days": 130,
            "max_candidates": 10,
            "weights": {
                "relative_strength": 0.20,
                "amount_expansion": 0.20,
                "trend_structure": 0.15,
                "turnover_activity": 0.10,
                "rsi_atr_risk": 0.10,
                "fund_flow": 0.10,
                "risk_control": 0.15,
            },
            "degraded_weights_no_fund_flow": {
                "relative_strength": 0.23,
                "amount_expansion": 0.22,
                "trend_structure": 0.17,
                "turnover_activity": 0.11,
                "rsi_atr_risk": 0.11,
                "risk_control": 0.16,
            },
            "thresholds": {
                "relative_return_weak_below": -2.0,
                "relative_return_sync_low": -1.0,
                "relative_return_sync_high": 3.0,
                "relative_return_extreme_above": 7.0,
                "amount_ratio_active": 1.0,
                "amount_ratio_strong": 2.0,
                "amount_ratio_extreme": 4.0,
                "rsi_oversold": 30.0,
                "rsi_hot": 70.0,
                "rsi_extreme_hot": 80.0,
                "atr_high_percentile": 80.0,
                "turnover_high_percentile": 85.0,
                "limit_up_pct": 9.8,
            },
            "risk_penalties": {
                "risk_check_incomplete": 15.0,
                "announcement_incomplete": 10.0,
                "abnormal_volatility": 8.0,
                "lhb": 5.0,
                "major_reduction": 10.0,
                "unlock": 10.0,
                "rsi_extreme_hot": 12.0,
                "extreme_amount": 8.0,
                "high_volatility": 8.0,
                "chasing_risk": 8.0,
            },
        }
    }


def load_settings(config_file: str | Path) -> Settings:
    config_path = Path(config_file).resolve()
    project_root = config_path.parent.parent
    with config_path.open("r", encoding="utf-8") as fp:
        raw: dict[str, Any] = yaml.safe_load(fp) or {}

    app = raw.get("app", {})
    paths = raw.get("paths", {})
    market = raw.get("market", {})
    rules = raw.get("rules", {})
    notifications = raw.get("notifications", {})
    wecom = notifications.get("wecom", {})
    sms = notifications.get("aliyun_sms", {})
    web = raw.get("web", {})
    hot_sector = raw.get("hot_sector_monitor", {})
    sector_scope = hot_sector.get("sector_scope", {})
    sector_schedule = hot_sector.get("schedule", {})
    sector_candidate = hot_sector.get("candidate", {})
    sector_risk = hot_sector.get("common_risk_filter", {})
    strategy_configs = hot_sector.get("strategy_configs") or _default_strategy_configs()
    sector_filter = hot_sector.get("sector_filter", {})
    sector_notification = hot_sector.get("notification", {})
    sector_http = hot_sector.get("http", {})
    sector_cache = hot_sector.get("cache", {})
    sector_report = hot_sector.get("report", {})

    return Settings(
        project_root=project_root,
        app=AppConfig(
            name=str(app.get("name", "A股监控")),
            timezone=str(app.get("timezone", "Asia/Shanghai")),
            schedule_minutes=int(app.get("schedule_minutes", 5)),
            run_delay_seconds=int(app.get("run_delay_seconds", 20)),
            only_trading_session=bool(app.get("only_trading_session", True)),
            dry_run=bool(app.get("dry_run", True)),
            log_level=str(app.get("log_level", "INFO")),
        ),
        paths=PathConfig(
            positions_file=_resolve_path(project_root, str(paths.get("positions_file", "data/positions_template.xlsx"))),
            sqlite_file=_resolve_path(project_root, str(paths.get("sqlite_file", "state/stock_monitor.db"))),
            log_file=_resolve_path(project_root, str(paths.get("log_file", "logs/stock_monitor.log"))),
        ),
        market=MarketConfig(
            provider=str(market.get("provider", "akshare_dual_source")),
            frequency=str(market.get("frequency", "5分钟")),
            period=str(market.get("period", "5")),
            adjust=str(market.get("adjust", "")),
            cached_bars=int(market.get("cached_bars", 80)),
            primary_source=str(market.get("primary_source", "sina")),
            fallback_source=str(market.get("fallback_source", "eastmoney")),
            retry_times=int(market.get("retry_times", 2)),
            retry_backoff_seconds=float(market.get("retry_backoff_seconds", 0.8)),
            bypass_proxy_for_market_hosts=bool(market.get("bypass_proxy_for_market_hosts", True)),
        ),
        rules=RuleConfig(
            minimum_bars_for_trend=int(rules.get("minimum_bars_for_trend", 20)),
            minimum_bars_for_breakout=int(rules.get("minimum_bars_for_breakout", 21)),
            volume_spike_ratio=float(rules.get("volume_spike_ratio", 1.5)),
            alert_cooldown_minutes=int(rules.get("alert_cooldown_minutes", 30)),
            sms_levels=tuple(str(level).upper() for level in rules.get("sms_levels", ["HIGH"])),
            amount_surge_ratio=float(rules.get("amount_surge_ratio", 3.0)),
            rsi_overbought=float(rules.get("rsi_overbought", 80.0)),
            intraday_atr_multiplier=float(rules.get("intraday_atr_multiplier", 1.0)),
            daily_return_alert_pct=float(rules.get("daily_return_alert_pct", 0.05)),
        ),
        notifications=NotificationConfig(
            wecom=WeComConfig(
                enabled=bool(wecom.get("enabled", True)),
                webhook_env=str(wecom.get("webhook_env", "WECOM_BOT_WEBHOOK")),
                msg_type=str(wecom.get("msg_type", "markdown")).lower(),
                max_markdown_chars=int(wecom.get("max_markdown_chars", 4000)),
            ),
            aliyun_sms=AliyunSmsConfig(
                enabled=bool(sms.get("enabled", True)),
                access_key_id_env=str(sms.get("access_key_id_env", "ALIBABA_CLOUD_ACCESS_KEY_ID")),
                access_key_secret_env=str(sms.get("access_key_secret_env", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")),
                endpoint=str(sms.get("endpoint", "dysmsapi.aliyuncs.com")),
                phone_numbers=str(sms.get("phone_numbers", "")),
                sign_name=str(sms.get("sign_name", "")),
                template_code=str(sms.get("template_code", "")),
            ),
        ),
        web=WebConfig(
            host=str(web.get("host", "127.0.0.1")),
            port=int(web.get("port", 8501)),
            auto_start_scheduler=bool(web.get("auto_start_scheduler", True)),
            access_token_env=str(web.get("access_token_env", "MONITOR_WEB_ACCESS_TOKEN")),
            secret_key_env=str(web.get("secret_key_env", "MONITOR_WEB_SECRET_KEY")),
        ),
        hot_sector_monitor=HotSectorMonitorConfig(
            enabled=bool(hot_sector.get("enabled", True)),
            sector_scope=HotSectorScopeConfig(
                types=_str_tuple(sector_scope.get("types"), ["industry"]),
                top_n_display=int(sector_scope.get("top_n_display", 10)),
                top_n_notify=int(sector_scope.get("top_n_notify", 3)),
            ),
            schedule=HotSectorScheduleConfig(
                close_report_time=str(sector_schedule.get("close_report_time", "15:10")),
                intraday_scan_enabled=bool(sector_schedule.get("intraday_scan_enabled", False)),
                intraday_interval_minutes=int(sector_schedule.get("intraday_interval_minutes", 5)),
            ),
            candidate=HotSectorCandidateConfig(
                max_count_per_sector=int(sector_candidate.get("max_count_per_sector", 10)),
                default_strategy=str(sector_candidate.get("default_strategy", "short_term_resonance")),
                available_strategies=_str_tuple(
                    sector_candidate.get("available_strategies"),
                    ["short_term_resonance", "medium_term_quality_value"],
                ),
            ),
            common_risk_filter=HotSectorRiskFilterConfig(
                exclude_st=bool(sector_risk.get("exclude_st", True)),
                exclude_suspended=bool(sector_risk.get("exclude_suspended", True)),
                exclude_delisting_risk=bool(sector_risk.get("exclude_delisting_risk", True)),
                exclude_recent_ipo_trading_days=int(sector_risk.get("exclude_recent_ipo_trading_days", 60)),
                severe_risk_announcement_days=int(sector_risk.get("severe_risk_announcement_days", 30)),
                sealed_limit_up_as_observe_only=bool(sector_risk.get("sealed_limit_up_as_observe_only", True)),
            ),
            strategy_configs=dict(strategy_configs),
            sector_filter=HotSectorFilterConfig(
                min_pct_change=float(sector_filter.get("min_pct_change", 0.0)),
                min_up_stock_ratio=float(sector_filter.get("min_up_stock_ratio", 0.55)),
            ),
            notification=HotSectorNotificationConfig(
                enable_wecom_webhook=bool(sector_notification.get("enable_wecom_webhook", True)),
                send_close_summary=bool(sector_notification.get("send_close_summary", True)),
                enable_intraday_sector_alert=bool(sector_notification.get("enable_intraday_sector_alert", False)),
                cooldown_minutes=int(sector_notification.get("cooldown_minutes", 30)),
                prevent_duplicate=bool(sector_notification.get("prevent_duplicate", True)),
            ),
            http=HotSectorHttpConfig(
                timeout_seconds=float(sector_http.get("timeout_seconds", 10)),
                retry_count=int(sector_http.get("retry_count", 3)),
                retry_backoff_seconds=_float_tuple(sector_http.get("retry_backoff_seconds"), [1, 3, 8]),
            ),
            cache=HotSectorCacheConfig(
                enabled=bool(sector_cache.get("enabled", True)),
                directory=_resolve_path(project_root, str(sector_cache.get("directory", "./cache/hot_sector"))),
            ),
            report=HotSectorReportConfig(
                directory=_resolve_path(project_root, str(sector_report.get("directory", "./reports/hot_sector"))),
            ),
        ),
    )
