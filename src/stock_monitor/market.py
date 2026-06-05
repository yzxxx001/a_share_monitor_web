from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import pandas as pd

LOGGER = logging.getLogger(__name__)
_DATA_HOSTS_NO_PROXY = (
    ".eastmoney.com",
    ".push2.eastmoney.com",
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "17.push2.eastmoney.com",
    "datacenter-web.eastmoney.com",
    ".10jqka.com.cn",
    "q.10jqka.com.cn",
    ".sina.com.cn",
)
_STANDARD_COLUMNS = ["ts_code", "time", "open", "close", "high", "low", "vol", "amount"]


def infer_exchange(symbol: str) -> str:
    """Infer A-share exchange suffix for the app's internal code format."""
    symbol = str(symbol).strip()
    if symbol.startswith("6"):
        return "SH"
    if symbol.startswith(("0", "3")):
        return "SZ"
    if symbol.startswith(("4", "8", "9")):
        return "BJ"
    return ""


def internal_ts_code(symbol: str) -> str:
    cleaned = str(symbol).strip().zfill(6)
    exchange = infer_exchange(cleaned)
    return f"{cleaned}.{exchange}" if exchange else cleaned


def append_market_no_proxy_hosts() -> None:
    """Append quote hosts to NO_PROXY without discarding the user's existing exceptions."""
    current: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        value = os.getenv(key, "")
        if value:
            current.extend(item.strip() for item in value.split(",") if item.strip())
    merged = list(dict.fromkeys([*current, *_DATA_HOSTS_NO_PROXY]))
    value = ",".join(merged)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


def _append_no_proxy_hosts() -> None:
    append_market_no_proxy_hosts()


def _short_error(exc: Exception, limit: int = 260) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message if len(message) <= limit else message[: limit - 3] + "..."


@dataclass
class AKShareMultiSourceProvider:
    """Free-data prototype provider with Sina/EastMoney automatic fallback.

    The app stores identifiers such as ``600276.SH``. For minute bars, Sina is
    used by default because some networks currently see EastMoney close
    connections unexpectedly. If Sina fails, EastMoney is tried automatically.
    """

    period: str = "5"
    adjust: str = ""
    max_bars: int = 80
    primary_source: str = "sina"
    fallback_source: str = "eastmoney"
    retry_times: int = 2
    retry_backoff_seconds: float = 0.8
    bypass_proxy_for_market_hosts: bool = True

    def __post_init__(self) -> None:
        valid = {"sina", "eastmoney"}
        self.primary_source = self.primary_source.lower().strip()
        self.fallback_source = self.fallback_source.lower().strip()
        if self.primary_source not in valid or self.fallback_source not in valid:
            raise ValueError("AKShare 数据源仅支持 sina 或 eastmoney")
        if self.bypass_proxy_for_market_hosts:
            _append_no_proxy_hosts()

    def _sources(self) -> list[str]:
        return list(dict.fromkeys([self.primary_source, self.fallback_source]))

    def fetch_latest_bars(self, ts_code: str) -> pd.DataFrame:
        """Return five-minute bars, falling back between public data sources."""
        errors: list[str] = []
        attempts = max(int(self.retry_times), 1)
        for source in self._sources():
            for attempt in range(1, attempts + 1):
                try:
                    bars = self._fetch_source(source, ts_code)
                    if bars.empty:
                        raise RuntimeError("接口返回为空")
                    LOGGER.info("%s 行情获取成功，数据源=%s，记录数=%d。", ts_code, source, len(bars))
                    return bars
                except Exception as exc:
                    detail = f"{source} 第{attempt}/{attempts}次失败: {_short_error(exc)}"
                    errors.append(detail)
                    LOGGER.warning("%s %s", ts_code, detail)
                    if attempt < attempts:
                        time.sleep(max(float(self.retry_backoff_seconds), 0.0))
        joined = "；".join(errors)
        raise RuntimeError(f"新浪与东方财富分钟行情均不可用。{joined}")

    def _fetch_source(self, source: str, ts_code: str) -> pd.DataFrame:
        import akshare as ak

        symbol = ts_code.split(".", maxsplit=1)[0].strip()
        if source == "sina":
            exchange = ts_code.split(".", maxsplit=1)[1].lower() if "." in ts_code else infer_exchange(symbol).lower()
            raw = ak.stock_zh_a_minute(symbol=f"{exchange}{symbol}", period=self.period, adjust=self.adjust)
            return self._convert_sina(ts_code, raw)

        raw = ak.stock_zh_a_hist_min_em(symbol=symbol, period=self.period, adjust=self.adjust)
        return self._convert_eastmoney(ts_code, raw)

    def _convert_sina(self, ts_code: str, raw: pd.DataFrame | None) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame(columns=_STANDARD_COLUMNS)
        missing = {"day", "open", "close", "high", "low", "volume", "amount"}.difference(raw.columns)
        if missing:
            raise ValueError(f"新浪分钟行情字段缺失：{', '.join(sorted(missing))}")
        bars = raw.rename(columns={"day": "time", "volume": "vol"})[
            ["time", "open", "close", "high", "low", "vol", "amount"]
        ].copy()
        return self._normalize_bars(ts_code, bars)

    def _convert_eastmoney(self, ts_code: str, raw: pd.DataFrame | None) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame(columns=_STANDARD_COLUMNS)
        missing = {"时间", "开盘", "收盘", "最高", "最低", "成交量", "成交额"}.difference(raw.columns)
        if missing:
            raise ValueError(f"东方财富分钟行情字段缺失：{', '.join(sorted(missing))}")
        bars = raw.rename(
            columns={"时间": "time", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "vol", "成交额": "amount"}
        )[["time", "open", "close", "high", "low", "vol", "amount"]].copy()
        return self._normalize_bars(ts_code, bars)

    def _normalize_bars(self, ts_code: str, bars: pd.DataFrame) -> pd.DataFrame:
        bars.insert(0, "ts_code", ts_code)
        bars["time"] = pd.to_datetime(bars["time"], errors="coerce")
        for col in ("open", "close", "high", "low", "vol", "amount"):
            bars[col] = pd.to_numeric(bars[col], errors="coerce")
        bars = bars.dropna(subset=["time", "close"]).sort_values("time")
        bars = bars.drop_duplicates(subset=["ts_code", "time"], keep="last")
        return bars.tail(max(self.max_bars, 25)).reset_index(drop=True)

    def fetch_stock_master(self) -> pd.DataFrame:
        """Fetch Shanghai/Shenzhen/Beijing A-share code-name pairs for UI search."""
        import akshare as ak

        raw = ak.stock_info_a_code_name()
        columns = ["ts_code", "symbol", "name", "market", "exchange", "industry", "list_date"]
        if raw is None or raw.empty:
            return pd.DataFrame(columns=columns)
        missing = {"code", "name"}.difference(raw.columns)
        if missing:
            raise ValueError(f"AKShare 股票名称库字段缺失：{', '.join(sorted(missing))}")

        master = raw[["code", "name"]].rename(columns={"code": "symbol"}).copy()
        master["symbol"] = master["symbol"].astype(str).str.strip().str.zfill(6)
        master["exchange"] = master["symbol"].map(infer_exchange)
        master = master[master["exchange"] != ""].copy()
        master["ts_code"] = master["symbol"] + "." + master["exchange"]
        master["market"] = "沪深京A股"
        master["industry"] = ""
        master["list_date"] = ""
        return master[columns].drop_duplicates(subset=["ts_code"], keep="last").reset_index(drop=True)


# Backward-compatible import name for any local extension made on the V3 package.
AKShareEastMoneyProvider = AKShareMultiSourceProvider
