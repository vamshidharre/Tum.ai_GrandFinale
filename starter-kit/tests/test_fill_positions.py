"""fill_positions (submit.py): the clerk enters one position by hand, then hands its steps over as a template; code
enters every further position the form shows with that position's values from the bid sheet. Runs 15a3aaf9 and
49f7be51 spent 35-100 clerk turns on the positions and looped. Invented form and values only."""
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


def line(position, number, quantity, price, weeks):
    material = Material(number=number, bore_mm=50, stroke_mm=200, pressure_bar=160, mounting="flange",
                        certification="none")
    return Line(position, number, quantity, unit_price=Decimal(price), lead_time_weeks=weeks, base_unit_price=None,
                surcharge_per_unit=None, packaging_per_unit=None, rebate_total=None, material=material)


def three_positions() -> Bid:
    lines = [line("1", "M-1", 4, "10.50", 2), line("2", "M-2", 30, "1234.56", 6), line("3", "M-3", 7, "99.00", 1)]
    return Bid("T-0001", "bid", Decimal("0"), lines, Decimal("38099.80"), omitted=[])


TEMPLATE = [{"field": "Material", "value": "{material_number}"}, {"field": "Menge", "value": "{quantity}"},
            {"field": "Preis", "value": "{unit_price|de}"}, {"field": "Lieferzeit", "value": "{lead_time_weeks}"},
            {"press": "Weiter"}]


class PositionForm:
    """Shows one position at a time ("# Position <n>"); its element ids change on every page. "Weiter" takes the
    entered values when all are there and moves on, else it shows an error and stays."""

    def __init__(self, order=("1", "2", "3"), refuse=None):
        self.order, self.index, self.offset, self.refuse = list(order), 0, 0, refuse
        self.base_url, self.site_id = "http://form.test", "tender"
        self.page = SimpleNamespace(url=self.base_url + "/form", main_frame=SimpleNamespace(evaluate=self._alive))
        self.document, self.document_source, self.current, self.saved, self.error = "", "", {}, {}, ""

    async def _alive(self, script):
        return True

    def take_events(self):
        return []

    def ids(self):
        return {name: str(i + self.offset) for i, name in enumerate(("material", "qty", "price", "weeks", "next"))}

    async def observe(self):
        i = self.ids()
        if self.index >= len(self.order):
            view = 'url: http://form.test/form\n# Konditionen\n[90] input "Rabatt" value=""'
        else:
            view = "\n".join(["url: http://form.test/form", f"# Angebot - Position {self.order[self.index]}",
                              f'[{i["material"]}] select "Material" options: "M-1"; "M-2"; "M-3"',
                              f'[{i["qty"]}] input "Menge" value=""', f'[{i["price"]}] input "Preis" value=""',
                              f'[{i["weeks"]}] input "Lieferzeit" value=""', f'[{i["next"]}] button "Weiter"']
                             + ([f"(alert) {self.error}"] if self.error else []))
        self.document = view
        return view

    async def select(self, aid, value):
        self.current["material"] = value
        return f'selected "{value}"'

    async def fill(self, aid, text, enter=False):
        key = {v: k for k, v in self.ids().items()}.get(str(aid))
        self.current[key] = text
        return f"filled [{aid}]"

    async def click(self, aid):
        if str(aid) != self.ids()["next"]:
            return f"[{aid}] not found"
        position = self.order[self.index]
        if position == self.refuse or len(self.current) < 4:
            self.error, self.offset = "Bitte alle Felder ausfuellen", self.offset + 10
            return "clicked; the page reloaded"
        self.saved[position], self.current, self.error = dict(self.current), {}, ""
        self.index, self.offset = self.index + 1, self.offset + 10
        return "clicked; the page reloaded"


class Script:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    async def __call__(self, messages, *, think=True, max_tokens=6000, label=""):
        self.seen.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else {"thought": "", "actions": [{"do": "done", "summary": "x"}]}


async def run(form, replies):
    script = Script(replies)
    with patch.object(llm, "chat_json", script):
        await submit.submit_bid(form, None, three_positions(), CONTEXT, "THE BRIEF", time.time() + 900)
    return script


def replay(done=("1",)):
    return {"thought": "position 1 is in; the others the same way",
            "actions": [{"do": "fill_positions", "done": list(done), "template": TEMPLATE}]}


