from __future__ import annotations

from .models import Evaluation, Severity, Signal


LEVEL_LABEL = {
    Severity.HIGH: "高风险",
    Severity.MEDIUM: "需关注",
    Severity.LOW: "观察候选",
    Severity.INFO: "信息",
}
LEVEL_COLOR = {
    Severity.HIGH: "warning",
    Severity.MEDIUM: "comment",
    Severity.LOW: "info",
    Severity.INFO: "info",
}

# ── 提醒分层 ─────────────────────────────────────────────────────────────────
# 盘中强提醒 rule IDs
_INTRADAY_RULES = frozenset({"INTRADAY_ATR_BREAK", "INTRADAY_BB_VOLUME", "AMOUNT_SURGE"})
# 收盘风险提醒 rule IDs
_DAILY_RULES = frozenset({"DAILY_EXTREME_MOVE", "DAILY_RSI_HIGH_VOL", "DAILY_RISK_MACD_BREAK"})


def highest_signal(signals: list[Signal]) -> Signal:
    return max(signals, key=lambda signal: signal.severity)


def _alert_tier(signals: list[Signal]) -> str:
    """返回提醒所属分层标签：盘中强提醒 / 收盘风险提醒 / AI综合提醒 / 持仓规则提醒"""
    rule_ids = {s.rule_id for s in signals}
    has_intraday = bool(rule_ids & _INTRADAY_RULES)
    has_daily = bool(rule_ids & _DAILY_RULES)
    if has_intraday and has_daily:
        return "AI综合提醒"
    if has_intraday:
        return "盘中强提醒"
    if has_daily:
        return "收盘风险提醒"
    return "持仓规则提醒"


def _fmt_market_state(evaluation: Evaluation) -> str | None:
    """返回一行紧凑的市场状态摘要供消息正文使用。"""
    ms = evaluation.market_state
    if ms is None:
        return None
    parts = [
        f"趋势:{ms.trend_state}",
        f"动量:{ms.momentum_state}",
        f"波动:{ms.volatility_state}",
        f"量能:{ms.volume_state}",
    ]
    if ms.relative_state:
        parts.append(f"行业:{ms.relative_state}")
    return "　".join(parts)


def _fmt_indicator_line(evaluation: Evaluation) -> str | None:
    """Build a compact indicator reference line for the notification message."""
    ind = evaluation.indicators
    if ind is None:
        return None
    parts: list[str] = []
    if ind.rsi14 is not None:
        parts.append(f"RSI={ind.rsi14:.0f}")
    if ind.bb_pct is not None:
        parts.append(f"布林%B={ind.bb_pct:.2f}")
    if ind.macd_hist is not None:
        direction = "↑" if ind.macd_hist > 0 else "↓"
        parts.append(f"MACD{direction}={ind.macd_hist:.4f}")
    if ind.adx14 is not None:
        parts.append(f"ADX={ind.adx14:.0f}")
    if ind.amount_ratio is not None:
        parts.append(f"量比={ind.amount_ratio:.1f}×")
    if ind.ma60 is not None:
        parts.append(f"MA60={ind.ma60:.2f}")
    return "　".join(parts) if parts else None


def build_wecom_markdown(evaluation: Evaluation) -> str:
    top = highest_signal(evaluation.signals)
    tier = _alert_tier(evaluation.signals)
    position = evaluation.position
    pnl = "无持仓" if evaluation.pnl_pct is None else f"{evaluation.pnl_pct:.2%}"
    ma_info = "数据积累中" if evaluation.ma20 is None else f"MA5={evaluation.ma5:.2f}，MA20={evaluation.ma20:.2f}"
    lines = [
        f"### A股监控提醒｜<font color=\"{LEVEL_COLOR[top.severity]}\">{LEVEL_LABEL[top.severity]}</font>　[{tier}]",
        f"> 标的：**{position.display_name}**",
        f"> 时间：{evaluation.trade_time:%Y-%m-%d %H:%M:%S}",
        f"> 最新 5 分钟收盘价：**{evaluation.latest_price:.2f} 元**",
        f"> 持仓：{position.shares} 股；可卖：{position.available_to_sell} 股；当前盈亏：{pnl}",
        f"> 当前市值仓位：{evaluation.position_pct:.2%}；均线状态：{ma_info}",
    ]
    ms_line = _fmt_market_state(evaluation)
    if ms_line:
        lines.append(f"> 市场状态：{ms_line}")
    ind_line = _fmt_indicator_line(evaluation)
    if ind_line:
        lines.append(f"> 技术参考：{ind_line}")
    lines += [
        "",
        "**触发原因：**",
    ]
    for idx, signal in enumerate(evaluation.signals, start=1):
        lines.append(f"{idx}. [{LEVEL_LABEL[signal.severity]}] {signal.title}：{signal.reason}")
    # AI 综合提醒附言
    if tier == "AI综合提醒":
        lines += ["", "> ⚠️ 盘中异动与收盘风险信号同时触发，建议人工复核持仓计划。"]
    lines += ["", f"**建议处理：** {top.action}", "提醒依据为预设规则，仅供检查持仓计划，不自动执行交易。"]
    return "\n".join(lines)


def build_sms_params(evaluation: Evaluation) -> dict[str, str]:
    top = highest_signal(evaluation.signals)
    return {
        "stock": evaluation.position.name or evaluation.position.ts_code,
        "signal": top.title[:12],
        "price": f"{evaluation.latest_price:.2f}",
    }


