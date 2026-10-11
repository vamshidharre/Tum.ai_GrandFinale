"""The coverage record and the gate in front of closing a question, on invented pages (no scenario values)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
from coverage import LIMIT, Ledger  # noqa: E402
from worker import Agent  # noqa: E402


def page(path: str, title: str = "List") -> str:
    return f'url: http://site.test{path}\ntitle: {title}\n[1] input "Search" value=""'


class Record(unittest.TestCase):
    def test_it_holds_each_page_and_each_typed_text_once(self):
        ledger = Ledger()
        for text in (page("/inbox", "Inbox"), page("/inbox", "Inbox"), page("/archive", "Archive"), "(the page could not be read)"):
            ledger.saw(text)
        ledger.typed("http://site.test/inbox", "T-0001")
        ledger.typed("http://site.test/inbox", "t-0001 ")
        ledger.typed("http://site.test/archive", "T-0001")
        ledger.typed("http://site.test/archive", "")
        self.assertEqual(list(ledger.views.values()), ["Inbox", "Archive"])
        self.assertEqual(len(ledger.queries), 2)
        report = ledger.report()
        self.assertIn('"Archive" /archive', report)
        self.assertIn('"T-0001" at /inbox', report)
        self.assertIn("lists followed page by page (0): none", report)

    def test_a_list_cut_at_its_page_limit_stays_incomplete_until_the_same_control_reaches_the_end(self):
        ledger = Ledger()
        ledger.followed({"where": "http://site.test/qa", "control": "Next", "pages": 3, "capped": True})
        self.assertEqual([run.control for run in ledger.incomplete()], ["Next"])
        ledger.followed({"where": "http://site.test/qa?page=3", "control": "Next", "pages": 2, "capped": False})
        self.assertEqual(ledger.incomplete(), [])
        ledger.followed(None)  # a collect that never started leaves no trace
        self.assertEqual(len(ledger.lists), 2)


class Gate(unittest.TestCase):
    def test_complete_passes_when_no_list_was_cut_short(self):
        self.assertIsNone(Ledger().gate("clarifications", "complete"))

    def test_nothing_is_closed_over_a_list_that_stopped_at_its_limit(self):
        ledger = Ledger()
        ledger.followed({"where": "http://site.test/qa", "control": "Next", "pages": 5, "capped": True})
        for status in ("complete", "absent"):
            refusal = ledger.gate("q-" + status, status)
            self.assertIn("stopped at its limit of 5 page(s)", refusal)
            self.assertIn("higher max_pages", refusal)
        ledger.followed({"where": "http://site.test/qa?page=5", "control": "Next", "pages": 4, "capped": False})
        self.assertIsNone(ledger.gate("q-complete", "complete"))

    def test_absent_is_answered_with_the_record_first_and_accepted_after_a_further_search(self):
        ledger = Ledger()
        ledger.saw(page("/inbox", "Inbox"))
        ledger.typed("http://site.test/inbox", "T-0001")
        refusal = ledger.gate("discount", "absent")
        self.assertIn("not closed yet", refusal)
        self.assertIn('"T-0001" at /inbox', refusal)  # the record is what the agent did, not what it says
        self.assertIn("covers only that search term in that place", refusal)
        again = ledger.gate("discount", "absent")
        self.assertIn("nothing new was searched since the last check", again)
        ledger = Ledger()
        ledger.gate("discount", "absent")
        ledger.saw(page("/archive", "Archive"))
        self.assertIsNone(ledger.gate("discount", "absent"))

    def test_every_question_is_challenged_on_its_own(self):
        ledger = Ledger()
        ledger.gate("discount", "absent")
        ledger.moved()
        self.assertIsNone(ledger.gate("discount", "absent"))
        self.assertIsNotNone(ledger.gate("index:X-1", "absent"))

    def test_the_gate_is_bounded_so_it_can_never_hold_a_run(self):
        ledger = Ledger()
        ledger.followed({"where": "http://site.test/huge", "control": "Next", "pages": 30, "capped": True})
        refusals = [ledger.gate("discount", "absent") for _ in range(LIMIT + 2)]
        self.assertEqual([refusal is None for refusal in refusals], [False] * LIMIT + [True, True])


class FakeTab:
    """As much of a Tab as the agent loop touches: a mailbox with two folders, a search field and a paged list."""

    def __init__(self):
        self.base_url = "http://site.test"
        self.page = SimpleNamespace(url=self.base_url + "/inbox", main_frame=SimpleNamespace(evaluate=self._alive))
        self.document, self.document_source = "", ""
        self.last_collect = None
        self.query = ""

    async def _alive(self, script):
        return True

    async def observe(self):
        return f'url: {self.page.url}\ntitle: Mail\n[1] input "Search" value="{self.query}"\n[2] link "Archive"\n[3] link "Next"'

    def take_events(self):
        return []

    async def fill(self, aid, text, enter=False):
        if text == "refused":  # the field keeps its old content
            return f'filled [{aid}]; BUT the field now holds "{self.query}", not what was typed'
        self.query = text
        return f"filled [{aid}]"

    async def click(self, aid):
        self.page.url = self.base_url + "/archive"
        return f"clicked [{aid}]"

    async def collect(self, aid, max_pages=10):
        capped = int(max_pages) < 4
        self.last_collect = {"where": self.page.url, "control": "Next", "pages": min(int(max_pages), 4), "capped": capped}
        return "followed" + (" THE LIST IS NOT COMPLETE" if capped else "")


class InTheAgentLoop(unittest.IsolatedAsyncioTestCase):
    async def run_agent(self, replies):
        asked, closed = [], []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        async def answer(action):
            closed.append((action["question"], action["status"]))
            return f"question {action['question']} closed as {action['status']}"

        agent = Agent("mail", FakeTab(), "system", lambda: "job", extra={"answer": answer}, max_steps=len(replies) + 1)
        with patch.object(llm, "chat_json", model):
            await agent.run()
        return agent, asked, closed

    @staticmethod
    def turn(*actions):
        return {"thought": "", "actions": list(actions)}

    async def test_absent_after_one_search_is_held_back_until_the_agent_searched_further(self):
        absent = {"do": "answer", "question": "discount", "status": "absent", "note": "searched everywhere"}
        agent, asked, closed = await self.run_agent([
            self.turn({"do": "fill", "id": 1, "text": "T-0001", "enter": True}),
            self.turn(absent),
            self.turn({"do": "click", "id": 2}),
            self.turn(absent),
            self.turn({"do": "done", "summary": "finished"})])
        self.assertIn("not closed yet", asked[2])
        self.assertIn('"T-0001" at /inbox', asked[2])
        self.assertNotIn("/archive", asked[2].split("CURRENT PAGE")[0])  # the folder it had not opened is not in the record
        self.assertIn("question discount closed as absent", asked[4])
        self.assertEqual(closed, [("discount", "absent")])
        self.assertEqual(list(agent.coverage.views), ["http://site.test/inbox", "http://site.test/archive"])

    async def test_a_text_the_field_did_not_keep_is_not_recorded_as_searched(self):
        agent, _, _ = await self.run_agent([
            self.turn({"do": "fill", "id": 1, "text": "T-0001", "enter": True}),
            self.turn({"do": "fill", "id": 1, "text": "refused", "enter": True}),
            self.turn({"do": "done", "summary": "finished"})])
        self.assertEqual([text for _, text in agent.coverage.queries], ["T-0001"])

    async def test_complete_is_held_back_while_a_collected_list_is_cut_short(self):
        complete = {"do": "answer", "question": "clarifications", "status": "complete", "note": "all pages"}
        agent, asked, closed = await self.run_agent([
            self.turn({"do": "collect", "id": 3, "max_pages": 2}),
            self.turn(complete),
            self.turn({"do": "collect", "id": 3, "max_pages": 10}),
            self.turn(complete),
            self.turn({"do": "done", "summary": "finished"})])
        self.assertIn("stopped at its limit of 2 page(s)", asked[2])
        self.assertEqual(closed, [("clarifications", "complete")])
        self.assertEqual([run.capped for run in agent.coverage.lists], [True, False])

    async def test_the_gate_can_be_switched_off_like_every_sanity_check(self):
        absent = {"do": "answer", "question": "discount", "status": "absent", "note": ""}
        with patch.dict(os.environ, {"SANITY_CHECKS": "none"}):
            _, _, closed = await self.run_agent([self.turn(absent), self.turn({"do": "done", "summary": "finished"})])
        self.assertEqual(closed, [("discount", "absent")])

    async def test_other_extra_actions_are_not_gated(self):
        async def model(messages, **kwargs):
            return replies.pop(0)

        async def keep(action):
            return "filed"

        replies = [self.turn({"do": "keep", "kind": "mail", "status": "absent"}), self.turn({"do": "done", "summary": "x"})]
        agent = Agent("mail", FakeTab(), "system", lambda: "job", extra={"keep": keep}, max_steps=3)
        with patch.object(llm, "chat_json", model):
            await agent.run()
        self.assertEqual(agent.coverage.refused, {})


if __name__ == "__main__":
    unittest.main()
