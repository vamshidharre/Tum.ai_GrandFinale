"""A document an agent pages through in place (an embedded viewer whose address does not change) is filed whole when
the agent keeps it, and an agent that finishes while a question it filed evidence for is still open is told what is
missing (agent.investigator, worker.Agent). Sandbox run dafda06b: the agent clicked "next page" through a 5-page
specification, kept only the last page, and finished with 4 of 5 requirements never filed. Invented site and values."""
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
from agent import DONE_REFUSALS, investigator  # noqa: E402
from browser_env import PAGE_BREAK  # noqa: E402
from casefile import CaseFile  # noqa: E402

SHEETS = 4


class Viewer:
    """A portal page with an embedded data sheet of four pages, one item each, paged in place: the address stays.
    [5] pages on, [6] toggles the item's note, [7] opens another document, [9] ticks the access check."""

    def __init__(self):
        self.site_id, self.base_url = "portal", "http://portal.test"
        self.page = SimpleNamespace(url="http://portal.test/sheet?doc=DS-7", main_frame=SimpleNamespace(evaluate=self._alive))
        self.document, self.document_source = "", ""
        self.number, self.note, self.unlocked = 1, True, True

    async def _alive(self, script):
        return True

    def view(self) -> str:
        if self.page.url.endswith("/other"):
            return "url: http://portal.test/other\ntitle: Other\nAn unrelated notice\n[8] link \"Back\""
        lines = [f"url: {self.page.url}", "title: Data sheet viewer", "# Data sheet DS-7", "Portal menu",
                 '[2] link "Home"', '[3] link "Documents"']
        if not self.unlocked:
            return "\n".join(lines + ["(LOCKED AREA: the controls below are disabled until this area is unlocked)",
                                      '[9] checkbox "I have read the terms" [unchecked]'])
        lines += ["┌── embedded document: DS-7", f"│ Item R-{self.number} | length {self.number * 111} mm"]
        if self.note:
            lines.append(f"│ Note: item R-{self.number} is supplied in sets")
        lines.append(f"│ Sheet {self.number} of {SHEETS}")
        if self.number < SHEETS:
            lines.append('│ [5] link "Next page"')
        lines += ['│ [6] toggle "Note"', "└── end of embedded document", '[7] link "Other document"']
        return "\n".join(lines)

    async def observe(self):
        self.document, self.document_source = self.view(), self.page.url
        return self.document

    async def click(self, aid):
        if str(aid) == "5" and self.number < SHEETS:
            self.number += 1
        elif str(aid) == "6":
            self.note = not self.note
        elif str(aid) == "7":
            self.page.url = "http://portal.test/other"
            return f"clicked [{aid}]; now at {self.page.url}"
        return f"clicked [{aid}]"

    async def set(self, aid, value):
        self.unlocked = bool(value)
        return f"set [{aid}] to {value}"

    def take_events(self):
        return []


THOUGHTS = iter(range(10 ** 6))


def turn(*actions):
    return {"thought": f"step {next(THOUGHTS)}", "actions": list(actions)}  # distinct: no going-in-circles hints


KEEP = {"do": "keep", "kind": "requirements", "note": "the data sheet"}
NEXT = {"do": "click", "id": 5}
DONE = {"do": "done", "summary": "finished"}


async def run_investigator(replies, tab=None, questions=None, filed=None):
    """The real investigator on the viewer; the case file records what is filed (in `filed`) instead of extracting
    it. Returns the filings, the prompts the model was shown and the outcome."""
    tab = tab or Viewer()
    case = CaseFile("Bid for tender T-1.", {"tender_reference": "T-1", "scenario_date": "2030-01-15"})
    filed = [] if filed is None else filed
    asked = []

    def file(kind, text, source, agent):
        filed.append((kind, text, source))
        return f"filed {len(text)} characters ({text.count(PAGE_BREAK) or 1} page(s)) from {source} as {kind}"

    case.file = file
    case.questions = questions or (lambda: [])

    async def model(messages, **kwargs):
        asked.append(messages[-1]["content"])
        return replies.pop(0) if replies else turn(DONE)

    async def idle(why):
        return None

    team = SimpleNamespace(hook=lambda name: idle)
    with patch.object(llm, "chat_json", model), patch.dict(os.environ, {"AGENT_MAX_STEPS": "20"}):
        outcome = await investigator(case, team, tab, time.time() + 600).run()
    return filed, asked, outcome


def items(text: str) -> list[int]:
    return [number for number in range(1, SHEETS + 1) if f"Item R-{number} |" in text]


