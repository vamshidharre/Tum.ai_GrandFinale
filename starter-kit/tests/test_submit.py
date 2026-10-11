"""The bid-entry stage against a scripted model and fake tabs: what the clerk is told, how the signing code travels
from the mailbox agent to the clerk, and what `submit_bid` reports."""
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
import submit  # noqa: E402
from calculator import Bid, Line, Material  # noqa: E402

CONTEXT = {"tender_reference": "T-0001", "scenario_date": "2030-03-01T09:00:00+00:00", "company": "Example Works",
           "vat_id": "XX000000001", "email": "sales@bidder.example"}


def a_bid(**changes) -> Bid:
    material = Material(number="M-1", bore_mm=50, stroke_mm=200, pressure_bar=160, mounting="flange",
                        certification="none")
    priced = Line("1", "M-1", 40, unit_price=Decimal("12.50"), lead_time_weeks=3, base_unit_price=Decimal("12.00"),
                  surcharge_per_unit=Decimal("0.50"), packaging_per_unit=Decimal("0"), rebate_total=None,
                  material=material)
    bid = Bid("T-0001", "bid", Decimal("2.5"), [priced], Decimal("500.00"), omitted=["2"])
    for name, value in changes.items():
        setattr(bid, name, value)
    return bid


class FakeTab:
    """As much of a Tab as the agent loop touches; it records the actions it was given."""

    def __init__(self, site_id: str, view: str | None = None):
        self.site_id, self.base_url = site_id, f"http://{site_id}.test"
        self.page = SimpleNamespace(url=self.base_url + "/start", main_frame=self)
        self.document, self.document_source, self.done = "", "", []
        self.view = view or f"url: {self.page.url}\ntitle: {self.site_id}\n[1] button \"go\""

    async def evaluate(self, script):
        return True

    async def observe(self) -> str:
        return self.view

    def take_events(self) -> list[str]:
        return []

    async def click(self, aid) -> str:
        self.done.append(("click", aid))
        return f"clicked [{aid}]"

    async def fill(self, aid, text, enter=False) -> str:
        self.done.append(("fill", aid, text))
        return f"filled [{aid}]"

    async def login(self) -> str:
        self.done.append(("login",))
        return "logged in"


class Script:
    """Stands in for llm.chat_json: answers each agent, named by `label`, with its next scripted reply."""

    def __init__(self, **replies):
        self.replies, self.asked = replies, []

    async def __call__(self, messages, *, think=True, max_tokens=6000, label=""):
        self.asked.append({"label": label, "think": think, "system": messages[0]["content"],
                           "user": messages[-1]["content"]})
        return self.replies[label].pop(0) if self.replies.get(label) else {}

    def turns(self, label: str) -> list[dict]:
        return [call for call in self.asked if call["label"] == label]


def done(**fields) -> dict:
    return {"thought": "", "actions": [{"do": "done", **fields}]}


class TheBidSheet(unittest.TestCase):
    def test_amounts_are_exact_text_and_unknown_values_stay_empty(self):
        sheet = submit.bid_sheet(a_bid())
        line = sheet["positions"][0]
        self.assertEqual((line["unit_price"], line["base_unit_price"], line["surcharge_per_unit"]),
                         ("12.50", "12.00", "0.50"))
        self.assertEqual(line["packaging_per_unit"], "0.00")  # a known zero is entered
        self.assertIsNone(line["rebate_total"])  # an unknown value is left blank
        self.assertEqual((line["quantity"], line["lead_time_weeks"]), (40, 3))
        self.assertEqual((sheet["total_net"], sheet["discount_percent"], sheet["omitted_positions"]),
                         ("500.00", 2.5, ["2"]))

    def test_each_position_carries_the_offered_materials_attributes(self):
        material = submit.bid_sheet(a_bid())["positions"][0]["material"]
        self.assertEqual(material, {"bore_mm": 50, "stroke_mm": 200, "pressure_bar": 160, "mounting": "flange",
                                    "certification": "none"})

    def test_a_no_bid_is_an_empty_sheet_with_a_whole_number_discount(self):
        sheet = submit.bid_sheet(a_bid(decision="no_bid", lines=[], omitted=[], total_net=Decimal("0"),
                                       discount_percent=Decimal("0")))
        self.assertEqual((sheet["decision"], sheet["positions"], sheet["total_net"], sheet["discount_percent"]),
                         ("no_bid", [], "0.00", 0))


