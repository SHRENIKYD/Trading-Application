"""Read-only view of the agent's own wallet on the blockchain, over a public JSON-RPC endpoint.

Used to charge the treasury only for money that actually left the wallet (including network fees).
Nothing here signs or sends anything: the only calls are eth_getBalance and eth_getTransactionCount.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import httpx

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
WEI_PER_ETH = 10 ** 18


@dataclass(frozen=True)
class Network:
    name: str
    chain_id: int
    rpc_url: str      # public, read-only endpoint (verified to answer with chain_id)
    coin: str
    real_money: bool  # False only for test networks, whose coins have no value


NETWORKS = {
    "sepolia": Network("Sepolia (Ethereum test network)", 11155111, "https://ethereum-sepolia-rpc.publicnode.com",
                       "SepoliaETH", False),
    "base": Network("Base", 8453, "https://base-rpc.publicnode.com", "ETH", True),
    "arbitrum": Network("Arbitrum One", 42161, "https://arbitrum-one-rpc.publicnode.com", "ETH", True),
    "polygon": Network("Polygon PoS", 137, "https://polygon-bor-rpc.publicnode.com", "POL", True),
}


class ChainError(RuntimeError):
    """The RPC endpoint could not be reached or answered with an error."""


@dataclass(frozen=True)
class WalletSnapshot:
    balance_wei: int     # confirmed balance ("latest")
    nonce_pending: int   # transactions sent, including ones not yet mined


@dataclass(frozen=True)
class SpendCheck:
    sent: bool                # a transaction left the wallet
    confirmed: bool           # it was mined and the balance change is final
    spent_wei: int            # balance drop, fees included (0 when nothing was sent)
    note: str

    @property
    def spent_eth(self) -> float:
        return self.spent_wei / WEI_PER_ETH


class WalletChain:
    def __init__(self, rpc_url: str, address: str, timeout_s: float = 15.0) -> None:
        if not ADDRESS_RE.match(address):
            raise ValueError(f"not a 0x wallet address: {address!r}")
        self.rpc_url = rpc_url
        self.address = address
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._id = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, params: list) -> str:
        self._id += 1
        try:
            response = await self._client.post(self.rpc_url, json={"jsonrpc": "2.0", "id": self._id,
                                                                   "method": method, "params": params})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainError(f"{method} failed: {exc}") from exc
        if "error" in body:
            raise ChainError(f"{method} failed: {body['error']}")
        return body["result"]

    async def balance_wei(self, tag: str = "latest") -> int:
        return int(await self._call("eth_getBalance", [self.address, tag]), 16)

    async def nonce(self, tag: str = "latest") -> int:
        return int(await self._call("eth_getTransactionCount", [self.address, tag]), 16)

    async def chain_id(self) -> int:
        return int(await self._call("eth_chainId", []), 16)

    async def snapshot(self) -> WalletSnapshot:
        return WalletSnapshot(await self.balance_wei("latest"), await self.nonce("pending"))

    async def check_spend(self, before: WalletSnapshot, detect_s: float = 20.0, confirm_s: float = 180.0,
                          poll_s: float = 3.0, stop_waiting=lambda: False) -> SpendCheck:
        """Did a transaction leave the wallet since `before`, and how much did the balance drop once mined?
        Looks for a new transaction for up to detect_s, or until stop_waiting() is true (checked once more first)."""
        deadline = time.monotonic() + detect_s
        while await self.nonce("pending") <= before.nonce_pending:
            if stop_waiting():
                return SpendCheck(False, True, 0, "no transaction left the wallet (you said you were done)")
            if time.monotonic() >= deadline:
                return SpendCheck(False, False, 0, f"no transaction seen within {detect_s:.0f}s")
            await asyncio.sleep(poll_s)

        deadline = time.monotonic() + confirm_s
        while await self.nonce("latest") <= before.nonce_pending:
            if time.monotonic() >= deadline:
                return SpendCheck(True, False, 0, f"a transaction was sent but not mined within {confirm_s:.0f}s")
            await asyncio.sleep(poll_s)
        spent = max(before.balance_wei - await self.balance_wei("latest"), 0)
        return SpendCheck(True, True, spent, "confirmed on-chain")
