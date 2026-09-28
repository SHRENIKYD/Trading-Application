# DESIGN.md — Agent Control Dashboard

Source of truth for the dashboard's visual values. Nothing outside these values is used.
Direction: dark control room — navy base, softly tinted cards, one gold accent, icons on every fact.

## Spacing
Base unit 4px. Allowed values only:

| Token | px |
|---|---|
| `--space-1` | 4 |
| `--space-2` | 8 |
| `--space-3` | 12 |
| `--space-4` | 16 |
| `--space-5` | 20 |
| `--space-6` | 24 |
| `--space-8` | 32 |
| `--space-12` | 48 |

Page gutter `--space-8` (`--space-4` under 640px); gap between blocks `--space-6`; gap between fact cards `--space-5`;
padding inside cards and banners `--space-6`.

## Type
- Font: **Nunito** (variable, bundled locally from Google Fonts, SIL Open Font License) with the system UI stack as fallback.
  No font is loaded from the internet.
- Two weights only: **400** (body, labels) and **700** (title, values, buttons, status).
- Numbers use `font-variant-numeric: tabular-nums`.

| Token | Size / line height | Use |
|---|---|---|
| `--text-xs` | 13 / 18 | notes under values, timestamps |
| `--text-sm` | 15 / 22 | card labels, status bar text |
| `--text-md` | 17 / 24 | goal text, buttons, banner detail |
| `--text-lg` | 22 / 30 | card values, banner title |
| `--text-xl` | 40 / 48 | page title (letter-spacing -0.02em) |

Sentence case everywhere ("Start page", "Can still spend"). The only uppercase text is the status badge and the "Session goal" eyebrow.

## Colour
Dark theme is the design. Light theme uses the same roles with lighter surfaces and follows the OS setting.

Base:

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | #0B1020 | #F4F6FA | page (dark adds two faint radial glows: navy top-left, teal bottom-right) |
| `--surface` | #121A2C | #FFFFFF | cards, banners |
| `--border` | #22304A | #D9DFEA | 1px card borders |
| `--text` | #EEF2F8 | #111827 | primary text |
| `--text-muted` | #97A3B8 | #5A6478 | labels, notes |
| `--accent` | #F2C46D | #A86B00 | the one accent: "Control" in the title, active status, focus ring |

Roles — every tinted card and icon tile takes its colour from what it means, never from decoration:

| Role | Dark | Light | Used for |
|---|---|---|---|
| `--info` | #6EA8FF | #2757C7 | Start page, Model, Money actions this run |
| `--safe` | #4CC98A | #1C7C4B | Allowed sites, Can still spend (healthy), finished banner, running status |
| `--money` | #F2C46D | #A86B00 | Balance, Floor, Auto-approve |
| `--limit` | #F27A7A | #B42318 | Largest amount, stopped/blocked states, Stop button |
| `--warn` | #F5A35C | #9A5B00 | Can still spend when under $0.50, stopping status, unverified-claim warnings |

A card's tint is its role colour at 8% over `--surface`, its border the role colour at 28%, its icon tile the role colour
at 16% with the icon in the full role colour. Status is never shown by colour alone: every status also has a text label.

## Shape and depth
- Radius: 16px on cards and banners, 12px on icon tiles and buttons, 8px on the status badge.
- Borders 1px. Shadows only on the page's cards in dark theme: `0 8px 24px` in `--bg` at 60% (tinted, never pure black).
- Focus: 2px `--accent` outline, offset 2px, on every interactive element.

## Icons
Phosphor Icons, bold weight, bundled as one local SVG sprite (MIT License). 24px inside a 48px tile (56px tile for the logo).
One icon per fact card, chosen for meaning: globe (start page), shield-check (allowed sites), cpu (model),
coins (balance), scales (largest amount), hand-coins (auto-approve), lightning (can still spend),
list-checks (money actions), shield-warning (floor), target (goal), robot (logo).

## Components
- **Title block** — logo tile, "Agent **Control**" (Control in `--accent`), session id underneath in `--text-muted`.
- **Activity indicator** (replaces a stage stepper; the agent loops, it has no fixed stages) — current activity
  (Reading page / Deciding / Acting / Waiting for your approval / Stopping / Finished / Stopped) with its icon,
  and "Step N of M" underneath.
- **Status badge** — icon + uppercase word, `--text-sm` 700, tinted by role.
- **Goal banner** — target tile, "Session goal" eyebrow, quoted goal at `--text-md` 700.
- **Fact card** — icon tile left; label (`--text-sm` muted), value (`--text-lg` 700), optional note (`--text-xs` muted).
- **Result banner** — appears when the run ends. Title "Run finished" (safe) or "Run stopped" (limit); the model's final
  claim quoted and labelled "Model says (unverified)"; the verified audit line; "Ended at" with local time.
- **Approval card** — appears directly under the top row whenever the agent waits for you; hidden otherwise.
  `--warn` role with a 2px full-colour border (the only 2px border on the page). Head: hand-coins tile,
  "Your approval is needed", one-line summary of the action, countdown "Blocks itself in m:ss" on the right.
  Facts in four columns: cost, budget after, why it was flagged, money actions used; then button-or-field and page
  (each spanning two columns). Form table "Form as it will be sent" with empty values shown as an EMPTY badge
  and a warning line. Buttons: **Block** (limit) first, **Approve** (safe) second; the approve button reads
  "Approve anyway" in the warn colour when a field is empty or the cost is unknown. Nothing is pre-focused on a
  button; focus moves to the card's heading. The same card asks yes/no questions (health check, retry) with
  "Yes, continue" / "No, stop", where no answer counts as No.
- **Waiting-for-MetaMask banner** — shown under the top row while the agent waits on a wallet window or on a
  payment to appear on-chain: `--warn` tint, 1px border, hand-coins tile, "Waiting for you in MetaMask", one line of
  instruction, and a quiet button "I'm done in MetaMask – continue". Hidden as soon as the wait ends.
- **Session log page** — same top row (list-checks tile, "Session **log**"), Dashboard and "Raw file (JSONL)"
  buttons on the right, the goal banner with a one-line setup summary, two opt-in checkboxes ("what the agent saw",
  "raw replies"), then one card per step: `--surface` with a 4px left border in the step's role colour, title and
  local time on one line, and lines of icon (20px, role colour) + plain sentence. Blocked and warning lines take
  their role colour; details open in a monospace panel on `--bg`, max 320px tall and scrolling.
- **Action bar** — Stop button (limit role, stop icon), status note, and "View session log" link button on the right.
- **Buttons** — 48px tall: `--text-md` line height 24 + `--space-3` padding top/bottom; `--space-5` padding sides.
  Hover: role colour at 12% lighter background. Disabled: `--border` background, `--text-muted` text.

## Layout
- Max width 1200px, centred. Nine fact cards in three rows of three, grouped by meaning:
  run (start page, allowed sites, model), budget (balance, floor, can still spend),
  limits (auto-approve, largest amount, money actions). Two columns under 1000px, one under 640px.
- Top row: title block left, activity indicator centre, status badge right; wraps to stacked rows under 900px.
- Under 640px the page gutter drops to `--space-4` and the title to 32 / 40.

## Motion
None until the layout is approved.
