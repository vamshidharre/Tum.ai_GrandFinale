"""Several agents on one website, mail records fetched ahead of their questions, and an investigation that ends
when nothing is open (agent.py, casefile.py, worker.py).

Sandbox run 9902e32b (100 in 6.3 min): the ERP agent read its materials one click per model turn, 58 turns in a row,
and the mailbox agent its messages one kind after another, 36 turns; the investigation waited for both. All values
here are invented."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # the fakes of the neighbouring test files

import agent  # noqa: E402
import llm  # noqa: E402
from agent import Team, questions_for_site, share_out, work_share  # noqa: E402
from casefile import CaseFile  # noqa: E402
from test_steps_action import STEPS, CascadeForm  # noqa: E402
from worker import Agent  # noqa: E402


def reset():
    agent.SITE_ROLES.clear()
    agent.RELEASED.clear()
    agent.POOLS.clear()
    agent.CLAIMS.clear()
    agent.LOOKING_AT.clear()
    agent.SITE_PAGES.clear()
    agent.LOOKED_AROUND.clear()


class SharedWebsite(unittest.TestCase):
    setUp = tearDown = staticmethod(reset)

    def test_materials_are_shared_out_one_agent_each(self):
        agent.POOLS["erp"] = ["erp", "erp#2", "erp#3"]
        questions = [(f"material:REQ-{n}", "m") for n in (1, 2, 3)] + [("discount", "d")]
        seen = {name: [q for q, _ in questions_for_site(name, questions)] for name in agent.POOLS["erp"]}
        # the further agents first, the website's first agent (the one that looked around) last
        self.assertEqual(seen, {"erp": ["material:REQ-3"], "erp#2": ["material:REQ-1"], "erp#3": ["material:REQ-2"]})
        # asked again in another order, every agent still has the share it took
        again = {name: [q for q, _ in questions_for_site(name, questions[::-1])] for name in agent.POOLS["erp"]}
        self.assertEqual(again, seen)

    def test_the_agent_looking_at_a_material_gets_the_question_about_its_data(self):
        agent.POOLS["erp"] = ["erp", "erp#2"]
        agent.LOOKING_AT.update({"erp": lambda: "search", "erp#2": lambda: "url: /material?id=MAT-77\nMAT-77 status"})
        self.assertEqual(questions_for_site("erp", [("data:MAT-77", "d")]), [])
        self.assertEqual([q for q, _ in questions_for_site("erp#2", [("data:MAT-77", "d")])], ["data:MAT-77"])

    def test_every_key_of_one_kind_of_mail_record_is_one_share(self):
        self.assertEqual(work_share("condition_group:K-1"), work_share("condition_group:K-2"))
        self.assertEqual(work_share("condition_group:K-1"), work_share("topic:condition_rules"))
        self.assertNotEqual(work_share("index:M-1"), work_share("fx:F-1"))
        agent.POOLS["mail"] = ["mail", "mail#2"]
        questions = [("topic:condition_rules", "t"), ("topic:fx_fixing", "t"), ("condition_group:K-1", "c"), ("fx:F-1", "f")]
        self.assertEqual([q for q, _ in questions_for_site("mail#2", questions)], ["topic:condition_rules", "condition_group:K-1"])
        self.assertEqual([q for q, _ in questions_for_site("mail", questions)], ["topic:fx_fixing", "fx:F-1"])

    def test_the_share_of_an_agent_that_ended_goes_to_the_others(self):
        agent.POOLS["erp"] = ["erp", "erp#2"]
        questions = [("material:REQ-1", "m"), ("material:REQ-2", "m")]
        self.assertEqual([q for q, _ in questions_for_site("erp#2", questions)], ["material:REQ-1"])
        agent.retire("erp#2")
        self.assertEqual([q for q, _ in questions_for_site("erp", questions)], ["material:REQ-1", "material:REQ-2"])

    def test_a_website_without_a_role_is_not_shown_what_the_erp_and_the_mailbox_are_routed_for(self):
        agent.SITE_ROLES.update({"erp": "stock", "mail": "post"})
        questions = [("positions", "p"), ("material:REQ-1", "m"), ("discount", "d"), ("topic:fx_fixing", "t")]
        self.assertEqual([q for q, _ in questions_for_site("portal", questions)], ["positions"])
        agent.RELEASED.add("material:REQ-1")  # the ERP agents gave it up: now everyone is asked
        self.assertEqual([q for q, _ in questions_for_site("portal", questions)], ["positions", "material:REQ-1"])


class KnownPages(unittest.TestCase):
    """What one agent of a website found, the others open directly."""
    setUp = tearDown = staticmethod(reset)

    def test_one_example_is_kept_per_kind_of_page(self):
        agent.saw_page("stock", "https://stock.test/item?no=MAT-1&view=technical", "Item MAT-1")
        agent.saw_page("stock", "https://stock.test/item?no=MAT-2&view=planning", "Item MAT-2")
        agent.saw_page("stock", "https://stock.test/item?no=MAT-3&view=technical", "Item MAT-3")  # the same kind again
        agent.saw_page("stock", "https://stock.test/find?q=GRP-9", "Search")
        self.assertEqual(agent.known_pages("stock"),
                         "- /item?no=MAT-2&view=planning  (Item MAT-2)\n- /item?no=MAT-3&view=technical  (Item MAT-3)\n"
                         "- /find?q=GRP-9  (Search)")
        self.assertEqual(agent.known_pages("post"), "")  # another website has pages of its own

    def test_a_page_that_only_leads_on_is_not_listed(self):
        agent.saw_page("stock", "https://stock.test/login?back=%2Fitem%3Fno%3DMAT-1", "Login")
        agent.saw_page("stock", "about:blank")
        self.assertEqual(agent.known_pages("stock"), "")

    def test_the_erp_agent_looks_around_until_its_first_question_is_open(self):
        case = SimpleNamespace(questions=lambda: list(case.open), open=[("positions", "p")])
        agent.POOLS["erp"] = ["erp", "erp#2"]
        self.assertEqual([q for q, _ in questions_for_site("erp", agent.open_work(case))], ["explore:erp"])
        self.assertEqual(questions_for_site("erp#2", agent.open_work(case)), [])  # one agent looks around, not all
        case.open.append(("material:REQ-1", "m"))
        self.assertNotIn("explore:erp", [q for q, _ in agent.open_work(case)])
        case.open.remove(("material:REQ-1", "m"))
        self.assertNotIn("explore:erp", [q for q, _ in agent.open_work(case)])  # over for good


class FetchedAhead(unittest.TestCase):
    def case(self):
        return CaseFile("brief", {"tender_reference": "T-0001", "scenario_date": "2031-03-10T09:00:00+00:00",
                                  "authorized_senders": {"fx_fixing": "treasury", "packaging_tariffs": "shipping"}})

    def test_a_record_is_asked_for_from_the_start_for_every_sender_the_brief_names(self):
        case = self.case()
        self.assertEqual([qid for qid, _ in case.prefetch()], ["topic:packaging_tariffs", "topic:fx_fixing"])
        self.assertNotIn("topic:fx_fixing", [qid for qid, _ in case.questions()])  # it never holds the investigation up

    def test_it_closes_once_an_authorized_message_of_that_kind_is_on_file_or_is_answered_absent(self):
        case = self.case()
        case.merge("mail", {"messages": [{"sender": "treasury@firm.example", "timestamp": "2031-03-09T10:00:00+00:00",
                                          "subject": "FX", "tender_reference": "T-0001", "topic": "fx_fixing",
                                          "fx_fixing": [{"key": "F-1", "currency": "USD", "eur_per_unit": "0,9"}]}]})
        self.assertEqual([qid for qid, _ in case.prefetch()], ["topic:packaging_tariffs"])
        self.assertIn("closed as absent", case.answer("topic:packaging_tariffs", "absent", "searched both folders"))
        self.assertEqual(case.prefetch(), [])

    def test_a_message_for_another_reference_does_not_close_it(self):
        case = self.case()
        case.merge("mail", {"messages": [{"sender": "treasury@firm.example", "timestamp": "2031-03-09T10:00:00+00:00",
                                          "subject": "FX", "tender_reference": "T-0001-A", "topic": "fx_fixing",
                                          "fx_fixing": [{"key": "F-1", "currency": "USD", "eur_per_unit": "0,9"}]}]})
        self.assertIn("topic:fx_fixing", [qid for qid, _ in case.prefetch()])


class Investigation(unittest.IsolatedAsyncioTestCase):
    setUp = tearDown = staticmethod(reset)

    async def test_it_ends_when_no_question_is_open_and_nothing_is_being_read(self):
        case = SimpleNamespace(pending=1, questions=lambda: [])
        team = Team(case, ["erp"], time.time() + 5)
        watching = asyncio.create_task(team.watch())
        await asyncio.sleep(0.7)
        self.assertFalse(team.finished.is_set())  # a filed document is still being read
        case.pending = 0
        await asyncio.wait_for(watching, 2)
        self.assertTrue(team.finished.is_set())

    async def test_a_further_agent_starts_only_when_a_share_is_its_own(self):
        class Case:
            version, pending, open = 0, 0, []
            def questions(self):
                return self.open
            async def wait_change(self, seen, timeout):
                await asyncio.sleep(0.01)
                return self.version != seen
        case = Case()
        agent.POOLS["erp"] = ["erp", "erp#2"]
        team = Team(case, ["erp", "erp#2", "mail"], time.time() + 5)
        waiting = asyncio.create_task(team.wait_for_work("erp#2"))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        case.open, case.version = [("discount", "d")], 1  # a question for another website: still nothing for it
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        case.open, case.version = case.open + [("material:REQ-1", "m")], 2
        self.assertTrue(await asyncio.wait_for(waiting, 2))

    async def test_an_agent_without_work_is_held_before_the_model_is_asked(self):
        asked, held = [], []

        async def model(messages, **kwargs):
            asked.append(1)
            return {"thought": f"look {len(asked)}", "actions": [{"do": "select", "id": 0, "value": "SML"[len(asked) % 3]}]}

        async def idle(why):
            held.append(why)
            return None

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("erp", CascadeForm(), "system", lambda: "job", idle=idle, work=lambda: False).run()
        self.assertEqual((len(asked), held), (3, ["no work"]))  # its first turns (login, consent), then no more calls
        self.assertTrue(outcome["done"])


class FormChain(unittest.IsolatedAsyncioTestCase):
    async def test_the_page_is_not_read_again_before_a_field_that_is_only_typed_into(self):
        class Counting(CascadeForm):
            reads = 0
            async def observe(self):
                self.reads += 1
                return await super().observe()
        form = Counting()
        result = await Agent("clerk", form, "system", lambda: "job")._steps(
            STEPS[:4] + [{"field": "Unit price", "value": "1,00"}, {"field": "Unit price", "value": "123,45"}, {"press": "Continue"}])
        self.assertIn("ran all 7 steps", result)
        self.assertEqual(form.values, {"size": "M", "finish": "matt", "price": "123,45", "continued": True})
        self.assertEqual(form.reads, 6)  # before step 1, after each of the 3 presses, after each of the 2 choices


if __name__ == "__main__":
    unittest.main()