class EnteringTheBid(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, script: Script, mail_tab="mail", seconds: float = 900) -> tuple[dict, FakeTab]:
        tender = FakeTab("tender")
        mail = FakeTab("mail") if mail_tab else None
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, mail, a_bid(), CONTEXT, "THE BRIEF", time.time() + seconds)
        return result, tender

    async def test_the_clerk_is_given_the_brief_the_bidder_and_the_sheet(self):
        script = Script(submit=[done(answer="R-1")])
        await self.run_with(script)
        first = script.turns("submit")[0]
        for expected in ("THE BRIEF", "Example Works", "XX000000001", "sales@bidder.example", '"unit_price": "12.50"',
                         '"omitted_positions"', "ask_mail", "http://tender.test"):
            self.assertIn(expected, first["system"])
        self.assertIn("tender T-0001", first["user"])
        self.assertNotIn("TIME IS SHORT", first["user"])

    async def test_the_signing_code_comes_from_an_agent_on_the_mailbox(self):
        script = Script(
            submit=[{"thought": "", "actions": [{"do": "ask_mail", "task": "the code I requested just now"}]},
                    {"thought": "", "actions": [{"do": "fill", "id": 1, "text": "CODE-7"}, {"do": "click", "id": 1}]},
                    done(answer="R-1", summary="received")],
            courier=[done(answer=" CODE-7 ")])
        result, tender = await self.run_with(script)
        courier = script.turns("courier")[0]
        self.assertIn("http://mail.test", courier["system"])
        self.assertIn("T-0001", courier["system"])
        self.assertIn("the code I requested just now", courier["user"])
        self.assertIn('the mailbox colleague answers: "CODE-7"', script.turns("submit")[1]["user"])
        self.assertIn(("fill", 1, "CODE-7"), tender.done)
        self.assertEqual(result, {"submitted": True, "receipt": "R-1", "notes": "received"})

    async def test_a_code_that_was_handed_out_is_not_handed_out_again(self):
        ask = {"thought": "", "actions": [{"do": "ask_mail", "task": "a fresh code"}]}
        script = Script(submit=[ask, ask, done(answer="R-1")], courier=[done(answer="CODE-1"), done(answer="CODE-2")])
        await self.run_with(script)
        first, second = script.turns("courier")
        self.assertNotIn("CODE-1", first["user"])
        self.assertIn("- CODE-1", second["user"])
        self.assertIn("Codes from the mailbox so far, oldest first: CODE-1, CODE-2", script.turns("submit")[2]["user"])

    async def test_the_clerk_hears_when_the_mailbox_has_nothing(self):
        ask = {"thought": "", "actions": [{"do": "ask_mail", "task": "the code"}]}
        script = Script(submit=[ask, done(summary="no code")], courier=[done(summary="no such message")])
        result, _ = await self.run_with(script)
        self.assertIn("the mailbox colleague found nothing: no such message", script.turns("submit")[1]["user"])
        self.assertEqual(result, {"submitted": False, "receipt": "", "notes": "no code"})

    async def test_without_a_mailbox_in_the_run_the_clerk_is_told_so(self):
        ask = {"thought": "", "actions": [{"do": "ask_mail", "task": "the code"}]}
        script = Script(submit=[ask, done(summary="cannot sign")])
        result, _ = await self.run_with(script, mail_tab=None)
        self.assertIn("there is no mailbox in this run", script.turns("submit")[1]["user"])
        self.assertEqual(script.turns("courier"), [])
        self.assertFalse(result["submitted"])

    async def test_a_bid_without_confirmation_is_not_reported_as_submitted(self):
        result, _ = await self.run_with(Script(submit=[]))  # the model never gives a usable reply
        self.assertFalse(result["submitted"])
        self.assertIn("without a confirmation", result["notes"])

    async def test_when_time_is_short_the_clerk_is_told_to_sign_and_submit(self):
        script = Script(submit=[done(answer="R-1")])
        await self.run_with(script, seconds=submit.WRAP_UP - 30)
        self.assertIn("TIME IS SHORT", script.turns("submit")[0]["user"])

    async def test_after_the_deadline_nothing_is_entered(self):
        script = Script(submit=[done(answer="R-1")])
        result, tender = await self.run_with(script, seconds=-1)
        self.assertEqual((script.asked, tender.done, result["submitted"]), ([], [], False))

    async def test_final_confirmation_requires_a_clean_live_checklist(self):
        script = Script(submit=[{"thought": "", "actions": [{"do": "check_submission"}, {"do": "finalize", "id": 1}]},
                                done(answer="R-1")])
        tender = FakeTab("tender", "Tender T-0001\nPosition 1\nTotal 500.00\n[1] button \"Submit\"")
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertIn(("click", 1), tender.done)
        self.assertTrue(result["submitted"])
        # submitting ends the run token: the confirmation is read in code and no further model turn follows
        self.assertEqual(len(script.turns("submit")), 1)
        self.assertIn("Total 500.00", result["receipt"])

    async def test_final_confirmation_is_blocked_by_an_unfilled_required_control(self):
        script = Script(submit=[{"thought": "", "actions": [{"do": "check_submission"}, {"do": "finalize", "id": 1}]},
                                done(summary="needs correction")])
        tender = FakeTab("tender", "Tender T-0001\nPosition 1\nTotal 500.00\n[1] input \"VAT ID\" value=\"\" (required)\n[2] button \"Submit\"")
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertEqual(tender.done, [])
        self.assertIn("PRE-SUBMISSION CHECKLIST: BLOCKED", script.turns("submit")[1]["user"])
        self.assertFalse(result["submitted"])

    async def test_validation_error_can_be_corrected_then_rechecked_before_finalising(self):
        class CorrectingTab(FakeTab):
            async def fill(self, aid, text, enter=False):
                self.view = "Tender T-0001\nPosition 1\nTotal 500.00\n[2] button \"Submit\""
                return await super().fill(aid, text, enter)

        script = Script(submit=[{"thought": "", "actions": [{"do": "check_submission"}]},
                                {"thought": "", "actions": [{"do": "fill", "id": 1, "text": "XX000000001"},
                                                             {"do": "check_submission"}, {"do": "finalize", "id": 2}]},
                                done(answer="R-1")])
        tender = CorrectingTab("tender", "Tender T-0001\nPosition 1\nTotal 500.00\n[1] input \"VAT ID\" value=\"\" (required)")
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertIn(("click", 2), tender.done)
        self.assertIn("PRE-SUBMISSION CHECKLIST: BLOCKED", script.turns("submit")[1]["user"])
        self.assertTrue(result["submitted"])

    async def test_final_step_waits_for_a_page_that_shows_the_bid(self):
        class ReviewTab(FakeTab):
            async def click(self, aid):
                if aid == 5:  # the clerk opens the review step
                    self.view = 'Tender T-0001\nPosition 1\nTotal 500.00\n[2] button "Submit"'
                    return "clicked [5]"
                return await super().click(aid)

        finalize = {"thought": "", "actions": [{"do": "finalize", "id": 2}]}
        script = Script(submit=[finalize, {"thought": "", "actions": [{"do": "click", "id": 5}]}, finalize,
                                done(answer="R-1")])
        tender = ReviewTab("tender", 'Signature\n[1] input "Code" value="X"\n[2] button "Submit"\n[5] link "Review"')
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        refused = script.turns("submit")[1]["user"]
        self.assertIn("PRE-SUBMISSION CHECKLIST: BLOCKED", refused)
        self.assertIn("NOT SHOWN: tender reference T-0001", refused)
        self.assertIn("Final submission was not pressed", refused)
        self.assertEqual(tender.done, [("click", 2)])  # pressed once, on the page that showed the bid
        self.assertTrue(result["submitted"])

    async def test_missing_review_evidence_never_leaves_a_bid_unsigned(self):
        finalize = {"thought": "", "actions": [{"do": "finalize", "id": 2}]}
        page = 'Signature\n[1] input "Code" value="X"\n[2] button "Submit"'
        tender = FakeTab("tender", page)
        script = Script(submit=[finalize] * (submit.UNPROVEN_REFUSALS + 1) + [done(answer="R-1")])
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertEqual(tender.done, [("click", 2)])  # held back twice, then pressed
        self.assertTrue(result["submitted"])

        tender = FakeTab("tender", page)  # with little time left it is pressed at once
        script = Script(submit=[finalize, done(answer="R-1")])
        with patch.object(llm, "chat_json", script):
            await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + submit.WRAP_UP - 30)
        self.assertEqual(tender.done, [("click", 2)])

    async def test_a_visible_error_blocks_the_final_step_however_often_it_is_tried(self):
        finalize = {"thought": "", "actions": [{"do": "finalize", "id": 2}]}
        tender = FakeTab("tender", 'Tender T-0001\nPosition 1\nTotal 500.00\n(error) code rejected\n[2] button "Submit"')
        script = Script(submit=[finalize] * (submit.UNPROVEN_REFUSALS + 3) + [done(summary="blocked")])
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertEqual(tender.done, [])
        self.assertFalse(result["submitted"])

    async def test_clerk_can_request_a_fresh_tan_after_a_delayed_mailbox_delivery(self):
        ask = {"thought": "", "actions": [{"do": "ask_mail", "task": "a fresh signing code"}]}
        script = Script(submit=[ask, ask, done(answer="R-1")],
                        courier=[done(summary="the new message has not arrived"), done(answer="CODE-2")])
        await self.run_with(script)
        self.assertIn("found nothing", script.turns("submit")[1]["user"])
        self.assertIn('answers: "CODE-2"', script.turns("submit")[2]["user"])

    async def test_a_second_code_request_before_the_code_came_back_is_blocked(self):
        # sandbox run a85be021: request, the courier finds nothing once, the clerk requests again (voiding the first
        # code) and the deadline hits. The second click must not reach the portal until the courier searched twice.
        request = {"thought": "", "actions": [{"do": "click", "id": 7}]}
        ask = {"thought": "", "actions": [{"do": "ask_mail", "task": "the signing code"}]}
        script = Script(submit=[request, ask, request, done(summary="stopped")],
                        courier=[done(summary="the new message has not arrived")])
        tender = FakeTab("tender", 'Signing\n[7] button "Request TAN"')
        with patch.object(llm, "chat_json", script):
            await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertEqual([step for step in tender.done if step == ("click", 7)], [("click", 7)])
        self.assertIn("blocked", script.turns("submit")[3]["user"])

    async def omitting(self, seconds: float, replies: list[dict], position: str = "1") -> tuple[FakeTab, Script]:
        script = Script(submit=[*replies, done(summary="stopped")])
        tender = FakeTab("tender", f'Position {position}\n[21] button "Position auslassen (Teilpunkte)"\n'
                                   '[24] button "Weiter"\n[25] input "Menge"')
        tender.document = tender.view  # the page as last read, as Tab.observe keeps it
        with patch.object(llm, "chat_json", script):
            await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + seconds)
        return tender, script

    @staticmethod
    def one_after_another() -> list[dict]:
        # run 8a2e1426: stuck on one position, the clerk left out that one and then every position after it, each
        # straight away, without trying to enter it
        omit = lambda n: {"thought": f"leave out position {n}", "actions": [{"do": "click", "id": 21}]}
        typing = lambda n: {"thought": f"quantity try {n}", "actions": [{"do": "fill", "id": 25, "text": str(40 + n)}]}
        return [omit(1), typing(1), omit(2), typing(2), omit(3)]

    async def test_only_the_stuck_position_may_be_left_out_beyond_the_calculators(self):
        tender, script = await self.omitting(900, self.one_after_another())
        # a_bid() has one position the calculator left out, so two omits are allowed and the third is refused
        self.assertEqual([step for step in tender.done if step == ("click", 21)], [("click", 21)] * 2)
        self.assertIn("has its values on the bid sheet", script.turns("submit")[5]["user"])

    async def test_when_turns_run_out_the_clerk_is_told_to_sign_and_may_leave_positions_out(self):
        # run 15a3aaf9: a four-step cycle on one position ran the clerk into its step cap, unsigned
        with patch.object(submit, "CLERK_STEPS", 30), patch.object(submit, "TURNS_RESERVE", 26):
            tender, script = await self.omitting(900, self.one_after_another())
        users = [turn["user"] for turn in script.turns("submit")]
        self.assertNotIn("TURNS ARE RUNNING OUT", users[0])
        self.assertTrue(any("YOUR TURNS ARE RUNNING OUT" in user for user in users[4:]))
        self.assertEqual(len([step for step in tender.done if step == ("click", 21)]), 3)  # the cap no longer holds

    async def test_a_form_position_that_is_not_on_the_bid_sheet_may_always_be_left_out(self):
        # run 9ac58b69: the form held an extra position the bid does not have; leaving it out is right
        tender, script = await self.omitting(900, self.one_after_another(), position="EXTRA-9")
        self.assertEqual(len([step for step in tender.done if step == ("click", 21)]), 3)
        self.assertNotIn("has its values on the bid sheet", "".join(t["user"] for t in script.turns("submit")))

    async def test_a_clerk_caught_going_in_circles_again_may_leave_out_one_more(self):
        # run 012496d3: stuck on a second position, the refused omit became a loop of its own
        omit = {"thought": "the form is stuck, leave this position out", "actions": [{"do": "click", "id": 21}]}
        tender, _ = await self.omitting(900, [omit] * 6)
        self.assertEqual(len([step for step in tender.done if step == ("click", 21)]), 3)

    async def test_when_time_is_short_positions_may_still_be_left_out(self):
        tender, _ = await self.omitting(submit.WRAP_UP - 30, self.one_after_another())
        self.assertEqual([step for step in tender.done if step == ("click", 21)], [("click", 21)] * 3)

    async def test_other_controls_are_not_counted_as_omitting(self):
        go = {"thought": "", "actions": [{"do": "click", "id": 24}]}
        script = Script(submit=[go, go, go, go, done(summary="stopped")])
        tender = FakeTab("tender", 'Position\n[21] button "Position auslassen (Teilpunkte)"\n[24] button "Weiter"')
        with patch.object(llm, "chat_json", script):
            await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertEqual(tender.done.count(("click", 24)), 4)

    async def test_the_circle_hint_restarts_the_position_and_omits_only_that_one(self):
        self.assertIn("start this position over", submit.CLERK_CIRCLE_HINT)
        self.assertIn("THIS ONE position", submit.CLERK_CIRCLE_HINT)

    async def test_clerk_recovers_from_an_interrupted_session_before_review(self):
        class RestoringTab(FakeTab):
            async def login(self):
                self.view = 'Tender T-0001\nPosition 1\nTotal 500.00\n[2] button "Submit"'
                return await super().login()

        script = Script(submit=[{"thought": "", "actions": [{"do": "login"}]},
                                {"thought": "", "actions": [{"do": "check_submission"}, {"do": "finalize", "id": 2}]},
                                done(answer="R-1")])
        tender = RestoringTab("tender", 'Your session has expired\n[1] button "Log in again"')
        with patch.object(llm, "chat_json", script):
            result = await submit.submit_bid(tender, FakeTab("mail"), a_bid(), CONTEXT, "THE BRIEF", time.time() + 900)
        self.assertIn(("login",), tender.done)
        self.assertIn(("click", 2), tender.done)
        self.assertTrue(result["submitted"])


if __name__ == "__main__":
    unittest.main()
