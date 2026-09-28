"use strict";

// Shared by the dashboard and the session log page.
const token = document.querySelector('meta[name="dashboard-token"]')?.content || "";
const $ = (id) => document.getElementById(id);
const ICONS = "/static/icons.svg#i-";
const usd = (n) => (n === null || n === undefined ? "—" : `$${Number(n).toFixed(2)}`);

function parseAction(json) {
  try { return typeof json === "string" ? JSON.parse(json) : json; } catch { return null; }
}

function describeAction(json) {
  const a = parseAction(json);
  if (!a) return "Acting";
  return { GOTO: `Opening ${a.url}`, CLICK: `Clicking “${a.selector}”`, TYPE: `Typing “${a.text}” into ${a.selector}`,
           WAIT: `Waiting ${a.seconds}s`, EVALUATE: "Wrapping up" }[a.action] || "Acting";
}

function icon(name, extraClass = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `icon ${extraClass}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", ICONS + name);
  svg.append(use);
  return svg;
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// One line describing what really happened to money actions (from the agent's audit event).
function auditSummary(a) {
  const parts = [];
  if (a.onchain) {
    parts.push(`${plural(a.paid, "payment")} confirmed${a.paid ? ` (${usd(a.paid_usd)} incl. fees)` : ""}`);
    if (a.no_payment) parts.push(`${plural(a.no_payment, "approved action")} with no payment`);
  }
  if (a.unchecked) parts.push(`${plural(a.unchecked, "approved action")} not verified on-chain`);
  parts.push(`${a.blocked} blocked`);
  return parts.join(", ");
}

function listenToEvents(onEvent, onLost) {
  const source = new EventSource("/events");
  source.onmessage = (message) => onEvent(JSON.parse(message.data));
  source.onerror = () => { source.close(); onLost?.(); };
}
