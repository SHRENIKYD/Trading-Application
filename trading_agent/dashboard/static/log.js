"use strict";
// Needs common.js. Turns the session's event stream into a plain-language, step-by-step log.

const log = $("log");
let group = null;       // the <li> for the current step
let pendingLatency = null;
let lastUrl = null;

const time = (ts) => new Date(ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function newGroup(title, ts, role = "info") {
  const list = el("ul", { className: "log-lines" });
  const li = el("li", { className: "log-step" },
    el("h2", { className: "log-step-title" }, title, el("span", { className: "subtle log-time" }, time(ts))),
    list);
  li.dataset.role = role;
  li.lines = list;
  log.append(li);
  group = li;
  return li;
}

// One line in the current step: icon, text, optional detail block.
function line(role, iconName, text, detail = null, detailKind = "") {
  if (!group) newGroup("Before the first step", new Date().toISOString());
  const item = el("li", { className: "log-line" }, el("span", { className: "log-icon" }, icon(iconName)),
    el("div", { className: "log-body" }, el("p", {}, ...[].concat(text)), detail));
  item.dataset.role = role;
  if (detailKind) item.dataset.detail = detailKind;
  group.lines.append(item);
  return item;
}

function details(summary, content) {
  return el("details", { className: "log-details" }, el("summary", {}, summary), el("pre", {}, content));
}

const strong = (text) => el("strong", {}, text);

function resultRole(result) {
  if (result.startsWith("OK")) return "safe";
  if (result.startsWith("BLOCKED")) return "limit";
  return "warn";
}

const handlers = {
  session_started(d, e) {
    $("session").textContent = e.session;
    $("goal").textContent = d.goal;
    $("setup").textContent = `Start ${d.start_url} · allowed ${d.allowed_domains.join(", ")} · model ${d.model} · ` +
      `budget ${usd(d.treasury.balance_usd)}, floor ${usd(d.treasury.floor_usd)}, auto-approve up to ${usd(d.auto_approve_usd)}`;
    newGroup("Run started", e.ts, "safe");
    line("safe", "robot", ["Agent started on ", strong(d.start_url), "."]);
  },
  goal_started(d, e) {
    newGroup(`Goal ${d.number}`, e.ts, d.chosen_by === "agent" ? "money" : "info");
    line("info", "target", [d.chosen_by === "agent" ? "The agent chose: " : "Your task: ", strong(d.goal),
      ` (starting at ${d.start_url})`]);
  },
  plan(d) {
    if (d.stop) line("warn", "warning", ["The agent decided to stop the mission: ", strong(d.stop)]);
  },
  observation(d, e) {
    lastUrl = d.url;
    newGroup(`Step ${log.querySelectorAll(".log-step[data-step]").length + 1}`, e.ts).dataset.step = "1";
    line("info", "eye", ["Looked at ", strong(d.url)],
      details("What the agent saw",
        `CLICKABLE\n${d.clickables}\n\nINPUT FIELDS\n${d.inputs}\n\nPAGE TEXT\n${d.text}`), "seen");
  },
  llm_response(d) {
    pendingLatency = d.latency_ms;
    line("info", "brain", `Model replied in ${(d.latency_ms / 1000).toFixed(1)}s`, details("Raw reply", d.raw), "raw");
  },
  llm_invalid(d) {
    line("warn", "warning", [`Model's reply was not a valid action (try ${d.attempt} of ${d.max_attempts}): `, strong(d.error),
      " It was asked to try again."]);
  },
  action_proposed(d) {
    const a = parseAction(d.action);
    if (a?.action === "EVALUATE") return;
    const took = pendingLatency === null ? "" : ` (thought for ${(pendingLatency / 1000).toFixed(1)}s)`;
    pendingLatency = null;
    line("info", "cursor-click", ["Decided: ", strong(describeAction(a)), took]);
  },
  guardrail_blocked(d) {
    line("limit", "shield-warning", [`Guardrail “${d.rule.replace(/_/g, " ")}” blocked it: `, strong(d.reason)]);
  },
  approval_requested(d) {
    const cost = d.usd === null ? "unknown cost" : usd(d.usd);
    const empty = d.empty_fields.length ? ` Empty fields: ${d.empty_fields.join(", ")}.` : "";
    line("warn", "hand-coins", ["Asked you to approve (", strong(cost), `, flagged by: ${d.triggers.join(", ")}).${empty}`]);
  },
  approval_decided(d) {
    const who = { dashboard: "by you", terminal: "by you", timeout: "no answer in time" }[d.by] || d.by || "";
    if (d.decision === "AUTO-APPROVED") {
      line("safe", "check-circle", ["Approved automatically: ", strong(usd(d.usd)), " is within the auto-approve limit."]);
    } else {
      line(d.decision === "APPROVED" ? "safe" : "limit", d.decision === "APPROVED" ? "check-circle" : "x-circle",
        [strong(d.decision === "APPROVED" ? "Approved" : "Blocked"), who ? ` ${who}.` : "."]);
    }
  },
  action_result(d) {
    const role = resultRole(d.result);
    if (role === "safe") {
      const moved = d.url !== lastUrl ? ` Now on ${d.url}.` : "";
      line("safe", "check-circle", [strong("Done."), moved]);
      return;
    }
    const label = role === "limit" ? "Did not happen: " : "Failed: ";
    const text = d.result.replace(/^(FAILED|BLOCKED)\s*(\([^)]*\))?:?\s*/, "").replace(/^by guardrail \[[^\]]+\]:\s*/, "");
    line(role, "x-circle", [strong(label), text]);
  },
  treasury(d) {
    line("money", "coins", ["Budget now ", strong(usd(d.balance_usd)), ` (spent ${usd(d.spent_usd)}, can still spend ${usd(d.headroom_usd)}).`]);
  },
  health_check(d, e) {
    newGroup("Health check", e.ts, "warn");
    line("warn", "hourglass", d.automatic
      ? `Health check after ${d.actions} actions: continued automatically (unattended mode).`
      : `Paused after ${d.actions} actions to ask whether to continue.`);
  },
  confirm_decided(d) {
    const who = { dashboard: "you", timeout: "no answer in time" }[d.by] || d.by;
    line(d.answer === "yes" ? "safe" : "limit", d.answer === "yes" ? "check-circle" : "x-circle",
      [strong(d.answer === "yes" ? "Continue" : "Stop"), ` (${who}).`]);
  },
  stop_requested() { line("limit", "stop", "Stop pressed on the dashboard."); },
  kill_switch(d) { line("limit", "stop", ["Stopped: ", strong(d.reason)]); },
  evaluate(d, e) {
    newGroup("Result", e.ts, "info");
    line("warn", "warning", ["Model says (unverified): ", strong(`“${d.reason}”`)]);
  },
  audit(d) {
    line(d.paid ? "safe" : "info", "list-checks",
      [d.onchain ? "Verified on-chain: " : "Verified from the log: ", strong(auditSummary(d))]);
  },
  wallet_popup_open() {
    line("warn", "hand-coins", "MetaMask opened in the agent's browser (Chrome for Testing) - the agent paused for you.");
  },
  awaiting_wallet_confirmation(d) {
    line("warn", "hand-coins", `Waiting up to ${Math.round(d.timeout_s / 60)} min for the payment to leave the wallet (confirm or reject in MetaMask).`);
  },
  wallet_continue_requested() { line("info", "check-circle", "You pressed “I'm done in MetaMask”; the agent stopped waiting."); },
  wallet_popup_closed(d) {
    line(d.timed_out ? "warn" : "info", d.timed_out ? "warning" : "check-circle",
      d.timed_out ? "Gave up waiting for MetaMask; the agent moved on." : "MetaMask closed; the agent continued.");
  },
  payment_check(d) {
    if (!d.sent) { line("info", "coins", ["Checked on-chain: ", strong("no payment left the wallet"), "."]); return; }
    line(d.confirmed ? "safe" : "warn", "coins", ["Checked on-chain: ",
      strong(`${d.spent_eth} ETH left the wallet${d.usd === null ? "" : ` (${usd(d.usd)})`}`), `, fees included. ${d.note}.`]);
  },
  chain_connected(d) {
    line("info", "globe", ["Watching wallet ", strong(d.wallet), ` on ${d.network || "chain " + d.chain_id} (read-only).`]);
  },
  audit_warning(d) { line("warn", "warning", strong(d.message)); },
  session_ended(d, e) {
    if (!group || group.querySelector(".log-step-title").firstChild.textContent !== "Result") newGroup("Result", e.ts);
    const done = d.reason.startsWith("model finished");
    line(done ? "safe" : "limit", done ? "check-circle" : "x-circle",
      [strong(done ? "Run finished" : "Run stopped"), ` after ${d.actions} action${d.actions === 1 ? "" : "s"}: ${d.reason}.`]);
    $("live").textContent = `Ended ${time(e.ts)}`;
  },
};

function applyFilters() {
  document.body.dataset.showSeen = $("show-seen").checked;
  document.body.dataset.showRaw = $("show-raw").checked;
}
$("show-seen").addEventListener("change", applyFilters);
$("show-raw").addEventListener("change", applyFilters);
applyFilters();

listenToEvents((event) => {
  handlers[event.type]?.(event.data, event);
  if (event.type !== "session_ended" && $("live").textContent === "Connecting") $("live").textContent = "Live";
}, () => { if ($("live").textContent === "Live") $("live").textContent = "Disconnected - the agent may have exited"; });
