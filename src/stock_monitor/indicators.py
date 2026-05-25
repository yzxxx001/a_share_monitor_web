from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class IndicatorSnapshot:
    """
    One-shot technical indicator snapshot computed from recent 5-minute bars.
    All values are None when there are insufficient bars for reliable calculation.

    用途对照：
      change_pct / amount_ratio / atr14 / bb_*  ← 盘中异动发现
      ma60 / macd_* / adx14                     ← 趋势背景判断
      rsi14                                      ← 动量风险提示
      bb_bandwidth / atr_percentile / daily_return ← 状态压缩辅助
    """

    # 盘中异动
    change_pct: float | None = None       # 最新K线涨跌幅（收盘/开盘 - 1）
    amount_ratio: float | None = None     # 成交额放大倍数（最新 / 近20根均值）
    atr14: float | None = None            # ATR(14) 平均真实波幅

    # 布林带 (20, 2σ)
    bb_upper: float | None = None
    bb_mid: float | None = None
    bb_lower: float | None = None
    bb_pct: float | None = None           # %B = (price - lower) / (upper - lower)
    bb_bandwidth: float | None = None     # (upper - lower) / mid，表征波动率宽度

    # 趋势背景
    ma60: float | None = None
    macd_dif: float | None = None         # EMA12 - EMA26 (DIF)
    macd_dea: float | None = None         # 9周期 DIF EMA (DEA)
    macd_hist: float | None = None        # DIF - DEA（当前柱）
    macd_hist_prev: float | None = None   # 前一根 MACD 柱（用于判断金叉/死叉）
    adx14: float | None = None            # ADX(14) 趋势强度

    # 动量
    rsi14: float | None = None

    # 状态压缩辅助
    atr_percentile: float | None = None   # ATR(14) 在本批K线历史中的百分位（0–100）
    daily_return: float | None = None     # 今日首根K线 open → 最新 close 的涨跌幅


@dataclass(frozen=True)
class MarketStateSnapshot:
    """
    将原始技术指标压缩为可解释的状态标签，用于通知推送和界面展示。
    每个字段均为中文可读标签。
    """
    trend_state: str               # 上升 / 下降 / 震荡
    momentum_state: str            # 偏热 / 正常 / 偏冷
    volatility_state: str          # 低 / 正常 / 高 / 异常
    volume_state: str              # 缩量 / 正常 / 放量 / 极端放量
    relative_state: str | None     # 强于行业 / 同步 / 弱于行业 / None（无行业数据）
    trend_basis: str = ""          # 趋势状态依据说明
    momentum_basis: str = ""       # 动量状态依据说明
    volatility_basis: str = ""     # 波动率状态依据说明
    volume_basis: str = ""         # 量能状态依据说明


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, min_periods=span, adjust=False).mean()


