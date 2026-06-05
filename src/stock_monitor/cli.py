from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from .config import load_settings
from .notifiers import AliyunSmsNotifier, WeComNotifier
from .positions import load_positions
from .runner import MonitorService, build_service, configure_logging

LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG = "config/config.yaml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A股 5 分钟仓位监控、网页面板与企业微信/短信提醒")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验 Excel 持仓模板是否可读取")
    validate.add_argument("--config", default=DEFAULT_CONFIG)

    imp = sub.add_parser("import-excel", help="将 Excel 中启用的持仓导入网页监控数据库")
    imp.add_argument("--config", default=DEFAULT_CONFIG)

    sync = sub.add_parser("sync-stock-master", help="通过 AKShare 同步股票基础名称库")
    sync.add_argument("--config", default=DEFAULT_CONFIG)

    once = sub.add_parser("once", help="执行一次监控")
    once.add_argument("--config", default=DEFAULT_CONFIG)
    once.add_argument("--force-session", action="store_true", help="忽略交易时段限制，用于联调")

    loop = sub.add_parser("loop", help="按照配置每 5 分钟持续运行，不启动网页")
    loop.add_argument("--config", default=DEFAULT_CONFIG)
    loop.add_argument("--run-now", action="store_true", help="启动后立即检查一次")
    loop.add_argument("--force-session", action="store_true", help="忽略交易时段限制，用于联调")

    web = sub.add_parser("web", help="启动浏览器监控界面，并按配置启动后台定时监控")
    web.add_argument("--config", default=DEFAULT_CONFIG)
    web.add_argument("--host", default=None, help="覆盖配置中的监听地址")
    web.add_argument("--port", default=None, type=int, help="覆盖配置中的端口")
    web.add_argument("--no-scheduler", action="store_true", help="只启用网页，不启动后台定时任务")

    notify = sub.add_parser("notify-test", help="测试通知通道；受 dry_run 配置保护")
    notify.add_argument("--config", default=DEFAULT_CONFIG)
    notify.add_argument("--channel", choices=["wecom", "sms", "both"], default="both")

    hot_sector = sub.add_parser("hot-sector", help="生成收盘后热门行业板块 Top 10 报告")
    hot_sector.add_argument("--config", default=DEFAULT_CONFIG)
    hot_sector.add_argument("--date", default="today", help="交易日期：today、YYYYMMDD 或 YYYY-MM-DD")
    notify_group = hot_sector.add_mutually_exclusive_group()
    notify_group.add_argument("--notify", action="store_true", help="推送企业微信 Top 3 摘要")
    notify_group.add_argument("--no-notify", action="store_true", help="只生成报告，不推送")
    hot_sector.add_argument("--force-send", action="store_true", help="忽略同日通知去重，用于测试")
    hot_sector_sub = hot_sector.add_subparsers(dest="hot_sector_action")
    candidates = hot_sector_sub.add_parser("candidates", help="生成热门行业板块内短线候选观察股")
    candidates.add_argument("--date", default="today", help="交易日期：today、YYYYMMDD 或 YYYY-MM-DD")
    candidates.add_argument("--sector", required=True, help="板块名称或代码，例如：电网设备")
    candidates.add_argument("--strategy", default="short_term_resonance", help="候选策略，默认 short_term_resonance")
    candidate_notify = candidates.add_mutually_exclusive_group()
    candidate_notify.add_argument("--notify", action="store_true", help="推送候选观察摘要")
    candidate_notify.add_argument("--no-notify", action="store_true", help="只生成报告，不推送")
    candidates.add_argument("--force-refresh", action="store_true", help="强制刷新本次板块、成分股与个股缓存")
    return parser


def _load_for_non_market_commands(config_path: str):
    path = Path(config_path).resolve()
    load_dotenv(path.parent.parent / ".env")
    settings = load_settings(path)
    configure_logging(settings)
    return settings


def _validate(config_path: str) -> int:
    settings = _load_for_non_market_commands(config_path)
    positions, warnings = load_positions(settings.paths.positions_file)
    for warning in warnings:
        LOGGER.warning(warning)
    LOGGER.info("模板读取成功：Excel 中有效启用标的 %d 个。股票代码为空时为 0 个，属于正常状态。", len(positions))
    return 0


def _notify_test(config_path: str, channel: str) -> int:
    settings = _load_for_non_market_commands(config_path)
    message = "### A股监控通知测试\n> 企业微信通知通道配置测试\n> 当前为测试消息，不包含交易信号。"
    if channel in {"wecom", "both"}:
        result = WeComNotifier(settings.notifications.wecom, settings.app.dry_run).send(message)
        LOGGER.info("企业微信测试：%s", result.detail)
    if channel in {"sms", "both"}:
        result = AliyunSmsNotifier(settings.notifications.aliyun_sms, settings.app.dry_run).send(
            {"stock": "测试标的", "signal": "通道测试", "price": "0.00"}
        )
        LOGGER.info("短信测试：%s", result.detail)
    return 0