class PagedInPlace(unittest.IsolatedAsyncioTestCase):
    async def test_keep_after_paging_through_a_document_in_place_files_every_page_read(self):
        filed, asked, _ = await run_investigator([turn(NEXT), turn(NEXT), turn(NEXT), turn(KEEP), turn(DONE)])
        self.assertEqual(len(filed), 1)
        kind, text, source = filed[0]
        self.assertEqual((kind, source), ("requirements", "http://portal.test/sheet?doc=DS-7"))
        self.assertEqual(items(text), [1, 2, 3, 4])
        self.assertEqual(text.count(PAGE_BREAK), SHEETS)
        self.assertIn("the 4 pages you read of it since your last keep were filed together", asked[4])

    async def test_a_keep_starts_a_new_trail(self):
        filed, _, _ = await run_investigator([turn(KEEP), turn(NEXT), turn(NEXT), turn(KEEP), turn(DONE)])
        self.assertEqual([items(text) for _, text, _ in filed], [[1], [2, 3]])
        self.assertNotIn(PAGE_BREAK, filed[0][1])

    async def test_a_click_and_keep_in_one_turn_includes_the_page_the_click_led_to(self):
        filed, _, _ = await run_investigator([turn(NEXT, KEEP), turn(DONE)])
        self.assertEqual(items(filed[0][1]), [1, 2])

    async def test_a_page_whose_lines_are_all_on_another_page_is_left_out(self):
        # the note toggled closed and open again: one page, not three
        filed, _, _ = await run_investigator([turn({"do": "click", "id": 6}), turn({"do": "click", "id": 6}),
                                              turn(KEEP), turn(DONE)])
        self.assertNotIn(PAGE_BREAK, filed[0][1])
        self.assertIn("Note: item R-1", filed[0][1])

    async def test_a_form_action_ends_the_trail(self):
        tab = Viewer()
        tab.unlocked = False
        filed, _, _ = await run_investigator([turn({"do": "set", "id": 9, "value": True}), turn(KEEP), turn(DONE)], tab)
        self.assertNotIn(PAGE_BREAK, filed[0][1])
        self.assertNotIn("LOCKED AREA", filed[0][1])

    async def test_pages_at_another_address_are_not_joined(self):
        filed, _, _ = await run_investigator([turn(NEXT), turn({"do": "click", "id": 7}), turn(KEEP), turn(DONE)])
        self.assertEqual(filed[0][2], "http://portal.test/other")
        self.assertNotIn(PAGE_BREAK, filed[0][1])
        self.assertEqual(items(filed[0][1]), [])


class DoneWhileItsQuestionIsOpen(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def requirements_question(filed):
        """Open until every item of the sheet is on file: what the case file's question would say."""
        def questions():
            have = {n for _, text, _ in filed for n in items(text)}
            missing = [f"R-{n}" for n in range(1, SHEETS + 1) if n not in have]
            return [("requirements", f"the requirements {', '.join(missing)} in full")] if missing else []
        return questions

    async def _run(self, replies, filed_box, questions):
        return await run_investigator(replies, questions=questions, filed=filed_box)

    async def test_done_is_refused_with_the_missing_ids_and_the_agent_files_the_rest(self):
        filed_box = []
        questions = self.requirements_question(filed_box)
        # page 1 kept, page 2 read, then done (the run's mistake); told what is missing, it pages on and keeps
        filed, asked, outcome = await self._run(
            [turn(KEEP), turn(NEXT), turn(DONE), turn(NEXT), turn(NEXT), turn(KEEP), turn(DONE)], filed_box, questions)
        refusal = asked[3]
        self.assertIn("not finished", refusal)
        self.assertIn("[requirements] the requirements R-2, R-3, R-4 in full", refusal)
        self.assertEqual([items(text) for _, text, _ in filed], [[1], [2, 3, 4]])
        self.assertEqual(questions(), [])
        self.assertTrue(outcome["done"])
        self.assertEqual(outcome["steps"], 7)

    async def test_done_is_refused_at_most_twice_per_question(self):
        filed_box = []
        questions = self.requirements_question(filed_box)
        filed, asked, outcome = await self._run([turn(KEEP)] + [turn(DONE)] * 5, filed_box, questions)
        self.assertTrue(outcome["done"])
        self.assertEqual(sum("not finished" in prompt for prompt in asked), DONE_REFUSALS)
        self.assertEqual(outcome["steps"], 2 + DONE_REFUSALS)

    async def test_an_agent_that_filed_nothing_for_the_question_may_finish(self):
        filed_box = []
        questions = self.requirements_question(filed_box)
        _, asked, outcome = await self._run([turn(DONE)], filed_box, questions)
        self.assertTrue(outcome["done"])
        self.assertEqual(outcome["steps"], 1)

    async def test_the_gate_can_be_switched_off(self):
        filed_box = []
        questions = self.requirements_question(filed_box)
        with patch.dict(os.environ, {"SANITY_CHECKS": "none"}):
            _, _, outcome = await self._run([turn(KEEP), turn(DONE)], filed_box, questions)
        self.assertEqual(outcome["steps"], 2)


if __name__ == "__main__":
    unittest.main()
