from __future__ import annotations

import argparse
import asyncio
import logging
import os
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from .agent import BrowserAgent
from .approvals import TerminalApprover, UnattendedApprover, WebApprover
from .browser import run_setup
from .chain import ADDRESS_RE, NETWORKS
from .config import AgentConfig, GuardrailConfig
from .dashboard.server import Dashboard
from .errors import AgentFatalError
from .events import EventLog, configure_logging
from .guardrails import host_allowed

log = logging.getLogger("agent")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local autonomous browser agent (Ollama + Playwright).")
    parser.add_argument("--start-url", default="https://example.com", help="First page to open.")
    parser.add_argument("--goal", help="Task for the agent. With --mission it is the first task; otherwise required.")
    parser.add_argument("--mission",
                        help="Self-directed mode: a standing mission. After each task the agent plans its next one.")
    parser.add_argument("--max-goals", type=int, default=10, help="Most tasks one self-directed run may plan and do.")
    parser.add_argument("--unattended", action="store_true",
                        help="No dashboard decisions: health checks continue automatically, wallet-connect requests "
                             "are allowed, every other approval is refused. MetaMask confirmations stay yours.")
    parser.add_argument("--max-total-actions", type=int, default=200,
                        help="Hard stop for unattended runs, across all tasks.")
    parser.add_argument("--profile-dir", default="./agent_profile", help="Persistent Chromium profile directory.")
    parser.add_argument("--extension", action="append", default=[],
                        help="Unpacked extension directory (repeatable). Use the same path on every run.")
    parser.add_argument("--model", default=os.environ.get("AGENT_MODEL", "llama3.1:8b"))
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    parser.add_argument("--max-actions", type=int, default=30, help="Actions before a human health check.")
    parser.add_argument("--allow-domain", action="append", default=[],
                        help="Site the agent may visit (repeatable; subdomains included). Default: the start URL's site.")
    parser.add_argument("--max-amount", type=float, default=0.0,
                        help="Largest amount the agent may type or submit, in the page's unit. Default 0 blocks all amounts.")
    parser.add_argument("--max-approvals", type=int, default=3,
                        help="Human approvals allowed per session before guarded actions are blocked outright.")
    parser.add_argument("--kill-file", default="STOP", help="Create this file to stop the agent before its next action.")
    parser.add_argument("--unit-usd", type=float, default=None,
                        help="Dollar value of one unit of the page's amounts (e.g. ETH price). Needed to budget token amounts.")
    parser.add_argument("--auto-approve-usd", type=float, default=0.50,
                        help="Money actions up to this many dollars run without asking. 0 disables auto-approval.")
    parser.add_argument("--treasury-file", default="data/treasury.json", help="The agent's persistent budget.")
    parser.add_argument("--network", choices=sorted(NETWORKS),
                        help="Wallet network preset: sets --rpc-url and the chain the run must be on. "
                             "Real-money networks also need --allow-real-money.")
    parser.add_argument("--allow-real-money", action="store_true",
                        help="Required to run on a network whose coins have real value (base, arbitrum, polygon).")
    parser.add_argument("--rpc-url", default=os.environ.get("AGENT_RPC_URL"),
                        help="Public JSON-RPC endpoint for the wallet's network, used read-only to confirm payments.")
    parser.add_argument("--wallet-address", default=os.environ.get("AGENT_WALLET_ADDRESS"),
                        help="The agent wallet's 0x address, used read-only with --rpc-url.")
    parser.add_argument("--wallet-wait", type=float, default=300,
                        help="Seconds the agent pauses for you inside a wallet popup.")
    parser.add_argument("--start-usd", type=float, default=5.0, help="Starting capital when the treasury is first created.")
    parser.add_argument("--floor-usd", type=float, default=4.0, help="Balance the agent must never go below (first creation).")
    parser.add_argument("--log-dir", default="logs", help="Directory for agent.log and per-session JSONL files.")
    parser.add_argument("--dashboard", action="store_true",
                        help="Serve the control dashboard on 127.0.0.1 and open it; it stays up after the run until Ctrl+C.")
    parser.add_argument("--dashboard-port", type=int, default=8787)
    parser.add_argument("--approval-timeout", type=float, default=300,
                        help="Seconds to wait for a dashboard decision before it counts as Block.")
    parser.add_argument("--setup", action="store_true", help="Open the profile for manual wallet setup and exit.")
    args = parser.parse_args(argv)
    if not args.setup and not args.goal and not args.mission:
        parser.error("--goal or --mission is required unless --setup is used.")
    if args.max_goals < 1 or args.max_total_actions < 1:
        parser.error("--max-goals and --max-total-actions must be >= 1.")
    if args.max_actions < 1:
        parser.error("--max-actions must be >= 1.")
    if min(args.max_amount, args.max_approvals, args.auto_approve_usd, args.start_usd, args.floor_usd) < 0:
        parser.error("amounts and limits must be >= 0.")
    if args.approval_timeout <= 0 or args.wallet_wait <= 0:
        parser.error("--approval-timeout and --wallet-wait must be > 0.")
    if args.network:
        network = NETWORKS[args.network]
        if network.real_money and not args.allow_real_money:
            parser.error(f"{network.name} uses real money ({network.coin}); add --allow-real-money to confirm.")
        if args.rpc_url and args.rpc_url != network.rpc_url:
            parser.error("use either --network or --rpc-url, not both.")
        args.rpc_url = network.rpc_url
    elif args.allow_real_money:
        parser.error("--allow-real-money only applies together with --network.")
    if bool(args.rpc_url) != bool(args.wallet_address):
        parser.error("--rpc-url and --wallet-address must be given together.")
    if args.wallet_address and not ADDRESS_RE.match(args.wallet_address):
        parser.error("--wallet-address must be a 0x address with 40 hex characters.")
    if args.unit_usd is not None and args.unit_usd <= 0:
        parser.error("--unit-usd must be > 0.")
    return args


