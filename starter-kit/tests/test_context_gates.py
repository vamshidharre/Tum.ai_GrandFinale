"""Context gates (gates.py) and the slimmer investigator context: a task card instead of the whole brief, the case
file lines an agent's own questions concern, more context only while it has trouble, the clerk's submission part of
the brief, and a soft token budget. Invented brief and records only."""
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import gates as gates_module  # noqa: E402
import llm  # noqa: E402
import submit  # noqa: E402
from casefile import CaseFile, brief_intro, brief_rules, status_sections  # noqa: E402
from gates import Gates  # noqa: E402
from test_worker import FakeTab  # noqa: E402
from worker import Agent  # noqa: E402

BRIEF = """# Quote the widgets

You work for Example Works AG. Prepare one bid for tender T-77 of the town of Nowhere. The scenario date is
2030-03-01T08:00:00Z. Get the tender from `portal-a`, the drawings from `portal-b`, parts from `stock-db` and
approvals from `post`.

## Rules

1. Quantity: use the tender quantity unless a clarification changes it.
2. Material: select an active part whose diameter equals the requirement. Blocked parts do not qualify.
3. Discount: use the latest approval by boss@ for exactly this reference.

## More rules

- Finding records: search the parts database for the catalogue group of the requirement.
- Packaging: the tariff key comes from the Q&A, the carton size from the parts database.
- Scoring: material 20 %, quantity 15 %, packaging 10 %.
- Partial submission: the form lets you omit a position; it gets no points.

## Site access

- portal-a: https://a.test
  Username: `u_1` · Password: `pw-secret-1`

## Submission

Submit in portal-a under "Offer". Sign with the code from the mailbox.
"""

CONTEXT = {"tender_reference": "T-77", "scenario_date": "2030-03-01T08:00:00Z", "company_name": "Example Works AG",
           "mail_site": "post", "erp_site": "stock-db",
           "authorized_senders": {"discount_approval": "boss", "packaging_tariffs": "store"},
           "authorized_subjects": {"packaging_tariffs": "Crates"}, "mail_search_hint": "use the reference plus a word"}


def filled_case() -> CaseFile:
    case = CaseFile(BRIEF, CONTEXT)
    case.merge("positions", {"positions": [{"position": "1", "quantity": 5, "unit": "pcs", "requirement_ref": "R1"}]})
    case.merge("revisions", {"revisions": [{"revision": "B", "status": "current", "date": "2030-01-01"}]})
    case.merge("requirements", {"revision": "B", "requirements": [{"requirement_ref": "R1", "bore_mm": 10, "stroke_mm": 20,
                                                                  "pressure_bar": 30, "mounting": "foot",
                                                                  "catalogue_group": "G-1"}]})
    case.merge("material", {"materials": [{"material_number": "P-9", "catalogue_group": "G-1", "is_active": True,
                                           "bore_mm": 10, "stroke_mm": 20, "pressure_bar": 30, "mounting": "foot"}]})
    return case