def _hot_sector(config_path: str, trade_date: str, notify: bool, force_send: bool) -> int:
    from .providers.hot_sector import AKShareHotSectorProvider
    from .services.hot_sector_report import HotSectorReportService

    settings = _load_for_non_market_commands(config_path)
    provider = AKShareHotSectorProvider(settings.hot_sector_monitor.http, settings.hot_sector_monitor.cache)
    result = HotSectorReportService(settings, provider).generate(trade_date, notify=notify, force_send=force_send)
    LOGGER.info("热门行业板块报告 Markdown：%s", result.markdown_path)
    LOGGER.info("热门行业板块报告 JSON：%s", result.json_path)
    if result.notification:
        LOGGER.info("企业微信摘要：%s", result.notification.detail)
    if result.notification_skipped_reason:
        LOGGER.info("企业微信摘要未发送：%s", result.notification_skipped_reason)
    return 0


def _hot_sector_candidates(
    config_path: str,
    trade_date: str,
    sector: str,
    strategy: str,
    notify: bool,
    force_refresh: bool,
) -> int:
    from .providers.hot_sector import AKShareHotSectorProvider, RiskEventProvider
    from .services.hot_sector_candidates import HotSectorCandidateService

    settings = _load_for_non_market_commands(config_path)
    provider = AKShareHotSectorProvider(settings.hot_sector_monitor.http, settings.hot_sector_monitor.cache)
    result = HotSectorCandidateService(settings, provider, provider, RiskEventProvider()).generate(
        trade_date,
        sector=sector,
        strategy=strategy,
        notify=notify,
        force_refresh=force_refresh,
    )
    LOGGER.info("短线热点共振候选报告 Markdown：%s", result.markdown_path)
    LOGGER.info("短线热点共振候选报告 JSON：%s", result.json_path)
    LOGGER.info("短线热点共振候选报告 HTML：%s", result.html_path)
    if result.notification:
        LOGGER.info("候选摘要推送：%s", result.notification.detail)
    return 0


def _run_loop(service: MonitorService, run_now: bool, force_session: bool) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    if run_now:
        summary = service.run_once(force_session=force_session)
        LOGGER.info(summary.message)
    settings = service.settings
    scheduler = BlockingScheduler(timezone=settings.app.timezone)
    trigger = CronTrigger(
        minute=f"*/{settings.app.schedule_minutes}",
        second=settings.app.run_delay_seconds,
        timezone=settings.app.timezone,
    )
    scheduler.add_job(
        lambda: service.run_once(force_session=force_session), trigger=trigger,
        id="stock_monitor", max_instances=1, coalesce=True, replace_existing=True,
    )
    LOGGER.info(
        "定时监控已启动：每 %d 分钟、K线结束后 %d 秒执行一次；dry_run=%s。",
        settings.app.schedule_minutes, settings.app.run_delay_seconds, settings.app.dry_run,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        LOGGER.info("监控已停止。")
    return 0


def _run_web(config_path: str, host_override: str | None, port_override: int | None, no_scheduler: bool) -> int:
    from waitress import serve

    from .webapp import create_app

    app = create_app(config_path, scheduler_enabled=(False if no_scheduler else None))
    settings = app.config["MONITOR_SETTINGS"]
    host = host_override or settings.web.host
    port = port_override or settings.web.port
    LOGGER.info("网页监控界面已启动：http://%s:%d；后台定时监控=%s。", host, port, not no_scheduler)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        LOGGER.warning("当前界面已允许局域网访问，请在 .env 配置 MONITOR_WEB_ACCESS_TOKEN，且勿直接暴露到公网。")
    serve(app, host=host, port=port, threads=4)
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "validate":
        return _validate(args.config)
    if args.command == "notify-test":
        return _notify_test(args.config, args.channel)
    if args.command == "hot-sector":
        if getattr(args, "hot_sector_action", None) == "candidates":
            return _hot_sector_candidates(
                args.config,
                args.date,
                args.sector,
                args.strategy,
                args.notify,
                args.force_refresh,
            )
        return _hot_sector(args.config, args.date, args.notify, args.force_send)
    service = build_service(args.config)
    if args.command == "import-excel":
        count, warnings = service.import_excel_positions()
        for warning in warnings:
            LOGGER.warning(warning)
        LOGGER.info("已导入或更新 %d 个 Excel 持仓到网页监控数据库。", count)
        return 0
    if args.command == "sync-stock-master":
        count = service.sync_stock_master()
        LOGGER.info("已同步股票基础名称库：%d 条。", count)
        return 0
    if args.command == "once":
        summary = service.run_once(force_session=args.force_session)
        LOGGER.info(summary.message)
        return 0
    if args.command == "web":
        return _run_web(args.config, args.host, args.port, args.no_scheduler)
    return _run_loop(service, args.run_now, args.force_session)