def build_config(args: argparse.Namespace) -> AgentConfig:
    extension_dirs = tuple(Path(path).expanduser().resolve() for path in args.extension)
    for path in extension_dirs:
        if not (path / "manifest.json").is_file():
            raise AgentFatalError(f"Extension directory has no manifest.json: {path}")
    start_host = (urlparse(args.start_url).hostname or "").lower()
    domains = tuple(d.lower().strip().removeprefix("www.") for d in args.allow_domain) or (start_host.removeprefix("www."),)
    if not host_allowed(args.start_url, domains):
        raise AgentFatalError(f"--start-url host {start_host} is not in --allow-domain {list(domains)}")
    return AgentConfig(
        start_url=args.start_url,
        goal=args.goal or "",
        profile_dir=Path(args.profile_dir).expanduser().resolve(),
        extension_dirs=extension_dirs,
        guardrails=GuardrailConfig(
            allowed_domains=domains,
            max_amount=args.max_amount,
            max_approvals=args.max_approvals,
            kill_file=Path(args.kill_file),
            auto_approve_usd=args.auto_approve_usd,
            unit_usd=args.unit_usd,
        ),
        log_dir=Path(args.log_dir),
        treasury_file=Path(args.treasury_file),
        rpc_url=args.rpc_url,
        wallet_address=args.wallet_address,
        expected_chain_id=NETWORKS[args.network].chain_id if args.network else None,
        network_name=NETWORKS[args.network].name if args.network else None,
        wallet_wait_s=args.wallet_wait,
        start_usd=args.start_usd,
        floor_usd=args.floor_usd,
        model=args.model,
        ollama_host=args.ollama_host,
        max_actions=args.max_actions,
        mission=args.mission,
        max_goals=args.max_goals,
        unattended=args.unattended,
        max_total_actions=args.max_total_actions,
    )


async def run_agent(cfg: AgentConfig, events: EventLog, dashboard_port: int | None, approval_timeout_s: float) -> None:
    """With the dashboard, every approval and confirmation is answered there; otherwise in this terminal."""
    if cfg.unattended:
        approver = UnattendedApprover(events, approval_timeout_s)
    else:
        approver = WebApprover(events, approval_timeout_s) if dashboard_port else TerminalApprover()
    agent = BrowserAgent(cfg, approver, events)
    dashboard = None
    if dashboard_port:
        dashboard = Dashboard(events, agent.guardrails, approver, dashboard_port,
                              wallet_continue=agent.request_wallet_continue)
        await dashboard.start()
        webbrowser.open(dashboard.url)
    try:
        try:
            await agent.run()
        except Exception:
            if not dashboard:
                raise
            log.exception("The agent crashed; the dashboard stays open so you can see what happened.")
        if dashboard:
            log.info("Run finished. Dashboard still open at %s - press Ctrl+C to exit.", dashboard.url)
            await asyncio.Event().wait()
    finally:
        if dashboard:
            await dashboard.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(Path(args.log_dir))
    events = None
    try:
        cfg = build_config(args)
        if cfg.guardrails.kill_file.exists():
            raise AgentFatalError(f"Kill switch file '{cfg.guardrails.kill_file}' exists; delete it to run.")
        if args.setup:
            asyncio.run(run_setup(cfg))
            return 0
        events = EventLog(cfg.log_dir)
        log.info("Session log: %s", events.path)
        asyncio.run(run_agent(cfg, events, args.dashboard_port if args.dashboard else None, args.approval_timeout))
    except AgentFatalError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        return 130
    finally:
        if events:
            events.close()
    return 0
