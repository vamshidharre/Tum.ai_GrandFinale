"""The clerk's enter_positions action (submit.py): the model writes the steps of one position once, with bid-sheet
placeholders, and code enters every position the form asks for, in the form's own order.

Scored run a176359f: 25 of the clerk's 35 turns (~100 s of a 6:15 run) went on five positions of a configurator,
one choice per turn. The form here is invented: a cascade (bore, apply, certificate, apply), then the amounts and a
save button; it asks for the positions in an order of its own, and every element id changes on every reload."""
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
from preflight import check  # noqa: E402

CONTEXT = {"tender_reference": "T-0001", "scenario_date": "2030-03-01T09:00:00+00:00", "company": "Example Works",
           "vat_id": "XX000000001", "email": "sales@bidder.example"}


def line(position, material, bore, certification, quantity, price):
    return Line(position, material, quantity, unit_price=Decimal(price), lead_time_weeks=4,
                base_unit_price=Decimal(price), surcharge_per_unit=Decimal("1.50"), packaging_per_unit=None,
                rebate_total=Decimal("0"),
                material=Material(number=material, bore_mm=bore, stroke_mm=300, pressure_bar=200, mounting="flange",
                                  certification=certification))


def two_positions() -> Bid:
    lines = [line("7.10", "M-A", 40, None, 12, "1234.50"), line("7.20", "M-B", 63, "EN 9", 30, "99.95")]
    return Bid("T-0001", "bid", Decimal("0"), lines, Decimal("17812.50"), omitted=[])


class Configurator:
    """Asks for 7.20 first, then 7.10. Choose a bore, apply; choose a certificate, apply; then material, quantity
    and price, and "Save and next". `errors` maps a position to the message its save is refused with."""

    def __init__(self, order=("7.20", "7.10"), errors=None):
        self.site_id, self.base_url = "tender", "http://tender.test"
        self.page = SimpleNamespace(url=self.base_url + "/bid", main_frame=self)
        self.document, self.document_source = "", ""
        self.order, self.errors = list(order), dict(errors or {})
        self.stage, self.offset, self.alert = 0, 0, ""
        self.current: dict = {}
        self.saved: dict[str, dict] = {}
        self.typed: list[str] = []

    async def evaluate(self, script):
        return True

    def take_events(self):
        return []

    def ids(self):
        names = ("bore", "apply", "cert", "material", "quantity", "price", "save")
        return {name: str(index + self.offset) for index, name in enumerate(names)}

    async def observe(self) -> str:
        if not self.order:
            return f"url: {self.base_url}/terms\ntitle: Terms\n# Conditions\n[{self.offset}] input \"Discount\" value=\"\""
        i, position = self.ids(), self.order[0]
        lines = [f"url: {self.base_url}/bid", "title: Bid form", f"# Position {position}"]
        if self.alert:
            lines.append(f"(status) {self.alert}")
        lines.append(f'[{i["bore"]}] select "Bore (mm)" options(* = selected): "40"; "63"')
        lines.append(f'[{i["apply"]}] button "Apply selection"')
        if self.stage >= 1:
            lines.append(f'[{i["cert"]}] select "Certificate" options(* = selected): "no certificate"; "EN 9"')
        if self.stage >= 2:
            lines.append(f'[{i["material"]}] select "Catalogue number" options(* = selected): "M-A"; "M-B"')
            lines.append(f'[{i["quantity"]}] input "Quantity (pcs)" value=""')
            lines.append(f'[{i["price"]}] input "Unit price net (EUR)" value=""')
            lines.append(f'[{i["save"]}] button "Save and next"')
        lines.append("Positions of this tender: 7.10, 7.20")  # an overview that names every position
        return "\n".join(lines)

    def _key(self, aid):
        return next((name for name, number in self.ids().items() if number == str(aid)), None)

    async def select(self, aid, value):
        key = self._key(aid)
        if key not in ("bore", "cert", "material"):
            return f"[{aid}] is not a list on this page"
        self.current[key] = value
        return f'selected "{value}" in [{aid}]'

    async def fill(self, aid, text, enter=False):
        key = self._key(aid)
        if key not in ("quantity", "price"):
            return f"there is no element [{aid}] on the page any more"
        self.current[key] = text
        self.typed.append(text)
        return f"filled [{aid}]"

    async def click(self, aid):
        key = self._key(aid)
        if key == "apply":
            self.stage, self.offset = self.stage + 1, self.offset + 10
            return "clicked; the page reloaded"
        if key == "save":
            position = self.order[0]
            if position in self.errors:
                self.alert, self.offset = self.errors[position], self.offset + 10
                return "clicked; the page reloaded"
            self.saved[position] = dict(self.current)
            self.order.pop(0)
            self.current, self.stage, self.offset = {}, 0, self.offset + 10
            self.alert = f"Position {position} saved"
            return "clicked; the page reloaded"
        return f"[{aid}] not found"


TEMPLATE = [{"field": "Bore (mm)", "value": "{bore_mm}"}, {"press": "Apply selection"},
            {"field": "Certificate", "value": "{certification}", "if_null": "no certificate"},
            {"press": "Apply selection"}, {"field": "Catalogue number", "value": "{material_number}"},
            {"field": "Quantity (pcs)", "value": "{quantity}"}, {"field": "Unit price net (EUR)", "value": "{unit_price}"},
            {"press": "Save and next"}]


class Script:
    def __init__(self, *replies):
        self.replies, self.users = list(replies), []

    async def __call__(self, messages, *, think=True, max_tokens=6000, label=""):
        self.users.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else {}


