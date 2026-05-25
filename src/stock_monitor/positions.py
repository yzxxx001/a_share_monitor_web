from __future__ import annotations

from pathlib import Path

import pandas as pd

from .models import Position


COLUMN_MAP = {
    "启用": "enabled",
    "股票代码": "ts_code",
    "股票名称": "name",
    "持仓股数": "shares",
    "可卖股数": "available_to_sell",
    "持仓成本": "average_cost",
    "账户总资产": "account_total_value",
    "可用现金": "cash_available",
    "最大仓位比例": "max_position_pct",
    "固定止损比例": "stop_loss_pct",
    "止盈触发比例": "take_profit_pct",
    "移动止盈回撤比例": "trailing_stop_pct",
    "单次最大新增仓位比例": "max_single_buy_pct",
    "历史持有最高价": "peak_price_since_entry",
    "备注": "note",
}

TRUE_VALUES = {"是", "y", "yes", "true", "1", "启用"}


def _text(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _number(row: pd.Series, key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if pd.isna(value) or value == "":
        return float(default)
    return float(value)


def load_positions(file_path: str | Path) -> tuple[list[Position], list[str]]:
    """Load enabled positions from the workbook; blank stock-code rows are treated as unused templates."""
    df = pd.read_excel(file_path, sheet_name="持仓录入", header=3, dtype={"股票代码": str})
    df = df.rename(columns=COLUMN_MAP)
    missing = [source for source, target in COLUMN_MAP.items() if target not in df.columns]
    if missing:
        raise ValueError(f"持仓录入表缺少列：{', '.join(missing)}")

    positions: list[Position] = []
    warnings: list[str] = []
    for excel_row, row in enumerate(df.to_dict("records"), start=2):
        enabled = _text(row.get("enabled")).lower() in TRUE_VALUES
        ts_code = _text(row.get("ts_code")).upper()
        if not enabled:
            continue
        if not ts_code:
            warnings.append(f"第 {excel_row} 行已启用但股票代码为空，已跳过。")
            continue

        peak_raw = row.get("peak_price_since_entry")
        peak = None if pd.isna(peak_raw) or peak_raw == "" else float(peak_raw)
        position = Position(
            enabled=True,
            ts_code=ts_code,
            name=_text(row.get("name")),
            shares=int(_number(pd.Series(row), "shares")),
            available_to_sell=int(_number(pd.Series(row), "available_to_sell")),
            average_cost=_number(pd.Series(row), "average_cost"),
            account_total_value=_number(pd.Series(row), "account_total_value"),
            cash_available=_number(pd.Series(row), "cash_available"),
            max_position_pct=_number(pd.Series(row), "max_position_pct", 0.30),
            stop_loss_pct=_number(pd.Series(row), "stop_loss_pct", 0.05),
            take_profit_pct=_number(pd.Series(row), "take_profit_pct", 0.12),
            trailing_stop_pct=_number(pd.Series(row), "trailing_stop_pct", 0.04),
            max_single_buy_pct=_number(pd.Series(row), "max_single_buy_pct", 0.10),
            peak_price_since_entry=peak,
            note=_text(row.get("note")),
        )
        errors = position.validate()
        if errors:
            warnings.append(f"第 {excel_row} 行 {ts_code} 配置无效：{'；'.join(errors)}")
            continue
        positions.append(position)
    return positions, warnings