class GateSteps(unittest.TestCase):
    def gates(self):
        logged = []
        return Gates(enabled=True, log=logged.append), logged

    def test_one_turn_of_trouble_raises_the_gate_and_two_calm_turns_bring_it_down(self):
        gate, logged = self.gates()
        gate.start_turn("url: x")
        gate.end_turn([{"do": "click", "id": 1}], ["verification: click caused no visible page change; inspect"],
                      url_before="x", url_after="x")
        self.assertEqual(gate.gate, 1)
        self.assertIn("gate 0->1: an action changed nothing", logged)
        for _ in range(2):
            gate.start_turn("url: x")
            gate.end_turn([{"do": "click", "id": 2}], ["verification: click visibly changed the page state"],
                          url_before="x", url_after="x")
        self.assertEqual(gate.gate, 0)
        self.assertTrue(logged[-1].startswith("gate 1->0"))

    def test_progress_brings_the_gate_down_at_once(self):
        for result, url_after in (("filed 900 characters (1 page(s)) from x as material", "x"),
                                  ("question discount closed as absent", "x"), ("opened y", "y")):
            gate, _ = self.gates()
            gate.trouble("an extraction found nothing")
            self.assertEqual(gate.gate, 1)
            gate.start_turn("url: x")
            gate.end_turn([{"do": "keep"}], [result], url_before="x", url_after=url_after)
            self.assertEqual(gate.gate, 0, result)

    def test_still_in_trouble_at_gate_one_asks_for_the_escalated_turn_then_steps_back(self):
        gate, logged = self.gates()
        failing = ["could not click [4]: timeout"]
        verdicts = []
        for _ in range(3):
            gate.start_turn("url: x")
            verdicts.append(gate.end_turn([{"do": "click", "id": 4}], failing, url_before="x", url_after="x"))
        self.assertEqual(verdicts, [False, False, True])
        self.assertEqual(gate.gate, 2)
        gate.start_turn("url: x")  # the escalated turn
        gate.end_turn([{"do": "click", "id": 5}], ["verification: click visibly changed the page state"],
                      url_before="x", url_after="x")
        self.assertEqual(gate.gate, 1)

    def test_no_escalation_when_the_agent_may_not_escalate(self):
        gate, _ = self.gates()
        results = [gate.end_turn([], ["could not click"], can_escalate=False) for _ in range(4)]
        self.assertFalse(any(results))
        self.assertEqual(gate.gate, 1)

    def test_a_search_without_hits_and_a_repeated_search_are_trouble(self):
        gate, logged = self.gates()
        gate.start_turn("url: list")
        gate.end_turn([{"do": "fill", "id": 3, "text": "Q-1 crates", "enter": True}], ["filled [3]"],
                      url_before="list", url_after="list")
        self.assertEqual(gate.gate, 0)
        gate.start_turn("url: list\nKeine Treffer für Ihre Suche")
        self.assertEqual(gate.gate, 1)
        self.assertIn("search found nothing", logged[-1])
        gate, logged = self.gates()
        for _ in range(2):
            gate.start_turn("url: list")
            gate.end_turn([{"do": "fill", "id": 3, "text": "Q-1  Crates"}], ["filled [3]"], url_before="list",
                          url_after="list")
        self.assertEqual(gate.gate, 1)
        self.assertIn("the same search again", logged[-1])

    def test_waiting_and_repeating_are_trouble(self):
        gate, _ = self.gates()
        gate.end_turn([{"do": "wait"}], ["waited two seconds"])
        self.assertEqual(gate.gate, 1)
        gate, _ = self.gates()
        gate.end_turn([{"do": "click", "id": 1}], ["verification: click visibly changed the page state"], repeated=True)
        self.assertEqual(gate.gate, 1)
        gate, _ = self.gates()
        gate.start_turn("(the page could not be read: boom)")
        self.assertEqual(gate.gate, 1)

    def test_refusals_are_trouble(self):
        for refusal in ("not closed yet. Absent needs a full search", "not finished: you filed evidence",
                        "this document is already filed as material", '"x" is not an open question'):
            gate, _ = self.gates()
            gate.end_turn([{"do": "answer"}], [refusal])
            self.assertEqual(gate.gate, 1, refusal)

    def test_history_is_short_at_gate_zero_only(self):
        gate, _ = self.gates()
        self.assertEqual(gate.history(10), gates_module.GATE0_HISTORY)
        self.assertEqual(gate.history(0), gates_module.GATE0_HISTORY)
        gate.trouble("x")
        self.assertEqual(gate.history(10), 10)
        self.assertEqual(Gates(enabled=False).history(10), 10)

    def test_gates_off_never_change(self):
        gate = Gates(enabled=False, log=lambda text: None)
        gate.trouble("x")
        self.assertFalse(gate.end_turn([{"do": "wait"}], ["could not click"]))
        self.assertEqual(gate.gate, 0)
        self.assertTrue(gate.full())
        with patch.dict(os.environ, {"CONTEXT_GATES": "0"}):
            self.assertFalse(Gates().enabled)


