"""An agent still going in circles after a hint gets one stronger turn: reasoning on, with a screenshot (worker.py,
sanity plugin "escalation"), before it is given up on."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
from test_worker import FakeTab  # noqa: E402
from worker import Agent  # noqa: E402


class ScreenshotTab(FakeTab):
    async def screenshot(self):
        return "SCREENSHOT-BASE64"


async def run_looping_investigator(env: str):
    replies = [{"thought": "move the slider to unlock", "actions": [{"do": "fill", "id": 1, "text": str(i)}]}
               for i in range(30)]
    calls = []

    async def model(messages, **kwargs):
        content = messages[-1]["content"]
        calls.append({"think": kwargs.get("think"), "image": isinstance(content, list), "label": kwargs.get("label")})
        return replies.pop(0) if replies else {"thought": "end", "actions": [{"do": "done", "summary": "end"}]}

    async def idle(state):
        return None

    with patch.dict(os.environ, {"SANITY_CHECKS": env}), patch.object(llm, "chat_json", model):
        outcome = await Agent("erp", ScreenshotTab(True), "system", lambda: "job", max_steps=40, idle=idle).run()
    return outcome, calls


class Escalation(unittest.IsolatedAsyncioTestCase):
    async def test_one_escalated_turn_with_screenshot_before_the_agent_is_sent_idle(self):
        outcome, calls = await run_looping_investigator("default")
        escalated = [c for c in calls if c["label"] == "erp:escalated"]
        self.assertEqual(len(escalated), 1)
        self.assertEqual((escalated[0]["think"], escalated[0]["image"]), (True, True))
        self.assertTrue(outcome["done"])  # still given up on (sent idle) when the stronger turn did not help
        self.assertLess(len(calls), 30)  # well before the recording runs out

    async def clerk_calls(self, thoughts):
        replies = [{"thought": t, "actions": [{"do": "fill", "id": 1, "text": str(i)}]} for i, t in enumerate(thoughts)]
        calls = []

        async def model(messages, **kwargs):
            calls.append(kwargs.get("label"))
            return replies.pop(0) if replies else {"thought": "end", "actions": [{"do": "done", "summary": "end"}]}

        with patch.dict(os.environ, {"SANITY_CHECKS": "default"}), patch.object(llm, "chat_json", model):
            await Agent("submit", ScreenshotTab(True), "system", lambda: "job", max_steps=len(thoughts) + 3).run()
        return calls

    async def test_the_clerk_filling_similar_positions_is_not_escalated(self):
        """Run bb0655a2: the same per-position pattern spread over the form is not a loop."""
        cycle = ["choose the bore and apply", "choose the stroke and apply", "choose the mounting and apply",
                 "fill the amounts", "save the position", "open the next position"]
        calls = await self.clerk_calls([cycle[i % 6] for i in range(24)])
        self.assertNotIn("submit:escalated", calls)

    async def test_the_clerk_in_a_strict_loop_gets_one_escalated_turn(self):
        """Run d3ca14df: "fill the fields, add the position" / "the fields are empty again" for 95 turns."""
        calls = await self.clerk_calls(["the fields are filled, add the position", "the fields are empty again"] * 10)
        self.assertEqual(calls.count("submit:escalated"), 1)

    async def test_switched_off_there_is_no_escalated_turn(self):
        _, calls = await run_looping_investigator("validator")
        self.assertFalse([c for c in calls if c["label"] == "erp:escalated"])


if __name__ == "__main__":
    unittest.main()
