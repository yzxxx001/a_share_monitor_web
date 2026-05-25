"""Run direct AKShare minute-source diagnostics without starting the web app."""
from __future__ import annotations

import os
import sys
import urllib.request

import akshare as ak


def source_symbols(ts_code: str) -> tuple[str, str]:
    code = ts_code.strip().upper()
    if "." not in code:
        suffix = "SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else "BJ"
        code = f"{code}.{suffix}"
    symbol, suffix = code.split(".", maxsplit=1)
    return symbol, f"{suffix.lower()}{symbol}"


def main() -> int:
    ts_code = (sys.argv[1] if len(sys.argv) > 1 else "600276.SH").upper()
    symbol, sina_symbol = source_symbols(ts_code)
    print(f"测试标的: {ts_code}")
    print("环境代理:", {key: value for key, value in os.environ.items() if "proxy" in key.lower()})
    print("Python 识别代理:", urllib.request.getproxies())
    print()
    success = False
    try:
        data = ak.stock_zh_a_minute(symbol=sina_symbol, period="5", adjust="")
        print(f"[新浪成功] {sina_symbol} 行数={len(data)}")
        print(data.tail(3).to_string(index=False))
        success = True
    except Exception as exc:
        print(f"[新浪失败] {type(exc).__name__}: {exc}")
    print()
    try:
        data = ak.stock_zh_a_hist_min_em(symbol=symbol, period="5", adjust="")
        print(f"[东方财富成功] {symbol} 行数={len(data)}")
        print(data.tail(3).to_string(index=False))
        success = True
    except Exception as exc:
        print(f"[东方财富失败] {type(exc).__name__}: {exc}")
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
