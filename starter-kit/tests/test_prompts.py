"""Slimmer prompts: input tokens are most of a run's cost, and the fixed prompt of every call is about two thirds of
them (run 472732d2). Invented brief only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker  # noqa: E402


class SlimmerPrompts(unittest.TestCase):
    BRIEF = ("# Task\nBid for tender T-1.\n\n## Rules\n1. Quantity: ...\n\n## Site access\n\n- portal: https://p.test\n"
             "  Username: `u_1` · Password: `secret-1`\n- erp: https://e.test\n  Passwort: `secret-2`\n\n"
             "## Submission\n\nSubmit through the portal; request a TAN.\n\n## Additional rules\n\n- Units: ...\n")

    def test_credentials_never_reach_a_prompt(self):
        text = worker.brief_for(self.BRIEF)
        self.assertNotIn("secret-1", text)
        self.assertNotIn("secret-2", text)
        self.assertIn("https://p.test", text)  # where the sites are stays

    def test_the_investigators_brief_leaves_out_how_to_submit_and_keeps_the_rules(self):
        text = worker.brief_for(self.BRIEF, drop_sections=("submission",))
        self.assertNotIn("request a TAN", text)
        self.assertIn("## Additional rules", text)
        self.assertIn("Units", text)

    def test_a_brief_without_such_a_heading_is_kept(self):
        plain = "Bid for tender T-1. Submit through the portal."
        self.assertEqual(worker.brief_for(plain, drop_sections=("submission",)), plain)

    def test_the_research_action_list_has_no_form_chain(self):
        self.assertNotIn('"do": "steps"', worker.ACTIONS_HELP_RESEARCH)
        self.assertIn('"do": "done"', worker.ACTIONS_HELP_RESEARCH)


if __name__ == "__main__":
    unittest.main()
