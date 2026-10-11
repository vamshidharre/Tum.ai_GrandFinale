"""Assumptions of the public scenario that another world breaks, found by running the agent on our own evals
(atira eval). Invented values only.

Eval variant A (a world with fewer rules) scored 0: its specification states no pressure, so no material matched and
every position was left out; its buyer changed a quantity by mail, which only Q&A answers could do; its ERP shows one
plain stock figure."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import casefile  # noqa: E402
from calculator import Material, Requirement, calculate_bid, match_material, same_revision  # noqa: E402


class AWorldWithFewerRules(unittest.TestCase):
    def test_a_requirement_without_a_pressure_matches_on_the_attributes_it_states(self):
        need = Requirement("R-9", bore_mm=40, stroke_mm=250, pressure_bar=None, mounting="Flansch", certification=None)
        right = Material("M-1", active=True, bore_mm=40, stroke_mm=250, pressure_bar=160, mounting="Flansch")
        other = Material("M-2", active=True, bore_mm=40, stroke_mm=300, pressure_bar=160, mounting="Flansch")
        self.assertIs(match_material(need, [other, right]), right)

    def test_a_stated_pressure_is_still_a_criterion(self):
        need = Requirement("R-9", bore_mm=40, stroke_mm=250, pressure_bar=210, mounting="Flansch", certification=None)
        low = Material("M-1", active=True, bore_mm=40, stroke_mm=250, pressure_bar=160, mounting="Flansch")
        self.assertIsNone(match_material(need, [low]))

    def test_a_quantity_change_by_mail_counts_for_this_tender_only(self):
        case = casefile.CaseFile("brief", {"tender_reference": "T-77", "scenario_date": "2030-05-01"})
        case.merge("positions", {"positions": [{"position": "30", "quantity": 20, "unit": "Stk", "requirement_ref": "R-3"},
                                               {"position": "40", "quantity": 8, "unit": "Stk", "requirement_ref": "R-4"}]})
        case.merge("mail", {"messages": [
            {"sender": "buyer@city.example", "timestamp": "2030-04-02T09:00:00Z", "subject": "Request T-77",
             "tender_reference": "T-77", "topic": "other", "quantity_changes": [{"position": "30", "quantity": 24}]},
            {"sender": "buyer@city.example", "timestamp": "2030-04-03T09:00:00Z", "subject": "Request T-78",
             "tender_reference": "T-78", "topic": "other", "quantity_changes": [{"position": "40", "quantity": 99}]},
            {"sender": "buyer@city.example", "timestamp": "2030-06-01T09:00:00Z", "subject": "Later T-77",
             "tender_reference": "T-77", "topic": "other", "quantity_changes": [{"position": "40", "quantity": 50}]}]})
        changes = [change for entry in case.inputs().clarifications for change in entry.quantity_changes]
        self.assertEqual(changes, [("30", 24, None)])  # not the other tender's, not the one after the scenario date

    def test_a_change_notice_listed_as_current_does_not_replace_the_specification(self):
        # eval variant B: a change notice filed among the revisions with status "current" became the governing
        # revision; it has no requirements, so no position could be priced
        case = casefile.CaseFile("brief", {"tender_reference": "T-77", "scenario_date": "2030-05-01"})
        case.merge("revisions", {"document": "SPEC-9", "revisions": [
            {"revision": "2", "date": "2030-03-01", "status": "current"},
            {"revision": "CN-1", "date": "2030-04-01", "status": "current"}]})
        case.merge("requirements", {"revision": "2", "requirements": [
            {"requirement_ref": "R-1", "bore_mm": 40, "stroke_mm": 250, "mounting": "Flansch"}]})
        self.assertEqual(case.governing_revision(), "2")

    def test_revision_ids_that_differ_only_by_a_leading_word_are_the_same(self):
        # eval variant B: revisions filed as "Revision 2", the requirements as "2": no requirement governed
        self.assertTrue(same_revision("Revision 2", "2"))
        self.assertTrue(same_revision("Rev. B", "b"))
        self.assertTrue(same_revision("R-17", "R-17"))
        self.assertFalse(same_revision("Revision 2", "3"))
        self.assertFalse(same_revision("R-17", "17"))  # a prefix that is part of the id is kept
        case = casefile.CaseFile("brief", {"tender_reference": "T-77", "scenario_date": "2030-05-01"})
        case.merge("revisions", {"document": "SPEC-9", "revisions": [
            {"revision": "Revision 1", "date": "2030-02-01", "status": "superseded"},
            {"revision": "Revision 2", "date": "2030-03-01", "status": "current"}]})
        case.merge("requirements", {"revision": "2", "requirements": [
            {"requirement_ref": "R-1", "bore_mm": 40, "stroke_mm": 250, "mounting": "Flansch"}]})
        case.merge("requirements", {"revision": "1", "requirements": [
            {"requirement_ref": "R-1", "bore_mm": 40, "stroke_mm": 200, "mounting": "Flansch"}]})
        self.assertEqual(case.inputs().requirements["R-1"].stroke_mm, 250)

    def test_change_notices_and_approval_registers_have_a_kind_to_be_read_with(self):
        self.assertIn("change or amendment", casefile.SHAPES["clarifications"])
        self.assertIn("A register of approvals", casefile.SHAPES["mail"])
        self.assertIn("never a\nchange notice", casefile.SHAPES["revisions"])

    def test_a_world_without_pressures_is_priced_end_to_end(self):
        # the whole path the run takes: filed facts → case file inputs → calculator (eval variant A still scored 0
        # after the matching fix, because a requirement without a pressure counted as incomplete and was skipped)
        case = casefile.CaseFile("brief", {"tender_reference": "T-77", "scenario_date": "2030-05-01"})
        case.merge("positions", {"positions": [{"position": "10", "quantity": 20, "unit": "Stk", "requirement_ref": "R-1"}]})
        case.merge("revisions", {"document": "SPEC-9", "revisions": [{"revision": "B", "date": "2030-03-01", "status": "current"}]})
        case.merge("requirements", {"revision": "B", "requirements": [
            {"requirement_ref": "R-1", "bore_mm": 40, "stroke_mm": 250, "mounting": "Flansch", "certification": None}]})
        case.merge("material", {"materials": [
            {"material_number": "M-1", "is_active": True, "bore_mm": 40, "stroke_mm": 250, "pressure_bar": 160,
             "mounting": "Flansch", "certification": None, "free_stock": 30, "stock_lead_weeks": 2, "production_lead_weeks": 7},
            {"material_number": "M-2", "is_active": True, "bore_mm": 40, "stroke_mm": 300, "pressure_bar": 160,
             "mounting": "Flansch", "certification": None, "free_stock": 99, "stock_lead_weeks": 1, "production_lead_weeks": 7}]})
        case.merge("price_conditions", {"material_number": "M-1", "conditions": [
            {"valid_from": None, "valid_to": None, "currency": "EUR", "price_unit_pieces": 1, "number_format": "de-DE",
             "scales": [{"min_quantity": "1", "price": "100,00"}, {"min_quantity": "20", "price": "90,00"}]}]})
        case.merge("mail", {"messages": [
            {"sender": "director@bidder.example", "timestamp": "2030-04-01T08:00:00Z", "subject": "Discount T-77",
             "tender_reference": "T-77", "topic": "discount_approval", "number_format": "de-DE",
             "discount": {"approves": True, "percent": "5 %"}},
            {"sender": "buyer@city.example", "timestamp": "2030-04-02T09:00:00Z", "subject": "Request T-77",
             "tender_reference": "T-77", "topic": "other", "quantity_changes": [{"position": "10", "quantity": 24}]}]})
        facts = case.inputs()
        bid = calculate_bid(case.reference, case.scenario, facts.governing, facts.positions, facts.clarifications,
                            facts.requirements, facts.materials, facts.authorities, facts.no_material)
        answer = bid.answer()
        line = answer["positions"][0]
        # 24 pieces (the mail), scale 90.00 (from 20), minus 5 % = 85.50; stock 30 covers 24 → 2 weeks
        self.assertEqual((line["material_number"], line["quantity"], line["unit_price"], line["lead_time_weeks"]),
                         ("M-1", 24, 85.5, 2))
        self.assertEqual(answer["total_net"], 2052.0)

    def test_the_extraction_is_told_that_one_plain_stock_figure_is_the_free_stock(self):
        self.assertIn("A single stock figure with no breakdown", casefile.SHAPES["material"])


if __name__ == "__main__":
    unittest.main()
