# PRODUCT.md — Agent Control Dashboard (draft for approval)

## What it is
A local web page (http://127.0.0.1:8787) served by the agent itself. It is the one place to
start a run, watch every step live, approve or block money actions, and stop the agent.
Nothing leaves the machine.

## Who uses it
One person: the owner of the wallet, sitting at the same computer as the agent.

## Jobs, in priority order
1. **Decide on a money action** — see the action, the button's text, every form value, which
   guardrail flagged it, and approvals used. Approve or Block. This must be impossible to miss.
2. **Stop the agent now** — one Stop control, always visible, works mid-step.
3. **Know what really happened** — the verified audit (executed vs blocked) shown apart from the
   model's own claim, which is always labelled "unverified".
4. **Follow the run** — a live timeline: observation → proposed action → guardrail verdict → result.
5. **Start a run** — goal, start URL, allowed sites, max amount, max approvals.
6. **Review past runs** — open any `logs/session-*.jsonl` file.

## Sections (built one at a time, each approved before the next)
1. Run header: title, live activity + step count, status, goal banner, fact cards (limits and budget),
   result banner when the run ends (model's claim marked unverified, next to the verified audit), Stop,
   and a link to the raw session log.
2. Approval card (appears only while an approval is pending).
3. Live timeline — built as the Session log page (`/log`, opened from "View session log"): the run grouped into
   steps in plain language (looked at, decided, asked you, approved/blocked, done/failed, budget), the model's claim
   marked unverified beside the verified audit; what the agent saw and the model's raw replies are opt-in; the raw
   JSONL file is one click away.
4. Audit panel.
5. Start-run form.
6. Past sessions list.

## Tax Ledger page (second page, same dashboard)
Reads the local ledger (`data/ledger.db`, built by `python -m trading_agent.tax`). Every figure is labelled
"estimate - confirm with a chartered accountant".
1. Financial-year summary: gains, losses (shown, never set off), tax + cess, TDS credit, balance due,
   next advance-tax date and amount.
2. Needs review: unclassified or unpriced items, each fixable in place (type, rupee value, TDS).
3. Transactions table: filter by financial year and asset; import buttons for the ledger CSV and explorer CSV.
4. Schedule VDA export: preview of the rows, then download the CSV for the CA.

## Voice
- Plain and literal. Say what happened: "Blocked: amount 1.5 exceeds limit 0.01."
- Never soften a block or a failure. Never celebrate.
- The model's words are always quoted and marked "model says (unverified)".
- Money amounts are shown exactly as the page had them, never rounded.

## Out of scope
Remote access, multiple users, accounts, charts, notifications, mobile layout beyond "readable".
