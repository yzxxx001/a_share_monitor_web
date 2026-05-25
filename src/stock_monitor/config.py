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
    amount_surge_ratio: float         # 成交额放大倍数触发阈值
    rsi_overbought: float             # RSI 超买风险提示阈值
    intraday_atr_multiplier: float    # 盘中单根K线涨跌幅 ≥ ATR/价格×N 时触发强提醒
    daily_return_alert_pct: float     # 日内累计涨跌幅阈值（触发收盘风险提醒）


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
class Settings:
    project_root: Path
    app: AppConfig
    paths: PathConfig
    market: MarketConfig
    rules: RuleConfig
    notifications: NotificationConfig
    web: WebConfig


def _resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


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
    )
