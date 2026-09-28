import asyncio
import tempfile
import unittest
from pathlib import Path

from trading_agent.approvals import ApprovalRequest, WebApprover
from trading_agent.events import EventLog


def request() -> ApprovalRequest:
    return ApprovalRequest("https://shop.test", {"action": "CLICK", "selector": "Pay"}, "Pay", ["pay"],
                           [("amount", "0.0003")], 0, 3, 0.75, {"balance_usd": 5, "floor_usd": 4, "headroom_usd": 1})


class WebApproverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = EventLog(Path(self.tmp.name))

    async def asyncTearDown(self):
        self.events.close()
        self.tmp.cleanup()

    async def test_dashboard_decision_resolves_request(self):
        approver = WebApprover(self.events, timeout_s=5)
        req = request()
        waiting = asyncio.create_task(approver.approve(req))
        await asyncio.sleep(0)
        self.assertTrue(approver.decide(req.id, True))
        self.assertTrue(await waiting)
        self.assertEqual(approver.last_decided_by, "dashboard")
        self.assertFalse(approver.decide(req.id, False))  # already decided

    async def test_no_answer_blocks(self):
        approver = WebApprover(self.events, timeout_s=0.05)
        self.assertFalse(await approver.approve(request()))
        self.assertEqual(approver.last_decided_by, "timeout")

    async def test_stop_blocks_pending(self):
        approver = WebApprover(self.events, timeout_s=5)
        waiting = asyncio.create_task(approver.approve(request()))
        await asyncio.sleep(0)
        approver.cancel_all("Stop pressed on the dashboard")
        self.assertFalse(await waiting)
        self.assertEqual(approver.last_decided_by, "Stop pressed on the dashboard")

    async def test_unknown_id_is_rejected(self):
        self.assertFalse(WebApprover(self.events, timeout_s=5).decide("nope", True))

    async def test_confirm_emits_question_and_answer(self):
        approver = WebApprover(self.events, timeout_s=5)
        waiting = asyncio.create_task(approver.confirm("Continue?"))
        await asyncio.sleep(0)
        asked = [e for e in self.events.history if e["type"] == "confirm_requested"][0]
        approver.decide(asked["data"]["id"], False)
        self.assertFalse(await waiting)
        self.assertEqual(self.events.history[-1]["data"]["answer"], "no")


if __name__ == "__main__":
    unittest.main()
