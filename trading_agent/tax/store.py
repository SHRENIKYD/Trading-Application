from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .models import TYPES, Transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    asset_in TEXT NOT NULL DEFAULT '',
    qty_in REAL NOT NULL DEFAULT 0,
    asset_out TEXT NOT NULL DEFAULT '',
    qty_out REAL NOT NULL DEFAULT 0,
    inr_value REAL,
    tds_inr REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    tx_ref TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS prices (
    asset TEXT NOT NULL,
    date TEXT NOT NULL,
    inr REAL NOT NULL,
    PRIMARY KEY (asset, date)
);
"""

_COLUMNS = ("uid", "date", "type", "asset_in", "qty_in", "asset_out", "qty_out", "inr_value", "tds_inr",
            "source", "tx_ref", "notes")


class LedgerStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, tx: Transaction) -> bool:
        """Insert unless a transaction with the same uid exists. Returns True when inserted."""
        if tx.type not in TYPES:
            raise ValueError(f"unknown type {tx.type!r}; use one of {', '.join(TYPES)}")
        values = [getattr(tx, c) for c in _COLUMNS]
        values[1] = tx.date.isoformat()
        cursor = self.db.execute(
            f"INSERT OR IGNORE INTO transactions ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
            values)
        self.db.commit()
        return cursor.rowcount == 1

    def all(self) -> list[Transaction]:
        rows = self.db.execute(f"SELECT id, {', '.join(_COLUMNS)} FROM transactions ORDER BY date, id").fetchall()
        result = []
        for row in rows:
            fields = dict(zip(("id",) + _COLUMNS, row))
            fields["date"] = date.fromisoformat(fields["date"])
            result.append(Transaction(**fields))
        return result

    def update(self, tx_id: int, **fields) -> None:
        if "type" in fields and fields["type"] not in TYPES:
            raise ValueError(f"unknown type {fields['type']!r}; use one of {', '.join(TYPES)}")
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cursor = self.db.execute(f"UPDATE transactions SET {assignments} WHERE id = ?", [*fields.values(), tx_id])
        self.db.commit()
        if cursor.rowcount != 1:
            raise ValueError(f"no transaction with id {tx_id}")

    def set_price(self, asset: str, day: date, inr: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO prices (asset, date, inr) VALUES (?, ?, ?)",
                        (asset.upper(), day.isoformat(), inr))
        self.db.commit()

    def prices(self) -> dict[tuple[str, date], float]:
        return {(asset, date.fromisoformat(day)): inr
                for asset, day, inr in self.db.execute("SELECT asset, date, inr FROM prices")}
