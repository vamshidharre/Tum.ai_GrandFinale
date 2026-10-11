"""The speed fixes of BEST_RUN_TIMING.md, measured on run a176359f (100, 6:15):

Fix #1  a page that cannot be read is a broken tab: replaced at once, within a bounded wait (128 s were lost on one
        flaky tender-portal login).
Fix #2  the tender agent stops once its evidence is on file: the Q&A list followed to its end closes its question by
        itself (41 s of re-opened documents), and routine "document read" notices no longer wake an idle agent.
(Fix #3, the clerk's enter_positions, is tested in test_enter_positions.py; the browser side of Fix #1 in
test_browser_env.py.)"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import llm  # noqa: E402
import worker  # noqa: E402
from casefile import CaseFile  # noqa: E402
from worker import Agent  # noqa: E402

CONTEXT = {"tender_reference": "T-0001", "scenario_date": "2030-03-01T09:00:00+00:00"}


class HangingTab:
    """Its first page read never finishes; after `renew` every read answers at once."""

    def __init__(self):
        self.site_id, self.base_url = "portal", "http://portal.test"
        self.page = SimpleNamespace(url=self.base_url + "/login", main_frame=SimpleNamespace(evaluate=self._alive))
        self.document, self.document_source = "", ""
        self.renewals, self.reads = 0, 0

    async def _alive(self, script):
        return True

    def take_events(self):
        return []

    async def observe(self):
        self.reads += 1
        if self.renewals == 0:
            await asyncio.sleep(30)
        return 'url: http://portal.test/home\ntitle: Home\n[1] button "Go"'

    async def renew(self):
        self.renewals += 1
        return "opened http://portal.test/"


class Fix1BrokenTabIsReplacedAtOnce(unittest.IsolatedAsyncioTestCase):
    async def test_a_page_read_that_hangs_gets_a_fresh_tab_within_a_bounded_wait(self):
        tab, asked = HangingTab(), []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return {"thought": "", "actions": [{"do": "done", "summary": "ok"}]}

        started = time.monotonic()
        with patch.object(llm, "chat_json", model), patch.object(worker, "READ_TIMEOUT", 0.3):
            outcome = await Agent("portal", tab, "system", lambda: "job", max_steps=3).run()
        self.assertTrue(outcome["done"])
        self.assertEqual(tab.renewals, 1)
        self.assertLess(time.monotonic() - started, 5)  # not 45 s of waiting, then a second failure
        self.assertIn("replaced by a fresh one", asked[0])
        self.assertIn("title: Home", asked[0])  # the model sees the fresh tab's page, not a timeout message


class Fix2QuestionCloses(unittest.TestCase):
    def case(self) -> CaseFile:
        case = CaseFile("BRIEF", CONTEXT)
        case.merge("clarifications", {"answers": [{"number": "Q-1", "timestamp": "2030-02-01T10:00:00Z"}]})
        return case

    def test_the_q_and_a_followed_to_its_end_and_read_closes_its_question(self):
        case = self.case()
        self.assertIn("clarifications", [qid for qid, _ in case.questions()])  # filed, but nobody vouched for "all"
        case.listed_in_full["clarifications"] = "portal"
        self.assertNotIn("clarifications", [qid for qid, _ in case.questions()])
        self.assertEqual(case.answers["clarifications"][0], "complete")

    def test_it_stays_open_while_the_list_is_still_being_read(self):
        case = self.case()
        case.listed_in_full["clarifications"] = "portal"
        case.pending_by_agent["portal"] = 1
        self.assertIn("clarifications", [qid for qid, _ in case.questions()])


class PortalTab:
    def __init__(self):
        self.site_id, self.base_url = "portal", "http://portal.test"
        self.page = SimpleNamespace(url=self.base_url + "/qa")
        self.document, self.document_source = "", ""
        self.last_collect = None


class Fix2KeepMarksAWholeList(unittest.IsolatedAsyncioTestCase):
    async def keep(self, held_by: str, run: dict | None) -> CaseFile:
        case = CaseFile("BRIEF", CONTEXT)
        team = agent.Team(case, ["portal"], time.time() + 600)
        tab = PortalTab()
        tab.last_collect = run
        investigator = agent.investigator(case, team, tab, time.time() + 600)
        investigator.held = ("Q-1 answered: quantity of position 1 is 40. " * 3, "http://portal.test/qa")
        investigator.held_by = held_by

        async def nothing(*args, **kwargs):
            return {}

        with patch.object(llm, "chat_json", nothing):
            said = await investigator.extra["keep"]({"do": "keep", "kind": "clarifications"})
            await asyncio.sleep(0.05)  # let the background extraction finish
        self.assertTrue(said.startswith("filed"), said)
        return case

    async def test_a_list_collected_to_its_end_is_marked(self):
        case = await self.keep("collect", {"pages": 6, "capped": False})
        self.assertEqual(case.listed_in_full, {"clarifications": "portal"})

    async def test_a_capped_list_a_single_page_or_a_download_is_not(self):
        for held_by, run in (("collect", {"pages": 10, "capped": True}), ("collect", {"pages": 1, "capped": False}),
                             ("fetch", {"pages": 6, "capped": False})):
            case = await self.keep(held_by, run)
            self.assertEqual(case.listed_in_full, {}, (held_by, run))


class Fix2RoutineNewsDoesNotWake(unittest.IsolatedAsyncioTestCase):
    async def hold_with(self, news: str):
        agent.SITE_ROLES.clear()
        case = CaseFile("BRIEF", CONTEXT)
        team = agent.Team(case, ["portal"], time.time() + 600)
        waiting = asyncio.ensure_future(team.hook("portal")("done"))
        await asyncio.sleep(0.2)
        case.news.setdefault("portal", []).append(news)
        case._bump()
        return await asyncio.wait_for(waiting, 10), team

    async def test_a_document_that_was_read_fine_lets_the_agent_rest(self):
        woke, team = await self.hold_with("From the positions document you filed (http://x): 1: 40 pcs -> A-1")
        self.assertIsNone(woke)  # not woken; the investigation ends with every agent idle
        self.assertTrue(team.finished.is_set())

    async def test_a_document_that_failed_wakes_the_agent(self):
        woke, _ = await self.hold_with("From the positions document you filed (http://x): the extraction failed; "
                                       "file the document again")
        self.assertIn("The case file has news", woke)


if __name__ == "__main__":
    unittest.main()
