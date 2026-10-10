"""Evidence provenance remains attached to the extracted facts it produced."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import casefile  # noqa: E402
import llm  # noqa: E402


class EvidenceProvenance(unittest.IsolatedAsyncioTestCase):
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


class LogFailureScenarios(unittest.IsolatedAsyncioTestCase):
    """Regression & diagnostic tests for failure modes observed in sandbox runs."""

    def make_casefile(self) -> casefile.CaseFile:
        return casefile.CaseFile(
            "brief",
            {
                "tender_reference": "T-9001",
                "scenario_date": "2030-06-15T12:00:00Z",
                "authorized_senders": {
                    "discount_approval": "director",
                    "condition_rules": "commercial",
                    "price_indices": "pricing",
                    "tech_release": "engineering",
                    "packaging_tariffs": "logistics",
                    "delivery_release": "planning",
                    "fx_fixing": "treasury",
                    "setup_pools": "controlling",
                    "project_bonus": "rebates",
                },
            },
        )

    def test_requirements_filed_before_revisions_are_hidden_until_revisions_are_filed(self):
        """When requirements are filed with a revision tag before the revisions table is filed,
        governing_revision() is None and inputs().requirements hides them; once revisions is filed,
        the previously extracted requirements become active immediately without re-filing."""
        facts = self.make_casefile()
        facts.merge("positions", {"positions": [
            {"position": "01.01", "quantity": 10, "unit": "pcs", "unit_is_set": False, "requirement_ref": "REQ-A1"},
            {"position": "01.05", "quantity": 3, "unit": "sets", "unit_is_set": True, "requirement_ref": "REQ-A5"},
        ]})
        facts.merge("requirements", {
            "revision": "REV-200",
            "requirements": [
                {"requirement_ref": "REQ-A1", "bore_mm": 50, "stroke_mm": 200, "stroke_is_usable_travel": False,
                 "pressure_bar": 160, "mounting": "flange", "certification": "ISO-1", "catalogue_group": "GRP-10"},
                {"requirement_ref": "REQ-A5", "bore_mm": 80, "stroke_mm": 400, "stroke_is_usable_travel": False,
                 "pressure_bar": 250, "mounting": "clevis", "certification": None, "catalogue_group": "GRP-50",
                 "pieces_per_set": 4},
            ],
        })
        # Before revisions are on file, governing_revision() is None and requirements are withheld
        self.assertIsNone(facts.governing_revision())
        self.assertEqual(facts.inputs().requirements, {})
        qids_before = dict(facts.questions())
        self.assertIn("revision", qids_before)
        self.assertNotIn("material:REQ-A1", qids_before)
        self.assertIn("final ? pieces", facts.status())

        # Filing the revision table unlocks the already-stored requirements and set size
        facts.merge("revisions", {"revisions": [
            {"revision": "REV-100", "status": "superseded", "date": "2030-01-10"},
            {"revision": "REV-200", "status": "current", "date": "2030-05-01"},
        ]})
        self.assertEqual(facts.governing_revision(), "REV-200")
        self.assertEqual(set(facts.inputs().requirements), {"REQ-A1", "REQ-A5"})
        self.assertEqual(facts.inputs().requirements["REQ-A5"].pieces_per_set, 4)
        qids_after = dict(facts.questions())
        self.assertNotIn("revision", qids_after)
        self.assertNotIn("requirements", qids_after)
        self.assertIn("material:REQ-A1", qids_after)
        self.assertIn("material:REQ-A5", qids_after)
        self.assertIn("final 12 pieces", facts.status())

    def test_expired_or_scaleless_price_condition_keeps_data_question_open(self):
        """Filing a price condition without scales or outside the scenario date window must keep
        data:<material> open until a valid condition with scales is merged."""
        facts = self.make_casefile()
        facts.merge("positions", {"positions": [
            {"position": "01.01", "quantity": 10, "unit": "pcs", "unit_is_set": False, "requirement_ref": "REQ-A1"},
        ]})
        facts.merge("revisions", {"revisions": [{"revision": "REV-200", "status": "current", "date": "2030-05-01"}]})
        facts.merge("requirements", {
            "revision": "REV-200",
            "requirements": [{
                "requirement_ref": "REQ-A1", "bore_mm": 50, "stroke_mm": 200, "stroke_is_usable_travel": False,
                "pressure_bar": 160, "mounting": "flange", "certification": None, "catalogue_group": "GRP-10",
            }],
        })
        facts.merge("material", {"materials": [{
            "material_number": "MAT-101", "is_active": True, "catalogue_group": "GRP-10",
            "bore_mm": 50, "stroke_mm": 200, "pressure_bar": 160, "mounting": "flange", "certification": None,
            "carton_size": 5, "allocation_mass": "1000", "allocation_mass_unit": "g",
            "surcharge_mass": "200", "surcharge_mass_unit": "g", "metal_index_code": None,
            "free_stock": 50, "stock_lead_weeks": 1, "production_lead_weeks": 6,
        }]})
        # 1) Condition without scales (e.g. header-only page) is ignored by merge()
        summary = facts.merge("price_conditions", {
            "material_number": "MAT-101",
            "conditions": [{"valid_from": "2030-01-01", "valid_to": "2030-12-31", "currency": "EUR",
                            "price_unit_pieces": 1, "scales": []}],
        })
        self.assertEqual(summary, "nothing usable was found in it")
        self.assertIn("data:MAT-101", dict(facts.questions()))

        # 2) Expired condition with scales is recorded, but still 0 valid on scenario date
        facts.merge("price_conditions", {
            "material_number": "MAT-101",
            "conditions": [{"valid_from": "2029-01-01", "valid_to": "2029-12-31", "currency": "EUR",
                            "price_unit_pieces": 1, "scales": [{"min_quantity": 1, "price": "99.00"}]}],
        })
        self.assertIn("conditions on file 1 (0 valid on the scenario date)", facts.status())
        self.assertIn("the price condition valid on the scenario date", dict(facts.questions())["data:MAT-101"])

        # 3) Valid condition with scales closes data:MAT-101
        facts.merge("price_conditions", {
            "material_number": "MAT-101",
            "conditions": [{"valid_from": "2030-01-01", "valid_to": "2030-12-31", "currency": "USD",
                            "fx_key": "FX-USD", "price_unit_pieces": 100, "number_format": "en-US",
                            "scales": [{"min_quantity": 1, "price": "245,750"}]}],
        })
        self.assertNotIn("data:MAT-101", dict(facts.questions()))
        self.assertIn("fx:FX-USD", dict(facts.questions()))
        [mat] = facts.inputs().materials
        valid_cond = [c for c in mat.conditions if c.valid_on(facts.scenario)][0]
        self.assertEqual(valid_cond.scales, [(1, casefile.Decimal("245750"))])
        self.assertEqual(valid_cond.price_unit, 100)

    def test_partial_material_merges_keep_stock_question_open_until_stock_is_filed(self):
        """Filing technical data and planning data in separate merge calls preserves earlier fields
        and keeps data:<material> open for stock quantity until the stock page is merged."""
        facts = self.make_casefile()
        facts.merge("positions", {"positions": [
            {"position": "01.01", "quantity": 10, "unit": "pcs", "unit_is_set": False, "requirement_ref": "REQ-A1"},
        ]})
        facts.merge("revisions", {"revisions": [{"revision": "REV-200", "status": "current", "date": "2030-05-01"}]})
        facts.merge("requirements", {
            "revision": "REV-200",
            "requirements": [{
                "requirement_ref": "REQ-A1", "bore_mm": 50, "stroke_mm": 200, "stroke_is_usable_travel": False,
                "pressure_bar": 160, "mounting": "flange", "certification": None, "catalogue_group": "GRP-10",
            }],
        })
        # Step 1: technical view only
        facts.merge("material", {"materials": [{
            "material_number": "MAT-101", "is_active": True, "catalogue_group": "GRP-10",
            "bore_mm": 50, "stroke_mm": 200, "pressure_bar": 160, "mounting": "flange", "certification": None,
        }]})
        # Step 2: logistics/planning view + valid price condition, but no stock yet
        facts.merge("material", {"materials": [{
            "material_number": "MAT-101", "carton_size": 5,
            "allocation_mass": "1,25", "allocation_mass_unit": "kg",
            "surcharge_mass": "250", "surcharge_mass_unit": "g",
            "metal_index_code": "IDX-STEEL", "stock_lead_weeks": 1, "production_lead_weeks": 6,
            "number_format": "de-DE",
        }]})
        facts.merge("price_conditions", {
            "material_number": "MAT-101",
            "conditions": [{"valid_from": "2030-01-01", "valid_to": "2030-12-31", "currency": "EUR",
                            "price_unit_pieces": 1, "scales": [{"min_quantity": 1, "price": "40.00"}]}],
        })
        qids = dict(facts.questions())
        self.assertIn("data:MAT-101", qids)
        self.assertEqual(qids["data:MAT-101"],
                         "ERP material MAT-101 (it matches requirement REQ-A1) still lacks: stock quantity.")
        # Step 3: stock view filed
        facts.merge("material", {"materials": [{"material_number": "MAT-101", "free_stock": 75}]})
        self.assertNotIn("data:MAT-101", dict(facts.questions()))
        [mat] = facts.inputs().materials
        self.assertEqual((mat.bore_mm, mat.carton_size, mat.stock, mat.allocation_mass_g),
                         (50, 5, 75, casefile.Decimal("1250.00")))

    def test_single_message_folder_header_does_not_overwrite_valid_mailbox_address(self):
        """A folder header without '@' (such as 'Postfach / Lesebereich') must not overwrite a valid mailbox."""
        facts = self.make_casefile()
        facts.merge("mail", {"mailbox_address": "bids@acme-industrial.test", "messages": []})
        self.assertEqual(facts.mailbox, "bids@acme-industrial.test")
        facts.merge("mail", {"mailbox_address": "Werkpost / Lesebereich", "messages": []})
        self.assertEqual(facts.mailbox, "bids@acme-industrial.test")

    async def test_failed_or_empty_extraction_discards_digest_so_document_can_be_refiled(self):
        """When an LLM extraction fails or returns {}, the page digest is removed from self.filed
        so the agent can file the same page again."""
        facts = self.make_casefile()

        async def empty_reply(*args, **kwargs):
            return {}

        async def valid_reply(*args, **kwargs):
            return {"answers": [{"number": "Q-101", "timestamp": "2030-06-01T10:00:00Z"}]}

        page_text = "Clarification Q-101: quantity unchanged."
        digest = casefile.hashlib.sha1(("clarifications\n" + page_text).encode("utf-8")).hexdigest()
        facts.filed.add(digest)
        facts.pending = 1
        with patch.object(llm, "chat_json", empty_reply):
            await facts._extract("clarifications", page_text, "https://portal.test/qa", "tender", digest)
        self.assertNotIn(digest, facts.filed)
        self.assertEqual(facts.pending, 0)

        # Now re-filing the exact same page succeeds
        facts.filed.add(digest)
        facts.pending = 1
        with patch.object(llm, "chat_json", valid_reply):
            await facts._extract("clarifications", page_text, "https://portal.test/qa", "tender", digest)
        self.assertIn(digest, facts.filed)
        self.assertIn("Q-101", facts.clarifications)

    async def test_multi_page_file_splits_on_page_break_and_deduplicates_each_page(self):
        """CaseFile.file() splits collected multi-page documents on PAGE_BREAK and deduplicates per page."""
        facts = self.make_casefile()
        calls = []

        async def fake_extract(kind, page, source, agent, digest=""):
            calls.append((kind, page, digest))
            facts.pending -= 1

        page1 = "Clarification page one with more than twenty characters."
        page2 = "Clarification page two with more than twenty characters."
        combined = f"{page1}{casefile.PAGE_BREAK} 2 =====\n{page2}{casefile.PAGE_BREAK} 3 =====\n{page1}"
        with patch.object(facts, "_extract", side_effect=fake_extract):
            msg1 = facts.file("clarifications", combined, "https://portal.test/qa", "tender")
            await casefile.asyncio.sleep(0)
            msg2 = facts.file("clarifications", page2, "https://portal.test/qa", "tender")
        self.assertIn("2 page(s)", msg1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(msg2, "this document is already filed as clarifications")


class MailAuthoritiesAndFiltering(unittest.TestCase):
    """Tests for CaseFile.authorities() sender/domain/reference/date filtering and topic resolution."""

    def make_casefile(self) -> casefile.CaseFile:
        facts = casefile.CaseFile(
            "brief",
            {
                "tender_reference": "T-9001",
                "scenario_date": "2030-06-15T12:00:00Z",
                "authorized_senders": {
                    "discount_approval": "director",
                    "condition_rules": "commercial",
                    "price_indices": "pricing",
                    "tech_release": "engineering",
                    "packaging_tariffs": "logistics",
                    "delivery_release": "planning",
                    "fx_fixing": "treasury",
                    "setup_pools": "controlling",
                    "project_bonus": "rebates",
                },
            },
        )
        facts.mailbox = "quotes@supplier-corp.test"
        return facts

    def test_latest_authorized_discount_wins_while_decoy_ref_wrong_sender_and_future_messages_are_ignored(self):
        facts = self.make_casefile()
        facts.merge("mail", {"messages": [
            # Older valid approval in archive (4%, no min order)
            {"sender": "director@supplier-corp.test", "timestamp": "2030-06-01T09:00:00Z",
             "subject": "Initial discount", "tender_reference": "T-9001", "topic": "discount_approval",
             "discount": {"approves": True, "percent": "4", "min_order_value": None}},
            # Newer valid approval in inbox (6.5%, min order 15,000.00)
            {"sender": "director@supplier-corp.test", "timestamp": "2030-06-10T14:00:00Z",
             "subject": "Updated discount", "tender_reference": "T-9001", "topic": "discount_approval",
             "number_format": "de-DE",
             "discount": {"approves": True, "percent": "6,5", "min_order_value": "15.000,00"}},
            # Decoy tender reference suffix (-A)
            {"sender": "director@supplier-corp.test", "timestamp": "2030-06-12T10:00:00Z",
             "subject": "Other tender", "tender_reference": "T-9001-A", "topic": "discount_approval",
             "discount": {"approves": True, "percent": "15", "min_order_value": None}},
            # Wrong sender local part
            {"sender": "intern@supplier-corp.test", "timestamp": "2030-06-13T10:00:00Z",
             "subject": "Unauthorized discount", "tender_reference": "T-9001", "topic": "discount_approval",
             "discount": {"approves": True, "percent": "20", "min_order_value": None}},
            # Wrong sender domain
            {"sender": "director@external-domain.test", "timestamp": "2030-06-14T10:00:00Z",
             "subject": "Spoofed domain", "tender_reference": "T-9001", "topic": "discount_approval",
             "discount": {"approves": True, "percent": "25", "min_order_value": None}},
            # Future timestamp after scenario_date
            {"sender": "director@supplier-corp.test", "timestamp": "2030-06-16T08:00:00Z",
             "subject": "Future approval", "tender_reference": "T-9001", "topic": "discount_approval",
             "discount": {"approves": True, "percent": "30", "min_order_value": None}},
        ]})
        auth = facts.authorities()
        self.assertEqual(auth.discount_percent, casefile.Decimal("6.5"))
        self.assertEqual(auth.discount_min_order, casefile.Decimal("15000.00"))

    def test_price_indices_use_latest_message_per_code_and_respect_validity_window_and_mass_unit(self):
        facts = self.make_casefile()
        facts.merge("mail", {"messages": [
            {"sender": "pricing@supplier-corp.test", "timestamp": "2030-06-01T08:00:00Z",
             "subject": "Old index", "tender_reference": "T-9001", "topic": "price_indices",
             "price_indices": [
                 {"code": "IDX-ALLOY", "rate": "20.00", "unit": "EUR/kg",
                  "valid_from": "2030-06-01", "valid_to": "2030-06-30"},
             ]},
            {"sender": "pricing@supplier-corp.test", "timestamp": "2030-06-12T08:00:00Z",
             "subject": "New index bulletin", "tender_reference": "T-9001", "topic": "price_indices",
             "price_indices": [
                 {"code": "IDX-ALLOY", "rate": "99.00", "unit": "EUR/kg",
                  "valid_from": "2030-05-01", "valid_to": "2030-05-31"},
                 {"code": "IDX-ALLOY", "rate": "35.00", "unit": "EUR/kg",
                  "valid_from": "2030-06-01", "valid_to": "2030-06-30"},
                 {"code": "IDX-COPPER", "rate": "5000.00", "unit": "EUR/t",
                  "valid_from": "2030-06-01", "valid_to": "2030-06-30"},
             ]},
        ]})
        auth = facts.authorities()
        # 35.00 EUR/kg -> 0.035 EUR/g; 5000.00 EUR/t -> 0.005 EUR/g
        self.assertEqual(auth.index_rates["IDX-ALLOY"], casefile.Decimal("0.035"))
        self.assertEqual(auth.index_rates["IDX-COPPER"], casefile.Decimal("0.005"))

    def test_all_commercial_mail_topics_merge_and_close_open_questions(self):
        facts = self.make_casefile()
        facts.merge("positions", {"positions": [
            {"position": "01.01", "quantity": 10, "unit": "pcs", "unit_is_set": False, "requirement_ref": "REQ-A1"},
        ]})
        facts.merge("revisions", {"revisions": [{"revision": "REV-200", "status": "current", "date": "2030-05-01"}]})
        facts.merge("requirements", {
            "revision": "REV-200",
            "requirements": [{
                "requirement_ref": "REQ-A1", "bore_mm": 50, "stroke_mm": 180, "stroke_is_usable_travel": True,
                "reserve_key": "RES-200", "pressure_bar": 160, "mounting": "flange", "certification": None,
                "catalogue_group": "GRP-10",
            }],
        })
        facts.merge("clarifications", {"answers": [{
            "number": "Q-1", "timestamp": "2030-06-02T10:00:00Z",
            "assignments": [{
                "position": "01.01", "condition_group": "CG-BOTH", "packaging_tariff": "PKG-STD",
                "delivery_commitment": "DC-FAST", "setup_pool": "SP-MAIN", "project_bonus": "PB-TIER",
                "ordinary_setup_fee": "45.00",
            }],
        }]})
        # Initially reserve:REQ-A1 and all commercial key questions are open
        qids = dict(facts.questions())
        for expected in ("reserve:REQ-A1", "condition_group:CG-BOTH", "packaging_tariff:PKG-STD",
                         "delivery_commitment:DC-FAST", "setup_pool:SP-MAIN", "project_bonus:PB-TIER"):
            self.assertIn(expected, qids)

        facts.merge("mail", {"messages": [
            {"sender": "engineering@supplier-corp.test", "timestamp": "2030-06-03T10:00:00Z",
             "subject": "Reserve", "tender_reference": "T-9001", "topic": "tech_release",
             "tech_release": [{"revision": "REV-200", "reserve_key": "RES-200", "reserve_mm_per_end": 10}]},
            {"sender": "commercial@supplier-corp.test", "timestamp": "2030-06-03T11:00:00Z",
             "subject": "Clauses", "tender_reference": "T-9001", "topic": "condition_rules",
             "condition_rules": [{"group": "CG-BOTH", "discount_applies_to": "base_and_surcharge"}]},
            {"sender": "logistics@supplier-corp.test", "timestamp": "2030-06-03T12:00:00Z",
             "subject": "Tariffs", "tender_reference": "T-9001", "topic": "packaging_tariffs",
             "packaging_tariffs": [{"key": "PKG-STD", "eur_per_carton": "12.50"}]},
            {"sender": "planning@supplier-corp.test", "timestamp": "2030-06-03T13:00:00Z",
             "subject": "Delivery", "tender_reference": "T-9001", "topic": "delivery_release",
             "delivery_release": [{"key": "DC-FAST", "material_number": "MAT-101", "weeks": 2, "max_quantity": 50}]},
            {"sender": "controlling@supplier-corp.test", "timestamp": "2030-06-03T14:00:00Z",
             "subject": "Pools", "tender_reference": "T-9001", "topic": "setup_pools",
             "setup_pools": [{"key": "SP-MAIN", "fee": "120.00", "cent_allocation": "remainder_to_last"}]},
            {"sender": "rebates@supplier-corp.test", "timestamp": "2030-06-03T15:00:00Z",
             "subject": "Bonus", "tender_reference": "T-9001", "topic": "project_bonus",
             "project_bonus": [{"key": "PB-TIER", "bands": [{"from": "0", "to": "1000", "percent": "1.5"}]}]},
        ]})
        qids_after = dict(facts.questions())
        for closed in ("reserve:REQ-A1", "condition_group:CG-BOTH", "packaging_tariff:PKG-STD",
                       "delivery_commitment:DC-FAST", "setup_pool:SP-MAIN", "project_bonus:PB-TIER"):
            self.assertNotIn(closed, qids_after)
        # And the requirement now seeks nominal stroke 180 + 2*10 = 200 mm
        self.assertIn("stroke 200 mm", qids_after["material:REQ-A1"])


class AnswerGuardsAndLifecycle(unittest.TestCase):
    """Tests for CaseFile.answer() validation guards."""

    def make_casefile(self) -> casefile.CaseFile:
        facts = casefile.CaseFile("brief", {"tender_reference": "T-9001", "scenario_date": "2030-06-15T12:00:00Z"})
        facts.merge("positions", {"positions": [
            {"position": "01.01", "quantity": 10, "unit": "pcs", "unit_is_set": False, "requirement_ref": "REQ-A1"},
        ]})
        facts.merge("revisions", {"revisions": [{"revision": "REV-200", "status": "current", "date": "2030-05-01"}]})
        facts.merge("requirements", {
            "revision": "REV-200",
            "requirements": [{
                "requirement_ref": "REQ-A1", "bore_mm": 50, "stroke_mm": 200, "stroke_is_usable_travel": False,
                "pressure_bar": 160, "mounting": "flange", "certification": None, "catalogue_group": "GRP-10",
            }],
        })
        return facts

    def test_clarifications_cannot_be_closed_complete_when_none_are_extracted(self):
        facts = self.make_casefile()
        msg = facts.answer("clarifications", "complete", "read all pages")
        self.assertIn("no Q&A answers have been extracted yet", msg)
        self.assertIn("clarifications", dict(facts.questions()))

        facts.merge("clarifications", {"answers": [{"number": "Q-1", "timestamp": "2030-06-01T10:00:00Z"}]})
        msg2 = facts.answer("clarifications", "complete", "all pages filed")
        self.assertEqual(msg2, "question clarifications closed as complete")
        self.assertNotIn("clarifications", dict(facts.questions()))

    def test_material_absent_requires_all_group_candidates_to_have_status_and_technical_attributes(self):
        facts = self.make_casefile()
        # 1) No material in group GRP-10 on file yet
        self.assertIn("no material of that catalogue group is on file yet",
                      facts.answer("material:REQ-A1", "absent", "none found"))
        # 2) Candidate in group on file, but missing stroke_mm or active status
        facts.merge("material", {"materials": [{
            "material_number": "MAT-999", "is_active": None, "catalogue_group": "GRP-10",
            "bore_mm": 50, "stroke_mm": None, "pressure_bar": 160, "mounting": "flange",
        }]})
        self.assertIn("the technical data or status of MAT-999 is not on file yet",
                      facts.answer("material:REQ-A1", "absent", "none matches"))
        # 3) Once candidate has full technical attributes + status (e.g. blocked), absent is accepted
        facts.merge("material", {"materials": [{
            "material_number": "MAT-999", "is_active": False, "catalogue_group": "GRP-10",
            "bore_mm": 50, "stroke_mm": 200, "pressure_bar": 160, "mounting": "flange",
        }]})
        self.assertEqual(facts.answer("material:REQ-A1", "absent", "MAT-999 is blocked"),
                         "question material:REQ-A1 closed as absent")
        self.assertEqual(facts.inputs().no_material, {"REQ-A1"})

    def test_auto_closing_questions_reject_manual_complete_and_pending_extractions_block_answers(self):
        facts = self.make_casefile()
        self.assertIn("closes by itself", facts.answer("material:REQ-A1", "complete", "found it"))
        facts.pending = 1
        self.assertIn("still being extracted", facts.answer("discount", "absent", "no discount"))
        facts.pending = 0
        self.assertEqual(facts.answer("discount", "absent", "checked inbox and archive"),
                         "question discount closed as absent")

    def test_per_agent_pending_allows_other_agents_to_answer_when_one_site_is_still_extracting(self):
        facts = self.make_casefile()
        facts.pending = 1
        facts.pending_by_agent["tender-portal"] = 1
        self.assertIn("still being extracted", facts.answer("clarifications", "complete", "done", agent="tender-portal"))
        self.assertEqual(facts.answer("discount", "absent", "checked inbox and archive", agent="mail"),
                         "question discount closed as absent")

    def test_qa_quantity_change_with_spurious_unit_is_set_true_on_piece_position_does_not_omit_position(self):
        facts = self.make_casefile()
        facts.merge("clarifications", {"answers": [{
            "number": "A-100", "timestamp": "2030-05-05T10:00:00Z",
            "quantity_changes": [{"position": "01.01", "quantity": 25, "unit": "Stück", "unit_is_set": True}],
        }, {
            "number": "A-101", "timestamp": "2030-05-06T10:00:00Z",
            "quantity_changes": [{"position": "01.01", "quantity": 25, "unit_is_set": True}],
        }]})
        inp = facts.inputs()
        from calculator import active_clarifications, final_quantities
        answers = active_clarifications(inp.clarifications, facts.scenario)
        pieces, unknown = final_quantities(inp.positions, answers, inp.requirements)
        self.assertEqual(pieces["01.01"], 25)
        self.assertEqual(unknown, [])

    def test_strip_physical_only_stock_rejects_plant_summary_but_keeps_storage_location_free_stock(self):
        from casefile import _strip_physical_only_stock
        plant_data = {"materials": [{"material_number": "MAT-101", "free_stock": 95}]}
        note = _strip_physical_only_stock(
            "Verdichtete Bestandsanzeige\nPhysischer Bestand einschließlich Prüfbestand. "
            "Lagerorte für die Verfügbarkeitsprüfung öffnen.\nWerk 1000 | 95",
            plant_data,
        )
        self.assertIn("physical stock", note)
        self.assertIsNone(plant_data["materials"][0].get("free_stock"))

        loc_data = {"materials": [{"material_number": "MAT-101", "free_stock": 29}]}
        note2 = _strip_physical_only_stock(
            "1000 | 0001 | Frei verwendbar | 29\n1000 | 0099 | Prüfbestand | 66\n"
            "Summe physisch (einschließlich gesperrtem Bestand) | 95",
            loc_data,
        )
        self.assertEqual(note2, "")
        self.assertEqual(loc_data["materials"][0].get("free_stock"), 29)


if __name__ == "__main__":
    unittest.main()

