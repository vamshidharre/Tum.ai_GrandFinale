"""The agent's history in each model call (worker.Agent._messages): only the last turns are kept in full or short
form; older ones shrink to one line with their thought. Input tokens are most of a run's cost, and an agent with
40-60 turns otherwise resends every earlier turn on every call."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker  # noqa: E402
from worker import Agent  # noqa: E402


def agent_with(turns: int) -> Agent:
    tab = SimpleNamespace(base_url="http://site.test", page=SimpleNamespace(url="http://site.test"),
                          document="", document_source="")
    agent = Agent("erp", tab, "SYSTEM", lambda: "job")
    for number in range(turns):
        agent.turns.append({"user": f"FULL PAGE {number}", "short": f"SHORT {number}",
                            "assistant": json.dumps({"thought": f"looked at record {number}", "actions": []})})
    return agent


class History(unittest.TestCase):
    def test_a_short_task_keeps_every_turn(self):
        messages = agent_with(5)._messages("NOW")
        self.assertEqual(len(messages), 1 + 2 * 5 + 1)
        self.assertNotIn("EARLIER IN THIS TASK", json.dumps(messages))

    def test_older_turns_shrink_to_their_thought(self):
        messages = agent_with(worker.HISTORY_TURNS + 5)._messages("NOW")
        text = json.dumps(messages)
        self.assertEqual(len(messages), 1 + 2 * worker.HISTORY_TURNS + 1)
        self.assertIn("EARLIER IN THIS TASK", messages[1]["content"])
        self.assertIn("- looked at record 0", messages[1]["content"])  # the oldest is still known by its thought
        self.assertNotIn("SHORT 0", text)
        self.assertNotIn("FULL PAGE 0", text)
        self.assertIn("SHORT 5", text)  # the oldest kept turn, short
        if worker.FULL_TURNS:
            self.assertIn(f"FULL PAGE {worker.HISTORY_TURNS + 4}", text)  # the last turn in full
        else:  # no earlier page in full: the current one is in the new message
            self.assertNotIn("FULL PAGE", text)
            self.assertIn(f"SHORT {worker.HISTORY_TURNS + 4}", text)
        self.assertEqual([m["role"] for m in messages[1:]], ["user", "assistant"] * worker.HISTORY_TURNS + ["user"])

    def test_history_grows_slowly_after_the_cap(self):
        size = lambda n: len(json.dumps(agent_with(n)._messages("NOW")))
        self.assertLess(size(60) - size(30), (size(30) - size(1)) / 2)


if __name__ == "__main__":
    unittest.main()
