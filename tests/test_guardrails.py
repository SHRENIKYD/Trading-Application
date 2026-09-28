import tempfile
import unittest
from pathlib import Path

from trading_agent.config import GuardrailConfig
from trading_agent.guardrails import ALLOW, APPROVE, BLOCK, Guardrails, find_amounts, host_allowed, verify_financial_safety


def rails(**overrides) -> Guardrails:
    settings = {"allowed_domains": ("example.com",), "max_amount": 0.01, "max_approvals": 2,
                "kill_file": Path(tempfile.gettempdir()) / "agent-test-no-such-stop-file"}
    settings.update(overrides)
    return Guardrails(GuardrailConfig(**settings))


class FinancialTermsTests(unittest.TestCase):
    def test_detects_terms_in_element_text(self):
        self.assertEqual(verify_financial_safety({"action": "CLICK", "selector": "#btn-3"}, "Send 5 ETH"), ["send"])

    def test_ignores_harmless_actions(self):
        self.assertEqual(verify_financial_safety({"action": "CLICK", "selector": "Next page"}, "Next page"), [])
        self.assertEqual(verify_financial_safety({"action": "WAIT", "seconds": 2}), [])


class DomainTests(unittest.TestCase):
    def test_allows_domain_and_subdomains_only(self):
        self.assertTrue(host_allowed("https://example.com/a", ("example.com",)))
        self.assertTrue(host_allowed("https://www.example.com/a", ("example.com",)))
        self.assertFalse(host_allowed("https://evil-example.com", ("example.com",)))
        self.assertFalse(host_allowed("https://example.com.evil.io", ("example.com",)))

    def test_blocks_goto_outside_allowlist(self):
        verdict = rails().evaluate({"action": "GOTO", "url": "https://iana.org"})
        self.assertEqual((verdict.decision, verdict.rule), (BLOCK, "domain_allowlist"))


class AmountTests(unittest.TestCase):
    def test_finds_currency_amounts_typed_values_and_form_values(self):
        self.assertEqual(find_amounts({"action": "CLICK", "selector": "x"}, "Send 5 ETH", []), [5.0])
        self.assertEqual(find_amounts({"action": "TYPE", "selector": 'input[name="amount"]', "text": "0.5"}, "", []), [0.5])
        self.assertEqual(find_amounts({"action": "CLICK", "selector": "Pay"}, "Pay", [("amount", "1,200.5"), ("to", "0xabc")]),
                         [1200.5])

    def test_blocks_amount_over_limit_without_asking(self):
        verdict = rails().evaluate({"action": "CLICK", "selector": "Pay"}, "Pay", [("amount", "0.5")])
        self.assertEqual((verdict.decision, verdict.rule), (BLOCK, "amount_limit"))

    def test_amount_within_limit_still_needs_approval(self):
        verdict = rails().evaluate({"action": "CLICK", "selector": "Pay"}, "Pay", [("amount", "0.001")])
        self.assertEqual(verdict.decision, APPROVE)

    def test_zero_limit_blocks_every_amount(self):
        verdict = rails(max_amount=0.0).evaluate({"action": "TYPE", "selector": 'input[name="amount"]', "text": "0.001"})
        self.assertEqual(verdict.decision, BLOCK)


class SpendCapAndKillTests(unittest.TestCase):
    def test_spend_cap_blocks_after_max_approvals(self):
        guard = rails()
        action = {"action": "CLICK", "selector": "Confirm"}
        for _ in range(2):
            verdict = guard.evaluate(action, "Confirm")
            self.assertEqual(verdict.decision, APPROVE)
            guard.record_execution(verdict, "test")
        verdict = guard.evaluate(action, "Confirm")
        self.assertEqual((verdict.decision, verdict.rule), (BLOCK, "spend_cap"))

    def test_plain_action_allowed(self):
        self.assertEqual(rails().evaluate({"action": "CLICK", "selector": "Learn more"}, "Learn more").decision, ALLOW)

    def test_kill_switch_file_and_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "STOP"
            guard = rails(kill_file=stop)
            self.assertIsNone(guard.kill_reason())
            stop.touch()
            self.assertIn("STOP", guard.kill_reason())
        guard = rails()
        guard.kill("stop button")
        self.assertEqual(guard.kill_reason(), "stop button")


if __name__ == "__main__":
    unittest.main()
