from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .config import Settings, load_settings
from .formatter import build_sms_params, build_wecom_markdown, highest_signal
from .market import AKShareMultiSourceProvider
from .models import Evaluation
from .notifiers import AliyunSmsNotifier, SendResult, WeComNotifier
from .positions import load_positions
from .signal_engine import evaluate_position
from .state import StateStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    status: str
    message: str
    positions_checked: int
    alerts_triggered: int
    notifications_delivered: int


def configure_logging(settings: Settings) -> None:
    settings.paths.log_file.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.app.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(settings.paths.log_file, encoding="utf-8")],
        force=True,
    )


def in_a_share_session(now: datetime) -> bool:
    """Simple trading-session guard; data availability naturally limits exchange holidays."""
    if now.weekday() >= 5:
        return False
    current = now.time().replace(tzinfo=None)
    morning = time(9, 30) <= current <= time(11, 30, 59)
    afternoon = time(13, 0) <= current <= time(15, 0, 59)
    return morning or afternoon


class MonitorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.zone = ZoneInfo(settings.app.timezone)
        self.store = StateStore(settings.paths.sqlite_file)
        self.wecom = WeComNotifier(settings.notifications.wecom, settings.app.dry_run)
        self.sms = AliyunSmsNotifier(settings.notifications.aliyun_sms, settings.app.dry_run)
        self.provider: AKShareMultiSourceProvider | None = None

    def _get_provider(self) -> AKShareMultiSourceProvider:
        if self.provider is None:
            self.provider = AKShareMultiSourceProvider(
                period=self.settings.market.period,
                adjust=self.settings.market.adjust,
                max_bars=self.settings.market.cached_bars,
                primary_source=self.settings.market.primary_source,
                fallback_source=self.settings.market.fallback_source,
                retry_times=self.settings.market.retry_times,
                retry_backoff_seconds=self.settings.market.retry_backoff_seconds,
                bypass_proxy_for_market_hosts=self.settings.market.bypass_proxy_for_market_hosts,
            )
        return self.provider

    def import_excel_positions(self) -> tuple[int, list[str]]:
        positions, warnings = load_positions(self.settings.paths.positions_file)
        count = self.store.import_watch_items(positions)
        return count, warnings

    def sync_stock_master(self) -> int:
        data = self._get_provider().fetch_stock_master()
        return self.store.save_stock_master(data, datetime.now(self.zone))

    def _indicator_text(self, evaluation: Evaluation) -> str:
        ind = evaluation.indicators
        if ind is None:
            return ""
        parts: list[str] = []
        if ind.rsi14 is not None:
            parts.append(f"RSI {ind.rsi14:.0f}")
        if ind.macd_hist is not None:
            parts.append("MACD↑" if ind.macd_hist > 0 else "MACD↓")
        if ind.amount_ratio is not None and ind.amount_ratio >= 2.0:
            parts.append(f"量比 {ind.amount_ratio:.1f}×")
        if ind.adx14 is not None:
            parts.append(f"ADX {ind.adx14:.0f}")
        return "　".join(parts)

    def snapshot(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for position in self.store.list_watchlist():
            bars = self.store.load_recent_bars(position.ts_code, self.settings.market.cached_bars)
            item: dict[str, object] = {"position": position, "evaluation": None, "last_time": None, "signal_text": "暂无行情"}
            if not bars.empty:
                evaluation = evaluate_position(position, bars, self.settings.rules, self.store.get_peak(position.ts_code))
                item.update(
                    evaluation=evaluation,
                    last_time=evaluation.trade_time,
                    signal_text=("、".join(signal.title for signal in evaluation.signals) if evaluation.signals else "无触发信号"),
                    indicator_text=self._indicator_text(evaluation),
                    market_state=evaluation.market_state,
                )
            result.append(item)
        return result

    def run_once(self, force_session: bool = False) -> RunSummary:
        now = datetime.now(self.zone)
        run_id = self.store.start_run(now)
        checked = 0
        alert_count = 0
        delivered_count = 0
        failures: list[str] = []
        try:
            if self.settings.app.only_trading_session and not force_session and not in_a_share_session(now):
                message = f"当前不在 A 股日内交易时段，跳过检查：{now:%Y-%m-%d %H:%M:%S}"
                LOGGER.info(message)
                summary = RunSummary("SKIPPED", message, 0, 0, 0)
                self.store.finish_run(run_id, datetime.now(self.zone), summary.status, summary.message, 0, 0, 0)
                return summary

            positions = self.store.list_watchlist(enabled_only=True)
            if not positions:
                message = "监控列表中没有已启用的标的；请在网页添加股票，或导入 Excel 模板。"
                LOGGER.info(message)
                summary = RunSummary("EMPTY", message, 0, 0, 0)
                self.store.finish_run(run_id, datetime.now(self.zone), summary.status, summary.message, 0, 0, 0)
                return summary

            provider = self._get_provider()
            for position in positions:
                checked += 1
                try:
                    received_bars = provider.fetch_latest_bars(position.ts_code)
                    if received_bars.empty:
                        LOGGER.warning("%s 未获取到实时分钟数据，已跳过。", position.ts_code)
                        continue
                    self.store.upsert_bars(received_bars)
                    bars = self.store.load_recent_bars(position.ts_code, self.settings.market.cached_bars)
                    evaluation = evaluate_position(position, bars, self.settings.rules, self.store.get_peak(position.ts_code))
                    if evaluation.observed_peak is not None:
                        self.store.set_peak(position.ts_code, evaluation.observed_peak, now)

                    due_signals = [
                        signal for signal in evaluation.signals
                        if self.store.may_send(position.ts_code, signal.rule_id, now, self.settings.rules.alert_cooldown_minutes)
                    ]
                    if not evaluation.signals:
                        LOGGER.info("%s 未触发提醒；最新价 %.2f。", position.ts_code, evaluation.latest_price)
                        continue
                    if not due_signals:
                        self.store.record_signal_events(evaluation, evaluation.signals, "冷却期内未重复通知", now)
                        LOGGER.info("%s 触发信号但处于提醒冷却期。", position.ts_code)
                        continue

                    alert_count += len(due_signals)
                    notified_evaluation = Evaluation(
                        position=evaluation.position,
                        trade_time=evaluation.trade_time,
                        latest_price=evaluation.latest_price,
                        market_value=evaluation.market_value,
                        position_pct=evaluation.position_pct,
                        pnl_pct=evaluation.pnl_pct,
                        observed_peak=evaluation.observed_peak,
                        ma5=evaluation.ma5,
                        ma20=evaluation.ma20,
                        signals=due_signals,
                        indicators=evaluation.indicators,
                        market_state=evaluation.market_state,
                    )
                    results: list[SendResult] = [self.wecom.send(build_wecom_markdown(notified_evaluation))]
                    top = highest_signal(due_signals)
                    if top.severity.name in self.settings.rules.sms_levels:
                        results.append(self.sms.send(build_sms_params(notified_evaluation)))
                    status_text = "；".join(f"{item.channel}:{item.detail}" for item in results)
                    self.store.record_signal_events(notified_evaluation, due_signals, status_text, now)
                    for result in results:
                        LOGGER.info("通知渠道 %s：%s", result.channel, result.detail)
                    delivered = any(result.sent for result in results)
                    if delivered:
                        delivered_count += 1
                        for signal in due_signals:
                            self.store.mark_sent(position.ts_code, signal.rule_id, now, notified_evaluation.latest_price)
                    elif self.settings.app.dry_run:
                        LOGGER.info("dry_run=true，提醒仅预览且不写入冷却记录。")
                    else:
                        LOGGER.error("%s 的提醒未通过任何渠道成功送达。", position.ts_code)
                except Exception as exc:
                    failures.append(f"{position.ts_code}: {exc}")
                    LOGGER.exception("处理 %s 时发生异常。", position.ts_code)

            if failures and len(failures) == checked:
                status = "ERROR"
                message = f"全部 {checked} 个标的获取或处理失败；首个错误：{failures[0]}"
            elif failures:
                status = "PARTIAL"
                message = (
                    f"已检查 {checked} 个标的，其中 {len(failures)} 个失败；"
                    f"触发 {alert_count} 条信号，实际送达 {delivered_count} 个标的通知。"
                    f"首个错误：{failures[0]}"
                )
            else:
                status = "OK"
                message = f"已检查 {checked} 个标的，触发 {alert_count} 条信号，实际送达 {delivered_count} 个标的通知。"
            summary = RunSummary(status, message, checked, alert_count, delivered_count)
            self.store.finish_run(run_id, datetime.now(self.zone), status, message, checked, alert_count, delivered_count)
            return summary
        except Exception as exc:
            message = f"监控执行失败：{exc}"
            LOGGER.exception(message)
            summary = RunSummary("ERROR", message, checked, alert_count, delivered_count)
            self.store.finish_run(run_id, datetime.now(self.zone), summary.status, summary.message, checked, alert_count, delivered_count)
            return summary


def build_service(config_path: str | Path) -> MonitorService:
    config_path = Path(config_path).resolve()
    load_dotenv(config_path.parent.parent / ".env")
    settings = load_settings(config_path)
    configure_logging(settings)
    return MonitorService(settings)
