from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ALLOWED_ACTIONS = frozenset({"GOTO", "CLICK", "TYPE", "WAIT", "EVALUATE"})
MAX_WAIT_SECONDS = 30
MAX_INPUT_FIELDS = 25
MAX_CLICKABLES = 40
MAX_ELEMENT_DESCRIPTION = 500


@dataclass(frozen=True)
class GuardrailConfig:
    # Hosts the agent may visit; subdomains of an entry are allowed too.
    allowed_domains: tuple[str, ...]
    # Largest amount that may be typed or submitted, in whatever unit the page uses. 0 blocks every amount.
    max_amount: float = 0.0
    # Human approvals granted per session before every further guarded action is blocked outright.
    max_approvals: int = 3
    # Creating this file stops the agent before its next action.
    kill_file: Path = Path("STOP")
    # Money actions worth at most this many dollars are approved without asking (needs a treasury).
    auto_approve_usd: float = 0.50
    # Dollar value of one unit of the amounts pages show (e.g. the ETH price). None: token amounts cannot be converted.
    unit_usd: float | None = None


@dataclass(frozen=True)
class AgentConfig:
    start_url: str
    goal: str
    profile_dir: Path
    extension_dirs: tuple[Path, ...]
    guardrails: GuardrailConfig
    log_dir: Path = Path("logs")
    treasury_file: Path = Path("data/treasury.json")
    start_usd: float = 5.0
    floor_usd: float = 4.0
    # Self-directed mode: after each goal the agent plans its next one from this standing mission.
    mission: str | None = None
    max_goals: int = 10
    # Unattended: no dashboard decisions. Health checks continue automatically (up to max_total_actions),
    # wallet-connect requests are allowed, every other approval is refused. MetaMask confirmation stays yours.
    unattended: bool = False
    max_total_actions: int = 200
    # Read-only on-chain check of the agent's wallet; both unset means payments are charged by page estimate.
    rpc_url: str | None = None
    wallet_address: str | None = None
    # The chain the RPC endpoint must report; the run refuses to start on any other chain.
    expected_chain_id: int | None = None
    network_name: str | None = None
    # How long the agent pauses for you inside a wallet popup before giving up on that step.
    wallet_wait_s: float = 300.0
    model: str = "llama3.1:8b"
    ollama_host: str = "http://localhost:11434"
    max_actions: int = 30
    max_format_retries: int = 3
    history_turns: int = 6
    max_page_chars: int = 6000
    num_ctx: int = 8192
    llm_timeout_s: float = 180.0
    navigation_timeout_ms: int = 30000
    action_timeout_ms: int = 10000
    network_idle_timeout_ms: int = 15000
