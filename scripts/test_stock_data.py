"""Verify Sina daily bar fallback works for individual stock data."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stock_monitor.providers.hot_sector import AKShareHotSectorProvider
from stock_monitor.config import HotSectorHttpConfig, HotSectorCacheConfig

http_cfg = HotSectorHttpConfig(timeout_seconds=15, retry_count=1, retry_backoff_seconds=(0.5,))
cache_cfg = HotSectorCacheConfig(enabled=False, directory=Path("./cache/test"))
provider = AKShareHotSectorProvider(http_cfg, cache_cfg)

# stocks from the failed report — mix of SH, SZ, BJ
test_stocks = [
    "600255.SH",  # 鑫科材料 SH
    "300127.SZ",  # 银河磁体 SZ
    "300748.SZ",  # 金力永磁 SZ
    "688077.SH",  # 大地熊 SH
    "920576.BJ",  # 天力复合 BJ (expected to use akshare or fail gracefully)
]

trade_date = "20260606"

print("=== snapshot (get_stock_snapshot) ===")
for code in test_stocks:
    try:
        snap = provider.get_stock_snapshot(code, trade_date)
        close = snap.get("收盘") or snap.get("close")
        pct = snap.get("涨跌幅") or snap.get("pct_change")
        amount = snap.get("成交额") or snap.get("amount")
        source = getattr(snap.get("_provider_status"), "source", "?")
        print(f"  {code}: close={close} pct={pct:.2f}% amount={amount:.0f} src={source}" if pct else f"  {code}: close={close} amount={amount} src={source}")
    except Exception as e:
        print(f"  {code}: FAIL {type(e).__name__}: {str(e)[:100]}")

print()
print("=== daily bars (get_daily_bars, last 5 rows) ===")
for code in test_stocks[:3]:
    try:
        bars = provider.get_daily_bars(code, 130)
        print(f"  {code}: {len(bars)} bars — last: {bars[-1].get('日期') or bars[-1].get('time')} close={bars[-1].get('收盘') or bars[-1].get('close')} turnover={bars[-1].get('换手率') or bars[-1].get('turnover_rate')}")
    except Exception as e:
        print(f"  {code}: FAIL {type(e).__name__}: {str(e)[:100]}")
