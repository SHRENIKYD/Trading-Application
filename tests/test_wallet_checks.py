import tempfile
import unittest
from pathlib import Path

from trading_agent.agent import BrowserAgent
from trading_agent.approvals import TerminalApprover
from trading_agent.browser import WalletPopups
from trading_agent.chain import WalletChain
from trading_agent.config import AgentConfig, GuardrailConfig
from trading_agent.events import EventLog
from trading_agent.guardrails import Verdict

ADDRESS = "0xE02F8C77d183F60c076024c3A63043BCFAfBc527"
ETH = 10 ** 18


class FakeChain(WalletChain):
    """Scripted wallet. Before `go()` nothing has happened; after it, a transaction was sent
    (if `sends`) and mined (if `mined`), dropping the balance by `spent_wei`."""

    def __init__(self, sends: bool, spent_wei: int = 0, mined: bool = True):
        super().__init__("http://rpc.invalid", ADDRESS)
        self.sends, self.spent_wei, self.mined = sends, spent_wei, mined
        self.balance, self.happened = 50 * ETH // 1000, False

    def go(self):
        self.happened = True

    async def nonce(self, tag="latest"):
        if not (self.happened and self.sends):
            return 0
        return 1 if (tag == "pending" or self.mined) else 0

    async def balance_wei(self, tag="latest"):
        return self.balance - (self.spent_wei if self.happened and self.sends and self.mined else 0)

    async def check_spend(self, before, detect_s=None, confirm_s=None, poll_s=None, stop_waiting=lambda: False):
        # Same logic, test-sized timings regardless of what the caller asked for.
        return await super().check_spend(before, 0.05, 0.05, 0.001, stop_waiting)


class ChainCheckTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, chain):
        before = await chain.snapshot()
        chain.go()
        return await chain.check_spend(before)

    async def test_nothing_sent_before_timeout_is_unconfirmed(self):
        check = await self.check(FakeChain(sends=False))
        self.assertEqual((check.sent, check.confirmed, check.spent_wei), (False, False, 0))

    async def test_you_said_done_and_nothing_was_sent(self):
        chain = FakeChain(sends=False)
        before = await chain.snapshot()
        check = await chain.check_spend(before, stop_waiting=lambda: True)
        self.assertEqual((check.sent, check.confirmed), (False, True))

    async def test_sent_and_mined_reports_balance_drop_with_fees(self):
        check = await self.check(FakeChain(sends=True, spent_wei=105 * ETH // 1_000_000))  # 0.000105 ETH incl. fee
        self.assertTrue(check.sent and check.confirmed)
        self.assertAlmostEqual(check.spent_eth, 0.000105)

    async def test_sent_but_not_mined(self):
        check = await self.check(FakeChain(sends=True, spent_wei=ETH // 10_000, mined=False))
        self.assertEqual((check.sent, check.confirmed), (True, False))

    def test_rejects_bad_address(self):
        with self.assertRaises(ValueError):
            WalletChain("http://rpc.invalid", "0x123")


class SettleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        cfg = AgentConfig(start_url="http://127.0.0.1", goal="x", profile_dir=d, extension_dirs=(),
                          guardrails=GuardrailConfig(allowed_domains=("127.0.0.1",), unit_usd=2500.0,
                                                     kill_file=d / "STOP"),
                          log_dir=d / "logs", treasury_file=d / "t.json")
        self.events = EventLog(cfg.log_dir)
        self.agent = BrowserAgent(cfg, TerminalApprover(), self.events)
        self.verdict = Verdict("auto_approve", usd=0.25)

    async def asyncTearDown(self):
        self.events.close()
        self.tmp.cleanup()

    async def settle(self, chain):
        self.agent.chain = chain
        before = None
        if chain:
            before = await chain.snapshot()
            chain.go()
        return await self.agent.settle(self.verdict, before)

    async def test_confirmed_payment_is_charged_at_real_cost(self):
        outcome, usd, note = await self.settle(FakeChain(sends=True, spent_wei=105 * ETH // 1_000_000))
        self.assertEqual(outcome, "PAID")
        self.assertAlmostEqual(usd, 0.2625)  # 0.000105 ETH x $2500, fee included
        self.assertIn("confirmed on-chain", note)

    async def test_expected_payment_that_never_shows_charges_estimate(self):
        outcome, usd, _ = await self.settle(FakeChain(sends=False))
        self.assertEqual((outcome, usd), ("UNCHECKED", 0.25))  # never silently undercount

    async def test_expected_payment_you_rejected_charges_nothing(self):
        self.agent.request_wallet_continue()
        outcome, usd, note = await self.settle(FakeChain(sends=False))
        self.assertEqual((outcome, usd), ("NO_PAYMENT", 0.0))
        self.assertIn("NO payment", note)
        self.assertFalse(self.agent.wallet_continue)  # the button press is used up

    async def test_non_payment_action_with_no_transaction_is_no_payment(self):
        self.verdict = Verdict("approve", usd=None)  # e.g. Connect wallet
        outcome, usd, _ = await self.settle(FakeChain(sends=False))
        self.assertEqual((outcome, usd), ("NO_PAYMENT", 0.0))

    async def test_without_chain_the_estimate_is_unchecked(self):
        outcome, usd, _ = await self.settle(None)
        self.assertEqual((outcome, usd), ("UNCHECKED", 0.25))


class FakePage:
    def __init__(self, url):
        self.url, self.closed, self.fronted = url, False, 0

    def is_closed(self):
        return self.closed

    async def bring_to_front(self):
        self.fronted += 1


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class WalletPopupTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_until_popup_closes(self):
        agent_page = FakePage("http://127.0.0.1/sepolia.html")
        context = FakeContext([agent_page])
        popups = WalletPopups(context)
        popups.mark()
        popup = FakePage("chrome-extension://abc/notification.html")
        context.pages.append(popup)
        seen = []

        async def close_soon():
            import asyncio
            await asyncio.sleep(0.6)
            popup.closed = True

        import asyncio
        closer = asyncio.create_task(close_soon())
        found = await popups.wait_for_user(0.5, 10, lambda: False, lambda: seen.append("open"),
                                           lambda timed_out: seen.append(("closed", timed_out)))
        await closer
        self.assertTrue(found)
        self.assertEqual(seen[:2], ["open", ("closed", False)])
        self.assertEqual(popup.fronted, 1)          # the MetaMask window was raised for you
        self.assertEqual(agent_page.fronted, 0)     # the agent's own page was left alone

    async def test_no_popup_returns_quickly(self):
        popups = WalletPopups(FakeContext([FakePage("http://127.0.0.1/")]))
        popups.mark()
        self.assertFalse(await popups.wait_for_user(0.05, 10, lambda: False, lambda: None, lambda timed_out: None))

    async def test_wallet_windows_open_at_startup_are_ignored(self):
        startup = [FakePage("chrome-extension://abc/home.html"), FakePage("chrome-extension://abc/notification.html")]
        popups = WalletPopups(FakeContext(startup))
        popups.mark()
        self.assertEqual(popups.open_popups(), [])

    async def test_continue_button_ends_the_wait(self):
        context = FakeContext([])
        popups = WalletPopups(context)
        popups.mark()
        context.pages.append(FakePage("chrome-extension://abc/notification.html"))  # never closed
        pressed = {"done": False}

        async def press_later():
            import asyncio
            await asyncio.sleep(0.3)
            pressed["done"] = True

        import asyncio
        presser = asyncio.create_task(press_later())
        self.assertTrue(await popups.wait_for_user(0.1, 30, lambda: pressed["done"], lambda: None, lambda timed_out: None))
        await presser


if __name__ == "__main__":
    unittest.main()
