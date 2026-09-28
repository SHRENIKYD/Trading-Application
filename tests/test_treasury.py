import tempfile
import unittest
from pathlib import Path

from trading_agent.config import GuardrailConfig
from trading_agent.guardrails import APPROVE, AUTO_APPROVE, BLOCK, Guardrails, spend_usd
from trading_agent.treasury import Treasury

PAY = {"action": "CLICK", "selector": "Pay"}


class TreasuryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "treasury.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_balance_floor_and_persistence(self):
        t = Treasury(self.path, 5, 4)
        self.assertEqual((t.balance, t.headroom), (5, 1))
        t.record_spend(0.4, "a")
        t.record_income(0.1, "b")
        again = Treasury(self.path, 999, 0)  # existing file wins over new arguments
        self.assertEqual((again.balance, again.headroom), (4.7, 0.7))
        self.assertTrue(again.can_spend(0.7))
        self.assertFalse(again.can_spend(0.71))

    def test_floor_above_start_rejected(self):
        with self.assertRaises(ValueError):
            Treasury(self.path, 5, 6)

    def rails(self, **kw):
        cfg = {"allowed_domains": ("shop.test",), "max_amount": 1.0, "max_approvals": 5,
               "kill_file": Path(self.tmp.name) / "STOP", "auto_approve_usd": 0.5, "unit_usd": 2500.0}
        cfg.update(kw)
        return Guardrails(GuardrailConfig(**cfg), Treasury(self.path, 5, 4))

    def test_small_known_spend_is_auto_approved(self):
        verdict = self.rails().evaluate(PAY, "Pay", [("amount", "0.0001")])  # 0.0001 ETH x $2500 = $0.25
        self.assertEqual(verdict.decision, AUTO_APPROVE)
        self.assertAlmostEqual(verdict.usd, 0.25)

    def test_larger_spend_needs_human(self):
        verdict = self.rails().evaluate(PAY, "Pay", [("amount", "0.0003")])  # $0.75
        self.assertEqual(verdict.decision, APPROVE)

    def test_spend_below_floor_is_blocked(self):
        verdict = self.rails().evaluate(PAY, "Pay", [("amount", "0.0005")])  # $1.25 > $1 headroom
        self.assertEqual((verdict.decision, verdict.rule), (BLOCK, "survival_floor"))

    def test_unknown_amount_never_auto_approved(self):
        self.assertEqual(self.rails().evaluate({"action": "CLICK", "selector": "Connect wallet"}, "Connect wallet").decision,
                         APPROVE)
        self.assertEqual(self.rails(unit_usd=None).evaluate(PAY, "Pay", [("amount", "0.0001")]).decision, APPROVE)

    def test_execution_charges_treasury_until_floor(self):
        guard = self.rails()
        for _ in range(4):
            verdict = guard.evaluate(PAY, "Pay", [("amount", "0.0001")])
            self.assertEqual(verdict.decision, AUTO_APPROVE)
            guard.record_execution(verdict, "test")
        self.assertAlmostEqual(guard.treasury.balance, 4.0)
        self.assertEqual(guard.evaluate(PAY, "Pay", [("amount", "0.0001")]).rule, "survival_floor")

    def test_dollar_amounts_need_no_rate(self):
        self.assertEqual(spend_usd(PAY, "Pay $0.30", [], None), 0.30)
        self.assertEqual(spend_usd(PAY, "Pay 0.3 USDC", [], None), 0.3)
        self.assertIsNone(spend_usd(PAY, "Pay 0.3 ETH", [], None))


if __name__ == "__main__":
    unittest.main()
