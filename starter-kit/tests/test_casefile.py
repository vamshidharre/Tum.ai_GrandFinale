"""Evidence provenance remains attached to the extracted facts it produced."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import casefile  # noqa: E402
import llm  # noqa: E402


class EvidenceProvenance(unittest.IsolatedAsyncioTestCase):
    def test_questions_deduplicates_shared_index_ids(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0006", "scenario_date": "2030-01-01"})
        requirement = SimpleNamespace(missing=lambda: [], catalogue_group="test group")
        condition = SimpleNamespace(scales=[(1, 1)], currency="EUR", fx_key=None,
                                   valid_on=lambda scenario: True)
        material = SimpleNamespace(number="MAT-TEST", stock=1, stock_lead_weeks=1,
                                   production_lead_weeks=1, carton_size=1, allocation_mass_g=1,
                                   surcharge_mass_g=1, conditions=[condition], index_code="IDX-TEST")
        authorities = SimpleNamespace(discount_percent=0, index_rates={}, fx={}, clauses={}, tariffs={},
                                      commitments={}, pools={}, bonus={})
        inputs = SimpleNamespace(
            positions=[SimpleNamespace(position="P-1", requirement_ref="REQ-1", is_set=False),
                       SimpleNamespace(position="P-2", requirement_ref="REQ-2", is_set=False)],
            clarifications=[], governing="REV-TEST", requirements={}, materials=[], authorities=authorities,
        )
        needs = {"REQ-1": requirement, "REQ-2": requirement}
        with (patch.object(facts, "inputs", return_value=inputs),
              patch.object(casefile, "active_clarifications", return_value=[]),
              patch.object(casefile, "resolve_requirements", return_value=(needs, {})),
              patch.object(casefile, "merged_assignments", return_value=({}, [])),
              patch.object(casefile, "final_quantities", return_value=({}, [])),
              patch.object(casefile, "match_material", return_value=material)):
            question_ids = [qid for qid, _ in facts.questions()]

        self.assertEqual(question_ids.count("index:IDX-TEST"), 1)

    def test_evidence_based_mail_questions_cannot_be_completed_without_facts(self):
        for question in ("mailbox", "discount", "index:IDX-TEST", "fx:FX-TEST", "condition_group:GROUP-TEST"):
            facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0005", "scenario_date": "2030-01-01"})
            with self.subTest(question=question), patch.object(facts, "questions", return_value=[(question, "find evidence")]):
                result = facts.answer(question, "complete", "found")
                self.assertIn("closes by itself", result)
                self.assertNotIn(question, facts.answers)

    def test_future_index_window_does_not_erase_rate_valid_on_scenario_date(self):
        facts = casefile.CaseFile("brief", {
            "tender_reference": "TEST-0003", "scenario_date": "2030-06-15",
            "authorized_senders": {"price_indices": "pricing@example.test"},
        })
        facts.messages[("pricing@example.test", "2030-06-01", "earlier rate")] = {
            "sender": "pricing@example.test", "timestamp": "2030-06-01",
            "tender_reference": "TEST-0003", "number_format": "en-US",
            "price_indices": [{"code": "IDX-TEST", "rate": "2.5", "unit": "EUR/kg",
                               "valid_from": "2030-01-01", "valid_to": "2030-12-31"}],
        }
        facts.messages[("pricing@example.test", "2030-06-10", "later rate")] = {
            "sender": "pricing@example.test", "timestamp": "2030-06-10",
            "tender_reference": "TEST-0003", "number_format": "en-US",
            "price_indices": [{"code": "IDX-TEST", "rate": "3.0", "unit": "EUR/kg",
                               "valid_from": "2031-01-01", "valid_to": "2031-12-31"}],
        }

        rates = facts.authorities().index_rates

        self.assertIn("IDX-TEST", rates)
        self.assertEqual(str(rates["IDX-TEST"]), "0.0025")

    async def test_mailbox_only_page_is_retained_as_evidence(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0004", "scenario_date": "2030-01-01"})
        self.assertIn("mailbox", {qid for qid, _ in facts.questions()})

        async def extracted(*args, **kwargs):
            return {"mailbox_address": "sales@example.test", "messages": []}

        with patch.object(llm, "chat_json", extracted):
            await facts._extract("mail", "Logged-in account page", "https://mail.example/account", "mail", "account")

        self.assertEqual(facts.mailbox, "sales@example.test")
        self.assertNotIn("mailbox", {qid for qid, _ in facts.questions()})
        self.assertEqual(len(facts.evidence), 1)
        self.assertIn("mailbox address", facts.evidence[0]["summary"])

    async def test_successful_extraction_records_its_source_and_typed_result(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0001", "scenario_date": "2030-01-01"})

        async def extracted(*args, **kwargs):
            return {"positions": [{"position": "1", "quantity": 4, "requirement_ref": "REQ-1"}]}

        with patch.object(llm, "chat_json", extracted):
            await facts._extract("positions", "Position 1: four items", "https://portal.example/rfq", "tender", "abc123")
        [evidence] = facts.evidence
        self.assertEqual((evidence["id"], evidence["kind"], evidence["source"], evidence["agent"]),
                         ("abc123", "positions", "https://portal.example/rfq", "tender"))
        self.assertEqual(evidence["extracted"]["positions"][0]["position"], "1")
        self.assertEqual(json.loads(facts.dump())["evidence"][0]["source"], "https://portal.example/rfq")

    async def test_schema_gate_drops_only_malformed_records_and_keeps_its_log(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0001", "scenario_date": "2030-01-01"})

        async def mixed(*args, **kwargs):
            return {"positions": [{"position": "1", "quantity": 4}, {"quantity": 9}, "not a record"]}

        with patch.object(llm, "chat_json", mixed):
            await facts._extract("positions", "Position records", "https://portal.example/rfq", "tender", "mixed")
        self.assertEqual(set(facts.positions), {"1"})
        self.assertIn("dropped 2 malformed", facts.evidence[0]["validation"])
        self.assertIn("dropped 2 malformed", facts.evidence[0]["summary"])

    async def test_schema_gate_rejects_an_invalid_record_list(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0001", "scenario_date": "2030-01-01"})

        async def malformed(*args, **kwargs):
            return {"positions": "not a list"}

        with patch.object(llm, "chat_json", malformed):
            await facts._extract("positions", "Position records", "https://portal.example/rfq", "tender", "bad")
        self.assertEqual(facts.positions, {})
        self.assertEqual(facts.evidence, [])
        self.assertIn("schema validation rejected", facts.take_news("tender")[0])

    async def test_only_malformed_rows_are_treated_as_no_usable_evidence(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0001", "scenario_date": "2030-01-01"})

        async def malformed_rows(*args, **kwargs):
            return {"positions": [{"quantity": 9}]}

        with patch.object(llm, "chat_json", malformed_rows):
            await facts._extract("positions", "Position records", "https://portal.example/rfq", "tender", "empty")
        self.assertEqual(facts.evidence, [])
        self.assertIn("no usable records", facts.take_news("tender")[0])

    async def test_stock_evidence_updates_material_and_closes_its_data_question(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0002", "scenario_date": "2030-01-01"})
        facts.positions["P-TEST"] = {"position": "P-TEST", "quantity": 2, "requirement_ref": "REQ-TEST"}
        facts.revisions["REV-TEST"] = {"revision": "REV-TEST", "status": "current", "date": "2029-01-01"}
        facts.requirements[("REV-TEST", "REQ-TEST")] = {
            "requirement_ref": "REQ-TEST", "bore_mm": 10, "stroke_mm": 20, "pressure_bar": 30,
            "mounting": "test mount", "certification": "test spec", "catalogue_group": "test group",
        }
        facts.materials["MAT-TEST"] = {
            "material_number": "MAT-TEST", "is_active": True, "catalogue_group": "test group",
            "bore_mm": 10, "stroke_mm": 20, "pressure_bar": 30, "mounting": "test mount",
            "certification": "test spec", "carton_size": 1, "allocation_mass": "1",
            "surcharge_mass": "1", "free_stock": None, "stock_lead_weeks": None,
            "production_lead_weeks": 5,
        }
        facts.conditions["MAT-TEST"] = {("2029-01-01", "2031-01-01"): {
            "valid_from": "2029-01-01", "valid_to": "2031-01-01", "currency": "EUR",
            "price_unit_pieces": 1, "scales": [{"min_quantity": "1", "price": "1"}],
        }}
        self.assertIn("data:MAT-TEST", {qid for qid, _ in facts.questions()})

        async def extracted(*args, **kwargs):
            return {"materials": [{"material_number": "MAT-TEST", "free_stock": 7, "stock_lead_weeks": 2}]}

        with patch.object(llm, "chat_json", extracted):
            await facts._extract("material", "Inventory record", "https://erp.example/material-stock", "erp", "stock")

        material = facts.inputs().materials[0]
        self.assertEqual((material.stock, material.stock_lead_weeks), (7, 2))
        self.assertNotIn("data:MAT-TEST", {qid for qid, _ in facts.questions()})


if __name__ == "__main__":
    unittest.main()
