import tempfile
import unittest
from pathlib import Path

from trading_agent.actions import parse_plan
from trading_agent.agent import BrowserAgent
from trading_agent.approvals import ApprovalRequest, UnattendedApprover
from trading_agent.config import AgentConfig, GuardrailConfig
from trading_agent.errors import ActionFormatError
from trading_agent.events import EventLog

ALLOWED = lambda url: "shop.test" in url  # noqa: E731


class ParsePlanTests(unittest.TestCase):
    def test_goal_on_allowed_site(self):
        self.assertEqual(parse_plan('{"goal": "Find a free task.", "start_url": "https://shop.test/tasks"}', ALLOWED),
                         {"goal": "Find a free task.", "start_url": "https://shop.test/tasks"})

    def test_stop(self):
        self.assertEqual(parse_plan('{"stop": "nothing left"}', ALLOWED), {"stop": "nothing left"})

    def test_rejects_unreachable_or_malformed(self):
        for raw in ('{"goal": "x", "start_url": "https://evil.test"}', '{"goal": "", "start_url": "https://shop.test"}',
                    '{"goal": "x", "start_url": "ftp://shop.test"}', "no json"):
            with self.subTest(raw=raw), self.assertRaises(ActionFormatError):
                parse_plan(raw, ALLOWED)


def request(triggers, usd=None):
    return ApprovalRequest("https://shop.test", {"action": "CLICK", "selector": "x"}, "x", triggers, [], 0, 3, usd,
                           {"balance_usd": 5, "floor_usd": 4, "headroom_usd": 1})


class UnattendedApproverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = EventLog(Path(self.tmp.name))
        self.approver = UnattendedApprover(self.events, 300)

    async def asyncTearDown(self):
        self.events.close()
        self.tmp.cleanup()

    async def test_wallet_connect_is_allowed(self):
        self.assertTrue(await self.approver.approve(request(["connect", "wallet"])))

    async def test_anything_else_is_refused(self):
        self.assertFalse(await self.approver.approve(request(["send", "pay"], usd=0.75)))
        self.assertFalse(await self.approver.approve(request(["sign"])))
        self.assertFalse(await self.approver.approve(request(["connect"], usd=0.10)))
        self.assertFalse(await self.approver.confirm("anything?"))


class ScriptedAgent(BrowserAgent):
    """Mission loop with the browser and model replaced by scripts."""

    def __init__(self, cfg, events, plans, results):
        super().__init__(cfg, UnattendedApprover(events, 300), events)
        self.plans, self.results, self.ran = list(plans), list(results), []

        async def ask(messages, parse):
            return parse(self.plans.pop(0)) if self.plans else None
        self.llm.ask = ask

    async def _loop(self, context):
        self.ran.append(self.goal)
        reason, spend = self.results.pop(0)
        if spend:
            self.treasury.record_spend(spend, "test")
        self.last_claim = "done"
        return reason

    async def open_start(self, context, url):
        pass


class MissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.events = EventLog(self.dir / "logs")

    async def asyncTearDown(self):
        self.events.close()
        self.tmp.cleanup()

    def cfg(self, **kw):
        base = dict(start_url="https://shop.test", goal=None, profile_dir=self.dir, extension_dirs=(),
                    guardrails=GuardrailConfig(allowed_domains=("shop.test",), kill_file=self.dir / "STOP"),
                    log_dir=self.dir / "logs", treasury_file=self.dir / "t.json", mission="earn", unattended=True)
        base.update(kw)
        return AgentConfig(**base)

    def plan(self, n):
        return f'{{"goal": "task {n}", "start_url": "https://shop.test/{n}"}}'

    async def test_plans_and_runs_goals_until_goal_limit(self):
        agent = ScriptedAgent(self.cfg(max_goals=3), self.events, [self.plan(n) for n in range(1, 5)],
                              [("model finished (EVALUATE)", 0)] * 3)
        self.assertEqual(await agent._mission(None), "stopped: goal limit (3) reached")
        self.assertEqual(agent.ran, ["task 1", "task 2", "task 3"])

    async def test_stops_at_the_budget_floor(self):
        agent = ScriptedAgent(self.cfg(), self.events, [self.plan(1), self.plan(2)],
                              [("model finished (EVALUATE)", 1.0)])  # spends the whole $1 above the floor
        self.assertEqual(await agent._mission(None), "stopped: reached the budget floor")

    async def test_model_can_end_the_mission(self):
        agent = ScriptedAgent(self.cfg(), self.events, [self.plan(1), '{"stop": "no paid tasks found"}'],
                              [("model finished (EVALUATE)", 0)])
        self.assertEqual(await agent._mission(None), "mission ended: no paid tasks found")

    async def test_a_stop_reason_other_than_finishing_ends_the_run(self):
        agent = ScriptedAgent(self.cfg(), self.events, [self.plan(1), self.plan(2)], [("killed: STOP file", 0)])
        self.assertEqual(await agent._mission(None), "killed: STOP file")

    async def test_given_goal_runs_once_without_mission(self):
        agent = ScriptedAgent(self.cfg(goal="only this", mission=None), self.events, [],
                              [("model finished (EVALUATE)", 0)])
        self.assertEqual(await agent._mission(None), "model finished (EVALUATE)")
        self.assertEqual(agent.ran, ["only this"])


if __name__ == "__main__":
    unittest.main()
