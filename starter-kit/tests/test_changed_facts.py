"""A requirement that changes after the agents worked with it is said out loud, on invented values (no scenario
values): the agents are told, the open question keeps saying it, and an "absent" given for the old values is void."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import casefile  # noqa: E402
import worker  # noqa: E402

AMENDMENT = {"answers": [{"number": "Q-1", "timestamp": "2029-12-20T10:00:00Z", "tech_amendments": [
    {"revision": "B", "requirement_ref": "REQ-1", "attribute": "stroke", "value": "200"}]}]}


def case(stroke: int = 207, **requirement) -> casefile.CaseFile:
    """A tender with one position whose requirement is on file in the current revision."""
    facts = casefile.CaseFile("brief", {"tender_reference": "TEST-0001", "scenario_date": "2030-01-01"})
    facts.agents.update({"erp", "mail"})
    facts.merge("positions", {"positions": [{"position": "1", "quantity": 4, "requirement_ref": "REQ-1"}]})
    facts.merge("revisions", {"revisions": [{"revision": "B", "status": "current", "date": "2029-12-01"}]})
    row = {"requirement_ref": "REQ-1", "bore_mm": 50, "stroke_mm": stroke, "pressure_bar": 160, "mounting": "flange",
           "catalogue_group": "G-1", **requirement}
    facts.merge("requirements", {"revision": "B", "requirements": [row]})
    return facts


def material(stroke: int) -> dict:
    return {"materials": [{"material_number": "M-1", "catalogue_group": "G-1", "is_active": True, "bore_mm": 50,
                           "stroke_mm": stroke, "pressure_bar": 160, "mounting": "flange"}]}


def question(facts: casefile.CaseFile, qid: str) -> str:
    return dict(facts.questions()).get(qid, "")


class ChangedRequirement(unittest.TestCase):
    def test_a_later_amendment_is_told_to_every_agent_and_stays_in_the_open_question(self):
        facts = case()
        self.assertIn("stroke 207 mm", question(facts, "material:REQ-1"))
        self.assertNotIn("NOTE", question(facts, "material:REQ-1"))
        facts.take_news("erp"), facts.take_news("mail")

        facts.merge("clarifications", AMENDMENT)

        self.assertEqual(facts.changed, {"REQ-1": ["stroke 207 -> 200"]})
        for agent in ("erp", "mail"):
            news = facts.take_news(agent)
            self.assertEqual(len(news), 1, news)
            self.assertIn("CHANGED FACT: requirement REQ-1", news[0])
            self.assertIn("stroke 207 -> 200", news[0])
            self.assertIn("stroke 200 mm", news[0])
            self.assertIn("also a candidate that was ruled out", news[0])
        asked = question(facts, "material:REQ-1")
        self.assertIn("stroke 200 mm", asked)
        self.assertIn("NOTE: this requirement changed while you were working (stroke 207 -> 200)", asked)
        self.assertEqual(json.loads(facts.dump())["changed"], facts.changed)

    def test_an_absent_given_for_the_old_values_no_longer_counts(self):
        facts = case()
        facts.merge("material", material(stroke=200))  # the only material of the group: no match at stroke 207
        self.assertIn("closed as absent", facts.answer("material:REQ-1", "absent", "the only one has stroke 200"))
        self.assertEqual(facts.inputs().no_material, {"REQ-1"})

        facts.merge("clarifications", AMENDMENT)

        self.assertNotIn("material:REQ-1", facts.answers)
        self.assertEqual(facts.inputs().no_material, set())
        self.assertIn("material:REQ-1 was for the old values and no longer counts", facts.take_news("erp")[-1])
        self.assertNotIn("material:REQ-1", dict(facts.questions()))  # and the material on file is the match now
        self.assertIn("data:M-1", dict(facts.questions()))

    def test_an_absent_stays_open_again_until_a_match_is_filed(self):
        facts = case()
        facts.merge("material", material(stroke=300))
        facts.answer("material:REQ-1", "absent", "the only one has stroke 300")
        facts.merge("clarifications", AMENDMENT)
        self.assertIn("NOTE: this requirement changed", question(facts, "material:REQ-1"))

    def test_a_first_value_and_a_repeated_value_are_no_change(self):
        # a usable travel waits for its reserve: nobody was asked to match it yet, so its first value is no change
        facts = case(stroke=180, stroke_is_usable_travel=True, reserve_key="ER-1")
        self.assertEqual(question(facts, "material:REQ-1"), "")
        facts.merge("mail", {"messages": [{"sender": "tech@bidder.example", "timestamp": "2029-12-10T09:00:00Z",
                                           "subject": "release", "tender_reference": "TEST-0001",
                                           "tech_release": [{"revision": "B", "reserve_key": "ER-1",
                                                             "reserve_mm_per_end": 10}]}]})
        self.assertIn("stroke 200 mm", question(facts, "material:REQ-1"))
        facts.merge("requirements", {"revision": "B", "requirements": [
            {"requirement_ref": "REQ-1", "bore_mm": "50", "mounting": "Flange"}]})  # the same values, filed again
        self.assertEqual(facts.changed, {})
        self.assertEqual(facts.take_news("erp"), [])

    def test_only_the_values_that_moved_are_named(self):
        facts = case()
        facts.merge("requirements", {"revision": "B", "requirements": [
            {"requirement_ref": "REQ-1", "pressure_bar": 210, "mounting": "foot"}]})
        self.assertEqual(facts.changed["REQ-1"], ["pressure 160 -> 210", "mounting flange -> foot"])


class Languages(unittest.TestCase):
    def test_every_agent_and_the_extraction_are_told_that_pages_come_in_german_and_english(self):
        for text in (worker.ACTIONS_HELP, casefile.EXTRACT_SYSTEM):
            self.assertIn("German", text)
            self.assertIn("English", text)
            self.assertIn("translate", text)


if __name__ == "__main__":
    unittest.main()
