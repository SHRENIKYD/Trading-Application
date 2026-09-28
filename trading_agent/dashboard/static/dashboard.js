"use strict";
// Needs common.js (token, $, ICONS, usd, describeAction, listenToEvents).

const state = { phase: "connecting", step: 0, maxActions: null, maxApprovals: null, approvalsUsed: 0,
                claim: null, audit: null, warning: null, pendingId: null };

// phase -> [status word, role, icon]
const PHASES = {
  connecting: ["Connecting", "info", "hourglass"],
  running: ["Running", "safe", "check-circle"],
  stopping: ["Stopping", "warn", "warning"],
  finished: ["Finished", "safe", "check-circle"],
  stopped: ["Stopped", "limit", "x-circle"],
  disconnected: ["Disconnected", "warn", "warning"],
};

function setPhase(phase) {
  state.phase = phase;
  const [word, role, icon] = PHASES[phase];
  $("status").dataset.role = role;
  $("status-text").textContent = word;
  $("status-icon").setAttribute("href", ICONS + icon);
  $("stop").disabled = phase !== "running";
}

function setActivity(text, icon, role = "info") {
  $("activity").textContent = text;
  $("activity-icon").setAttribute("href", ICONS + icon);
  $("activity-tile").dataset.role = role;
}

function showStep() {
  if (state.maxActions === null) return;
  $("step").textContent = state.step
    ? `Step ${state.step} of ${state.maxActions} before a health check`
    : `No actions yet (health check after ${state.maxActions})`;
}

function showTreasury(t) {
  if (!t) return;
  $("balance").textContent = usd(t.balance_usd);
  $("floor").textContent = usd(t.floor_usd);
  $("headroom").textContent = usd(t.headroom_usd);
  const card = $("headroom-card");
  if (t.headroom_usd <= 0) {
    card.dataset.role = "limit";
    $("headroom-note").textContent = "At the floor: every paid action is blocked";
  } else if (t.headroom_usd < 0.5) {
    card.dataset.role = "warn";
    $("headroom-note").textContent = "Close to the floor";
  } else {
    card.dataset.role = "safe";
    $("headroom-note").textContent = `Balance ${usd(t.balance_usd)} minus floor ${usd(t.floor_usd)}`;
  }
}

function showApprovals() {
  if (state.maxApprovals === null) return;
  $("approvals").textContent = `${state.approvalsUsed} of ${state.maxApprovals}`;
  $("approvals-card").dataset.role = state.approvalsUsed >= state.maxApprovals ? "limit" : "info";
}

function showResult(ended, ts) {
  const stopped = !ended.reason.startsWith("model finished");
  $("result").hidden = false;
  $("result").dataset.role = stopped ? "limit" : "safe";
  $("result-icon").setAttribute("href", ICONS + (stopped ? "x-circle" : "check-circle"));
  $("result-title").textContent = stopped ? "Run stopped" : "Run finished";
  $("result-reason").textContent =
    `Ended after ${ended.actions} action${ended.actions === 1 ? "" : "s"}: ${ended.reason}.`;
  if (state.claim !== null) {
    $("result-claim").hidden = false;
    $("result-claim").replaceChildren("Model says (unverified): ", Object.assign(document.createElement("strong"),
      { textContent: `“${state.claim}”` }));
  }
  if (state.audit) {
    $("result-audit").hidden = false;
    $("result-audit").replaceChildren(state.audit.onchain ? "Verified on-chain: " : "Verified from the log: ",
      Object.assign(document.createElement("strong"), { textContent: auditSummary(state.audit) }));
  }
  if (state.warning) {
    $("result-warning").hidden = false;
    $("result-warning").textContent = state.warning;
  }
  const when = new Date(ts);
  $("result-time").dateTime = when.toISOString();
  $("result-time").textContent = when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// ---- Approval card --------------------------------------------------------

let countdownTimer = null;

function startCountdown(ts, timeoutS) {
  clearInterval(countdownTimer);
  const deadline = Date.parse(ts) + timeoutS * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    $("approval-countdown").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  };
  tick();
  countdownTimer = setInterval(tick, 1000);
}

function openCard(id, ts, timeoutS) {
  state.pendingId = id;
  $("approval").hidden = false;
  $("approval-yes").disabled = false;
  $("approval-no").disabled = false;
  startCountdown(ts, timeoutS);
  $("approval-title").focus({ preventScroll: false });
}

function closeCard(id) {
  if (id && id !== state.pendingId) return;
  state.pendingId = null;
  clearInterval(countdownTimer);
  $("approval").hidden = true;
}

