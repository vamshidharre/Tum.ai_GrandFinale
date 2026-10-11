"""The mail questions carry the subject word and sender the brief prescribes, and the mail agent gets one checklist.

All tender references, subject words, senders and keys here are invented."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import casefile  # noqa: E402

CONTEXT = {
    "tender_reference": "ZX-4411",
    "scenario_date": "2031-03-10T08:00:00Z",
    "authorized_senders": {"discount_approval": "chief", "condition_rules": "terms", "delivery_release": "dispatch",
                           "packaging_tariffs": "dispatch@", "tech_release": "lab", "fx_fixing": "money",
                           "price_indices": "money", "setup_pools": "costing", "project_bonus": None},
    "authorized_subjects": {"condition_rules": "Termsheet", "delivery_release": "Shipslot ZX-4411",
                            "packaging_tariffs": "„Cratefees“", "tech_release": "Labnote",
                            "fx_fixing": ["Moneyfix"], "price_indices": "Metalrate", "setup_pools": "Toolpool",
                            "project_bonus": "Bonusplan", "discount_approval": None},
    "mail_search_hint": "Search words are combined with AND; use the reference and a subject.",
}


def case_with_keys(context=CONTEXT) -> casefile.CaseFile:
    case = casefile.CaseFile("brief", context)
    case.merge("positions", {"positions": [
        {"position": "10", "quantity": 5, "unit": "pcs", "unit_is_set": False, "requirement_ref": "RQ-1"},
        {"position": "20", "quantity": 7, "unit": "pcs", "unit_is_set": False, "requirement_ref": "RQ-2"}]})
    case.merge("clarifications", {"answers": [{"number": "3", "timestamp": "2031-03-01T10:00:00Z", "assignments": [
        {"position": "10", "delivery_commitment": "SL-1", "packaging_tariff": "CF-7", "condition_group": "TG-2"},
        {"position": "20", "delivery_commitment": "SL-2", "setup_pool": "TP-5", "project_bonus": "BP-9"}]}]})
    return case


class SubjectsFromTheBrief(unittest.TestCase):
    def test_subject_words_are_stored_per_topic_without_reference_or_quotes(self):
        case = casefile.CaseFile("brief", CONTEXT)
        self.assertEqual(case.subjects["delivery_release"], "Shipslot")
        self.assertEqual(case.subjects["packaging_tariffs"], "Cratefees")
        self.assertEqual(case.subjects["fx_fixing"], "Moneyfix")
        self.assertEqual(case.subjects["discount_approval"], "")
        self.assertEqual(case.senders["packaging_tariffs"], "dispatch")
        self.assertEqual(set(case.subjects), set(casefile.TOPICS))

    def test_a_context_without_subjects_still_works(self):
        case = casefile.CaseFile("brief", {"tender_reference": "ZX-4411", "scenario_date": "2031-03-10",
                                           "authorized_subjects": "not a mapping"})
        self.assertEqual(set(case.subjects.values()), {""})
        self.assertEqual(case.mail_hint, "")
        self.assertEqual(case.mail_search("delivery_release"),
                         "Search words: ZX-4411 plus a subject word from the brief; look in the inbox and the archive")

    def test_the_context_shape_asks_for_subjects_and_the_search_hint(self):
        self.assertIn('"authorized_subjects"', casefile.CONTEXT_SHAPE)
        self.assertIn('"mail_search_hint"', casefile.CONTEXT_SHAPE)


class MailQuestionTexts(unittest.TestCase):
    def test_each_mail_question_names_its_search_words_and_sender(self):
        questions = dict(case_with_keys().questions())
        self.assertIn("Search words: ZX-4411 Shipslot; authorized sender: dispatch@<bidder mailbox domain>; "
                      "look in the inbox and the archive", questions["delivery_commitment:SL-1"])
        self.assertIn("Search words: ZX-4411 Cratefees; authorized sender: dispatch@", questions["packaging_tariff:CF-7"])
        self.assertIn("Search words: ZX-4411 Termsheet; authorized sender: terms@", questions["condition_group:TG-2"])
        self.assertIn("Search words: ZX-4411 Toolpool; authorized sender: costing@", questions["setup_pool:TP-5"])
        # a topic the brief names no sender for gets no sender, and no subject means no invented one
        self.assertIn("Search words: ZX-4411 Bonusplan; look in", questions["project_bonus:BP-9"])
        self.assertIn("Search words: ZX-4411 plus a subject word from the brief; authorized sender: chief@",
                      questions["discount"])

    def test_the_mailbox_domain_is_filled_in_once_known(self):
        case = case_with_keys()
        case.mailbox = "sales@maker-invented.test"
        questions = dict(case.questions())
        self.assertIn("authorized sender: dispatch@maker-invented.test;", questions["delivery_commitment:SL-2"])
        self.assertIn("authorized sender: chief@maker-invented.test;", questions["discount"])


class MailChecklist(unittest.TestCase):
    def setUp(self):
        agent.SITE_ROLES.clear()
        agent.RELEASED.clear()

    tearDown = setUp

    def test_the_checklist_lists_every_needed_topic_once_with_its_keys(self):
        case = case_with_keys()
        mine = agent.questions_for_site("mail", case.questions())
        checklist = case.mail_checklist(mine)
        lines = checklist.splitlines()
        self.assertTrue(lines[0].startswith("MAIL CHECKLIST"))
        self.assertIn("The brief on mail search: Search words are combined with AND", checklist)
        topic_lines = [line for line in lines if line.startswith("- ")]
        self.assertEqual([line.split(":")[0] for line in topic_lines],
                         ["- discount approval", "- condition rules (TG-2)", "- packaging tariffs (CF-7)",
                          "- delivery release (SL-1, SL-2)", "- setup pools (TP-5)", "- project bonus (BP-9)"])
        self.assertIn("- delivery release (SL-1, SL-2): Search words: ZX-4411 Shipslot; authorized sender: dispatch@",
                      checklist)

    def test_reserve_index_and_fx_questions_map_to_their_topics(self):
        case = casefile.CaseFile("brief", CONTEXT)
        checklist = case.mail_checklist([("reserve:RQ-1", "r"), ("index:MX-1", "i"), ("fx:FK-3", "f"),
                                         ("mailbox", "m")])
        self.assertIn("- price indices (MX-1): Search words: ZX-4411 Metalrate; authorized sender: money@", checklist)
        self.assertIn("- tech release (RQ-1): Search words: ZX-4411 Labnote; authorized sender: lab@", checklist)
        self.assertIn("- fx fixing (FK-3): Search words: ZX-4411 Moneyfix; authorized sender: money@", checklist)

    def test_no_checklist_without_mail_questions(self):
        case = casefile.CaseFile("brief", CONTEXT)
        self.assertEqual(case.mail_checklist([("positions", "p"), ("material:RQ-1", "m")]), "")
        self.assertEqual(case.mail_checklist([]), "")


if __name__ == "__main__":
    unittest.main()
