"""Quick test: verify hot sector constituent fetch works end-to-end."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests


def direct_get(url, params, headers):
    merged = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
    merged.update(headers)
    session = requests.Session()
    session.trust_env = False
    resp = session.get(url, params=params, headers=merged, timeout=12)
    resp.raise_for_status()
    return resp.json()


def main():
    base_headers = {
        "Referer": "https://quote.eastmoney.com/center/boardlist.html",
        "Origin": "https://quote.eastmoney.com",
    }

    sector_code = "BK0537"
    sector_name = "半导体"

    # Step 1: get sector list via direct API
    print("=== 测试1: 直连获取行业板块列表 ===")
    step1_ok = False
    try:
        data = direct_get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1, "pz": 10, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": "m:90 t:2 f:!50",
                "fields": "f3,f6,f8,f12,f14,f62,f104,f105",
            },
            headers=base_headers,
        )
        rows = (data.get("data") or {}).get("diff") or []
        print(f"获取板块数: {len(rows)}")
        for r in rows[:3]:
            print(f"  代码={r.get('f12')}, 名称={r.get('f14')}, 涨跌幅={r.get('f3')}")
        if rows:
            sector_code = rows[0].get("f12", sector_code)
            sector_name = rows[0].get("f14", sector_name)
            step1_ok = True
        print(f"将用于测试: 代码={sector_code}, 名称={sector_name}")
    except Exception as exc:
        print(f"失败: {type(exc).__name__}: {exc}")
        print(f"使用默认: 代码={sector_code}, 名称={sector_name}")

    # Step 2: get constituents via direct API
    print(f"\n=== 测试2: 直连获取成分股 (代码={sector_code}) ===")
    step2_ok = False
    try:
        data2 = direct_get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1, "pz": 30, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": f"b:{sector_code}",
                "fields": "f2,f3,f6,f8,f12,f14,f15,f62",
            },
            headers=base_headers,
        )
        stocks = (data2.get("data") or {}).get("diff") or []
        print(f"获取成分股数: {len(stocks)}")
        for s in stocks[:5]:
            print(f"  代码={s.get('f12')}, 名称={s.get('f14')}, 涨跌幅={s.get('f3')}%")
        step2_ok = bool(stocks)
    except Exception as exc:
        print(f"失败: {type(exc).__name__}: {exc}")

    # Step 3: via AKShare (goes through proxy)
    print(f"\n=== 测试3: AKShare 获取成分股 (名称={sector_name}) ===")
    step3_ok = False
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=sector_name)
        print(f"获取成分股数: {len(df)}")
        print(df.head(5).to_string())
        step3_ok = not df.empty
    except Exception as exc:
        print(f"失败: {type(exc).__name__}: {exc}")

    # Step 4: via AKShareHotSectorProvider
    print("\n=== 测试4: 通过 AKShareHotSectorProvider 获取成分股 ===")
    step4_ok = False
    try:
        from stock_monitor.providers.hot_sector import AKShareHotSectorProvider
        from stock_monitor.config import HotSectorHttpConfig, HotSectorCacheConfig
        http_cfg = HotSectorHttpConfig(
            timeout_seconds=15,
            retry_count=1,
            retry_backoff_seconds=(0.5,),
        )
        cache_cfg = HotSectorCacheConfig(
            enabled=False,
            directory=Path("./cache/test"),
        )
        provider = AKShareHotSectorProvider(http_cfg, cache_cfg)
        rows = provider.get_sector_constituents(sector_code)
        print(f"获取成分股数 (BK code {sector_code}): {len(rows)}")
        for r in rows[:5]:
            print(f"  {r.get('代码')}, {r.get('名称')}, 涨跌幅={r.get('涨跌幅')}")
        step4_ok = bool(rows)
    except Exception as exc:
        print(f"失败 (BK code): {type(exc).__name__}: {exc}")
    if not step4_ok:
        # also try with sector name
        try:
            rows2 = provider.get_sector_constituents(sector_name)
            print(f"获取成分股数 (名称 {sector_name}): {len(rows2)}")
            for r in rows2[:5]:
                print(f"  {r.get('代码')}, {r.get('名称')}")
            step4_ok = bool(rows2)
        except Exception as exc2:
            print(f"失败 (名称): {type(exc2).__name__}: {exc2}")

    print("\n=== 总结 ===")
    print(f"直连板块列表 API : {'OK' if step1_ok else 'FAIL'}")
    print(f"直连成分股 API   : {'OK' if step2_ok else 'FAIL'}")
    print(f"AKShare (代理)   : {'OK' if step3_ok else 'FAIL'}")
    print(f"Provider 类封装  : {'OK' if step4_ok else 'FAIL'}")
    return 0 if (step2_ok or step4_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