class FillPositions(unittest.IsolatedAsyncioTestCase):
    async def test_every_further_position_is_entered_with_its_own_values(self):
        form = PositionForm()
        form.index = 1  # position 1 was entered by hand
        script = await run(form, [replay()])
        self.assertEqual(form.saved, {
            "2": {"material": "M-2", "qty": "30", "price": "1234,56", "weeks": "6"},
            "3": {"material": "M-3", "qty": "7", "price": "99,00", "weeks": "1"}})
        self.assertIn("no further bid position is shown", script.seen[1])

    async def test_the_form_order_decides_which_values_go_in(self):
        form = PositionForm(order=("1", "3", "2"))
        form.index = 1
        await run(form, [replay()])
        self.assertEqual(form.saved["3"]["material"], "M-3")
        self.assertEqual(form.saved["2"]["price"], "1234,56")

    async def test_a_position_the_form_does_not_accept_stops_the_replay(self):
        form = PositionForm(refuse="2")
        form.index = 1
        script = await run(form, [replay()])
        self.assertEqual(form.saved, {})
        self.assertIn("did not move on", script.seen[1])

    async def test_positions_entered_by_hand_are_never_entered_again(self):
        form = PositionForm()  # still shows position 1
        script = await run(form, [replay(done=("1",))])
        self.assertEqual(form.saved, {})
        self.assertIn("no further bid position is shown", script.seen[1])


FIRST_BY_HAND = {"thought": "position 1 in one go", "actions": [{"do": "steps", "steps": [
    {"field": "Material", "value": "M-1"}, {"field": "Menge", "value": "4"}, {"field": "Preis", "value": "10,50"},
    {"field": "Lieferzeit", "value": "2"}, {"press": "Weiter"}]}]}


class AutomaticReplay(unittest.IsolatedAsyncioTestCase):
    async def test_after_the_first_position_code_enters_the_others_without_being_asked(self):
        form = PositionForm()
        script = await run(form, [FIRST_BY_HAND])
        self.assertEqual(form.saved["1"], {"material": "M-1", "qty": "4", "price": "10,50", "weeks": "2"})
        self.assertEqual(form.saved["2"], {"material": "M-2", "qty": "30", "price": "1234,56", "weeks": "6"})
        self.assertEqual(form.saved["3"], {"material": "M-3", "qty": "7", "price": "99,00", "weeks": "1"})
        self.assertIn("entered automatically", script.seen[1])
        self.assertEqual(len(script.seen), 2)  # one model turn for all three positions, then done

    async def test_single_actions_by_id_are_learned_as_well(self):
        # run 472732d2: the clerk entered the first position with single selects, fills and clicks, never steps
        form = PositionForm()
        by_hand = {"thought": "position 1 field by field", "actions": [
            {"do": "select", "id": 0, "value": "M-1"}, {"do": "fill", "id": 1, "text": "4"},
            {"do": "fill", "id": 2, "text": "10,50"}, {"do": "fill", "id": 3, "text": "2"}, {"do": "click", "id": 4}]}
        script = await run(form, [by_hand])
        self.assertEqual(form.saved["2"], {"material": "M-2", "qty": "30", "price": "1234,56", "weeks": "6"})
        self.assertEqual(form.saved["3"], {"material": "M-3", "qty": "7", "price": "99,00", "weeks": "1"})
        self.assertEqual(len(script.seen), 2)

    async def test_nothing_is_replayed_while_the_form_stays_on_the_position(self):
        form = PositionForm(refuse="1")
        script = await run(form, [FIRST_BY_HAND])
        self.assertEqual(form.saved, {})
        self.assertNotIn("entered automatically", script.seen[1])


class StayingInStep(unittest.IsolatedAsyncioTestCase):
    async def test_each_turn_names_the_position_the_form_shows_and_its_values(self):
        # run 63f3be2e: the clerk typed the sheet's first position into the form showing another one
        form = PositionForm(order=("3", "1", "2"))
        script = await run(form, [])
        self.assertIn("THE FORM SHOWS POSITION 3 NOW", script.seen[0])
        self.assertIn('"material_number": "M-3"', script.seen[0])

    async def test_a_button_the_form_refuses_twice_is_blocked(self):
        # run 63f3be2e: "Position hinzufügen" pressed 10 times, refused each time
        form = PositionForm(refuse="1")
        press = lambda aid: {"thought": f"save it ({aid})", "actions": [{"do": "click", "id": aid}]}
        script = await run(form, [press(4), press(14), press(24)])
        self.assertIn('the form refused "Weiter" twice', script.seen[3])
        self.assertEqual(form.offset, 20)  # the third press never reached the form


