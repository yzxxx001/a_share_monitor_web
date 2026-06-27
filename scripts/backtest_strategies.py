"""回测对比：短线候选两版策略在历史交易日的次日 / 3 日胜率。

以 **as-of** 方式复用真实打分管线（CommonRiskFilter + build_stock_metrics +
score_short_term_resonance）：对每个回测交易日，把每只股票的行情截断到“当日及之前”，
模拟当日选股，再用之后 1 / 3 个交易日的收盘价计算前瞻收益，统计胜率与平均收益。

为什么不直接循环调用 HotSectorCandidateService.generate(历史日期)：
    该服务的快照/日线都取“最新”数据（end=20500101），并不按历史日期截断，直接回放会
    用到未来数据（look-ahead），胜率统计将失真。本脚本因此自行做 as-of 截断。

局限（务必知悉）：
  - 成分股取“当前”板块成分，不还原历史成分，无法规避幸存者 / 调入调出偏差。
  - 默认不含资金流维度（按 degraded 权重，两版完全一致），以隔离“累计涨幅惩罚”的影响。
    两版输入完全相同，差异仅来自策略配置，故对比是公平的。
  - 前瞻收益按 T 收盘买入、T+h 收盘卖出（close-to-close），不计成本与滑点。

用法示例：
  python scripts/backtest_strategies.py --sectors 硅料硅片,橡胶助剂 --start 20260501 --end 20260620 --max-candidates 2
  python scripts/backtest_strategies.py --top-sectors 6 --start 20260501 --end 20260620

提示：当板块合格成分股数 ≤ max_candidates 时，两版会把所有股票都选上，结果与基线相同、
无法体现选股差异。窄板块请用 --max-candidates 调小，或换用成分股较多的板块 / 多个板块以
扩大样本，结论才有统计意义。
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

# 允许从仓库根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stock_monitor.config import load_settings  # noqa: E402
from stock_monitor.providers.hot_sector import AKShareHotSectorProvider  # noqa: E402
from stock_monitor.services.hot_sector_candidates import build_stock_metrics  # noqa: E402
from stock_monitor.services.hot_sector_report import score_sector_snapshots as _score_sectors  # noqa: E402
from stock_monitor.services.risk_filter import CommonRiskFilter  # noqa: E402
from stock_monitor.services.strategy_registry import get_scorer  # noqa: E402


def _parse_date(token: str) -> date:
    return datetime.strptime(token.replace("-", ""), "%Y%m%d").date()


def _bar_date(bar: dict[str, Any]) -> date | None:
    raw = bar.get("日期") or bar.get("date") or bar.get("time")
    if raw is None:
        return None
    text = str(raw)[:10].replace("/", "-")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _bar_close(bar: dict[str, Any]) -> float | None:
    value = bar.get("收盘", bar.get("close"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_open(bar: dict[str, Any]) -> float | None:
    value = bar.get("开盘", bar.get("open"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_pct(bar: dict[str, Any]) -> float | None:
    value = bar.get("涨跌幅", bar.get("pct_change"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class StockHistory:
    """单只股票的有序日线，提供 as-of 切片与前瞻收盘价查询。"""

    def __init__(self, code: str, name: str, bars: list[dict[str, Any]]) -> None:
        self.code = code
        self.name = name
        rows = [(d, b) for b in bars if (d := _bar_date(b)) is not None]
        rows.sort(key=lambda item: item[0])
        self.dates = [d for d, _ in rows]
        self.bars = [b for _, b in rows]
        self.closes = [_bar_close(b) for b in self.bars]
        self.opens = [_bar_open(b) for b in self.bars]
        self._index = {d: i for i, d in enumerate(self.dates)}

    def index_of(self, d: date) -> int | None:
        return self._index.get(d)

    def bars_asof(self, idx: int) -> list[dict[str, Any]]:
        return self.bars[: idx + 1]

    def forward_return(self, idx: int, horizon: int, entry: str = "close") -> float | None:
        """前瞻收益（%）。

        entry="close"：D 收盘买入，D+horizon 收盘卖出（close-to-close）。
        entry="open" ：D+1 开盘买入，D+horizon 收盘卖出——选股仍基于 D 收盘信息，
                       但用次日开盘价入场，避免"追在走强当天的收盘高点"。
        """
        exit_i = idx + horizon
        if exit_i >= len(self.closes):
            return None
        future = self.closes[exit_i]
        if entry == "open":
            entry_i = idx + 1
            if entry_i >= len(self.opens):
                return None
            base = self.opens[entry_i]
        else:
            base = self.closes[idx]
        if base is None or future is None or base <= 0:
            return None
        return (future - base) / base * 100.0


def _synthetic_quote(bar: dict[str, Any]) -> dict[str, Any]:
    """用单根日线伪造一个“当日快照”，字段名与 _normalize_quote 的别名对齐。"""
    return {
        "收盘": bar.get("收盘", bar.get("close")),
        "最高": bar.get("最高", bar.get("high")),
        "涨跌幅": bar.get("涨跌幅", bar.get("pct_change")),
        "成交额": bar.get("成交额", bar.get("amount")),
        "换手率": bar.get("换手率", bar.get("turnover_rate")),
    }


def _resolve_sectors(provider: AKShareHotSectorProvider, settings: Any, sectors_arg: str | None, top_n: int) -> list[tuple[str, str]]:
    """返回 [(板块名, 板块代码)]。显式给定则用之；否则取当前热门行业 Top-N。"""
    if sectors_arg:
        names = [s.strip() for s in sectors_arg.split(",") if s.strip()]
        return [(name, name) for name in names]
    snapshots = provider.get_industry_sector_rank("today")
    scored = _score_sectors(snapshots, settings, [])
    out: list[tuple[str, str]] = []
    for row in scored[:top_n]:
        out.append((row["sector_name"], row.get("sector_code") or row["sector_name"]))
    return out


def _bootstrap_mean_diff(
    a_rows: list[tuple[date, float]],
    b_rows: list[tuple[date, float]],
    n_boot: int,
    seed: int,
) -> tuple[float, float, float, float] | None:
    """按交易日 block bootstrap 估计 (mean(a) − mean(b)) 的 95% CI 与双侧 p 值。

    每次重抽样以“交易日”为单元（连同当日全部收益一起抽），保留同日内的相关性，
    a、b 用同一批重抽样的交易日（配对），从而衡量两策略在相同日子上的差异。
    返回 (观测差, ci_low, ci_high, p)；样本天数不足时返回 None。
    """
    a_by_date: dict[date, list[float]] = defaultdict(list)
    b_by_date: dict[date, list[float]] = defaultdict(list)
    for dt, v in a_rows:
        a_by_date[dt].append(v)
    for dt, v in b_rows:
        b_by_date[dt].append(v)
    dates = sorted(set(a_by_date) | set(b_by_date))
    if len(dates) < 3:
        return None

    def _pooled(by_date: dict[date, list[float]], sample: list[date]) -> float | None:
        vals = [v for dt in sample for v in by_date.get(dt, ())]
        return statistics.fmean(vals) if vals else None

    obs_a = _pooled(a_by_date, dates)
    obs_b = _pooled(b_by_date, dates)
    if obs_a is None or obs_b is None:
        return None
    observed = obs_a - obs_b

    rng = random.Random(seed)
    n = len(dates)
    diffs: list[float] = []
    for _ in range(n_boot):
        sample = [dates[rng.randrange(n)] for _ in range(n)]
        ma = _pooled(a_by_date, sample)
        mb = _pooled(b_by_date, sample)
        if ma is not None and mb is not None:
            diffs.append(ma - mb)
    if len(diffs) < 2:
        return None
    diffs.sort()
    lo = diffs[int(0.025 * len(diffs))]
    hi = diffs[min(len(diffs) - 1, int(0.975 * len(diffs)))]
    # 双侧 p：bootstrap 分布落在 0 另一侧的比例 ×2
    p = 2.0 * min(
        sum(1 for x in diffs if x <= 0),
        sum(1 for x in diffs if x >= 0),
    ) / len(diffs)
    return observed, lo, hi, min(p, 1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="两版短线候选策略的历史胜率回测对比")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--sectors", default=None, help="逗号分隔的板块名（缺省则取当前热门行业 Top-N）")
    parser.add_argument("--top-sectors", type=int, default=5, help="未指定 --sectors 时取当前热门行业前 N 个")
    parser.add_argument("--start", default=None, help="回测开始日 YYYYMMDD（缺省=可用数据起点）")
    parser.add_argument("--end", default=None, help="回测结束日 YYYYMMDD（缺省=最新可回测日）")
    parser.add_argument("--strategies", default="short_term_resonance,short_term_resonance_v1")
    parser.add_argument("--horizons", default="1,3,5,10", help="前瞻交易日数，逗号分隔")
    parser.add_argument("--entry", choices=("close", "open"), default="close",
                        help="入场价：close=D 收盘买入；open=D+1 开盘买入（避免追当日收盘高点）")
    parser.add_argument("--max-candidates", type=int, default=None, help="覆盖每板块每日选股数；窄板块需调小才能体现选股差异")
    parser.add_argument("--history-days", type=int, default=130, help="打分所需的历史窗口（与策略 history_days 对齐）")
    parser.add_argument("--fetch-days", type=int, default=320, help="每只股票拉取的日线根数")
    parser.add_argument("--bootstrap", type=int, default=2000, help="按交易日 block bootstrap 的重抽样次数；0=关闭显著性检验")
    parser.add_argument("--seed", type=int, default=42, help="bootstrap 随机种子，保证结果可复现")
    parser.add_argument("--verbose", action="store_true", help="打印逐日逐票明细")
    args = parser.parse_args()

    settings = load_settings(args.config)
    provider = AKShareHotSectorProvider(settings.hot_sector_monitor.http, settings.hot_sector_monitor.cache)
    risk_filter = CommonRiskFilter(settings.hot_sector_monitor.common_risk_filter)
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    max_h = max(horizons)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    for name in strategies:
        if name not in settings.hot_sector_monitor.strategy_configs:
            print(f"未注册的策略：{name}（可用：{', '.join(sorted(settings.hot_sector_monitor.strategy_configs))}）")
            return 2
    configs = {name: dict(settings.hot_sector_monitor.strategy_configs[name]) for name in strategies}

    sectors = _resolve_sectors(provider, settings, args.sectors, args.top_sectors)
    print(f"回测板块（{len(sectors)}）：{', '.join(name for name, _ in sectors)}")

    # 拉取每个板块的成分股与每只股票的完整日线（一次性，带缓存）
    sector_members: dict[str, list[StockHistory]] = {}
    all_dates: set[date] = set()
    for sector_name, sector_id in sectors:
        try:
            constituents = provider.get_sector_constituents(sector_name) or provider.get_sector_constituents(sector_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳过] 板块 {sector_name} 成分股获取失败：{exc}")
            continue
        members: list[StockHistory] = []
        for raw in constituents:
            code = str(raw.get("代码") or raw.get("stock_code") or raw.get("ts_code") or "").split(".")[0].strip()
            name = str(raw.get("名称") or raw.get("stock_name") or "").strip()
            if not code:
                continue
            try:
                bars = provider.get_daily_bars(code, args.fetch_days)
            except Exception:  # noqa: BLE001
                continue
            hist = StockHistory(code, name, bars)
            if len(hist.dates) < args.history_days + max_h + 1:
                continue
            members.append(hist)
            all_dates.update(hist.dates)
        sector_members[sector_name] = members
        print(f"  板块 {sector_name}: 可回测成分股 {len(members)} 只")

    if not all_dates:
        print("无可用行情，退出。")
        return 2

    trading_dates = sorted(all_dates)
    start = _parse_date(args.start) if args.start else trading_dates[0]
    end = _parse_date(args.end) if args.end else trading_dates[-1]
    # 结束日须留出 max_h 个前瞻交易日
    last_ok = trading_dates[-(max_h + 1)] if len(trading_dates) > max_h else trading_dates[0]
    if end > last_ok:
        end = last_ok
    backtest_dates = [d for d in trading_dates if start <= d <= end]
    entry_label = "次日开盘买入" if args.entry == "open" else "当日收盘买入"
    print(f"回测区间：{start} ~ {end}，交易日 {len(backtest_dates)} 天；入场方式：{entry_label}（卖出=T+h 收盘）\n")

    # 统计容器：保存 (交易日, 前瞻收益) 以便按日做 block bootstrap 显著性检验。
    results: dict[str, dict[int, list[tuple[date, float]]]] = {s: {h: [] for h in horizons} for s in strategies}
    baseline: dict[int, list[tuple[date, float]]] = {h: [] for h in horizons}
    pick_counts: dict[str, int] = defaultdict(int)
    seen_baseline: set[tuple[str, date]] = set()

    for d in backtest_dates:
        for sector_name, members in sector_members.items():
            # 1) 计算当日板块涨幅（成分股当日涨跌幅均值）作为 relative_return 基准
            day_pcts: list[float] = []
            day_rows: list[tuple[StockHistory, int]] = []
            for hist in members:
                idx = hist.index_of(d)
                if idx is None or idx < args.history_days:
                    continue
                pct = _bar_pct(hist.bars[idx])
                if pct is not None:
                    day_pcts.append(pct)
                day_rows.append((hist, idx))
            if not day_rows:
                continue
            sector_change_pct = statistics.fmean(day_pcts) if day_pcts else None

            # 2) as-of 构建每只股票的指标，并过滤风险/涨停（与线上一致）
            metrics: list[dict[str, Any]] = []
            for hist, idx in day_rows:
                bar = hist.bars[idx]
                quote = _synthetic_quote(bar)
                asof_bars = hist.bars_asof(idx)
                decision = risk_filter.evaluate(
                    stock_code=hist.code,
                    stock_name=hist.name,
                    quote=quote,
                    daily_bars=asof_bars,
                    risk_events=None,
                    limit_up_pct=9.8,
                )
                if decision.is_excluded or decision.is_observe_only:
                    continue
                try:
                    metric = build_stock_metrics(
                        raw_stock={"代码": hist.code, "名称": hist.name},
                        quote=quote,
                        daily_bars=asof_bars,
                        sector_code=sector_name,
                        sector_name=sector_name,
                        sector_change_pct=sector_change_pct,
                        risk_decision=decision,
                        cum_return_lookback=10,
                    )
                except Exception:  # noqa: BLE001
                    continue
                metric["_hist"] = hist
                metric["_idx"] = idx
                metrics.append(metric)

            if not metrics:
                continue

            # baseline：该 (日期,板块) 全体合格股票的前瞻收益（去重）
            for metric in metrics:
                key = (metric["stock_code"], d)
                if key in seen_baseline:
                    continue
                seen_baseline.add(key)
                for h in horizons:
                    fr = metric["_hist"].forward_return(metric["_idx"], h, args.entry)
                    if fr is not None:
                        baseline[h].append((d, fr))

            # 3) 两版策略各自打分、取 Top-N、记录前瞻收益
            for strat in strategies:
                cfg = configs[strat]
                max_candidates = args.max_candidates or int(cfg.get("max_candidates") or settings.hot_sector_monitor.candidate.max_count_per_sector)
                scorer = get_scorer(str(cfg.get("scorer", "short_term_resonance")))
                scored, _w, _wt = scorer([dict(m) for m in metrics], cfg)
                # scorer 复制了 dict，丢了 _hist/_idx；用 stock_code 回查
                by_code = {m["stock_code"]: m for m in metrics}
                for row in scored[:max_candidates]:
                    src = by_code.get(row["stock_code"])
                    if src is None:
                        continue
                    pick_counts[strat] += 1
                    for h in horizons:
                        fr = src["_hist"].forward_return(src["_idx"], h, args.entry)
                        if fr is not None:
                            results[strat][h].append((d, fr))
                    if args.verbose:
                        print(f"  {d} {sector_name:>6} [{strat:>26}] {row['stock_code']} {src['stock_name']} "
                              f"score={row['short_term_score']} fwd1={src['_hist'].forward_return(src['_idx'],1,args.entry)}")

    # ===== 汇总输出 =====
    def _summ(rows: list[tuple[date, float]]) -> str:
        vals = [v for _, v in rows]
        if not vals:
            return "n=0"
        win = sum(1 for v in vals if v > 0) / len(vals) * 100
        ndays = len({dt for dt, _ in rows})
        return (f"n={len(vals):4d}（{ndays}天）  胜率={win:5.1f}%  "
                f"均值={statistics.fmean(vals):+5.2f}%  中位={statistics.median(vals):+5.2f}%")

    print("\n" + "=" * 78)
    print("基线（全体合格成分股，等价于该板块内随机选股的期望）")
    for h in horizons:
        print(f"  T+{h}: {_summ(baseline[h])}")
    print("-" * 78)
    for strat in strategies:
        print(f"策略 {strat}  （累计选出 {pick_counts[strat]} 次候选）")
        for h in horizons:
            print(f"  T+{h}: {_summ(results[strat][h])}")
        print("-" * 78)

    # ===== 显著性检验：按交易日 block bootstrap =====
    # 候选样本高度自相关（同股多日重复、前瞻窗口重叠），把“交易日”作为独立重抽样单元，
    # 比把每次候选当独立样本更诚实地反映有效样本量。报告均值差的 95% 置信区间与双侧 p 值。
    if args.bootstrap > 0:
        print(f"按交易日 block bootstrap（{args.bootstrap} 次，seed={args.seed}）：均值差 [95% 置信区间] p值")
        print("（CI 不含 0 / p<0.05 才算显著；样本天数少时几乎必然不显著）")
        for strat in strategies:
            for h in horizons:
                res = _bootstrap_mean_diff(results[strat][h], baseline[h], args.bootstrap, args.seed)
                if res:
                    diff, lo, hi, p = res
                    sig = "  ✓显著" if (lo > 0 or hi < 0) else ""
                    print(f"  {strat} − 基线  T+{h}: {diff:+.2f}pct  [{lo:+.2f}, {hi:+.2f}]  p={p:.3f}{sig}")
        if len(strategies) == 2:
            a, b = strategies
            print(f"  —— 两版对比 [{a}] − [{b}] ——")
            for h in horizons:
                res = _bootstrap_mean_diff(results[a][h], results[b][h], args.bootstrap, args.seed)
                if res:
                    diff, lo, hi, p = res
                    sig = "  ✓显著" if (lo > 0 or hi < 0) else "  （无显著差异）"
                    print(f"  T+{h}: {diff:+.2f}pct  [{lo:+.2f}, {hi:+.2f}]  p={p:.3f}{sig}")
        print("-" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
