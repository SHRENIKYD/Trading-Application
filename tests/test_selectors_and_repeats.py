import unittest

from playwright.async_api import async_playwright

from trading_agent.browser import normalize_selector, resolve_locator
from trading_agent.guardrails import RepeatGuard

PAGE = """<form><input name="amount"><button id="pay" type="submit">Send payment</button>
<button style="display:none">Send payment</button><a href="/x">Learn "more"</a></form>"""


class NormalizeSelectorTests(unittest.TestCase):
    def test_rewrites_jquery_contains(self):
        self.assertEqual(normalize_selector("button:contains('Send payment')"), 'button:has-text("Send payment")')
        self.assertEqual(normalize_selector('a:contains("Learn")'), 'a:has-text("Learn")')
        self.assertEqual(normalize_selector("button:contains(Pay)"), 'button:has-text("Pay")')

    def test_escapes_quotes_and_leaves_valid_css_alone(self):
        self.assertEqual(normalize_selector("""a:contains('Learn "more"')"""), 'a:has-text("Learn \\"more\\"")')
        self.assertEqual(normalize_selector('input[name="amount"]'), 'input[name="amount"]')


class ResolveInBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_contains_selector_finds_the_visible_button(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await page.set_content(PAGE)
            locator = await resolve_locator(page, "button:contains('Send payment')")
            self.assertIsNotNone(locator)
            self.assertEqual(await locator.get_attribute("id"), "pay")
            await browser.close()


class RepeatGuardTests(unittest.TestCase):
    CLICK = {"action": "CLICK", "selector": "button:contains('Send payment')"}

    def test_refuses_third_identical_failure(self):
        guard = RepeatGuard()
        for _ in range(2):
            self.assertIsNone(guard.refusal(self.CLICK))
            guard.record(self.CLICK, failed=True)
        self.assertIn("not tried again", guard.refusal(self.CLICK))

    def test_success_or_a_different_action_resets(self):
        guard = RepeatGuard()
        guard.record(self.CLICK, failed=True)
        guard.record(self.CLICK, failed=True)
        guard.record({"action": "TYPE", "selector": "#a", "text": "1"}, failed=False)
        self.assertIsNone(guard.refusal(self.CLICK))
        guard.record(self.CLICK, failed=True)
        guard.record({"action": "WAIT", "seconds": 1}, failed=True)
        self.assertIsNone(guard.refusal(self.CLICK))


if __name__ == "__main__":
    unittest.main()
