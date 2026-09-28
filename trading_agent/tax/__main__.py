"""Tax ledger command line: python -m trading_agent.tax <command> ...  (run with -h for help)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .engine import compute, summarize
from .export import write_schedule_vda
from .importers import read_agent_actions, read_etherscan_csv, read_ledger_csv
from .models import TYPES, financial_year
from .store import LedgerStore


def _money(value: float) -> str:
    return f"Rs {value:,.2f}"


def cmd_import(store: LedgerStore, transactions: list) -> None:
    added = sum(store.add(tx) for tx in transactions)
    print(f"Imported {added} new transaction(s); {len(transactions) - added} already in the ledger.")


def cmd_list(store: LedgerStore, args) -> None:
    result = compute(store.all(), store.prices())
    flagged = {tx_id for tx_id, _ in result.review}
    for tx in store.all():
        if args.fy and financial_year(tx.date) != args.fy:
            continue
        if args.review and tx.id not in flagged:
            continue
        legs = " ".join(filter(None, [f"+{tx.qty_in:g} {tx.asset_in}" if tx.asset_in else "",
                                      f"-{tx.qty_out:g} {tx.asset_out}" if tx.asset_out else ""]))
        value = _money(tx.inr_value) if tx.inr_value is not None else "no value"
        print(f"#{tx.id:<4} {tx.date}  {tx.type:<12} {legs:<30} {value:<18} {'REVIEW ' if tx.id in flagged else ''}{tx.notes}")


def cmd_report(store: LedgerStore, args) -> None:
    result = compute(store.all(), store.prices())
    s = summarize(result, args.fy)
    print(f"Financial year {s.fy}  (estimate - confirm with a chartered accountant)")
    print(f"  Gains from VDA transfers      {_money(s.gains)}")
    print(f"  Losses (NOT set off)          {_money(s.losses)}")
    print(f"  Tax at 30%                    {_money(s.tax)}")
    print(f"  Cess at 4%                    {_money(s.cess)}")
    print(f"  Total tax on VDA gains        {_money(s.total_tax)}")
    print(f"  TDS already deducted          {_money(s.tds_credit)}")
    print(f"  Balance to pay                {_money(s.balance_due)}")
    print(f"  Airdrop/reward/gift income    {_money(s.other_income)}  (taxed at your slab rate; not included above)")
    if s.advance_tax:
        print("  Advance tax due (cumulative):")
        for due, amount in s.advance_tax:
            print(f"    by {due}  {_money(amount)}")
    else:
        print("  Advance tax: not required on this estimate (VDA tax under Rs 10,000).")
    print(f"  Holdings now: {', '.join(f'{q:g} {a}' for a, q in result.holdings.items()) or 'none'}")
    if result.review:
        print(f"\n  INCOMPLETE: {len(result.review)} item(s) need review; figures above exclude them:")
        for tx_id, problem in result.review:
            print(f"    #{tx_id}: {problem}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trading_agent.tax", description="Local crypto tax ledger (India).")
    parser.add_argument("--db", default="data/ledger.db", help="Ledger database file.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-csv", help="Import a CSV in the ledger template format (see ledger_template.csv).")
    p.add_argument("file", type=Path)
    p = sub.add_parser("import-etherscan", help="Import a block explorer CSV export for one address.")
    p.add_argument("file", type=Path)
    p.add_argument("--asset", default="ETH")
    p = sub.add_parser("set-price", help="Record the rupee price of one unit of an asset on a day.")
    p.add_argument("asset")
    p.add_argument("day", type=date.fromisoformat)
    p.add_argument("inr", type=float)
    p = sub.add_parser("classify", help="Set the type and/or values of a transaction.")
    p.add_argument("id", type=int)
    p.add_argument("--type", choices=TYPES)
    p.add_argument("--inr", type=float, help="Rupee value of the transaction.")
    p.add_argument("--tds", type=float, help="TDS deducted in rupees.")
    p.add_argument("--notes")
    p = sub.add_parser("list", help="List transactions.")
    p.add_argument("--fy", help="Financial year, e.g. 2026-27.")
    p.add_argument("--review", action="store_true", help="Only items that need review.")
    p = sub.add_parser("report", help="Tax estimate for a financial year.")
    p.add_argument("--fy", default=financial_year(date.today()))
    p = sub.add_parser("export-vda", help="Write a Schedule VDA CSV for your CA.")
    p.add_argument("--fy", default=financial_year(date.today()))
    p.add_argument("--out", type=Path)
    p.add_argument("--head", default="Capital Gain", choices=("Capital Gain", "Business Income"))
    p = sub.add_parser("agent-actions", help="Money actions the agent executed, from its session logs.")
    p.add_argument("--log-dir", type=Path, default=Path("logs"))

    args = parser.parse_args(argv)
    store = LedgerStore(Path(args.db))
    try:
        if args.command == "import-csv":
            cmd_import(store, read_ledger_csv(args.file))
        elif args.command == "import-etherscan":
            cmd_import(store, read_etherscan_csv(args.file, args.asset))
        elif args.command == "set-price":
            store.set_price(args.asset, args.day, args.inr)
            print(f"Price set: 1 {args.asset.upper()} = {_money(args.inr)} on {args.day}")
        elif args.command == "classify":
            fields = {k: v for k, v in (("type", args.type), ("inr_value", args.inr), ("tds_inr", args.tds),
                                        ("notes", args.notes)) if v is not None}
            if not fields:
                parser.error("classify needs at least one of --type, --inr, --tds, --notes")
            store.update(args.id, **fields)
            print(f"Updated #{args.id}: {fields}")
        elif args.command == "list":
            cmd_list(store, args)
        elif args.command == "report":
            cmd_report(store, args)
        elif args.command == "export-vda":
            out = args.out or Path(f"data/schedule_vda_{args.fy}.csv")
            result = compute(store.all(), store.prices())
            rows = write_schedule_vda(result, args.fy, out, args.head)
            print(f"Wrote {rows} row(s) to {out}")
            if result.review:
                print(f"WARNING: {len(result.review)} item(s) still need review; run: list --review")
        elif args.command == "agent-actions":
            actions = read_agent_actions(args.log_dir)
            for item in actions:
                cost = f"${item['usd']:.2f}" if item.get("usd") else "amount unknown"
                print(f"{item['ts']}  session {item['session']}  {item.get('outcome', 'EXECUTED'):<9}  {cost:<15}  {item['action']}")
            print(f"{len(actions)} executed money action(s) in {args.log_dir}")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