class CaseFileViews(unittest.TestCase):
    def test_sections_follow_the_question_ids_and_filed_kinds(self):
        self.assertEqual(status_sections(["material:R1"]), {"requirements", "materials"})
        self.assertEqual(status_sections(["discount", "packaging_tariff:K"]), {"authorities"})
        self.assertEqual(status_sections(["clarifications"]), {"qa", "positions"})
        self.assertEqual(status_sections([], ["mail"]), {"authorities"})
        self.assertEqual(status_sections(["mailbox"]), set())
        self.assertIn("materials", status_sections(["something_new"]))  # an unknown question sees everything

    def test_status_shows_only_the_wanted_lines_and_always_the_header(self):
        case = filled_case()
        full = case.status()
        self.assertIn("Material P-9", full)
        self.assertIn("Position 1", full)
        header = case.status(set())
        self.assertEqual(header.count("\n"), 0)
        self.assertIn("Tender T-77", header)
        erp = case.status({"requirements", "materials"})
        self.assertIn("Material P-9", erp)
        self.assertIn("Requirement R1", erp)
        self.assertNotIn("Position 1", erp)
        self.assertNotIn("Authorized mail records", erp)

    def test_the_task_card_comes_from_the_context(self):
        case = filled_case()
        card = case.task_card(brief_intro(BRIEF), {"erp": "stock-db", "mail": "post"})
        self.assertIn("Tender T-77", card)
        self.assertIn("Example Works AG", card)
        self.assertIn("stock-db", card)
        self.assertIn("Get the tender from `portal-a`", card)
        self.assertNotIn("Blocked parts", card)  # no bid rules
        self.assertNotIn("boss@", card)  # authorized senders only for the mail agent
        mail = case.task_card("", {}, mail=True)
        self.assertIn('subject "Crates"', mail)
        self.assertIn("sender boss@<mailbox domain>", mail)
        self.assertIn("use the reference plus a word", mail)
        self.assertEqual(CaseFile(BRIEF, {}).task_card("intro"), "")  # no reference: no card

    def test_the_intro_is_the_part_before_the_first_rules_heading(self):
        intro = brief_intro(BRIEF)
        self.assertTrue(intro.startswith("# Quote the widgets"))
        self.assertIn("approvals from `post`", intro)
        self.assertNotIn("Rules", intro)
        self.assertEqual(brief_intro("Bid for T-1. Every rule is in this one sentence."), "")
        self.assertEqual(brief_intro("# Title\n## Rules\n1. x"), "")

    def test_rules_are_picked_by_the_question_topics(self):
        rules = brief_rules(BRIEF, ["packaging_tariff:K"])
        self.assertIn("Packaging: the tariff key", rules)
        self.assertNotIn("Scoring", rules)  # how the bid is scored is no help in finding records
        self.assertNotIn("Blocked parts", rules)
        material = brief_rules(BRIEF, ["material:R1"])
        self.assertIn("Blocked parts do not qualify", material)
        self.assertEqual(brief_rules("One paragraph brief about a widget.", ["material:R1"]),
                         "One paragraph brief about a widget.")
        self.assertEqual(brief_rules(BRIEF, ["unknown_kind"]), "")
        self.assertLessEqual(len(brief_rules(BRIEF, ["material:R1"], limit=80)), 80)


class ClerkBrief(unittest.TestCase):
    def test_the_clerk_gets_the_submission_section_and_the_omit_rule(self):
        text = submit.clerk_brief(BRIEF)
        self.assertIn('Submit in portal-a under "Offer"', text)
        self.assertIn("Partial submission", text)
        self.assertNotIn("Blocked parts", text)
        self.assertNotIn("pw-secret-1", text)

    def test_without_a_submission_heading_the_whole_brief_stays(self):
        plain = BRIEF.replace("## Submission", "## Last words")
        text = submit.clerk_brief(plain)
        self.assertIn("Blocked parts", text)
        self.assertNotIn("pw-secret-1", text)
        with patch.dict(os.environ, {"CONTEXT_GATES": "0"}):
            self.assertIn("Blocked parts", submit.clerk_brief(BRIEF))


def make_investigator(site: str, env: dict | None = None):
    agent.SITE_ROLES.clear()
    agent.SITE_ROLES.update({"erp": "stock-db", "mail": "post"})
    case = filled_case()
    tab = SimpleNamespace(site_id=site, base_url=f"https://{site}.test", page=SimpleNamespace(url=""))
    with patch.dict(os.environ, env or {}):
        inv = agent.investigator(case, SimpleNamespace(hook=lambda name: None), tab, time.time() + 600)
    return case, inv


