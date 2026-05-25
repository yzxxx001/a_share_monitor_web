from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from .models import Evaluation, Position, Signal


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_tables()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_tables(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    ts_code TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    shares INTEGER NOT NULL DEFAULT 0,
                    available_to_sell INTEGER NOT NULL DEFAULT 0,
                    average_cost REAL NOT NULL DEFAULT 0,
                    account_total_value REAL NOT NULL DEFAULT 100000,
                    cash_available REAL NOT NULL DEFAULT 0,
                    max_position_pct REAL NOT NULL DEFAULT 0.30,
                    stop_loss_pct REAL NOT NULL DEFAULT 0.05,
                    take_profit_pct REAL NOT NULL DEFAULT 0.12,
                    trailing_stop_pct REAL NOT NULL DEFAULT 0.04,
                    max_single_buy_pct REAL NOT NULL DEFAULT 0.10,
                    peak_price_since_entry REAL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bars (
                    ts_code TEXT NOT NULL,
                    trade_time TEXT NOT NULL,
                    open REAL NOT NULL,
                    close REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    vol REAL NOT NULL,
                    amount REAL NOT NULL,
                    PRIMARY KEY (ts_code, trade_time)
                );
                CREATE TABLE IF NOT EXISTS alert_history (
                    ts_code TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    latest_price REAL NOT NULL,
                    PRIMARY KEY (ts_code, rule_id)
                );
                CREATE TABLE IF NOT EXISTS peaks (
                    ts_code TEXT PRIMARY KEY,
                    observed_peak REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signal_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detected_at TEXT NOT NULL,
                    ts_code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    action TEXT NOT NULL,
                    latest_price REAL NOT NULL,
                    channel_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    positions_checked INTEGER NOT NULL DEFAULT 0,
                    alerts_triggered INTEGER NOT NULL DEFAULT 0,
                    notifications_delivered INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stock_master (
                    ts_code TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT '',
                    exchange TEXT NOT NULL DEFAULT '',
                    industry TEXT NOT NULL DEFAULT '',
                    list_date TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            enabled=bool(row["enabled"]),
            ts_code=str(row["ts_code"]),
            name=str(row["name"]),
            shares=int(row["shares"]),
            available_to_sell=int(row["available_to_sell"]),
            average_cost=float(row["average_cost"]),
            account_total_value=float(row["account_total_value"]),
            cash_available=float(row["cash_available"]),
            max_position_pct=float(row["max_position_pct"]),
            stop_loss_pct=float(row["stop_loss_pct"]),
            take_profit_pct=float(row["take_profit_pct"]),
            trailing_stop_pct=float(row["trailing_stop_pct"]),
            max_single_buy_pct=float(row["max_single_buy_pct"]),
            peak_price_since_entry=(None if row["peak_price_since_entry"] is None else float(row["peak_price_since_entry"])),
            note=str(row["note"]),
        )

    def upsert_watch_item(self, position: Position, now: datetime | None = None) -> None:
        now_text = (now or datetime.now()).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist(
                    ts_code, name, enabled, shares, available_to_sell, average_cost,
                    account_total_value, cash_available, max_position_pct, stop_loss_pct,
                    take_profit_pct, trailing_stop_pct, max_single_buy_pct,
                    peak_price_since_entry, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts_code) DO UPDATE SET
                    name=excluded.name, enabled=excluded.enabled, shares=excluded.shares,
                    available_to_sell=excluded.available_to_sell, average_cost=excluded.average_cost,
                    account_total_value=excluded.account_total_value, cash_available=excluded.cash_available,
                    max_position_pct=excluded.max_position_pct, stop_loss_pct=excluded.stop_loss_pct,
                    take_profit_pct=excluded.take_profit_pct, trailing_stop_pct=excluded.trailing_stop_pct,
                    max_single_buy_pct=excluded.max_single_buy_pct,
                    peak_price_since_entry=excluded.peak_price_since_entry,
                    note=excluded.note, updated_at=excluded.updated_at
                """,
                (
                    position.ts_code, position.name, int(position.enabled), position.shares,
                    position.available_to_sell, position.average_cost, position.account_total_value,
                    position.cash_available, position.max_position_pct, position.stop_loss_pct,
                    position.take_profit_pct, position.trailing_stop_pct, position.max_single_buy_pct,
                    position.peak_price_since_entry, position.note, now_text, now_text,
                ),
            )

    def import_watch_items(self, positions: Iterable[Position]) -> int:
        count = 0
        for position in positions:
            self.upsert_watch_item(position)
            count += 1
        return count

    def list_watchlist(self, enabled_only: bool = False) -> list[Position]:
        where = "WHERE enabled = 1" if enabled_only else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM watchlist {where} ORDER BY ts_code").fetchall()
        return [self._row_to_position(row) for row in rows]

    def get_watch_item(self, ts_code: str) -> Position | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM watchlist WHERE ts_code = ?", (ts_code,)).fetchone()
        return None if row is None else self._row_to_position(row)

    def set_watch_enabled(self, ts_code: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watchlist SET enabled = ?, updated_at = ? WHERE ts_code = ?",
                (int(enabled), datetime.now().isoformat(timespec="seconds"), ts_code),
            )

    def delete_watch_item(self, ts_code: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE ts_code = ?", (ts_code,))

    def backfill_watchlist_names(self) -> int:
        """Populate empty watchlist names from stock_master. Returns number of rows updated."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE watchlist
                SET name = (
                    SELECT sm.name FROM stock_master sm WHERE sm.ts_code = watchlist.ts_code
                )
                WHERE (name = '' OR name IS NULL)
                  AND EXISTS (
                    SELECT 1 FROM stock_master sm
                    WHERE sm.ts_code = watchlist.ts_code AND sm.name != ''
                  )
                """
            )
            return cursor.rowcount

    def rename_watch_item(self, old_code: str, new_code: str) -> None:
        """将监控标的的股票代码从 old_code 改为 new_code，同步更新所有关联表。"""
        now_text = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "UPDATE watchlist SET ts_code = ?, updated_at = ? WHERE ts_code = ?",
                (new_code, now_text, old_code),
            )
            conn.execute("UPDATE bars SET ts_code = ? WHERE ts_code = ?", (new_code, old_code))
            conn.execute("UPDATE alert_history SET ts_code = ? WHERE ts_code = ?", (new_code, old_code))
            conn.execute("UPDATE peaks SET ts_code = ? WHERE ts_code = ?", (new_code, old_code))
            conn.execute("UPDATE signal_events SET ts_code = ? WHERE ts_code = ?", (new_code, old_code))

    def upsert_bars(self, bars: pd.DataFrame) -> None:
        if bars.empty:
            return
        records = [
            (
                str(row.ts_code), pd.Timestamp(row.time).isoformat(), float(row.open), float(row.close),
                float(row.high), float(row.low), float(row.vol), float(row.amount),
            )
            for row in bars.itertuples(index=False)
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO bars(ts_code, trade_time, open, close, high, low, vol, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts_code, trade_time) DO UPDATE SET
                    open=excluded.open, close=excluded.close, high=excluded.high,
                    low=excluded.low, vol=excluded.vol, amount=excluded.amount
                """,
                records,
            )

    def load_recent_bars(self, ts_code: str, limit: int) -> pd.DataFrame:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts_code, trade_time AS time, open, close, high, low, vol, amount
                FROM bars WHERE ts_code = ? ORDER BY trade_time DESC LIMIT ?
                """,
                (ts_code, limit),
            ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["ts_code", "time", "open", "close", "high", "low", "vol", "amount"])
        data = pd.DataFrame([dict(row) for row in rows])
        data["time"] = pd.to_datetime(data["time"])
        return data.sort_values("time").reset_index(drop=True)

    def get_peak(self, ts_code: str) -> float | None:
        with self._connect() as conn:
            row = conn.execute("SELECT observed_peak FROM peaks WHERE ts_code = ?", (ts_code,)).fetchone()
        return None if row is None else float(row["observed_peak"])

    def set_peak(self, ts_code: str, observed_peak: float, now: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO peaks(ts_code, observed_peak, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(ts_code) DO UPDATE SET
                    observed_peak=excluded.observed_peak, updated_at=excluded.updated_at
                """,
                (ts_code, float(observed_peak), now.isoformat()),
            )

    def may_send(self, ts_code: str, rule_id: str, now: datetime, cooldown_minutes: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sent_at FROM alert_history WHERE ts_code = ? AND rule_id = ?", (ts_code, rule_id)
            ).fetchone()
        if row is None:
            return True
        last_sent = datetime.fromisoformat(str(row["sent_at"]))
        return now - last_sent >= timedelta(minutes=cooldown_minutes)

    def mark_sent(self, ts_code: str, rule_id: str, now: datetime, latest_price: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_history(ts_code, rule_id, sent_at, latest_price)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ts_code, rule_id) DO UPDATE SET sent_at=excluded.sent_at, latest_price=excluded.latest_price
                """,
                (ts_code, rule_id, now.isoformat(), float(latest_price)),
            )

    def record_signal_events(self, evaluation: Evaluation, signals: list[Signal], channel_status: str, detected_at: datetime) -> None:
        if not signals:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO signal_events(
                    detected_at, ts_code, name, rule_id, severity, title, reason, action, latest_price, channel_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        detected_at.isoformat(timespec="seconds"), evaluation.position.ts_code, evaluation.position.name,
                        signal.rule_id, signal.severity.name, signal.title, signal.reason, signal.action,
                        evaluation.latest_price, channel_status,
                    )
                    for signal in signals
                ],
            )

    def list_signal_events(self, limit: int = 20, ts_code: str | None = None) -> list[dict[str, object]]:
        where = "WHERE ts_code = ?" if ts_code else ""
        params: tuple[object, ...] = (ts_code, limit) if ts_code else (limit,)
        sql = f"SELECT * FROM signal_events {where} ORDER BY event_id DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def start_run(self, started_at: datetime) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO monitor_runs(started_at, status) VALUES (?, ?)",
                (started_at.isoformat(timespec="seconds"), "RUNNING"),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, finished_at: datetime, status: str, message: str,
        positions_checked: int, alerts_triggered: int, notifications_delivered: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE monitor_runs SET finished_at=?, status=?, message=?, positions_checked=?,
                    alerts_triggered=?, notifications_delivered=? WHERE run_id=?
                """,
                (
                    finished_at.isoformat(timespec="seconds"), status, message, positions_checked,
                    alerts_triggered, notifications_delivered, run_id,
                ),
            )

    def list_runs(self, limit: int = 10) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM monitor_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_stock_master(self, data: pd.DataFrame, synced_at: datetime) -> int:
        if data.empty:
            return 0
        records: list[tuple[str, ...]] = []
        for row in data.fillna("").itertuples(index=False):
            mapping = row._asdict()
            records.append(
                (
                    str(mapping.get("ts_code", "")), str(mapping.get("symbol", "")), str(mapping.get("name", "")),
                    str(mapping.get("market", "")), str(mapping.get("exchange", "")), str(mapping.get("industry", "")),
                    str(mapping.get("list_date", "")), synced_at.isoformat(timespec="seconds"),
                )
            )
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO stock_master(ts_code, symbol, name, market, exchange, industry, list_date, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts_code) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
                    market=excluded.market, exchange=excluded.exchange, industry=excluded.industry,
                    list_date=excluded.list_date, synced_at=excluded.synced_at
                """,
                records,
            )
        return len(records)

    def find_stock_candidates(self, query: str, limit: int = 20) -> list[dict[str, object]]:
        token = f"%{query.strip()}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts_code, symbol, name, market, exchange, industry
                FROM stock_master
                WHERE ts_code LIKE ? OR symbol LIKE ? OR name LIKE ?
                ORDER BY CASE WHEN name = ? OR ts_code = ? OR symbol = ? THEN 0 ELSE 1 END, ts_code
                LIMIT ?
                """,
                (token, token, token, query.strip(), query.strip().upper(), query.strip(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def stock_master_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM stock_master").fetchone()
        return 0 if row is None else int(row["count"])