class LearningTheTemplate(unittest.TestCase):
    LINE = {"position": "1", "material_number": "M-1", "quantity": 4, "unit_price": "1906.85", "lead_time_weeks": 3,
            "surcharge_per_unit": "0.00", "rebate_total": "0.00", "material": {"bore_mm": 50, "certification": None}}

    def test_typed_values_become_placeholders(self):
        steps = [{"field": "Bore", "value": "50"}, {"field": "Qty", "value": "4"},
                 {"field": "Price", "value": "1.906,85"}, {"press": "Next"}]
        template, ambiguous = submit.templatize(steps, self.LINE)
        self.assertEqual(template, [{"field": "Bore", "value": "{bore_mm}"}, {"field": "Qty", "value": "{quantity}"},
                                    {"field": "Price", "value": "{unit_price|de}"}, {"press": "Next"}])
        self.assertEqual(ambiguous, [])

    def test_the_forms_word_for_no_certificate_is_learned(self):
        template, _ = submit.templatize([{"field": "Certificate", "value": "Without certificate"}], self.LINE)
        self.assertEqual(template, [{"field": "Certificate", "value": "{certification|none=Without certificate}"}])

    def test_a_value_shared_by_two_fields_is_not_guessed(self):
        template, ambiguous = submit.templatize([{"field": "Rebate for the position", "value": "0,00"}], self.LINE)
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0][2], ["{rebate_total|de}", "{surcharge_per_unit|de}"])


class ResolvingAmbiguity(unittest.IsolatedAsyncioTestCase):
    async def test_the_model_names_the_field_from_its_label(self):
        template = [{"field": "Rebate for the position", "value": "0,00"}]
        ambiguous = [(0, "Rebate for the position", ["{rebate_total|de}", "{surcharge_per_unit|de}"])]

        async def model(messages, **kwargs):
            return {"choices": [{"field": "Rebate for the position", "placeholder": "{rebate_total|de}"}]}

        with patch.object(llm, "chat_json", model):
            resolved = await submit.resolve_template(template, ambiguous)
        self.assertEqual(resolved, [{"field": "Rebate for the position", "value": "{rebate_total|de}"}])

    async def test_an_answer_outside_the_candidates_stops_the_replay(self):
        async def model(messages, **kwargs):
            return {"choices": [{"field": "X", "placeholder": "{quantity}"}]}

        with patch.object(llm, "chat_json", model):
            self.assertIsNone(await submit.resolve_template([{"field": "X", "value": "0"}],
                                                            [(0, "X", ["{a}", "{b}"])]))


class TheTemplate(unittest.TestCase):
    LINE = {"position": "2", "material_number": "M-2", "quantity": 30, "unit_price": "1234.56", "lead_time_weeks": 6,
            "rebate_total": None, "material": {"bore_mm": 50, "certification": None}}

    def test_placeholders_take_the_lines_values(self):
        steps = submit.fill_template([{"field": "Bore", "value": "{bore_mm}"},
                                      {"field": "Price", "value": "{unit_price|de}"}, {"press": "Next"}], self.LINE)
        self.assertEqual(steps, [{"field": "Bore", "value": "50"}, {"field": "Price", "value": "1234,56"},
                                 {"press": "Next"}])

    def test_a_null_value_leaves_its_field_out_unless_the_template_names_a_text(self):
        steps = submit.fill_template([{"field": "Rebate", "value": "{rebate_total}"},
                                      {"field": "Certificate", "value": "{certification|none=Without}"}], self.LINE)
        self.assertEqual(steps, [{"field": "Certificate", "value": "Without"}])

    def test_the_shown_position_is_read_from_the_heading(self):
        page = 'Positions: 1, 2, 3\n# Offer - Position 3\n[4] input "Qty"'
        self.assertEqual(submit.shown_position(page, ["2", "3"]), "3")

    def test_a_step_number_or_an_option_is_no_position(self):
        page = '# Schritt 3 von 8\nPosition 2\n[4] select "Material" options: "M-3"'
        self.assertEqual(submit.shown_position(page, ["2", "3"]), "2")

    def test_a_position_already_entered_is_not_replaced_by_another(self):
        page = '# Angebot - Position 1\n[4] select "Material" options: "M-2"; "M-3"'
        self.assertIsNone(submit.shown_position(page, ["2", "3"], ["1", "2", "3"]))


if __name__ == "__main__":
    unittest.main()
