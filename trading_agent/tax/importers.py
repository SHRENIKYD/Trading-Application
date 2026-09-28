"""Importers. Each returns Transactions with stable uids so re-importing the same file adds nothing twice.

- ledger CSV: the documented template below; convert exchange statements into it.
- Etherscan CSV: the block explorer's transaction export for one address. Rows arrive UNCLASSIFIED
  (the explorer cannot tell a purchase from a transfer between your own wallets); network fees become FEE rows.
- agent logs: the executed money actions from logs/session-*.jsonl, for reconciling against the ledger.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Transaction

LEDGER_CSV_COLUMNS = ("date", "type", "asset_in", "qty_in", "asset_out", "qty_out", "inr_value", "tds_inr",
                      "tx_ref", "notes")


def _uid(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


def _num(text: str | None) -> float:
    text = (text or "").replace(",", "").strip()
    return float(text) if text else 0.0


def _optional_num(text: str | None) -> float | None:
    text = (text or "").replace(",", "").strip()
    return float(text) if text else None


def read_ledger_csv(path: Path) -> list[Transaction]:
    """Template columns: date (YYYY-MM-DD), type, asset_in, qty_in, asset_out, qty_out, inr_value, tds_inr, tx_ref, notes."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = {"date", "type"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} is missing columns {sorted(missing)}; expected {', '.join(LEDGER_CSV_COLUMNS)}")
        result = []
        for number, row in enumerate(reader, start=2):
            try:
                result.append(Transaction(
                    uid=_uid("ledger", path.name, number, *(row.get(c, "") for c in LEDGER_CSV_COLUMNS)),
                    date=date.fromisoformat(row["date"].strip()),
                    type=row["type"].strip().upper(),
                    asset_in=(row.get("asset_in") or "").strip().upper(),
                    qty_in=_num(row.get("qty_in")),
                    asset_out=(row.get("asset_out") or "").strip().upper(),
                    qty_out=_num(row.get("qty_out")),
                    inr_value=_optional_num(row.get("inr_value")),
                    tds_inr=_num(row.get("tds_inr")),
                    source=f"csv:{path.name}",
                    tx_ref=(row.get("tx_ref") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                ))
            except (ValueError, KeyError) as exc:
                raise ValueError(f"{path.name} line {number}: {exc}") from exc
        return result


def _pick(row: dict, *names: str) -> str:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return ""


def read_etherscan_csv(path: Path, asset: str = "ETH") -> list[Transaction]:
    """Etherscan-style export. Column names vary between explorer versions, so several spellings are accepted."""
    asset = asset.upper()
    result = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            tx_hash = _pick(row, "Transaction Hash", "Txhash", "TxHash", "Hash")
            stamp = _pick(row, "UnixTimestamp", "Timestamp")
            if stamp:
                day = datetime.fromtimestamp(int(stamp), tz=timezone.utc).date()
            else:
                day = datetime.fromisoformat(_pick(row, "DateTime (UTC)", "DateTime").replace(" ", "T")).date()
            failed = _pick(row, "Status").strip().lower() not in ("", "success", "1") or bool(_pick(row, "ErrCode"))
            value_in = _num(_pick(row, f"Value_IN({asset})", "Value_IN"))
            value_out = _num(_pick(row, f"Value_OUT({asset})", "Value_OUT"))
            fee = _num(_pick(row, f"TxnFee({asset})", "TxnFee(ETH)", "TxnFee"))
            method = _pick(row, "Method")
            if value_in and not failed:
                result.append(Transaction(_uid("etherscan", tx_hash, "in"), day, "UNCLASSIFIED", asset_in=asset,
                                          qty_in=value_in, source="etherscan", tx_ref=tx_hash,
                                          notes=f"received from {_pick(row, 'From')} {method}".strip()))
            if value_out and not failed:
                result.append(Transaction(_uid("etherscan", tx_hash, "out"), day, "UNCLASSIFIED", asset_out=asset,
                                          qty_out=value_out, source="etherscan", tx_ref=tx_hash,
                                          notes=f"sent to {_pick(row, 'To')} {method}".strip()))
            if fee:
                result.append(Transaction(_uid("etherscan", tx_hash, "fee"), day, "FEE", asset_out=asset,
                                          qty_out=fee, source="etherscan", tx_ref=tx_hash,
                                          notes="network fee" + (" (transaction failed)" if failed else "")))
    return result


def read_agent_actions(log_dir: Path) -> list[dict]:
    """Money actions the agent executed after approval, from its session logs. For reconciliation only."""
    actions = []
    for path in sorted(log_dir.glob("session-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["type"] == "audit":
                for entry in event["data"].get("entries", []):
                    # PAID = confirmed on-chain; UNCHECKED/EXECUTED = approved but not verified (older logs: EXECUTED)
                    if entry["outcome"] in ("PAID", "UNCHECKED", "EXECUTED"):
                        actions.append({"ts": event["ts"], "session": event["session"], "action": entry["action"],
                                        "outcome": entry["outcome"], "usd": entry.get("usd")})
    return actions
