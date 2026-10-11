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


class FastSteps(unittest.IsolatedAsyncioTestCase):
    """#2: a steps chain reuses its last page read while it only fills fields; a press reads the page again."""

    async def test_fills_reuse_the_read_and_a_press_reads_again(self):
        class Form:
            def __init__(self):
                self.base_url = "http://form.test"
                self.page = SimpleNamespace(url=self.base_url + "/f", main_frame=SimpleNamespace(evaluate=self._alive))
                self.document, self.document_source, self.reads, self.values = "", "", 0, {}

            async def _alive(self, script):
                return True

            def take_events(self):
                return []

            async def observe(self):
                self.reads += 1
                return ('url: http://form.test/f\n[1] input "Quantity" value=""\n[2] input "Price" value=""\n'
                        '[3] input "Weeks" value=""\n[4] button "Save"')

            async def fill(self, aid, text, enter=False):
                self.values[str(aid)] = text
                return f"filled [{aid}]"

            async def click(self, aid):
                self.values["saved"] = True
                return f"clicked [{aid}]"

        form = Form()
        steps = [{"field": "Quantity", "value": "40"}, {"field": "Price", "value": "12,50"},
                 {"field": "Weeks", "value": "3"}, {"press": "Save"}]
        said = await Agent("submit", form, "system", lambda: "job")._steps(steps)
        self.assertTrue(said.startswith("ran all 4 steps"), said)
        self.assertEqual(form.values, {"1": "40", "2": "12,50", "3": "3", "saved": True})
        self.assertEqual(form.reads, 2)  # one read for the three fills, one after the press (was 5)

    async def test_a_control_gone_since_the_read_is_found_again(self):
        class Rerendering:
            base_url = "http://form.test"

            def __init__(self):
                self.page = SimpleNamespace(url=self.base_url + "/f", main_frame=SimpleNamespace(evaluate=self._alive))
                self.document, self.document_source, self.offset, self.values = "", "", 0, {}

            async def _alive(self, script):
                return True

            def take_events(self):
                return []

            async def observe(self):
                return (f'url: http://form.test/f\n[{1 + self.offset}] input "A" value=""\n'
                        f'[{2 + self.offset}] input "B" value=""')

            async def fill(self, aid, text, enter=False):
                if int(aid) < 1 + self.offset:
                    return f"there is no element [{aid}] on the page any more"
                self.values[text] = aid
                self.offset += 10  # every fill re-renders the form with new ids
                return f"filled [{aid}]"

        form = Rerendering()
        said = await Agent("submit", form, "system", lambda: "job")._steps(
            [{"field": "A", "value": "a"}, {"field": "B", "value": "b"}])
        self.assertTrue(said.startswith("ran all 2 steps"), said)
        self.assertEqual(set(form.values), {"a", "b"})


class FakeSite:
    """For open_site: a sequence of screens ("login", "cookies") the site shows before its start page."""
    base_url, credentials = "http://shop.test", {"username": "u", "password": "p"}

    def __init__(self, screens):
        self.calls, self.screens = [], list(screens)

    async def goto(self, url):
        self.calls.append("goto")
        return f"opened {url}"

    async def password_field_shown(self, wait=0):
        return bool(self.screens) and self.screens[0] == "login"

    async def login(self):
        self.calls.append("login")
        self.screens.pop(0)
        return "entered the credentials and submitted the login form; logged in, now at http://shop.test/"

    async def choose_necessary_cookies(self):
        if self.screens and self.screens[0] == "cookies":
            self.screens.pop(0)
            self.calls.append("cookies")
            return 'chose "necessary only" on the cookie notice'
        return None


class OpeningASite(unittest.IsolatedAsyncioTestCase):
    """#3: the site is opened, logged in and its cookie notice answered before the agent's first model turn."""

    async def test_login_then_cookie_notice(self):
        site = FakeSite(["login", "cookies"])
        notes = await agent.open_site(site)
        self.assertEqual(site.calls, ["goto", "login", "cookies"])
        self.assertEqual(len(notes), 3)
        self.assertTrue(all(note.startswith("(before your first turn, by code)") for note in notes))

    async def test_cookie_notice_then_login(self):
        site = FakeSite(["cookies", "login"])
        await agent.open_site(site)
        self.assertEqual(site.calls, ["goto", "cookies", "login"])

    async def test_a_refused_login_is_left_to_the_model(self):
        site = FakeSite(["login"])

        async def refused():
            site.calls.append("login")
            return "submitted the login form, but the login form is still shown: read the page"

        site.login = refused
        await agent.open_site(site)
        self.assertEqual(site.calls, ["goto", "login"])  # no second try by code


