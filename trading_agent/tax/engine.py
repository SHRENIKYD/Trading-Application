"""FIFO cost basis and the Indian VDA tax computation. Pure functions: no I/O, fully unit-tested."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date

from .models import ACQUIRES, DISPOSES, PRICED, Transaction, financial_year, fy_bounds

TAX_RATE = 0.30
CESS_RATE = 0.04
ADVANCE_TAX_THRESHOLD = 10_000
EPSILON = 1e-12
# (month, day, cumulative share of the year's tax due by that date)
ADVANCE_TAX_SCHEDULE = ((6, 15, 0.15), (9, 15, 0.45), (12, 15, 0.75), (3, 15, 1.00))


@dataclass
class Disposal:
    """One Schedule VDA row: part of a transfer matched to one acquisition lot."""
    tx_id: int | None
    tx_type: str
    asset: str
    quantity: float
    acquired: date | None
    transferred: date
    cost: float
    consideration: float

    @property
    def gain(self) -> float:
        return self.consideration - self.cost

    @property
    def fy(self) -> str:
        return financial_year(self.transferred)


@dataclass
class IncomeReceipt:
    tx_id: int | None
    tx_type: str
    day: date
    asset: str
    quantity: float
    value: float


@dataclass
class LedgerResult:
    disposals: list[Disposal] = field(default_factory=list)
    income: list[IncomeReceipt] = field(default_factory=list)
    review: list[tuple[int | None, str]] = field(default_factory=list)
    holdings: dict[str, float] = field(default_factory=dict)
    tds: dict[str, float] = field(default_factory=lambda: defaultdict(float))


@dataclass
class FYSummary:
    fy: str
    gains: float
    losses: float
    tax: float
    cess: float
    total_tax: float
    tds_credit: float
    balance_due: float
    other_income: float
    advance_tax: list[tuple[date, float]]
    needs_review: int


def value_of(tx: Transaction, prices: dict[tuple[str, date], float]) -> float | None:
    """The transaction's rupee value: entered directly, or quantity x the price recorded for that day."""
    if tx.inr_value is not None:
        return tx.inr_value
    legs = [(tx.asset_in, tx.qty_in), (tx.asset_out, tx.qty_out)] if tx.type in ACQUIRES else [(tx.asset_out, tx.qty_out)]
    for asset, qty in legs:
        price = prices.get((asset.upper(), tx.date))
        if asset and qty and price is not None:
            return qty * price
    return None


def compute(transactions: list[Transaction], prices: dict[tuple[str, date], float]) -> LedgerResult:
    result = LedgerResult()
    lots: dict[str, deque[list]] = defaultdict(deque)  # asset -> [quantity, cost per unit, acquired date]

    for tx in sorted(transactions, key=lambda t: (t.date, t.id or 0)):
        if tx.tds_inr:
            result.tds[financial_year(tx.date)] += tx.tds_inr
        if tx.type == "UNCLASSIFIED":
            result.review.append((tx.id, "type not set (BUY, SELL, SWAP, PAYMENT, FEE, INCOME, GIFT_IN, TRANSFER_IN, TRANSFER_OUT)"))
            continue
        if tx.type in ("TRANSFER_IN", "TRANSFER_OUT"):
            continue

        value = value_of(tx, prices)
        if tx.type in PRICED and value is None:
            result.review.append((tx.id, f"no rupee value: enter inr_value or a price for "
                                         f"{(tx.asset_in if tx.type in ACQUIRES else tx.asset_out) or '?'} on {tx.date}"))
            continue

        if tx.type in DISPOSES:
            _dispose(tx, value, lots, result)
        if tx.type in ACQUIRES:
            asset = tx.asset_in.upper()
            if not asset or tx.qty_in <= 0:
                result.review.append((tx.id, "acquisition without asset_in / qty_in"))
                continue
            lots[asset].append([tx.qty_in, value / tx.qty_in, tx.date])
            if tx.type in ("INCOME", "GIFT_IN"):
                result.income.append(IncomeReceipt(tx.id, tx.type, tx.date, asset, tx.qty_in, value))

    result.holdings = {asset: sum(lot[0] for lot in queue) for asset, queue in lots.items()
                       if sum(lot[0] for lot in queue) > EPSILON}
    return result


def _dispose(tx: Transaction, consideration: float, lots: dict, result: LedgerResult) -> None:
    asset = tx.asset_out.upper()
    if not asset or tx.qty_out <= 0:
        result.review.append((tx.id, "disposal without asset_out / qty_out"))
        return
    remaining = tx.qty_out
    queue = lots[asset]
    while remaining > EPSILON and queue:
        lot = queue[0]
        used = min(lot[0], remaining)
        result.disposals.append(Disposal(tx.id, tx.type, asset, used, lot[2], tx.date, used * lot[1],
                                         consideration * used / tx.qty_out))
        lot[0] -= used
        remaining -= used
        if lot[0] <= EPSILON:
            queue.popleft()
    if remaining > EPSILON:
        result.disposals.append(Disposal(tx.id, tx.type, asset, remaining, None, tx.date, 0.0,
                                         consideration * remaining / tx.qty_out))
        result.review.append((tx.id, f"disposed {remaining:g} {asset} more than recorded holdings; "
                                     f"cost taken as 0 until the purchase is imported"))


def summarize(result: LedgerResult, fy: str) -> FYSummary:
    rows = [d for d in result.disposals if d.fy == fy]
    gains = sum(d.gain for d in rows if d.gain > 0)
    losses = sum(d.gain for d in rows if d.gain < 0)
    tax = gains * TAX_RATE
    cess = tax * CESS_RATE
    total = tax + cess
    tds = result.tds.get(fy, 0.0)
    balance = max(total - tds, 0.0)
    start, _ = fy_bounds(fy)
    schedule = []
    if total > ADVANCE_TAX_THRESHOLD:
        for month, day, share in ADVANCE_TAX_SCHEDULE:
            year = start.year if month >= 4 else start.year + 1
            schedule.append((date(year, month, day), round(balance * share, 2)))
    return FYSummary(
        fy=fy, gains=round(gains, 2), losses=round(losses, 2), tax=round(tax, 2), cess=round(cess, 2),
        total_tax=round(total, 2), tds_credit=round(tds, 2), balance_due=round(balance, 2),
        other_income=round(sum(i.value for i in result.income if financial_year(i.day) == fy), 2),
        advance_tax=schedule, needs_review=len(result.review),
    )