async def enter(form, action: dict, bid: Bid | None = None) -> Script:
    script = Script({"thought": "all positions at once", "actions": [action]},
                    {"thought": "stop here", "actions": [{"do": "done", "summary": "test"}]})
    with patch.object(llm, "chat_json", script):
        await submit.submit_bid(form, None, bid or two_positions(), CONTEXT, "BRIEF", time.time() + 900)
    return script


class EnterPositions(unittest.IsolatedAsyncioTestCase):
    async def test_every_position_is_entered_in_the_forms_order_in_one_turn(self):
        form = Configurator()
        script = await enter(form, {"do": "enter_positions", "decimal": ",", "steps": TEMPLATE})
        self.assertEqual(list(form.saved), ["7.20", "7.10"])
        self.assertEqual(form.saved["7.10"], {"bore": "40", "cert": "no certificate", "material": "M-A",
                                              "quantity": "12", "price": "1234,50"})
        self.assertEqual(form.saved["7.20"], {"bore": "63", "cert": "EN 9", "material": "M-B", "quantity": "30",
                                              "price": "99,95"})
        self.assertIn("every position of the bid sheet is entered", script.users[1])
        self.assertIn("Positions entered with enter_positions so far: 7.20, 7.10", script.users[1])

    async def test_a_refused_save_stops_and_says_what_the_page_reported(self):
        form = Configurator(errors={"7.20": "Error: quantity must be a whole number"})
        script = await enter(form, {"do": "enter_positions", "decimal": ",", "steps": TEMPLATE})
        self.assertEqual(form.saved, {})
        self.assertIn("position 7.20 stopped", script.users[1])
        self.assertIn("quantity must be a whole number", script.users[1])
        self.assertIn("not entered by enter_positions: 7.10, 7.20", script.users[1])

    async def test_a_named_position_is_entered_without_reading_which_one_the_page_shows(self):
        form = Configurator(order=("7.10", "7.20"))
        await enter(form, {"do": "enter_positions", "positions": ["7.10"], "steps": TEMPLATE})
        self.assertEqual(list(form.saved), ["7.10"])
        self.assertEqual(form.saved["7.10"]["price"], "1234.50")  # no decimal given: the sheet's point stays

    async def test_an_unknown_placeholder_is_reported_and_nothing_is_typed(self):
        form = Configurator()
        script = await enter(form, {"do": "enter_positions", "steps": [{"field": "Quantity (pcs)", "value": "{qty}"}]})
        self.assertEqual(form.typed, [])
        self.assertIn("{qty}", script.users[1])

    async def test_the_typing_guard_still_checks_every_value(self):
        form = Configurator()
        template = TEMPLATE[:-2] + [{"field": "Unit price net (EUR)", "value": "777"}, {"press": "Save and next"}]
        script = await enter(form, {"do": "enter_positions", "steps": template})
        self.assertNotIn("777", form.typed)
        self.assertIn("refused", script.users[1])


class Templates(unittest.TestCase):
    def test_null_values_leave_their_step_out_unless_the_form_takes_something_for_none(self):
        values = submit.position_values(submit.bid_sheet(two_positions())["positions"][0])
        steps, problem = submit.fill_template(
            [{"field": "Packaging", "value": "{packaging_per_unit}"}, {"field": "Cert", "value": "{certification}",
                                                                       "if_null": "none"},
             {"field": "Rebate", "value": "{rebate_total}"}, {"field": "Accept", "value": True}], values, ",")
        self.assertIsNone(problem)
        self.assertEqual(steps, [{"field": "Cert", "value": "none"}, {"field": "Rebate", "value": "0,00"},
                                 {"field": "Accept", "value": True}])

    def test_a_short_position_number_counts_only_next_to_a_word_for_position(self):
        self.assertEqual(submit.shown_positions("Total 1 piece, 2 cartons", ["1", "2"]), [])
        self.assertEqual(submit.shown_positions("# Position 2 of the tender", ["1", "2"]), ["2"])
        self.assertEqual(submit.shown_positions("Deadline 01.04.2030; Pos. 01.04 is open", ["01.04"]), ["01.04"])
        self.assertEqual(submit.shown_positions("Deadline 01.04.2030", ["01.04"]), [])


class SilentlyUnsavedForm(Configurator):
    """Its "Save and next" keeps the same position on screen with empty fields and says nothing (test run 10def205:
    the template's last press did not save, and the position was counted as entered)."""

    async def click(self, aid):
        if self._key(aid) == "save":
            self.current, self.stage, self.offset = {}, 0, self.offset + 10
            return "clicked; the page reloaded"
        return await super().click(aid)


class NotSavedAndReconciliation(unittest.IsolatedAsyncioTestCase):
    async def test_a_position_the_form_still_shows_after_the_last_press_is_not_counted(self):
        form = SilentlyUnsavedForm()
        script = await enter(form, {"do": "enter_positions", "decimal": ",", "steps": TEMPLATE})
        self.assertEqual(form.saved, {})
        self.assertIn("position 7.20 was NOT saved", script.users[1])
        self.assertIn("Entered by enter_positions so far: none", script.users[1])

    def test_the_review_check_still_catches_a_total_that_does_not_match(self):
        bid = two_positions()
        right = "Tender T-0001\nPosition 7.10\nPosition 7.20\nTotal 17.812,50 EUR"
        wrong = "Tender T-0001\nPosition 7.10\nPosition 7.20\nTotal 17.000,00 EUR"
        self.assertTrue(check(right, bid, "T-0001").ready, check(right, bid, "T-0001").text())
        report = check(wrong, bid, "T-0001")
        self.assertFalse(report.ready)
        self.assertIn("17812.50", " ".join(report.unproven))


if __name__ == "__main__":
    unittest.main()