class RecordBatchHint(unittest.TestCase):
    """#1: the ERP agent is told to work one record by hand and file the rest with one file_each."""

    def briefing(self, site: str, questions: list) -> str:
        agent.SITE_ROLES.clear()
        agent.SITE_ROLES["erp"] = "inventory"
        case = SimpleNamespace(questions=lambda: questions, take_news=lambda name: [], status=lambda: "STATUS",
                               pending=0, pending_for=lambda name=None: 0, brief="BRIEF")
        tab = SimpleNamespace(site_id=site, base_url=f"http://{site}.test")
        team = agent.Team(case, [site], time.time() + 600)
        return agent.investigator(case, team, tab, time.time() + 600).briefing()

    def test_two_open_records_bring_the_hint_on_the_erp_only(self):
        open_ = [("material:A-1", "find it"), ("material:A-2", "find it")]
        self.assertIn("SAVE TURNS", self.briefing("inventory", open_))
        self.assertNotIn("SAVE TURNS", self.briefing("inventory", open_[:1]))
        self.assertNotIn("SAVE TURNS", self.briefing("portal", open_))


class ReadingTheBrief(unittest.IsolatedAsyncioTestCase):
    """#4: the brief is read without reasoning; with reasoning only when a key fact is missing."""

    async def test_a_complete_answer_needs_one_quick_call(self):
        import casefile
        calls = []

        async def model(messages, **kwargs):
            calls.append(kwargs.get("think"))
            return {"tender_reference": "T-1", "scenario_date": "2030-01-01T09:00:00Z",
                    "authorized_senders": {"discount_approval": "boss"}}

        with patch.object(llm, "chat_json", model):
            context = await casefile.read_context("BRIEF")
        self.assertEqual(calls, [False])
        self.assertEqual(context["tender_reference"], "T-1")

    async def test_a_missing_reference_is_asked_again_with_reasoning(self):
        import casefile
        calls = []

        async def model(messages, **kwargs):
            calls.append(kwargs.get("think"))
            return {"scenario_date": "2030-01-01T09:00:00Z"} if len(calls) == 1 else {"tender_reference": "T-1"}

        with patch.object(llm, "chat_json", model):
            context = await casefile.read_context("BRIEF")
        self.assertEqual(calls, [False, True])
        self.assertEqual(context["tender_reference"], "T-1")


if __name__ == "__main__":
    unittest.main()


class BatchPathsGetThrough(unittest.IsolatedAsyncioTestCase):
    """Run ab00e578: file_each filed no stock or price page because a label appeared twice (menu and record) and a
    maintenance notice stood in front of a record."""

    def test_a_label_shown_twice_takes_the_records_own_link(self):
        page = ('url: http://erp.test/rec?material=M-7\n[3] link "Prices" -> /prices\n'
                '[9] link "Prices" -> /prices?material=M-7')
        aid, _ = agent.pick_control(page, "Prices", ("link",), "http://erp.test/rec?material=M-7")
        self.assertEqual(aid, "9")

    def test_two_links_to_the_same_address_are_one_choice(self):
        page = 'url: http://erp.test/rec\n[3] link "Stock" -> /stock?m=1\n[9] link "Stock" -> /stock?m=1'
        self.assertEqual(agent.pick_control(page, "Stock", ("link",), "http://erp.test/rec")[0], "3")

    def test_a_real_ambiguity_stays_unresolved(self):
        page = 'url: http://erp.test/rec\n[3] link "Stock" -> /stock?m=1\n[9] link "Stock" -> /stock?m=2'
        self.assertIsNone(agent.pick_control(page, "Stock", ("link",), "http://erp.test/rec")[0])

    async def test_a_notice_page_is_passed_and_a_form_is_not(self):
        clicked = []

        class Tab:
            async def click(self, aid):
                clicked.append(aid)
                return f"clicked [{aid}]"

        notice = 'url: http://erp.test/x\ntitle: Wartungshinweis\n# Wartungshinweis\nSome functions are under maintenance.\n[4] button "Akzeptieren und fortfahren"'
        self.assertEqual(await agent.pass_notice(Tab(), notice), "Akzeptieren und fortfahren")
        check = notice + '\n[5] input "Code" value=""'
        self.assertIsNone(await agent.pass_notice(Tab(), check))  # a field to fill is the model's
        record = 'url: http://erp.test/rec\ntitle: Material M-7\n# Material M-7\nHinweis: see stock.\n[6] link "Weiter" -> /next'
        self.assertIsNone(await agent.pass_notice(Tab(), record))  # a record that only mentions a notice
        self.assertEqual(clicked, ["4"])
