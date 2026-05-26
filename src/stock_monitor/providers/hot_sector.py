from __future__ import annotations

from abc import ABC, abstractmethod
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import HotSectorCacheConfig, HotSectorHttpConfig
from ..market import append_market_no_proxy_hosts, internal_ts_code
from ..models import ProviderStatus, SectorSnapshot
from .base import ProviderCallError, ProviderCallResult, ResilientProvider


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
    industry_rank_sources = (
        "eastmoney_direct",
        "akshare_eastmoney",
        "akshare_ths_summary",
    )

    def __init__(self, http_config: HotSectorHttpConfig, cache_config: HotSectorCacheConfig) -> None:
        append_market_no_proxy_hosts()
        super().__init__("akshare_hot_sector", http_config, cache_config)

    def get_industry_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        errors: list[ProviderStatus] = []
        for source, fetcher in self._industry_rank_fetchers(trade_date):
            try:
                result = self.call(
                    source=source,
                    target=f"industry_sector_rank:{trade_date}",
                    cache_key=f"industry_sector_rank:{source}:{trade_date}",
                    fetcher=fetcher,
                )
                rows = [self._sector_snapshot(row, trade_date, result.status) for row in self._as_records(result)]
                if rows and not self._has_rank_fields(rows):
                    raise ProviderCallError(
                        ProviderStatus(
                            provider=self.provider_name,
                            source=source,
                            target=f"industry_sector_rank:{trade_date}",
                            ok=False,
                            retry_count=result.status.retry_count,
                            error="行业板块数据源仅返回名称/代码，缺少涨跌幅或上涨覆盖率等排名字段。",
                            warnings=["行业板块数据字段不足，不能用于热门板块评分。"],
                        )
                    )
                if errors:
                    warning = "主数据源请求失败，已自动切换备用行业板块数据源。"
                    rows = [self._append_snapshot_warning(row, warning) for row in rows]
                if not result.status.is_cached:
                    self._save_last_successful_rank_source(source)
                return rows
            except ProviderCallError as exc:
                errors.append(exc.status)
        raise ProviderCallError(self._combined_failure_status("industry_sector_rank", trade_date, errors))

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

    def _fetch_industry_sector_rank_em(self) -> Any:
        import akshare as ak

        return ak.stock_board_industry_name_em().to_dict("records")

    def _fetch_industry_sector_rank_em_direct(self) -> list[dict[str, Any]]:
        payload = self.http_get_json_direct(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1,
                "pz": 200,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:90 t:2 f:!50",
                "fields": "f3,f6,f8,f12,f14,f62,f104,f105",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://quote.eastmoney.com/center/boardlist.html",
            },
        )
        rows = ((payload or {}).get("data") or {}).get("diff") or []
        return [self._convert_em_direct_row(row) for row in rows]

    def _fetch_industry_sector_rank_ths_summary(self) -> Any:
        import akshare as ak

        return ak.stock_board_industry_summary_ths().to_dict("records")

    def _industry_rank_fetchers(self, trade_date: str) -> list[tuple[str, Any]]:
        fetchers = {
            "eastmoney_direct": self._fetch_industry_sector_rank_em_direct,
            "akshare_eastmoney": self._fetch_industry_sector_rank_em,
            "akshare_ths_summary": self._fetch_industry_sector_rank_ths_summary,
        }
        ordered = list(self.industry_rank_sources)
        preferred: list[str] = []
        last_success = self._last_successful_rank_source()
        if last_success:
            preferred.append(last_success)
        preferred.extend(source for source in ordered if self.cache.exists(f"industry_sector_rank:{source}:{trade_date}"))
        ordered = list(dict.fromkeys([*preferred, *ordered]))
        return [(source, fetchers[source]) for source in ordered if source in fetchers]

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

    @staticmethod
    def _convert_em_direct_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "板块代码": row.get("f12"),
            "板块名称": row.get("f14"),
            "涨跌幅": row.get("f3"),
            "成交额": row.get("f6"),
            "换手率": row.get("f8"),
            "主力净流入": row.get("f62"),
            "上涨家数": row.get("f104"),
            "下跌家数": row.get("f105"),
        }

    def _sector_snapshot(self, row: dict[str, Any], trade_date: str, status: ProviderStatus) -> SectorSnapshot:
        code = self._first_text(row, "板块代码", "代码", "sector_code", "code", default=self._first_text(row, "板块", "板块名称", "名称", "行业", "name"))
        name = self._first_text(row, "板块", "板块名称", "名称", "行业", "sector_name", "name", default=code)
        return SectorSnapshot(
            sector_code=code,
            sector_name=name,
            trade_date=trade_date,
            change_pct=self._first_float(row, "涨跌幅", "涨幅", "change_pct", "pct_change"),
            turnover=self._first_float(row, "换手率", "换手", "turnover", "turnover_rate"),
            amount=self._first_float(row, "总成交额", "成交额", "amount"),
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
    def _append_snapshot_warning(snapshot: SectorSnapshot, warning: str) -> SectorSnapshot:
        from dataclasses import replace

        return replace(snapshot, warnings=list(dict.fromkeys([*snapshot.warnings, warning])))

    @staticmethod
    def _has_rank_fields(rows: list[SectorSnapshot]) -> bool:
        return any(snapshot.change_pct is not None for snapshot in rows)

    def _combined_failure_status(self, target: str, trade_date: str, errors: list[ProviderStatus]) -> ProviderStatus:
        details = "；".join(f"{item.source}: {item.error}" for item in errors if item.error)
        warnings = [warning for item in errors for warning in item.warnings]
        return ProviderStatus(
            provider=self.provider_name,
            source="+".join(self.industry_rank_sources),
            target=f"{target}:{trade_date}",
            ok=False,
            retry_count=sum(item.retry_count for item in errors),
            error=f"所有行业板块数据源均不可用：{details or 'unknown error'}",
            warnings=list(dict.fromkeys([*warnings, "所有行业板块数据源均不可用，且无可用缓存。"])),
        )

    def _source_health_path(self) -> Path:
        return self.cache.directory / "_source_health_industry_rank.json"

    def _last_successful_rank_source(self) -> str | None:
        if not self.cache.enabled:
            return None
        path = self._source_health_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        source = str(payload.get("source") or "")
        return source if source in self.industry_rank_sources else None

    def _save_last_successful_rank_source(self, source: str) -> None:
        if not self.cache.enabled:
            return
        try:
            self._source_health_path().write_text(
                json.dumps({"source": source, "saved_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return

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
