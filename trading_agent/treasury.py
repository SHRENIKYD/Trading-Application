"""The agent's own money, in US dollars, persisted across runs so it lives on the capital it started with.

balance = start + income - spent. A spend is refused when it would take the balance below the floor.
Only the agent's approved, executed payments are recorded as spent; income is recorded by you.

  python -m trading_agent.treasury status
  python -m trading_agent.treasury init --start 5 --floor 4
  python -m trading_agent.treasury add-income 0.25 --note "task payout"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path("data/treasury.json")


class Treasury:
    def __init__(self, path: Path, start_usd: float, floor_usd: float) -> None:
        self.path = path
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
        else:
            if floor_usd > start_usd:
                raise ValueError(f"floor ${floor_usd} is above the starting capital ${start_usd}")
            self.state = {"start_usd": start_usd, "floor_usd": floor_usd, "spent_usd": 0.0, "income_usd": 0.0,
                          "entries": []}
            self._save()

    @property
    def balance(self) -> float:
        return round(self.state["start_usd"] + self.state["income_usd"] - self.state["spent_usd"], 6)

    @property
    def floor(self) -> float:
        return self.state["floor_usd"]

    @property
    def headroom(self) -> float:
        """Dollars the agent may still spend before reaching the floor."""
        return round(max(self.balance - self.floor, 0.0), 6)

    def can_spend(self, usd: float) -> bool:
        return usd <= self.headroom + 1e-9

    def record_spend(self, usd: float, detail: str) -> None:
        self.state["spent_usd"] = round(self.state["spent_usd"] + usd, 6)
        self._entry("spend", usd, detail)

    def record_income(self, usd: float, detail: str) -> None:
        self.state["income_usd"] = round(self.state["income_usd"] + usd, 6)
        self._entry("income", usd, detail)

    def snapshot(self) -> dict:
        return {"start_usd": self.state["start_usd"], "floor_usd": self.floor, "spent_usd": self.state["spent_usd"],
                "income_usd": self.state["income_usd"], "balance_usd": self.balance, "headroom_usd": self.headroom}

    def _entry(self, kind: str, usd: float, detail: str) -> None:
        self.state["entries"].append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "kind": kind, "usd": usd, "detail": detail})
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trading_agent.treasury", description="The agent's own budget.")
    parser.add_argument("--file", type=Path, default=DEFAULT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("init", help="Create the treasury (refuses to overwrite an existing one).")
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--floor", type=float, required=True)
    p = sub.add_parser("add-income", help="Record money the agent earned, in USD.")
    p.add_argument("usd", type=float)
    p.add_argument("--note", default="")
    args = parser.parse_args(argv)

    if args.command == "init":
        if args.file.exists():
            print(f"{args.file} already exists; delete it first to start over.", file=sys.stderr)
            return 1
        Treasury(args.file, args.start, args.floor)
    elif not args.file.exists():
        print(f"No treasury at {args.file}; run: init --start 5 --floor 4", file=sys.stderr)
        return 1
    treasury = Treasury(args.file, 0, 0)
    if args.command == "add-income":
        if args.usd <= 0:
            print("Income must be positive.", file=sys.stderr)
            return 1
        treasury.record_income(args.usd, args.note)
    s = treasury.snapshot()
    print(f"Start ${s['start_usd']:.2f} + income ${s['income_usd']:.2f} - spent ${s['spent_usd']:.2f} "
          f"= balance ${s['balance_usd']:.2f}; floor ${s['floor_usd']:.2f}; may still spend ${s['headroom_usd']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
