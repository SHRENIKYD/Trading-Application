import unittest

from trading_agent.actions import parse_action
from trading_agent.errors import ActionFormatError


class ParseActionTests(unittest.TestCase):
    def test_accepts_fenced_json_and_normalizes_case(self):
        self.assertEqual(parse_action('```json\n{"action":"click","selector":"a"}\n```'),
                         {"action": "CLICK", "selector": "a"})

    def test_coerces_numeric_wait(self):
        self.assertEqual(parse_action('{"action":"WAIT","seconds":"5"}'), {"action": "WAIT", "seconds": 5})

    def test_rejects_invalid_outputs(self):
        cases = {
            "hello": "No JSON",
            '{"action":"FLY"}': "must be one of",
            '{"action":"GOTO","url":"chrome-extension://abc/home.html"}': "http",
            '{"action":"WAIT","seconds":999}': "between",
            '{"action":"TYPE","selector":"#q"}': "text",
            '{"action": "CLICK", "selector": }': "Invalid JSON",
        }
        for raw, fragment in cases.items():
            with self.subTest(raw=raw), self.assertRaises(ActionFormatError) as ctx:
                parse_action(raw)
            self.assertIn(fragment, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
