from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Tax treatment of each type:
#   BUY          acquire asset_in for inr_value (cost of a new lot)
#   SELL         dispose asset_out; consideration = inr_value
#   SWAP         dispose asset_out and acquire asset_in; both valued at inr_value (value of what was received)
#   PAYMENT      dispose asset_out for goods or services; consideration = inr_value
#   FEE          network fee paid in asset_out; treated as a disposal at inr_value (conservative), not deductible
#   INCOME       airdrop, staking or reward received in asset_in; income at inr_value, which becomes the lot cost
#   GIFT_IN      asset_in received as a gift; taxable when above Rs 50,000 in a year; inr_value becomes the lot cost
#   TRANSFER_IN  / TRANSFER_OUT  moves between your own wallets; not taxable, holdings are pooled per asset
#   UNCLASSIFIED imported but not yet typed; blocks the report until reviewed
TYPES = ("BUY", "SELL", "SWAP", "PAYMENT", "FEE", "INCOME", "GIFT_IN", "TRANSFER_IN", "TRANSFER_OUT", "UNCLASSIFIED")
ACQUIRES = {"BUY", "SWAP", "INCOME", "GIFT_IN"}
DISPOSES = {"SELL", "SWAP", "PAYMENT", "FEE"}
PRICED = ACQUIRES | DISPOSES


@dataclass
class Transaction:
    uid: str                 # stable id used to skip duplicates on re-import
    date: date
    type: str
    asset_in: str = ""
    qty_in: float = 0.0
    asset_out: str = ""
    qty_out: float = 0.0
    inr_value: float | None = None
    tds_inr: float = 0.0
    source: str = ""
    tx_ref: str = ""
    notes: str = ""
    id: int | None = None


def financial_year(day: date) -> str:
    """Indian financial year label, April to March: 2026-09-28 -> '2026-27'."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def fy_bounds(fy: str) -> tuple[date, date]:
    start = int(fy.split("-")[0])
    return date(start, 4, 1), date(start + 1, 3, 31)
