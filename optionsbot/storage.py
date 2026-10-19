"""SQLite persistence for paper positions, the equity curve and IV history."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    realized REAL NOT NULL,
    unrealized REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS iv_history (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    atm_iv REAL NOT NULL,
    PRIMARY KEY (symbol, day)
);
"""


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)

    def save_position(self, data: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO positions (id, symbol, status, entry_time, exit_time, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (data["id"], data["symbol"], data["status"], data["entry_time"], data.get("exit_time"),
                 json.dumps(data)),
            )

    def load_positions(self, status: str | None = None, limit: int | None = None) -> list[dict]:
        sql = "SELECT data FROM positions"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY entry_time DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            return [json.loads(row[0]) for row in self._conn.execute(sql, args)]

    def record_equity(self, ts: str, equity: float, realized: float, unrealized: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?)", (ts, equity, realized, unrealized))

    def equity_curve(self, limit: int = 2000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, equity FROM (SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts",
                (limit,)).fetchall()
        return [{"ts": ts, "equity": eq} for ts, eq in rows]

    def record_iv(self, symbol: str, day: str, atm_iv: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO iv_history VALUES (?, ?, ?)", (symbol, day, atm_iv))

    def iv_history(self, symbol: str, limit: int = 252) -> list[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT atm_iv FROM iv_history WHERE symbol = ? ORDER BY day DESC LIMIT ?", (symbol, limit))
            return [r[0] for r in rows]
