from __future__ import annotations

from abc import ABC, abstractmethod
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import HotSectorCacheConfig, HotSectorHttpConfig
from ..market import append_market_no_proxy_hosts, internal_ts_code
from ..models import ProviderStatus, SectorSnapshot
from .base import ProviderCallError, ProviderCallResult, ResilientProvider


def _reset_requests_sessions() -> None:
    """在 AKShare 调用前加入随机延迟并清理连接池，降低连续请求触发服务端限速或 RemoteDisconnected 的概率。"""
    import random
    import time

    time.sleep(random.uniform(0.15, 0.6))
    try:
        # 关闭 urllib3 连接池中可能残留的 keep-alive 连接
        import requests.adapters as _ra
        _pool_manager = getattr(_ra.HTTPAdapter(), "poolmanager", None)
        if _pool_manager is not None:
            _pool_manager.clear()
    except Exception:
        pass


class SectorDataProvider(ABC):
    @abstractmethod
    def get_industry_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        """获取行业板块当日排名所需数据。"""

    def get_concept_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        """获取概念板块当日排名所需数据。默认未实现，由支持的 provider 覆盖。"""
        raise NotImplementedError("当前数据源未实现概念板块排名。")

    def get_sector_five_day_metrics(self, sectors: list[tuple[str, str]], board_type: str = "industry") -> dict[str, dict[str, float | None]]:
        """补全给定板块的近5日板块涨跌与相对基准超额。默认不支持，返回空 dict。"""
        return {}

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
    concept_rank_sources = (
        "eastmoney_direct",
        "akshare_eastmoney",
        "ths_concept",
    )

    def __init__(self, http_config: HotSectorHttpConfig, cache_config: HotSectorCacheConfig) -> None:
        append_market_no_proxy_hosts()
        super().__init__("akshare_hot_sector", http_config, cache_config)

    def get_industry_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        return self._get_sector_rank(trade_date, "industry")

    def get_concept_sector_rank(self, trade_date: str) -> list[SectorSnapshot]:
        return self._get_sector_rank(trade_date, "concept")

    def _get_sector_rank(self, trade_date: str, board_type: str) -> list[SectorSnapshot]:
        label = "概念" if board_type == "concept" else "行业"
        errors: list[ProviderStatus] = []
        for source, fetcher in self._rank_fetchers(trade_date, board_type):
            try:
                result = self.call(
                    source=source,
                    target=f"{board_type}_sector_rank:{trade_date}",
                    cache_key=f"{board_type}_sector_rank:{source}:{trade_date}",
                    fetcher=fetcher,
                )
                rows = [self._sector_snapshot(row, trade_date, result.status) for row in self._as_records(result)]
                if rows and not self._has_rank_fields(rows):
                    raise ProviderCallError(
                        ProviderStatus(
                            provider=self.provider_name,
                            source=source,
                            target=f"{board_type}_sector_rank:{trade_date}",
                            ok=False,
                            retry_count=result.status.retry_count,
                            error=f"{label}板块数据源仅返回名称/代码，缺少涨跌幅或上涨覆盖率等排名字段。",
                            warnings=[f"{label}板块数据字段不足，不能用于热门板块评分。"],
                        )
                    )
                if errors:
                    warning = f"主数据源请求失败，已自动切换备用{label}板块数据源。"
                    rows = [self._append_snapshot_warning(row, warning) for row in rows]
                if not result.status.is_cached:
                    self._save_last_successful_rank_source(board_type, source)
                return rows
            except ProviderCallError as exc:
                errors.append(exc.status)
        raise ProviderCallError(self._combined_failure_status(f"{board_type}_sector_rank", trade_date, errors, board_type))

    def get_sector_constituents(self, sector_code: str) -> list[dict[str, Any]]:
        fetchers: list[tuple[str, Any]] = []
        if str(sector_code).strip().upper().startswith("BK"):
            fetchers.append(("eastmoney_direct", lambda: self._fetch_sector_constituents_em_direct(sector_code)))
        fetchers.append((self.source_name, lambda: self._fetch_sector_constituents(sector_code)))

        errors: list[ProviderStatus] = []
        for source, fetcher in fetchers:
            try:
                result = self.call(
                    source=source,
                    target=f"sector_constituents:{sector_code}",
                    cache_key=f"sector_constituents:{sector_code}",
                    fetcher=fetcher,
                )
                rows = self._as_records(result)
                if rows:
                    return [self._with_status(row, result.status) for row in rows]
            except ProviderCallError as exc:
                errors.append(exc.status)

        # 东方财富所有数据源均失败，尝试同花顺板块页面作为最后备用
        ths_rows = self._fetch_sector_constituents_ths(sector_code)
        if ths_rows:
            self.cache.save(f"sector_constituents:{sector_code}", ths_rows)
            ths_status = ProviderStatus(
                provider=self.provider_name,
                source="ths_fallback",
                target=f"sector_constituents:{sector_code}",
                ok=True,
                is_cached=False,
                is_stale=False,
                cached_at=datetime.now(),
                retry_count=0,
                warnings=[f"东方财富数据源不可用，已使用同花顺板块数据（共{len(ths_rows)}支、分类体系与东方财富略有差异）。"],
            )
            self.logger.warning(
                "provider=%s target=sector_constituents:%s using_ths_fallback stocks=%d",
                self.provider_name,
                sector_code,
                len(ths_rows),
            )
            return [self._with_status(row, ths_status) for row in ths_rows]

        raise ProviderCallError(self._combined_failure_status("sector_constituents", sector_code, errors))

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

        _reset_requests_sessions()
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
                "Referer": "https://quote.eastmoney.com/center/boardlist.html",
                "Origin": "https://quote.eastmoney.com",
            },
        )
        rows = ((payload or {}).get("data") or {}).get("diff") or []
        return [self._convert_em_direct_row(row) for row in rows]

    def _fetch_industry_sector_rank_ths_summary(self) -> Any:
        import akshare as ak

        _reset_requests_sessions()
        return ak.stock_board_industry_summary_ths().to_dict("records")

    def _fetch_concept_sector_rank_em(self) -> Any:
        import akshare as ak

        _reset_requests_sessions()
        return ak.stock_board_concept_name_em().to_dict("records")

    def _fetch_concept_sector_rank_em_direct(self) -> list[dict[str, Any]]:
        payload = self.http_get_json_direct(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1,
                "pz": 500,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:90 t:3 f:!50",
                "fields": "f3,f6,f8,f12,f14,f62,f104,f105",
            },
            headers={
                "Referer": "https://quote.eastmoney.com/center/boardlist.html",
                "Origin": "https://quote.eastmoney.com",
            },
        )
        rows = ((payload or {}).get("data") or {}).get("diff") or []
        return [self._convert_em_direct_row(row) for row in rows]

    _THS_CONCEPT_MAX_PAGES = 4  # 每页 50 个概念，按涨跌幅降序，4 页≈200 强势概念足够

    def _fetch_concept_sector_rank_ths(self) -> list[dict[str, Any]]:
        """同花顺「概念资金流」备用源（东方财富不可用时使用），抓取 data.10jqka.com.cn/funds/gnzjl。

        该页按涨跌幅降序，含涨跌幅与资金流（无上涨/下跌家数，评分时按缺覆盖率降级处理）。
        网络异常向上抛出（call() 按可重试错误处理）；解析为空抛 ValueError（不可重试，交由上层
        切换下一个数据源），避免把空结果当作成功缓存而拦住其它数据源。
        """
        import inspect
        import py_mini_racer
        import requests as _req
        import akshare as _ak

        _mod = inspect.getmodule(_ak.stock_board_industry_name_ths)
        _js = py_mini_racer.MiniRacer()
        _js.eval(_mod._get_file_content_ths("ths.js"))
        v_code = _js.call("v")

        session = _req.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Cookie": f"v={v_code}",
            "Referer": "http://data.10jqka.com.cn/funds/gnzjl/",
        })

        results: list[dict[str, Any]] = []
        for page in range(1, self._THS_CONCEPT_MAX_PAGES + 1):
            resp = session.get(
                f"http://data.10jqka.com.cn/funds/gnzjl/field/tradezdf/order/desc/page/{page}/ajax/1/",
                timeout=12,
            )
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "gbk"
            page_rows = self._parse_ths_concept_fund_page(resp.text)
            if not page_rows:
                break
            results.extend(page_rows)
            if len(page_rows) < 50:
                break
            time.sleep(0.3)

        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for row in results:
            name = row["板块名称"]
            if name in seen:
                continue
            seen.add(name)
            deduped.append(row)

        if not deduped:
            raise ValueError("同花顺概念资金流页解析为空")
        self.logger.info("THS concept fund fallback: concepts=%d", len(deduped))
        return deduped

    @staticmethod
    def _parse_ths_number(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).strip().replace(",", "").replace("%", "").replace("亿", "").replace("万", "")
        if text in ("", "-", "--", "nan", "None"):
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @classmethod
    def _parse_ths_concept_fund_page(cls, html: str) -> list[dict[str, Any]]:
        """解析同花顺概念资金流页（整页或 AJAX 片段）。

        实测列：序号 / 行业 / 行业指数 / 涨跌幅 / 流入资金(亿) / 流出资金(亿) / 净额(亿) /
        公司家数 / 领涨股 / 涨跌幅.1(领涨股) / 当前价(元)。按子串匹配列，并排除领涨股的涨跌幅列。
        """
        try:
            from io import StringIO
            import pandas as _pd
            tables = _pd.read_html(StringIO(html))
        except Exception:
            return []
        if not tables:
            return []
        df = max(tables, key=lambda t: t.shape[0])

        def col(*subs: str, exclude: tuple[str, ...] = ()) -> Any:
            for sub in subs:
                for original in df.columns:
                    name = str(original)
                    if sub in name and not any(bad in name for bad in exclude):
                        return original
            return None

        name_c = col("概念名称", "板块名称", "行业", "概念", "名称", exclude=("指数", "领涨", "领跌"))
        pct_c = col("涨跌幅", "涨幅", exclude=(".1", "领涨", "领跌", "当前", "5日", "20日"))
        if name_c is None or pct_c is None:
            return []
        net_c = col("净额", "净流入", "净额(亿)")
        inflow_c = col("流入资金", "流入")
        outflow_c = col("流出资金", "流出")
        count_c = col("公司家数", "成分股", "家数", exclude=("上涨", "下跌"))

        results: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            name = str(row.get(name_c, "")).strip()
            if not name or name in ("行业", "概念名称", "板块", "名称", "-", "--", "nan"):
                continue
            pct = cls._parse_ths_number(row.get(pct_c))
            if pct is None:
                continue
            record: dict[str, Any] = {"板块名称": name, "板块代码": name, "涨跌幅": pct}
            if net_c is not None:
                net = cls._parse_ths_number(row.get(net_c))
                if net is not None:
                    record["主力净流入"] = net
            inflow = cls._parse_ths_number(row.get(inflow_c)) if inflow_c is not None else None
            outflow = cls._parse_ths_number(row.get(outflow_c)) if outflow_c is not None else None
            if inflow is not None and outflow is not None:
                record["总成交额"] = inflow + outflow
            if count_c is not None:
                count = cls._parse_ths_number(row.get(count_c))
                if count is not None:
                    record["total_stock_count"] = count
            results.append(record)
        return results

    def _rank_fetchers(self, trade_date: str, board_type: str) -> list[tuple[str, Any]]:
        if board_type == "concept":
            fetchers: dict[str, Any] = {
                "eastmoney_direct": self._fetch_concept_sector_rank_em_direct,
                "akshare_eastmoney": self._fetch_concept_sector_rank_em,
                "ths_concept": self._fetch_concept_sector_rank_ths,
            }
            ordered = ["eastmoney_direct", "akshare_eastmoney", "ths_concept"]
        else:
            fetchers = {
                "eastmoney_direct": self._fetch_industry_sector_rank_em_direct,
                "akshare_eastmoney": self._fetch_industry_sector_rank_em,
                "akshare_ths_summary": self._fetch_industry_sector_rank_ths_summary,
            }
            ordered = list(self.industry_rank_sources)
        # 主源（默认顺序首位，即东方财富直连）始终最先尝试：历史上某次偶发失败会把降级源
        # 记为“上次成功源”，若允许它排到主源之前，则主源永远轮不到重试、即使早已恢复，
        # 形成自我维持的粘滞回退。这里把 last_success/缓存偏好限制在“非主源”范围内，仅用于
        # 主源失败后优先选已知可用的备用源，从而保留快速回退又避免锁死主源。
        primary = ordered[0]
        preferred: list[str] = []
        last_success = self._last_successful_rank_source(board_type)
        if last_success and last_success in fetchers and last_success != primary:
            preferred.append(last_success)
        preferred.extend(
            source
            for source in ordered
            if source != primary and self.cache.exists(f"{board_type}_sector_rank:{source}:{trade_date}")
        )
        ordered = list(dict.fromkeys([primary, *preferred, *ordered]))
        return [(source, fetchers[source]) for source in ordered if source in fetchers]

    def _fetch_sector_constituents(self, sector_code: str) -> Any:
        import akshare as ak

        _reset_requests_sessions()
        return ak.stock_board_industry_cons_em(symbol=sector_code).to_dict("records")

    _THS_MAX_PAGES = 15  # safety cap — 15 × ~20 stocks ≈ 300 max

    def _fetch_sector_constituents_ths(self, sector_name: str) -> list[dict[str, Any]]:
        """同花顺板块成分股备用数据源（东方财富不可用时使用），支持多页获取全量成分股。"""
        try:
            import inspect
            import py_mini_racer
            import requests as _req
            import akshare as _ak

            _mod = inspect.getmodule(_ak.stock_board_industry_name_ths)
            _js = py_mini_racer.MiniRacer()
            _js.eval(_mod._get_file_content_ths("ths.js"))
            v_code = _js.call("v")

            name_code_map: dict[str, str] = _mod._get_stock_board_industry_name_ths()
            ths_code = self._match_ths_sector(sector_name, name_code_map)
            if not ths_code:
                self.logger.debug("THS sector match not found for: %s", sector_name)
                return []

            session = _req.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Cookie": f"v={v_code}",
            })

            # Page 1: also determines total page count via "1/N" marker
            resp1 = session.get(
                f"http://q.10jqka.com.cn/thshy/detail/code/{ths_code}/",
                timeout=12,
            )
            resp1.raise_for_status()
            m = re.search(r'class="page_info">(\d+)/(\d+)', resp1.text)
            total_pages = min(int(m.group(2)) if m else 1, self._THS_MAX_PAGES)

            results = self._parse_ths_constituent_page(resp1.text)

            for page in range(2, total_pages + 1):
                time.sleep(0.3)
                try:
                    resp_n = session.get(
                        f"http://q.10jqka.com.cn/thshy/detail/code/{ths_code}/order/desc/page/{page}/ajax/1/",
                        timeout=10,
                    )
                    resp_n.raise_for_status()
                    if len(resp_n.text) < 500 and "chameleon" in resp_n.text.lower():
                        self.logger.debug(
                            "THS page %d: anti-scrape challenge, stopping with %d stocks so far",
                            page, len(results),
                        )
                        break
                    page_rows = self._parse_ths_constituent_page(resp_n.text)
                    if not page_rows:
                        break
                    results.extend(page_rows)
                except Exception as exc:
                    self.logger.debug("THS page %d fetch failed: %s", page, exc)
                    break

            self.logger.info(
                "THS constituent fallback: sector=%s ths_code=%s pages_fetched=%d/%d stocks=%d",
                sector_name, ths_code, min(page, total_pages) if total_pages > 1 else 1, total_pages, len(results),
            )
            return results
        except Exception as exc:
            self.logger.debug("THS constituent fallback error: %s", exc)
            return []

    @staticmethod
    def _parse_ths_constituent_page(html: str) -> list[dict[str, Any]]:
        """Parse one THS constituent HTML page (full page or AJAX fragment) into stock dicts."""
        try:
            from io import StringIO
            import pandas as _pd
            tables = _pd.read_html(StringIO(html))
            if not tables:
                return []
            df = tables[0]
            results: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                raw_code = row.get("代码")
                if raw_code is None:
                    continue
                code = str(raw_code).strip().split(".")[0].zfill(6)
                if not code or code == "000000":
                    continue
                results.append({
                    "代码": code,
                    "名称": str(row.get("名称", "")).strip(),
                    "涨跌幅": row.get("涨跌幅(%)"),
                    "换手率": row.get("换手(%)"),
                })
            return results
        except Exception:
            return []

    @staticmethod
    def _match_ths_sector(sector_name: str, name_code_map: dict[str, str]) -> str | None:
        """将东方财富板块名称模糊匹配到同花顺板块代码（Jaccard 字符集相似度）。"""
        if sector_name in name_code_map:
            return name_code_map[sector_name]
        chars = set(sector_name)
        best_code: str | None = None
        best_score = 0.0
        for name, code in name_code_map.items():
            overlap = len(chars & set(name))
            score = overlap / max(len(chars), len(name))
            if score > best_score:
                best_score = score
                best_code = code
        return best_code if best_score >= 0.25 else None

    def _fetch_sector_constituents_em_direct(self, sector_code: str) -> list[dict[str, Any]]:
        board_code = str(sector_code).strip().upper()
        payload = self.http_get_json_direct(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1,
                "pz": 500,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": f"b:{board_code}",
                "fields": "f2,f3,f6,f8,f12,f14,f15,f62",
            },
            headers={
                "Referer": "https://quote.eastmoney.com/center/boardlist.html",
                "Origin": "https://quote.eastmoney.com",
            },
        )
        rows = ((payload or {}).get("data") or {}).get("diff") or []
        return [self._convert_em_constituent_row(row) for row in rows]

    def _fetch_sector_history(self, sector_code: str, days: int) -> Any:
        import akshare as ak

        _reset_requests_sessions()
        return ak.stock_board_industry_hist_em(symbol=sector_code).tail(days).to_dict("records")

    def _fetch_stock_snapshot(self, stock_code: str) -> Any:
        symbol = stock_code.split(".", maxsplit=1)[0].zfill(6)
        # 1. 优先：东方财富单只股票实时接口（push2 单股查询，交易时间内有效）
        try:
            rows = self._fetch_stock_snapshot_realtime(symbol)
            if rows:
                return rows
        except Exception:
            pass
        # 2. 备用：直连 push2his kline 接口获取最近日线（绕过代理，避免 clist 拦截）
        try:
            return self._fetch_stock_snapshot_from_kline(symbol)
        except Exception:
            pass
        # 3. 新浪日线最近一行作为快照（stock_zh_a_daily 走新浪，不受东方财富代理影响）
        try:
            return self._fetch_stock_snapshot_from_sina_daily(symbol)
        except Exception:
            pass
        # 4. 最后备用：akshare 东方财富（走系统代理，可能被拦截）
        import akshare as ak
        _reset_requests_sessions()
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="")
        if df is None or df.empty:
            return []
        return [df.iloc[-1].to_dict()]

    def _fetch_stock_snapshot_from_kline(self, symbol: str) -> list[dict[str, Any]]:
        """通过 push2his kline 接口直连获取最近日线数据作为快照（绕过代理，该接口可访问）。"""
        from ..market import infer_exchange
        exchange = infer_exchange(symbol)
        exchange_id = "1" if exchange == "SH" else "0"
        secid = f"{exchange_id}.{symbol}"
        payload = self.http_get_json_direct(
            "http://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "0",
                "end": "20500101",
                "lmt": 1,
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        klines = ((payload or {}).get("data") or {}).get("klines") or []
        if not klines:
            return []
        # 格式: "日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率"
        parts = str(klines[-1]).split(",")
        if len(parts) < 11:
            return []
        try:
            return [{
                "日期": parts[0],
                "股票代码": symbol,
                "开盘": float(parts[1]),
                "收盘": float(parts[2]),
                "最高": float(parts[3]),
                "最低": float(parts[4]),
                "成交量": float(parts[5]),
                "成交额": float(parts[6]),
                "振幅": float(parts[7]),
                "涨跌幅": float(parts[8]),
                "涨跌额": float(parts[9]),
                "换手率": float(parts[10]),
            }]
        except (ValueError, IndexError):
            return []

    def _fetch_stock_snapshot_realtime(self, symbol: str) -> list[dict[str, Any]]:
        from ..market import infer_exchange
        exchange = infer_exchange(symbol)
        exchange_id = "1" if exchange == "SH" else "0"
        payload = self.http_get_json_direct(
            "http://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": f"{exchange_id}.{symbol}", "fields": "f2,f3,f6,f8,f12,f14,f62"},
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        data = (payload or {}).get("data") or {}
        if not data or data.get("f12") is None:
            return []
        return [{
            "代码": str(data.get("f12", symbol)).zfill(6),
            "名称": data.get("f14", ""),
            "最新价": data.get("f2"),
            "涨跌幅": data.get("f3"),
            "成交额": data.get("f6"),
            "换手率": data.get("f8"),
            "主力净流入": data.get("f62"),
        }]

    def _fetch_daily_bars(self, stock_code: str, days: int) -> Any:
        symbol = stock_code.split(".", maxsplit=1)[0].zfill(6)
        # 1. 直连 push2his kline 接口（绕过代理）
        try:
            return self._fetch_daily_bars_direct(symbol, days)
        except Exception:
            pass
        # 2. 新浪日线（stock_zh_a_daily 走新浪，不受东方财富代理影响）
        try:
            return self._fetch_daily_bars_sina(symbol, days)
        except Exception:
            pass
        # 3. akshare 东方财富备用（走系统代理）
        import akshare as ak
        _reset_requests_sessions()
        return ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="").tail(days).to_dict("records")

    def _fetch_daily_bars_direct(self, symbol: str, days: int) -> list[dict[str, Any]]:
        """直连 push2his kline 接口获取日线（绕过代理，避免 clist 拦截影响）。"""
        from ..market import infer_exchange
        exchange = infer_exchange(symbol)
        exchange_id = "1" if exchange == "SH" else "0"
        secid = f"{exchange_id}.{symbol}"
        payload = self.http_get_json_direct(
            "http://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "0",
                "end": "20500101",
                "lmt": days,
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        klines = ((payload or {}).get("data") or {}).get("klines") or []
        results: list[dict[str, Any]] = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 11:
                continue
            try:
                results.append({
                    "日期": parts[0],
                    "股票代码": symbol,
                    "开盘": float(parts[1]),
                    "收盘": float(parts[2]),
                    "最高": float(parts[3]),
                    "最低": float(parts[4]),
                    "成交量": float(parts[5]),
                    "成交额": float(parts[6]),
                    "振幅": float(parts[7]),
                    "涨跌幅": float(parts[8]),
                    "涨跌额": float(parts[9]),
                    "换手率": float(parts[10]),
                })
            except (ValueError, IndexError):
                continue
        if not results:
            raise ValueError(f"push2his returned empty klines for {symbol}")
        return results

    def _fetch_daily_bars_sina(self, symbol: str, days: int) -> list[dict[str, Any]]:
        """新浪日线日期获取（ak.stock_zh_a_daily），不走东方财富，代理受阻时的可靠备用。
        返回字段与 push2his 接口对齐（中文列名 + 换手率为百分比）。
        仅支持 SH/SZ，北交所股票会直接 raise 以便 caller 继续下一个 fallback。"""
        import akshare as ak
        from ..market import infer_exchange
        exchange = infer_exchange(symbol).lower()
        if exchange not in ("sh", "sz"):
            raise ValueError(f"stock_zh_a_daily: {exchange.upper()} not supported for {symbol}")
        df = ak.stock_zh_a_daily(symbol=f"{exchange}{symbol}", adjust="")
        if df is None or df.empty:
            raise ValueError(f"stock_zh_a_daily returned empty for {symbol}")
        df = df.tail(days + 1).copy()
        # pct_change from consecutive closes
        df["涨跌幅"] = df["close"].pct_change() * 100
        df = df.dropna(subset=["close"]).tail(days)
        results: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            turnover = row.get("turnover")
            results.append({
                "日期": str(row["date"]),
                "股票代码": symbol,
                "开盘": float(row["open"]),
                "收盘": float(row["close"]),
                "最高": float(row["high"]),
                "最低": float(row["low"]),
                "成交量": float(row["volume"]),
                "成交额": float(row["amount"]),
                "涨跌幅": float(row["涨跌幅"]) if row["涨跌幅"] == row["涨跌幅"] else None,
                "换手率": float(turnover) * 100 if turnover is not None else None,
            })
        return results

    def _fetch_stock_snapshot_from_sina_daily(self, symbol: str) -> list[dict[str, Any]]:
        """用新浪日线的最后一行作为当日快照，计算涨跌幅 = (close - prev_close) / prev_close * 100。"""
        import akshare as ak
        from ..market import infer_exchange
        exchange = infer_exchange(symbol).lower()
        if exchange not in ("sh", "sz"):
            raise ValueError(f"stock_zh_a_daily: {exchange.upper()} not supported for {symbol}")
        df = ak.stock_zh_a_daily(symbol=f"{exchange}{symbol}", adjust="")
        if df is None or len(df) < 2:
            raise ValueError(f"stock_zh_a_daily returned insufficient data for {symbol}")
        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev_close = float(prev["close"])
        pct = (float(last["close"]) - prev_close) / prev_close * 100 if prev_close else None
        turnover = last.get("turnover")
        return [{
            "日期": str(last["date"]),
            "股票代码": symbol,
            "开盘": float(last["open"]),
            "收盘": float(last["close"]),
            "最高": float(last["high"]),
            "最低": float(last["low"]),
            "成交量": float(last["volume"]),
            "成交额": float(last["amount"]),
            "涨跌幅": pct,
            "换手率": float(turnover) * 100 if turnover is not None else None,
        }]

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

    @staticmethod
    def _convert_em_constituent_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "代码": row.get("f12"),
            "名称": row.get("f14"),
            "最新价": row.get("f2"),
            "涨跌幅": row.get("f3"),
            "成交额": row.get("f6"),
            "换手率": row.get("f8"),
            "最高": row.get("f15"),
            "主力净流入": row.get("f62"),
        }

    # ---- 近5日相对表现补全（仅东方财富可达时生效，用于展示，不并入综合评分）----
    _FIVE_DAY_BENCHMARK_SECID = "1.000300"  # 沪深300 作为相对基准
    _FIVE_DAY_KLINE_LIMIT = 6  # 取6个交易日收盘，跨5个交易日间隔

    def get_sector_five_day_metrics(self, sectors: list[tuple[str, str]], board_type: str = "industry") -> dict[str, dict[str, float | None]]:
        """对 (名称, 代码) 列表补全近5日板块涨跌与相对沪深300超额。

        仅依赖东方财富 push2his 板块K线，best-effort：东方财富不可达时返回空 dict，
        调用方维持中性降级。
        """
        metrics: dict[str, dict[str, float | None]] = {}
        if not sectors:
            return metrics
        try:
            code_map = self._resolve_board_codes(sectors, board_type)
        except Exception as exc:
            self.logger.info("近5日补全：板块代码解析失败，跳过：%s", self._short_error(exc))
            return metrics
        if not code_map:
            return metrics
        try:
            benchmark = self._fetch_five_day_return(self._FIVE_DAY_BENCHMARK_SECID)
        except Exception:
            benchmark = None
        for name, _code in sectors:
            bk = code_map.get(name)
            if not bk:
                continue
            try:
                board_ret = self._fetch_five_day_return(f"90.{bk}")
            except Exception:
                board_ret = None
            if board_ret is None:
                continue
            relative = None if benchmark is None else round(board_ret - benchmark, 2)
            metrics[name] = {"return_5d": round(board_ret, 2), "relative_5d": relative}
        return metrics

    def _resolve_board_codes(self, sectors: list[tuple[str, str]], board_type: str) -> dict[str, str]:
        """把板块名映射到东方财富 BK 代码。已是 BK 代码的直接用；否则查一次东财板块名表并模糊匹配。"""
        resolved: dict[str, str] = {}
        need_lookup: list[str] = []
        for name, code in sectors:
            token = str(code or "").strip().upper()
            if token.startswith("BK"):
                resolved[name] = token
            elif name:
                need_lookup.append(name)
        if need_lookup:
            name_to_code = self._fetch_board_name_code_map(board_type)
            for name in need_lookup:
                bk = name_to_code.get(name) or self._match_ths_sector(name, name_to_code)
                if bk:
                    resolved[name] = bk
        return resolved

    def _fetch_board_name_code_map(self, board_type: str) -> dict[str, str]:
        fs = "m:90 t:3 f:!50" if board_type == "concept" else "m:90 t:2 f:!50"
        payload = self.http_get_json_direct(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1, "pz": 500, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": fs, "fields": "f12,f14",
            },
            headers={
                "Referer": "https://quote.eastmoney.com/center/boardlist.html",
                "Origin": "https://quote.eastmoney.com",
            },
        )
        rows = ((payload or {}).get("data") or {}).get("diff") or []
        mapping: dict[str, str] = {}
        for row in rows:
            name = str(row.get("f14") or "").strip()
            code = str(row.get("f12") or "").strip().upper()
            if name and code:
                mapping[name] = code
        return mapping

    def _fetch_five_day_return(self, secid: str) -> float | None:
        """取 secid 最近 N 个交易日收盘，返回近5日累计涨跌幅（百分比）。"""
        payload = self.http_get_json_direct(
            "http://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101", "fqt": "1", "end": "20500101",
                "lmt": self._FIVE_DAY_KLINE_LIMIT,
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        klines = ((payload or {}).get("data") or {}).get("klines") or []
        closes: list[float] = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 3:
                continue
            try:
                closes.append(float(parts[2]))
            except ValueError:
                continue
        if len(closes) < 2 or closes[0] == 0:
            return None
        return (closes[-1] / closes[0] - 1) * 100

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

    def _combined_failure_status(self, target: str, trade_date: str, errors: list[ProviderStatus], board_type: str = "industry") -> ProviderStatus:
        label = "概念" if board_type == "concept" else "行业"
        details = "；".join(f"{item.source}: {item.error}" for item in errors if item.error)
        warnings = [warning for item in errors for warning in item.warnings]
        attempted_sources = [item.source for item in errors if item.source] or list(self.industry_rank_sources)
        return ProviderStatus(
            provider=self.provider_name,
            source="+".join(dict.fromkeys(attempted_sources)),
            target=f"{target}:{trade_date}",
            ok=False,
            retry_count=sum(item.retry_count for item in errors),
            error=f"所有{label}板块数据源均不可用：{details or 'unknown error'}",
            warnings=list(dict.fromkeys([*warnings, f"所有{label}板块数据源均不可用，且无可用缓存。"])),
        )

    def _source_health_path(self, board_type: str = "industry") -> Path:
        return self.cache.directory / f"_source_health_{board_type}_rank.json"

    def _last_successful_rank_source(self, board_type: str = "industry") -> str | None:
        if not self.cache.enabled:
            return None
        path = self._source_health_path(board_type)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        source = str(payload.get("source") or "")
        return source or None

    def _save_last_successful_rank_source(self, board_type: str, source: str) -> None:
        if not self.cache.enabled:
            return
        try:
            self._source_health_path(board_type).write_text(
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
