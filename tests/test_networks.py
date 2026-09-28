import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from trading_agent.agent import BrowserAgent
from trading_agent.approvals import TerminalApprover
from trading_agent.chain import NETWORKS, ChainError
from trading_agent.cli import build_config, parse_args
from trading_agent.config import AgentConfig, GuardrailConfig
from trading_agent.events import EventLog

WALLET = "0xE02F8C77d183F60c076024c3A63043BCFAfBc527"
BASE = ["--goal", "x", "--wallet-address", WALLET]


def rejected(argv) -> bool:
    with contextlib.redirect_stderr(io.StringIO()), self_exit() as caught:
        parse_args(argv)
    return caught.exited


class self_exit:
    exited = False

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        self.exited = kind is SystemExit
        return self.exited


class NetworkPresetTests(unittest.TestCase):
    def test_test_network_needs_no_flag(self):
        args = parse_args(BASE + ["--network", "sepolia"])
        cfg = build_config(args)
        self.assertEqual((cfg.rpc_url, cfg.expected_chain_id), (NETWORKS["sepolia"].rpc_url, 11155111))

    def test_real_money_networks_are_locked_without_the_flag(self):
        for name in ("base", "arbitrum", "polygon"):
            with self.subTest(name=name):
                self.assertTrue(rejected(BASE + ["--network", name]))
                self.assertFalse(rejected(BASE + ["--network", name, "--allow-real-money"]))

    def test_flag_without_network_and_conflicting_rpc_are_rejected(self):
        self.assertTrue(rejected(BASE + ["--allow-real-money"]))
        self.assertTrue(rejected(BASE + ["--network", "sepolia", "--rpc-url", "https://other.example"]))


class FakeChain:
    def __init__(self, chain_id=None, error=False):
        self.id, self.error = chain_id, error

    async def chain_id(self):
        if self.error:
            raise ChainError("offline")
        return self.id

    async def close(self):
        pass


class ChainCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = AgentConfig(start_url="http://x", goal="x", profile_dir=d, extension_dirs=(),
                               guardrails=GuardrailConfig(allowed_domains=("x",), kill_file=d / "STOP"),
                               log_dir=d / "logs", treasury_file=d / "t.json", rpc_url="http://rpc", wallet_address=WALLET,
                               expected_chain_id=8453, network_name="Base")
        self.events = EventLog(self.cfg.log_dir)
        self.agent = BrowserAgent(self.cfg, TerminalApprover(), self.events)

    async def asyncTearDown(self):
        self.events.close()
        self.tmp.cleanup()

    async def test_right_chain_runs(self):
        self.agent.chain = FakeChain(8453)
        self.assertIsNone(await self.agent.check_chain())

    async def test_wrong_chain_refuses(self):
        self.agent.chain = FakeChain(1)
        self.assertIn("wrong blockchain", await self.agent.check_chain())

    async def test_unreachable_endpoint_refuses_on_a_preset_network(self):
        self.agent.chain = FakeChain(error=True)
        self.assertIn("unreachable", await self.agent.check_chain())


if __name__ == "__main__":
    unittest.main()