function showMoneyRequest(d, e) {
  $("approval-title").textContent = "Your approval is needed";
  const label = (d.element || "").split(" | ")[0] || d.action.selector || d.action.url;
  $("approval-summary").textContent = {
    CLICK: `The agent wants to click “${label}”.`,
    TYPE: `The agent wants to type “${d.action.text}” into “${label}”.`,
    GOTO: `The agent wants to open ${d.action.url}.`,
  }[d.action.action] || "The agent wants to act.";
  $("approval-money").hidden = false;
  $("approval-question").hidden = true;

  $("approval-cost").textContent = d.usd === null ? "Unknown - not charged to the budget" : usd(d.usd);
  $("approval-cost").dataset.tone = d.usd === null ? "warn" : "";
  if (d.usd === null) {
    $("approval-after").textContent = `${usd(d.treasury.balance_usd)} (cost unknown)`;
    $("approval-after").dataset.tone = "warn";
  } else {
    const after = d.treasury.balance_usd - d.usd;
    $("approval-after").textContent = `${usd(after)} (floor ${usd(d.treasury.floor_usd)})`;
    $("approval-after").dataset.tone = after - d.treasury.floor_usd < 0.5 ? "warn" : "";
  }
  $("approval-why").textContent = `Words: ${d.triggers.join(", ")}`;
  $("approval-count").textContent = `${d.approvals_used} of ${d.max_approvals} used`;
  $("approval-element").textContent = d.element || "(no readable text)";
  $("approval-page").textContent = d.page_url;

  const rows = d.form_fields.map(([name, value]) => {
    const tr = document.createElement("tr");
    const nameCell = Object.assign(document.createElement("td"), { textContent: name });
    const valueCell = document.createElement("td");
    valueCell.append(value ? value : Object.assign(document.createElement("span"),
      { className: "empty-badge", textContent: "Empty" }));
    tr.append(nameCell, valueCell);
    return tr;
  });
  $("approval-form-rows").replaceChildren(...rows);
  $("approval-form").hidden = rows.length === 0;
  const risky = d.empty_fields.length > 0 || d.usd === null;
  $("approval-empty").hidden = d.empty_fields.length === 0;
  $("approval-yes").textContent = risky ? "Approve anyway" : `Approve${d.usd === null ? "" : ` ${usd(d.usd)}`}`;
  $("approval-yes").toggleAttribute("data-risky", risky);
  $("approval-no").textContent = "Block";
  $("approval-note").textContent =
    "If a MetaMask window opens, check and confirm it there yourself. The agent never touches it.";
  openCard(d.id, e.ts, d.timeout_s);
}

function showConfirm(d, e) {
  $("approval-title").textContent = "The agent is asking";
  $("approval-summary").textContent = "No answer counts as No.";
  $("approval-money").hidden = true;
  $("approval-form").hidden = true;
  $("approval-question").hidden = false;
  $("approval-question").textContent = d.question;
  $("approval-yes").textContent = "Yes, continue";
  $("approval-yes").removeAttribute("data-risky");
  $("approval-no").textContent = "No, stop";
  $("approval-note").textContent = "";
  openCard(d.id, e.ts, d.timeout_s);
}

