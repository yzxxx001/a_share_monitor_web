from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from ..config import HotSectorCacheConfig, HotSectorHttpConfig
from ..market import internal_ts_code
from ..models import ProviderStatus, SectorSnapshot
from .base import ProviderCallResult, ResilientProvider


class SectorDataProvider(ABC):
    @abstractmethod
    def get_industry_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        """获取行业板块当日排名所需数据。"""

    @abstractmethod
    def get_sector_constituents(self, sector_code: str) -> list[dict[str, Any]]:
        """获取指定行业板块成分股。"""

    @abstractmethod
    def get_sector_history(self, sector_code: str, days: int) -> list[dict[str, Any]]:
        """获取板块历史行情，用于后续成交活跃度与持续性分析。"""


class StockDataProvider(ABC):
    @abstractmethod
    def get_stock_snapshot(self, stock_code: str, trade_date: str) -> dict[str, Any]:
        """获取个股当日快照。"""

    @abstractmethod
    def get_daily_bars(self, stock_code: str, days: int) -> list[dict[str, Any]]:
        """获取个股历史日线。"""


class RiskEventProvider:
    warning = "风险数据源暂未接入，不可视为无风险"

    def get_announcements(self, stock_code: str, start_date: str, end_date: str) -> dict[str, Any]:
        return self._not_implemented("announcements", stock_code, start_date, end_date)

    def get_lhb_records(self, start_date: str, end_date: str) -> dict[str, Any]:
        return self._not_implemented("lhb_records", "", start_date, end_date)

    def get_abnormal_trading_events(self, stock_code: str, trade_date: str) -> dict[str, Any]:
        return self._not_implemented("abnormal_trading_events", stock_code, trade_date, trade_date)

    def _not_implemented(self, data_type: str, stock_code: str, start_date: str, end_date: str) -> dict[str, Any]:
        return {
            "status": "not_implemented",
            "data_type": data_type,
            "stock_code": stock_code,
            "start_date": start_date,
            "end_date": end_date,
            "items": [],
            "warning": self.warning,
        }


class AKShareHotSectorProvider(ResilientProvider, SectorDataProvider, StockDataProvider):
    source_name = "akshare"

    def __init__(self, http_config: HotSectorHttpConfig, cache_config: HotSectorCacheConfig) -> None:
        super().__init__("akshare_hot_sector", http_config, cache_config)

    def get_industry_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        result = self.call(
            source=self.source_name,
            target=f"industry_sector_rank:{trade_date}",
            cache_key=f"industry_sector_rank:{trade_date}",
            fetcher=self._fetch_industry_sector_rank,
        )
        return [self._sector_snapshot(row, trade_date, result.status) for row in self._as_records(result)]

    def get_sector_constituents(self, sector_code: str) -> list[dict[str, Any]]:
        result = self.call(
            source=self.source_name,
            target=f"sector_constituents:{sector_code}",
            cache_key=f"sector_constituents:{sector_code}",
            fetcher=lambda: self._fetch_sector_constituents(sector_code),
        )
        return [self._with_status(row, result.status) for row in self._as_records(result)]

    def get_sector_history(self, sector_code: str, days: int) -> list[dict[str, Any]]:
        result = self.call(
            source=self.source_name,
            target=f"sector_history:{sector_code}:{days}",
            cache_key=f"sector_history:{sector_code}:{days}",
            fetcher=lambda: self._fetch_sector_history(sector_code, days),
        )
        return [self._with_status(row, result.status) for row in self._as_records(result)[-days:]]

    def get_stock_snapshot(self, stock_code: str, trade_date: str) -> dict[str, Any]:
        result = self.call(
            source=self.source_name,
            target=f"stock_snapshot:{stock_code}:{trade_date}",
            cache_key=f"stock_snapshot:{stock_code}:{trade_date}",
            fetcher=lambda: self._fetch_stock_snapshot(stock_code),
        )
        records = self._as_records(result)
        payload = records[0] if records else {}
        return self._with_status(payload, result.status)

    def get_daily_bars(self, stock_code: str, days: int) -> list[dict[str, Any]]:
        result = self.call(
            source=self.source_name,
            target=f"daily_bars:{stock_code}:{days}",
            cache_key=f"daily_bars:{stock_code}:{days}",
            fetcher=lambda: self._fetch_daily_bars(stock_code, days),
        )
        return [self._with_status(row, result.status) for row in self._as_records(result)[-days:]]

    def _fetch_industry_sector_rank(self) -> Any:
        import akshare as ak

        return ak.stock_board_industry_name_em().to_dict("records")

    def _fetch_sector_constituents(self, sector_code: str) -> Any:
        import akshare as ak

        return ak.stock_board_industry_cons_em(symbol=sector_code).to_dict("records")

    def _fetch_sector_history(self, sector_code: str, days: int) -> Any:
        import akshare as ak

        return ak.stock_board_industry_hist_em(symbol=sector_code).tail(days).to_dict("records")

    def _fetch_stock_snapshot(self, stock_code: str) -> Any:
        import akshare as ak

        symbol = stock_code.split(".", maxsplit=1)[0]
        raw = ak.stock_zh_a_spot_em()
        return raw[raw["代码"].astype(str).str.zfill(6) == symbol].to_dict("records")

    def _fetch_daily_bars(self, stock_code: str, days: int) -> Any:
        import akshare as ak

        symbol = stock_code.split(".", maxsplit=1)[0]
        return ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="").tail(days).to_dict("records")

    @staticmethod
    def _as_records(result: ProviderCallResult) -> list[dict[str, Any]]:
        payload = result.payload
        if payload is None:
            return []
        if hasattr(payload, "to_dict"):
            return payload.to_dict("records")
        if isinstance(payload, list):
            return [dict(item) for item in payload]
        if isinstance(payload, dict):
            return [payload]
        return []

    @staticmethod
    def _with_status(row: dict[str, Any], status: ProviderStatus) -> dict[str, Any]:
        enriched = dict(row)
        enriched["_provider_status"] = status
        return enriched

    def _sector_snapshot(self, row: dict[str, Any], trade_date: str, status: ProviderStatus) -> SectorSnapshot:
        code = self._first_text(row, "板块代码", "代码", "sector_code", "code", default=self._first_text(row, "板块名称", "名称", "name"))
        name = self._first_text(row, "板块名称", "名称", "sector_name", "name", default=code)
        return SectorSnapshot(
            sector_code=code,
            sector_name=name,
            trade_date=trade_date,
            change_pct=self._first_float(row, "涨跌幅", "change_pct"),
            turnover=self._first_float(row, "换手率", "turnover"),
            amount=self._first_float(row, "成交额", "amount"),
            net_inflow=self._first_float(row, "主力净流入", "净流入", "net_inflow"),
            up_count=self._first_int(row, "上涨家数", "up_count"),
            down_count=self._first_int(row, "下跌家数", "down_count"),
            source=self.source_name,
            data_time=datetime.now(),
            provider_status=status,
            is_complete=status.ok,
            warnings=list(status.warnings),
            raw=dict(row),
        )

    @staticmethod
    def normalize_stock_code(stock_code: str) -> str:
        return internal_ts_code(stock_code.split(".", maxsplit=1)[0])

    @staticmethod
    def _first_text(row: dict[str, Any], *keys: str, default: str = "") -> str:
        for key in keys:
            value = row.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return default

    @staticmethod
    def _first_float(row: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = row.get(key)
            try:
                if value is not None and str(value).strip() != "":
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _first_int(row: dict[str, Any], *keys: str) -> int | None:
        value = AKShareHotSectorProvider._first_float(row, *keys)
        return None if value is None else int(value)