def compute_indicators(bars: pd.DataFrame, price: float) -> IndicatorSnapshot:
    """
    Compute all technical indicators from a time-ascending bars DataFrame.

    Parameters
    ----------
    bars  : DataFrame with columns time, open, high, low, close, vol, amount
            sorted in ascending time order.
    price : Latest close price (bars.iloc[-1]["close"]).
    """
    if len(bars) < 5:
        return IndicatorSnapshot()

    close  = bars["close"].astype(float).reset_index(drop=True)
    high   = bars["high"].astype(float).reset_index(drop=True)
    low    = bars["low"].astype(float).reset_index(drop=True)
    amount = bars["amount"].astype(float).reset_index(drop=True)
    n = len(close)

    # ── 最新5分钟涨跌幅 ─────────────────────────────────────────────────────
    latest_open = float(bars.iloc[-1]["open"])
    change_pct: float | None = (price / latest_open - 1) if latest_open > 0 else None

    # ── 成交额放大倍数（最新 vs 近20根均值，去掉当根本身）─────────────────────
    amount_ratio: float | None = None
    if n >= 21:
        avg = float(amount.iloc[-21:-1].mean())
        if avg > 0:
            amount_ratio = float(amount.iloc[-1]) / avg

    # ── ATR(14) ─────────────────────────────────────────────────────────────
    atr14: float | None = None
    if n >= 15:
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        v = tr.ewm(span=14, min_periods=14, adjust=False).mean().iloc[-1]
        if pd.notna(v):
            atr14 = float(v)

    # ── 布林带 (20, 2σ) ─────────────────────────────────────────────────────
    bb_upper = bb_mid = bb_lower = bb_pct = None
    if n >= 20:
        mid_v = close.rolling(20).mean().iloc[-1]
        std_v = close.rolling(20).std(ddof=1).iloc[-1]
        if pd.notna(mid_v) and pd.notna(std_v) and std_v > 0:
            bb_mid   = float(mid_v)
            bb_upper = float(mid_v + 2 * std_v)
            bb_lower = float(mid_v - 2 * std_v)
            band_width = bb_upper - bb_lower
            bb_pct = (price - bb_lower) / band_width if band_width > 0 else None

    # ── MA60 ─────────────────────────────────────────────────────────────────
    ma60: float | None = float(close.tail(60).mean()) if n >= 60 else None

    # ── MACD(12, 26, 9) ──────────────────────────────────────────────────────
    macd_dif = macd_dea = macd_hist = macd_hist_prev = None
    if n >= 35:  # 26 + 9 minimum
        dif  = _ema(close, 12) - _ema(close, 26)
        dea  = dif.ewm(span=9, min_periods=9, adjust=False).mean()
        hist = dif - dea
        vd, vs, vh = dif.iloc[-1], dea.iloc[-1], hist.iloc[-1]
        if pd.notna(vd) and pd.notna(vs):
            macd_dif  = float(vd)
            macd_dea  = float(vs)
            macd_hist = float(vh) if pd.notna(vh) else None
        if n >= 36:
            vhp = hist.iloc[-2]
            if pd.notna(vhp):
                macd_hist_prev = float(vhp)

    # ── ADX(14) ──────────────────────────────────────────────────────────────
    adx14: float | None = None
    if n >= 28:
        ph = high.shift(1)
        pl = low.shift(1)
        pc = close.shift(1)
        tr = pd.concat(
            [high - low, (high - pc).abs(), (low - pc).abs()],
            axis=1,
        ).max(axis=1)
        dm_p = (high - ph).where((high - ph) > (pl - low), 0.0).clip(lower=0)
        dm_m = (pl - low).where((pl - low) > (high - ph), 0.0).clip(lower=0)
        atr_w = tr.ewm(span=14, min_periods=14, adjust=False).mean()
        no_zero = atr_w.replace(0, float("nan"))
        di_p  = 100 * dm_p.ewm(span=14, min_periods=14, adjust=False).mean() / no_zero
        di_m  = 100 * dm_m.ewm(span=14, min_periods=14, adjust=False).mean() / no_zero
        denom = (di_p + di_m).replace(0, float("nan"))
        dx    = 100 * (di_p - di_m).abs() / denom
        v = dx.ewm(span=14, min_periods=14, adjust=False).mean().iloc[-1]
        if pd.notna(v):
            adx14 = float(v)

    # ── RSI(14) ──────────────────────────────────────────────────────────────
    rsi14: float | None = None
    if n >= 15:
        delta = close.diff()
        gain  = delta.where(delta > 0, 0.0)
        loss  = (-delta).where(delta < 0, 0.0)
        ag    = gain.ewm(span=14, min_periods=14, adjust=False).mean()
        al    = loss.ewm(span=14, min_periods=14, adjust=False).mean()
        rs    = ag / al.replace(0, float("nan"))
        v = (100 - 100 / (1 + rs)).iloc[-1]
        if pd.notna(v):
            rsi14 = float(v)

    # ── 布林带宽 ─────────────────────────────────────────────────────────────
    bb_bandwidth: float | None = None
    if bb_upper is not None and bb_lower is not None and bb_mid is not None and bb_mid > 0:
        bb_bandwidth = (bb_upper - bb_lower) / bb_mid

    # ── ATR 百分位（当前 ATR 在本批历史序列中的排名）────────────────────────
    atr_percentile: float | None = None
    if n >= 30 and atr14 is not None:
        prev_close_p = close.shift(1)
        tr_p = pd.concat(
            [high - low, (high - prev_close_p).abs(), (low - prev_close_p).abs()],
            axis=1,
        ).max(axis=1)
        atr_series = tr_p.ewm(span=14, min_periods=14, adjust=False).mean().dropna()
        if len(atr_series) >= 2:
            atr_percentile = float((atr_series < atr14).mean() * 100)

    # ── 今日开盘至今涨跌幅 ────────────────────────────────────────────────────
    daily_return: float | None = None
    if "time" in bars.columns:
        try:
            today = pd.Timestamp(bars.iloc[-1]["time"]).date()
            today_bars = bars[pd.to_datetime(bars["time"]).dt.date == today]
            if not today_bars.empty:
                day_open = float(today_bars.iloc[0]["open"])
                if day_open > 0:
                    daily_return = (price - day_open) / day_open
        except Exception:
            pass

    return IndicatorSnapshot(
        change_pct=change_pct,
        amount_ratio=amount_ratio,
        atr14=atr14,
        bb_upper=bb_upper,
        bb_mid=bb_mid,
        bb_lower=bb_lower,
        bb_pct=bb_pct,
        bb_bandwidth=bb_bandwidth,
        ma60=ma60,
        macd_dif=macd_dif,
        macd_dea=macd_dea,
        macd_hist=macd_hist,
        macd_hist_prev=macd_hist_prev,
        adx14=adx14,
        rsi14=rsi14,
        atr_percentile=atr_percentile,
        daily_return=daily_return,
    )


# ─────────────────────────────────────────────────────────────────────────────
# State classification helpers
# ─────────────────────────────────────────────────────────────────────────────

