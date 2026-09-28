from __future__ import annotations

import csv
from pathlib import Path

from .engine import LedgerResult

SCHEDULE_VDA_HEADER = (
    "Date of Acquisition", "Date of Transfer", "Head under which income to be taxed",
    "Cost of Acquisition (Rs)", "Consideration Received (Rs)", "Income from transfer of VDA (Rs)",
    "Asset", "Quantity", "Ledger id", "Type",
)


def write_schedule_vda(result: LedgerResult, fy: str, path: Path, head: str = "Capital Gain") -> int:
    """One row per disposal slice in the financial year, in Schedule VDA column order. Returns the row count."""
    rows = [d for d in result.disposals if d.fy == fy]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SCHEDULE_VDA_HEADER)
        for d in rows:
            writer.writerow((
                d.acquired.isoformat() if d.acquired else "UNKNOWN",
                d.transferred.isoformat(), head,
                f"{d.cost:.2f}", f"{d.consideration:.2f}", f"{d.gain:.2f}",
                d.asset, f"{d.quantity:g}", d.tx_id, d.tx_type,
            ))
    return len(rows)
