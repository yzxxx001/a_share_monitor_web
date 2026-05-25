from __future__ import annotations

from datetime import datetime

import pandas as pd

from .config import RuleConfig
from .indicators import compute_indicators, compute_market_state
from .models import Evaluation, Position, Severity, Signal


def evaluate_position(
    position: Position,
    bars: pd.DataFrame,
    rules: RuleConfig,
    stored_peak: float | None = None,
) -> Evaluation:
    if bars.empty:
        raise ValueError("没有可用于判断的行情数据")

    bars = bars.sort_values("time").copy()
    latest = bars.iloc[-1]
    price = float(latest["close"])
    trade_time = pd.Timestamp(latest["time"]).to_pydatetime()
    market_value = price * position.shares
    position_pct = market_value / position.account_total_value if position.account_total_value > 0 else 0.0
    pnl_pct = (price / position.average_cost - 1) if position.has_position else None

    peak_candidates = [candidate for candidate in (stored_peak, position.peak_price_since_entry) if candidate is not None]
    if position.has_position:
        peak_candidates.append(float(bars["high"].max()))
    observed_peak = max(peak_candidates) if peak_candidates else None

    ma5 = float(bars["close"].tail(5).mean()) if len(bars) >= 5 else None
    ma20 = float(bars["close"].tail(20).mean()) if len(bars) >= rules.minimum_bars_for_trend else None
    signals: list[Signal] = []

    if position.has_position and pnl_pct is not None and pnl_pct <= -position.stop_loss_pct:
        signals.append(
            Signal(
                rule_id="STOP_LOSS",
                severity=Severity.HIGH,
                action="检查止损或减仓计划",
                title="固定止损线触发",
                reason=f"当前盈亏 {pnl_pct:.2%}，已低于设定止损阈值 -{position.stop_loss_pct:.2%}。",
                current_price=price,
                details={"pnl_pct": pnl_pct},
            )
        )

    if (
        position.has_position
        and observed_peak is not None
        and observed_peak >= position.average_cost * (1 + position.take_profit_pct)
        and price <= observed_peak * (1 - position.trailing_stop_pct)
    ):
        drawdown = price / observed_peak - 1
        signals.append(
            Signal(
                rule_id="TRAILING_STOP",
                severity=Severity.HIGH,
                action="检查移动止盈或减仓计划",
                title="盈利回撤保护触发",
                reason=(
                    f"历史/观测最高价 {observed_peak:.2f} 元，当前回撤 {drawdown:.2%}，"
                    f"已达到设定回撤阈值 -{position.trailing_stop_pct:.2%}。"
                ),
                current_price=price,
                details={"observed_peak": observed_peak, "drawdown": drawdown},
            )
        )

    if position.shares > 0 and position_pct > position.max_position_pct:
        signals.append(
            Signal(
                rule_id="OVERWEIGHT",
                severity=Severity.MEDIUM,
                action="检查仓位上限",
                title="当前仓位超过计划上限",
                reason=f"当前市值仓位 {position_pct:.2%}，超过设定上限 {position.max_position_pct:.2%}。",
                current_price=price,
                details={"position_pct": position_pct},
            )
        )

    if position.has_position and ma5 is not None and ma20 is not None and price < ma20 and ma5 < ma20:
        signals.append(
            Signal(
                rule_id="TREND_WEAK",
                severity=Severity.MEDIUM,
                action="检查走势是否仍符合持仓计划",
                title="5分钟趋势转弱",
                reason=f"收盘价 {price:.2f} 元低于 MA20 {ma20:.2f} 元，且 MA5 {ma5:.2f} 元低于 MA20。",
                current_price=price,
                details={"ma5": ma5, "ma20": ma20},
            )
        )

    if len(bars) >= rules.minimum_bars_for_breakout and position_pct < position.max_position_pct:
        previous = bars.iloc[:-1].tail(20)
        previous_high = float(previous["high"].max())
        average_volume = float(previous["vol"].mean())
        volume_ok = average_volume > 0 and float(latest["vol"]) >= average_volume * rules.volume_spike_ratio
        trend_ok = ma5 is not None and ma20 is not None and ma5 > ma20
        if price > previous_high and volume_ok and trend_ok:
            permitted_amount = position.account_total_value * position.max_single_buy_pct
            signals.append(
                Signal(
                    rule_id="BREAKOUT_WATCH",
                    severity=Severity.LOW,
                    action="仅作为加仓观察候选，需人工确认",
                    title="放量突破观察信号",
                    reason=(
                        f"价格突破近 20 根K线高点 {previous_high:.2f} 元，成交量达到均量的"
                        f" {float(latest['vol']) / average_volume:.2f} 倍；计划单次新增仓位上限约 {permitted_amount:.2f} 元。"
                    ),
                    current_price=price,
                    details={"previous_high": previous_high, "permitted_amount": permitted_amount},
                )
            )

    # ── 技术指标计算 ────────────────────────────────────────────────────────
    indicators = compute_indicators(bars, price)
    market_state = compute_market_state(indicators, price, ma5, ma20)

    # ── 成交额异动放大 ──────────────────────────────────────────────────────
    if indicators.amount_ratio is not None and indicators.amount_ratio >= rules.amount_surge_ratio:
        signals.append(
            Signal(
                rule_id="AMOUNT_SURGE",
                severity=Severity.INFO,
                action="留意后续量价配合情况，暂不操作",
                title="成交额异动放大",
                reason=(
                    f"当前成交额为近20根K线均值的 {indicators.amount_ratio:.1f} 倍，"
                    "出现量能异动，关注是否有资金进出。"
                ),
                current_price=price,
                details={"amount_ratio": indicators.amount_ratio},
            )
        )

    # ── 布林带下轨跌破（持仓时提示风险）──────────────────────────────────────
    if (
        position.has_position
        and indicators.bb_lower is not None
        and price < indicators.bb_lower
    ):
        bb_pct_str = f"，%B={indicators.bb_pct:.2f}" if indicators.bb_pct is not None else ""
        signals.append(
            Signal(
                rule_id="BB_LOWER_BREAK",
                severity=Severity.MEDIUM,
                action="关注是否进入超卖区域，结合成交量确认后续方向",
                title="布林带下轨跌破",
                reason=(
                    f"价格 {price:.2f} 元跌破布林带下轨 {indicators.bb_lower:.2f} 元{bb_pct_str}，"
                    "短期处于统计偏低区域。"
                ),
                current_price=price,
                details={"bb_lower": indicators.bb_lower, "bb_pct": indicators.bb_pct},
            )
        )

    # ── RSI 超买风险提示（持仓时触发）────────────────────────────────────────
    if (
        position.has_position
        and indicators.rsi14 is not None
        and indicators.rsi14 > rules.rsi_overbought
    ):
        signals.append(
            Signal(
                rule_id="RSI_OVERBOUGHT",
                severity=Severity.LOW,
                action="动量过热，注意回调风险，不宜在此位置追涨",
                title=f"RSI 超买区域（{indicators.rsi14:.0f}）",
                reason=(
                    f"RSI(14)={indicators.rsi14:.1f}，超过超买阈值 {rules.rsi_overbought:.0f}，"
                    "短期涨幅过快，存在获利回吐压力。"
                ),
                current_price=price,
                details={"rsi14": indicators.rsi14},
            )
        )

    # ── MACD 死叉（柱由正转负，持仓时提示动量转弱）──────────────────────────
    if (
        position.has_position
        and indicators.macd_hist is not None
        and indicators.macd_hist_prev is not None
        and indicators.macd_hist < 0
        and indicators.macd_hist_prev >= 0
    ):
        signals.append(
            Signal(
                rule_id="MACD_DEATH_CROSS",
                severity=Severity.MEDIUM,
                action="短期动量转弱，关注趋势是否持续，结合其他规则综合判断",
                title="MACD 死叉出现",
                reason=(
                    f"MACD柱由正（{indicators.macd_hist_prev:.4f}）转负（{indicators.macd_hist:.4f}），"
                    "DIF 下穿 DEA，短期动量转空。"
                ),
                current_price=price,
                details={
                    "macd_dif": indicators.macd_dif,
                    "macd_dea": indicators.macd_dea,
                    "macd_hist": indicators.macd_hist,
                },
            )
        )

    # ════════════════════════════════════════════════════════════════════════
    # 盘中强提醒
    # ════════════════════════════════════════════════════════════════════════

    # ── 单根K线异动：涨跌幅超过 ATR 动态阈值 ────────────────────────────────
    if (
        indicators.change_pct is not None
        and indicators.atr14 is not None
        and price > 0
    ):
        atr_threshold = indicators.atr14 / price * rules.intraday_atr_multiplier
        if abs(indicators.change_pct) >= atr_threshold:
            direction = "上涨" if indicators.change_pct > 0 else "下跌"
            signals.append(
                Signal(
                    rule_id="INTRADAY_ATR_BREAK",
                    severity=Severity.HIGH,
                    action="关注量价配合，避免追高或恐慌操作",
                    title=f"盘中异动{direction}（ATR动态阈值触发）",
                    reason=(
                        f"最新5分钟{direction} {abs(indicators.change_pct):.2%}，"
                        f"超过ATR动态阈值 {atr_threshold:.2%}"
                        f"（ATR={indicators.atr14:.3f}，当前价={price:.2f}）。"
                    ),
                    current_price=price,
                    details={"change_pct": indicators.change_pct, "atr_threshold": atr_threshold},
                )
            )

    # ── 布林带突破 + 成交额放大双重确认 ─────────────────────────────────────
    if (
        indicators.bb_pct is not None
        and (indicators.bb_pct > 1.0 or indicators.bb_pct < 0.0)
        and indicators.amount_ratio is not None
        and indicators.amount_ratio >= 2.0
    ):
        direction = "上轨" if indicators.bb_pct > 1.0 else "下轨"
        signals.append(
            Signal(
                rule_id="INTRADAY_BB_VOLUME",
                severity=Severity.MEDIUM,
                action="量价共振，关注突破的可持续性",
                title=f"布林带{direction}突破 + 量能确认",
                reason=(
                    f"价格突破布林带{direction}（%B={indicators.bb_pct:.2f}），"
                    f"同时成交额放大 {indicators.amount_ratio:.1f}×，量价共振出现。"
                ),
                current_price=price,
                details={"bb_pct": indicators.bb_pct, "amount_ratio": indicators.amount_ratio},
            )
        )

    # ════════════════════════════════════════════════════════════════════════
    # 收盘风险提醒
    # ════════════════════════════════════════════════════════════════════════

    # ── 日内异动涨跌幅 + 量能放大 ────────────────────────────────────────────
    if (
        indicators.daily_return is not None
        and abs(indicators.daily_return) >= rules.daily_return_alert_pct
        and indicators.amount_ratio is not None
        and indicators.amount_ratio >= 2.0
    ):
        direction = "上涨" if indicators.daily_return > 0 else "下跌"
        signals.append(
            Signal(
                rule_id="DAILY_EXTREME_MOVE",
                severity=Severity.HIGH,
                action="关注是否有重大消息驱动，核查持仓计划",
                title=f"日内异动{direction} + 量能放大",
                reason=(
                    f"今日开盘至今{direction} {abs(indicators.daily_return):.2%}"
                    f"（阈值 {rules.daily_return_alert_pct:.0%}），"
                    f"成交额放大 {indicators.amount_ratio:.1f}×，量价共振。"
                ),
                current_price=price,
                details={"daily_return": indicators.daily_return, "amount_ratio": indicators.amount_ratio},
            )
        )

    # ── RSI 动量高位 + ATR 分位偏高 ──────────────────────────────────────────
    if (
        indicators.rsi14 is not None
        and indicators.rsi14 >= 75
        and indicators.atr_percentile is not None
        and indicators.atr_percentile >= 80
    ):
        signals.append(
            Signal(
                rule_id="DAILY_RSI_HIGH_VOL",
                severity=Severity.MEDIUM,
                action="动量高位叠加波动率偏高，注意回调风险",
                title=f"RSI高位 + ATR分位偏高（{indicators.rsi14:.0f} / {indicators.atr_percentile:.0f}%）",
                reason=(
                    f"RSI(14)={indicators.rsi14:.1f}（≥75），"
                    f"ATR分位 {indicators.atr_percentile:.0f}%（≥80%），"
                    "动量高位叠加波动率放大，回调风险上升。"
                ),
                current_price=price,
                details={"rsi14": indicators.rsi14, "atr_percentile": indicators.atr_percentile},
            )
        )

    # ── 价格下破 MA20 + MACD 死叉双重确认 ────────────────────────────────────
    if (
        position.has_position
        and ma20 is not None
        and price < ma20
        and indicators.macd_hist is not None
        and indicators.macd_hist < 0
        and indicators.macd_hist_prev is not None
        and indicators.macd_hist_prev >= 0
    ):
        signals.append(
            Signal(
                rule_id="DAILY_RISK_MACD_BREAK",
                severity=Severity.HIGH,
                action="均线与MACD双重转弱，检查是否需要减仓",
                title="价格下破MA20 + MACD死叉双重确认",
                reason=(
                    f"收盘价 {price:.2f} 元跌破MA20 {ma20:.2f} 元，"
                    f"同时MACD柱由正（{indicators.macd_hist_prev:.4f}）转负（{indicators.macd_hist:.4f}），"
                    "趋势转弱信号双重确认。"
                ),
                current_price=price,
                details={"ma20": ma20, "macd_hist": indicators.macd_hist},
            )
        )

    return Evaluation(
        position=position,
        trade_time=trade_time,
        latest_price=price,
        market_value=market_value,
        position_pct=position_pct,
        pnl_pct=pnl_pct,
        observed_peak=observed_peak,
        ma5=ma5,
        ma20=ma20,
        signals=sorted(signals, key=lambda signal: signal.severity, reverse=True),
        indicators=indicators,
        market_state=market_state,
    )
