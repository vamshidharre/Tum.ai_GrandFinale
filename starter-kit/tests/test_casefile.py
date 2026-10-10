"""Evidence provenance remains attached to the extracted facts it produced."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import casefile  # noqa: E402
import calculator  # noqa: E402
import llm  # noqa: E402


class EvidenceProvenance(unittest.IsolatedAsyncioTestCase):
    async def test_quantity_change_without_its_own_unit_inherits_the_position_unit(self):
        facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0001", "scenario_date": "2030-01-01"})
        facts.merge("positions", {"positions": [{"position": "POS-A", "quantity": 7,
                                                    "unit": "piece", "unit_is_set": False,
                                                    "requirement_ref": "REQ-A"}]})
        prompts = []

        async def extracted(messages, **kwargs):
            prompts.extend(messages)
            return {"answers": [{"number": "QA-TEST-1", "timestamp": "2030-01-01",
                                  "quantity_changes": [{"position": "POS-A", "quantity": 11,
                                                        "unit_is_set": None}]}]}

        with patch.object(llm, "chat_json", extracted):
            await facts._extract("clarifications", "Answer QA-TEST-1: POS-A has a binding quantity of 11.",
                                 "https://portal.example/questions", "tender", "quantity-test")

        system_prompt = " ".join(prompts[0]["content"].split())
        self.assertIn("unit explicitly stated for that changed quantity", system_prompt)
        self.assertIn("Do not infer the unit from another tender position", system_prompt)
        inputs = facts.inputs()
        quantities, unknown = calculator.final_quantities(inputs.positions, inputs.clarifications, {})
        self.assertEqual(quantities, {"POS-A": 11})
        self.assertEqual(unknown, [])

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


if __name__ == "__main__":
    unittest.main()
