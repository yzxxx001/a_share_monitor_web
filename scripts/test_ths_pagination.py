"""Debug THS pagination to find where it stops."""
from __future__ import annotations
import re, time, sys
from io import StringIO
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import requests
import pandas as pd
import akshare as ak
import inspect
import py_mini_racer

_mod = inspect.getmodule(ak.stock_board_industry_name_ths)
_js = py_mini_racer.MiniRacer()
_js.eval(_mod._get_file_content_ths("ths.js"))
v_code = _js.call("v")
name_code_map = _mod._get_stock_board_industry_name_ths()
ths_code = name_code_map.get("半导体", "881121")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Cookie": f"v={v_code}",
})

resp1 = session.get(f"http://q.10jqka.com.cn/thshy/detail/code/{ths_code}/", timeout=12)
m = re.search(r'class="page_info">(\d+)/(\d+)', resp1.text)
total_pages = int(m.group(2)) if m else 1
print(f"总页数: {total_pages}")

total_stocks = 0
for pg in range(2, total_pages + 1):
    time.sleep(0.25)
    r = session.get(
        f"http://q.10jqka.com.cn/thshy/detail/code/{ths_code}/order/desc/page/{pg}/ajax/1/",
        timeout=10,
    )
    try:
        tables = pd.read_html(StringIO(r.text))
        rows = len(tables[0]) if tables else 0
    except Exception as e:
        rows = 0
        print(f"  第{pg}页解析失败: {e}, text前100: {r.text[:100]}")
    total_stocks += rows
    print(f"  第{pg}页: status={r.status_code} rows={rows} len={len(r.text)}")
    if rows == 0:
        print(f"  第{pg}页返回空，停止")
        break

print(f"第1页约20只 + 后续页面: {total_stocks} 只")