def _classify_trend(
    ind: IndicatorSnapshot, price: float, ma5: float | None, ma20: float | None
) -> tuple[str, str]:
    """趋势状态: 上升 / 下降 / 震荡（多数投票：MA20、MA60、MACD）"""
    score = 0
    bullets: list[str] = []
    if ma20 is not None:
        if price >= ma20:
            score += 1
            bullets.append(f"价格≥MA20({ma20:.2f})")
        else:
            score -= 1
            bullets.append(f"价格<MA20({ma20:.2f})")
    if ind.ma60 is not None:
        if price >= ind.ma60:
            score += 1
            bullets.append(f"价格≥MA60({ind.ma60:.2f})")
        else:
            score -= 1
            bullets.append(f"价格<MA60({ind.ma60:.2f})")
    if ind.macd_hist is not None:
        if ind.macd_hist > 0:
            score += 1
            bullets.append("MACD柱正")
        else:
            score -= 1
            bullets.append("MACD柱负")
    basis = "、".join(bullets) if bullets else "数据积累中"
    if score >= 2:
        return "上升", basis
    if score <= -2:
        return "下降", basis
    return "震荡", basis


def _classify_momentum(ind: IndicatorSnapshot) -> tuple[str, str]:
    """动量状态: 偏热 / 正常 / 偏冷（RSI14）"""
    if ind.rsi14 is None:
        return "正常", "RSI数据不足"
    if ind.rsi14 >= 70:
        return "偏热", f"RSI(14)={ind.rsi14:.0f}（≥70）"
    if ind.rsi14 <= 40:
        return "偏冷", f"RSI(14)={ind.rsi14:.0f}（≤40）"
    return "正常", f"RSI(14)={ind.rsi14:.0f}"


def _classify_volatility(ind: IndicatorSnapshot) -> tuple[str, str]:
    """波动率状态: 低 / 正常 / 高 / 异常（ATR百分位 + 布林带宽）"""
    score = 0  # −1=低, 0=正常, 1=高, 2=异常
    bullets: list[str] = []
    if ind.atr_percentile is not None:
        bullets.append(f"ATR分位{ind.atr_percentile:.0f}%")
        if ind.atr_percentile >= 90:
            score = max(score, 2)
        elif ind.atr_percentile >= 70:
            score = max(score, 1)
        elif ind.atr_percentile <= 25:
            score = min(score, -1)
    if ind.bb_bandwidth is not None:
        bullets.append(f"布林带宽{ind.bb_bandwidth * 100:.1f}%")
        if ind.bb_bandwidth >= 0.08:
            score = max(score, 2)
        elif ind.bb_bandwidth >= 0.04:
            score = max(score, 1)
        elif ind.bb_bandwidth <= 0.01:
            score = min(score, -1)
    basis = "、".join(bullets) if bullets else "数据积累中"
    state_map = {-1: "低", 0: "正常", 1: "高", 2: "异常"}
    return state_map[max(-1, min(2, score))], basis


def _classify_volume(ind: IndicatorSnapshot) -> tuple[str, str]:
    """量能状态: 缩量 / 正常 / 放量 / 极端放量（成交额相对近20根均值）"""
    if ind.amount_ratio is None:
        return "正常", "成交额数据不足"
    basis = f"成交额 {ind.amount_ratio:.1f}× 近20根均值"
    if ind.amount_ratio >= 5.0:
        return "极端放量", basis
    if ind.amount_ratio >= 3.0:
        return "放量", basis
    if ind.amount_ratio <= 0.5:
        return "缩量", basis
    return "正常", basis


def compute_market_state(
    ind: IndicatorSnapshot,
    price: float,
    ma5: float | None,
    ma20: float | None,
    industry_change_pct: float | None = None,
) -> MarketStateSnapshot:
    """
    Derive compressed market state labels from raw indicator snapshot.

    Parameters
    ----------
    ind                  : IndicatorSnapshot from compute_indicators()
    price                : Latest close price
    ma5, ma20            : Short/medium moving averages computed in signal_engine
    industry_change_pct  : Industry index daily return for relative comparison (optional)
    """
    trend_state, trend_basis = _classify_trend(ind, price, ma5, ma20)
    momentum_state, momentum_basis = _classify_momentum(ind)
    volatility_state, volatility_basis = _classify_volatility(ind)
    volume_state, volume_basis = _classify_volume(ind)

    relative_state: str | None = None
    if industry_change_pct is not None and ind.daily_return is not None:
        diff = ind.daily_return - industry_change_pct
        if diff >= 0.03:
            relative_state = "强于行业"
        elif diff <= -0.03:
            relative_state = "弱于行业"
        else:
            relative_state = "同步"

    return MarketStateSnapshot(
        trend_state=trend_state,
        momentum_state=momentum_state,
        volatility_state=volatility_state,
        volume_state=volume_state,
        relative_state=relative_state,
        trend_basis=trend_basis,
        momentum_basis=momentum_basis,
        volatility_basis=volatility_basis,
        volume_basis=volume_basis,
    )