class InvestigatorContext(unittest.TestCase):
    def tearDown(self):
        agent.SITE_ROLES.clear()

    def test_the_system_prompt_carries_the_task_card_and_the_escalated_one_the_whole_brief(self):
        _, inv = make_investigator("stock-db")
        self.assertIn("TASK CARD", inv.system)
        self.assertNotIn("Blocked parts do not qualify", inv.system)
        self.assertNotIn("The websites hold traps", inv.system)
        self.assertNotIn("pw-secret-1", inv.system)
        self.assertIn("Blocked parts do not qualify", inv.full_system)
        self.assertIn("The websites hold traps", inv.full_system)
        self.assertNotIn("Sign with the code", inv.full_system)  # investigators never get the submission section
        self.assertLess(len(inv.system), len(inv.full_system))

    def test_gates_off_bring_back_the_full_context(self):
        _, inv = make_investigator("stock-db", {"CONTEXT_GATES": "0"})
        self.assertEqual(inv.system, inv.full_system)
        self.assertIn("Position 1", inv.briefing())

    def test_gate_zero_shows_the_header_and_gate_one_the_lines_of_its_questions(self):
        case, inv = make_investigator("stock-db")
        case.questions = lambda: [("data:P-9", "ERP material P-9 still lacks: carton size.")]
        quiet = inv.briefing()
        self.assertIn("Tender T-77", quiet)
        self.assertNotIn("Material P-9:", quiet)
        self.assertNotIn("WHEN YOU CHECK A RECORD", quiet)
        self.assertIn("[data:P-9]", quiet)
        inv.gates.trouble("an action failed")
        busy = inv.briefing()
        self.assertIn("Material P-9:", busy)
        self.assertNotIn("Position 1:", busy)
        self.assertIn("WHEN YOU CHECK A RECORD", busy)
        self.assertIn("THE BRIEF'S RULES ON YOUR QUESTIONS", busy)
        self.assertIn("carton size", busy)
        full = inv.full_briefing()
        self.assertIn("Position 1:", full)

    def test_bad_news_raises_the_gate_for_the_turn_being_built(self):
        case, inv = make_investigator("portal-a")
        case.questions = lambda: [("positions", "the bill of quantities")]
        case.news["portal-a"] = ["From the positions document you filed (x): no usable records were extracted"]
        text = inv.briefing()
        self.assertEqual(inv.gates.gate, 1)
        self.assertIn("WHEN YOU CHECK A RECORD", text)


class TokenBudget(unittest.TestCase):
    def setUp(self):
        self.saved = llm.STATS["prompt_tokens"]
        agent.BUDGET_LOGGED.clear()

    def tearDown(self):
        llm.STATS["prompt_tokens"] = self.saved
        agent.SITE_ROLES.clear()

    def test_near_the_budget_the_briefing_says_finish_and_at_it_the_agent_stops(self):
        case, inv = make_investigator("portal-a")
        case.questions = lambda: []
        with patch.dict(os.environ, {"TOKEN_BUDGET": "1000"}):
            llm.STATS["prompt_tokens"] = 100
            self.assertNotIn("TOKEN BUDGET", inv.briefing())
            self.assertIsNone(inv.stop_when())
            llm.STATS["prompt_tokens"] = 950
            self.assertIn(agent.BUDGET_LINE, inv.briefing())
            self.assertIsNone(inv.stop_when())
            llm.STATS["prompt_tokens"] = 1000
            self.assertEqual(inv.stop_when(), "token budget used up")
            case.pending_by_agent["portal-a"] = 1  # its documents are being read: it may still answer
            self.assertIsNone(inv.stop_when())
        with patch.dict(os.environ, {"TOKEN_BUDGET": "0"}):
            case.pending_by_agent.clear()
            self.assertIsNone(inv.stop_when())


class AgentLoop(unittest.IsolatedAsyncioTestCase):
    async def test_trouble_raises_the_gate_then_the_escalated_turn_has_the_full_system_prompt(self):
        replies = [{"thought": f"type attempt {n}", "actions": [{"do": "fill", "id": 1, "text": f"v{n}"}]}
                   for n in range(4)] + [{"thought": "end", "actions": [{"do": "done", "summary": "end"}]}]
        calls = []

        async def model(messages, **kwargs):
            calls.append({"system": messages[0]["content"], "think": kwargs.get("think"),
                          "user": messages[-1]["content"] if isinstance(messages[-1]["content"], str)
                          else messages[-1]["content"][0]["text"]})
            return replies.pop(0)

        worker = Agent("t", FakeTab(False), "lean system", lambda: "brief view", max_steps=8)
        worker.gates = Gates(enabled=True, log=lambda text: None)
        worker.full_system = "FULL SYSTEM"
        worker.full_briefing = lambda: "full view"
        with patch.object(llm, "chat_json", model), patch.dict(os.environ, {"SANITY_CHECKS": "default"}):
            outcome = await worker.run()
        self.assertTrue(outcome["done"])
        self.assertEqual([c["system"] for c in calls[:3]], ["lean system"] * 3)
        self.assertEqual(calls[3]["system"], "FULL SYSTEM")
        self.assertTrue(calls[3]["think"])
        self.assertTrue(calls[3]["user"].startswith("full view"))
        self.assertEqual(calls[4]["system"], "lean system")

    async def test_stop_when_ends_the_agent_without_a_model_call(self):
        async def model(messages, **kwargs):
            raise AssertionError("no model call expected")

        worker = Agent("t", FakeTab(False), "system", lambda: "job", max_steps=8)
        worker.stop_when = lambda: "token budget used up"
        with patch.object(llm, "chat_json", model):
            outcome = await worker.run()
        self.assertTrue(outcome["done"])
        self.assertEqual(outcome["summary"], "token budget used up")


if __name__ == "__main__":
    unittest.main()