async function sendDecision(approve) {
  const id = state.pendingId;
  if (!id) return;
  $("approval-yes").disabled = true;
  $("approval-no").disabled = true;
  try {
    const response = await fetch("/api/decision", {
      method: "POST",
      headers: { "X-Dashboard-Token": token, "Content-Type": "application/json" },
      body: JSON.stringify({ id, approve }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${response.status}`);
    }
  } catch (error) {
    $("approval-note").textContent = `Not sent: ${error.message}.`;
    $("approval-yes").disabled = false;
    $("approval-no").disabled = false;
  }
}

$("approval-yes").addEventListener("click", () => sendDecision(true));
$("approval-no").addEventListener("click", () => sendDecision(false));

// ---- Waiting for MetaMask ---------------------------------------------------

function showWalletWait(text) {
  $("wallet-wait-text").textContent = text;
  $("wallet-done").disabled = false;
  $("wallet-wait").hidden = false;
}

function hideWalletWait() { $("wallet-wait").hidden = true; }

$("wallet-done").addEventListener("click", async () => {
  $("wallet-done").disabled = true;
  try {
    const response = await fetch("/api/wallet-done", { method: "POST", headers: { "X-Dashboard-Token": token } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    $("wallet-wait-text").textContent = `Not sent (${error.message}). Close the MetaMask window instead.`;
    $("wallet-done").disabled = false;
  }
});

// ---- Event handlers ------------------------------------------------------

const handlers = {
  session_started(d, e) {
    $("session").textContent = e.session;
    $("goal").textContent = d.goal;
    const start = new URL(d.start_url);
    $("start-url").textContent = (start.host + start.pathname).replace(/\/$/, "");
    $("start-url").href = d.start_url;
    $("start-url").title = d.start_url;
    $("domains").textContent = d.allowed_domains.join(", ");
    $("model").textContent = d.model;
    $("max-amount").textContent = `${d.max_amount} page units`;
    $("max-amount-note").textContent = d.max_amount === 0
      ? "Every amount is blocked (set ‑‑max‑amount)"
      : "In the page's own unit, e.g. ETH";
    $("auto-approve").textContent = d.auto_approve_usd === 0 ? "Off" : usd(d.auto_approve_usd);
    $("auto-approve-note").textContent = d.auto_approve_usd === 0 ? "Every paid action asks you"
      : d.unit_usd ? `Token amounts priced at $${d.unit_usd} per unit`
      : "Only for amounts shown in dollars; no token price set";
    state.maxActions = d.max_actions;
    state.maxApprovals = d.max_approvals;
    showStep();
    showApprovals();
    showTreasury(d.treasury);
    setPhase("running");
    setActivity("Starting", "hourglass");
  },
  goal_started(d) {
    $("goal").textContent = d.goal;
    $("goal-label").textContent = d.chosen_by === "agent" ? `Goal ${d.number} · chosen by the agent` : "Session goal";
  },
  planning() { if (state.phase === "running") setActivity("Choosing its next task", "brain"); },
  observation() { if (state.phase === "running") setActivity("Reading page", "eye"); },
  llm_response() { if (state.phase === "running") setActivity("Deciding", "brain"); },
  action_proposed(d) {
    state.step = d.step;
    showStep();
    if (state.phase === "running") setActivity(describeAction(d.action), "cursor-click");
  },
  approval_requested(d, e) {
    setActivity("Waiting for your approval", "hand-coins", "warn");
    if (d.timeout_s) showMoneyRequest(d, e);
  },
  approval_decided(d) {
    closeCard(d.id);
    if (state.phase === "running" && d.decision !== "AUTO-APPROVED") {
      const by = d.by === "timeout" ? "no answer in time" : d.by === "dashboard" ? "by you" : d.by;
      setActivity(`${d.decision === "APPROVED" ? "Approved" : "Blocked"} (${by})`, d.decision === "APPROVED"
        ? "check-circle" : "x-circle", d.decision === "APPROVED" ? "safe" : "limit");
    }
  },
  confirm_requested(d, e) {
    setActivity("Waiting for your answer", "warning", "warn");
    showConfirm(d, e);
  },
  confirm_decided(d) { closeCard(d.id); },
  wallet_popup_open() {
    setActivity("Waiting for you in MetaMask", "hand-coins", "warn");
    $("step").textContent = "Use the MetaMask window of the agent's browser (Chrome for Testing), now in front";
    showWalletWait("Finish in the MetaMask window of the agent's browser (Chrome for Testing). The agent continues " +
      "when that window closes, or when you press the button.");
  },
  awaiting_wallet_confirmation(d) {
    setActivity("Waiting for the payment in MetaMask", "hand-coins", "warn");
    showWalletWait(`Confirm or reject the payment in MetaMask. The agent is watching the blockchain for up to ` +
      `${Math.round(d.timeout_s / 60)} minutes. If you rejected it, press the button.`);
  },
  wallet_continue_requested() { hideWalletWait(); },
  payment_check() { hideWalletWait(); },
  wallet_popup_closed(d) {
    hideWalletWait();
    showStep();
    setActivity(d.timed_out ? "Gave up waiting for MetaMask" : "MetaMask closed, continuing",
      d.timed_out ? "warning" : "check-circle", d.timed_out ? "warn" : "info");
  },
  payment_check_started() { setActivity("Checking the wallet on-chain", "hourglass"); },
  guardrail_blocked(d) { if (state.phase === "running") setActivity(`Blocked by ${d.rule.replace(/_/g, " ")}`, "shield-warning", "limit"); },
  evaluate(d) { state.claim = d.reason; },
  audit(d) { state.audit = d; },
  audit_warning(d) { state.warning = d.message; },
  treasury(d) {
    state.approvalsUsed = d.approvals_used;
    showApprovals();
    showTreasury(d);
  },
  stop_requested() {
    setPhase("stopping");
    setActivity("Stopping before the next action", "warning", "warn");
    $("stop-note").textContent = "Stop requested. The agent stops before its next action.";
  },
  session_ended(d, e) {
    closeCard();
    hideWalletWait();
    const stopped = !d.reason.startsWith("model finished");
    setPhase(stopped ? "stopped" : "finished");
    setActivity(stopped ? "Stopped" : "Finished", stopped ? "x-circle" : "check-circle", stopped ? "limit" : "safe");
    $("stop-note").textContent = "The run is over.";
    showResult(d, e.ts);
  },
};

function connect() {
  listenToEvents((event) => handlers[event.type]?.(event.data, event), () => {
    if (["connecting", "running", "stopping"].includes(state.phase)) {
      setPhase("disconnected");
      setActivity("Lost contact with the agent", "warning", "warn");
      $("stop-note").textContent = "The agent may have exited. Check the terminal.";
    }
  });
}

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  try {
    const response = await fetch("/api/stop", { method: "POST", headers: { "X-Dashboard-Token": token } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    $("stop-note").textContent = `Stop failed (${error.message}). Create a file named STOP in the project folder instead.`;
    $("stop").disabled = state.phase !== "running";
  }
});

connect();
