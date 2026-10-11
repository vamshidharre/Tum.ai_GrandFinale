"""The "path" action: a model-named chain of clicks by label across page loads in one turn (worker.py Agent._path).

Run 63f3be2e: the investigators spent one model turn (2-3 s) per click, ERP 64 turns, customer portal 54. These tests
use an invented multi-page site whose element ids change on every page load."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
import worker  # noqa: E402
from worker import Agent  # noqa: E402

# page -> (title, [(kind, label, page it opens)])
SITE = {
    "home": ("Garden supply desk", [("link", "Seed catalogue", "catalogue"), ("link", "Help", "help"),
                                    ("button", "Sign out", "home")]),
    "catalogue": ("Seed catalogue", [("link", "Tomato Roma 10734", "item-10734"), ("link", "Basil Genovese 20311", "item-10734"),
                                     ("link", "Back to desk", "home")]),
    "item-10734": ("Tomato Roma 10734", [("clickable", "Overview", "item-10734"), ("clickable", "Sowing calendar", "calendar"),
                               ("clickable", "Packet prices", "prices")]),
    "calendar": ("Sowing calendar", [("clickable", "Overview", "item-10734"), ("link", "Next month", "calendar2")]),
    "calendar2": ("Sowing calendar, page 2", [("link", "Next month", "calendar3")]),
    "calendar3": ("Sowing calendar, page 3", [("link", "Next month", "calendar4")]),
    "calendar4": ("Sowing calendar, page 4", [("link", "Next month", "calendar5")]),
    "calendar5": ("Sowing calendar, page 5", [("link", "Next month", "calendar6")]),
    "calendar6": ("Sowing calendar, page 6", [("link", "Next month", "calendar7")]),
    "calendar7": ("Sowing calendar, page 7", [("link", "Next month", "calendar8")]),
    "calendar8": ("Sowing calendar, page 8", []),
    "prices": ("Packet prices", [("link", "Bulk tiers", "tiers"), ("link", "Bulk tiers archive", "tiers")]),
    "tiers": ("Bulk tiers", []),
    "help": ("Help", []),
}


class GardenDesk:
    """An invented shop back office: every click loads another page, and every page load renumbers the ids."""

    def __init__(self, start="home", alert_on=None):
        self.base_url = "http://garden.test"
        self.at, self.offset, self.clicks, self.alert_on = start, 0, [], alert_on
        self.page = SimpleNamespace(url=self._url(), main_frame=SimpleNamespace(evaluate=self._alive))
        self.document, self.document_source = "", ""

    def _url(self):
        return f"{self.base_url}/{self.at}"

    async def _alive(self, script):
        return True

    def take_events(self):
        return []

    def controls(self):
        return {str(self.offset + index): entry for index, entry in enumerate(SITE[self.at][1], start=1)}

    async def observe(self):
        lines = [f"url: {self._url()}", f"title: {SITE[self.at][0]}"]
        lines += [f'[{aid}] {kind} "{label}"' for aid, (kind, label, _) in self.controls().items()]
        if self.alert_on == self.at:
            lines.append("(alert) Session about to expire")
        self.document = "\n".join(lines)
        self.document_source = self._url()
        return self.document

    async def click(self, aid):
        control = self.controls().get(str(aid))
        if control is None:
            return f"there is no element [{aid}] on the page any more; read the current page"
        self.clicks.append(control[1])
        self.at, self.offset = control[2], self.offset + 20
        self.page.url = self._url()
        return f'clicked [{aid}] "{control[1]}"; now at {self.page.url}'


async def run(site, labels, guard=None, on_action=None):
    replies = [{"thought": "go to the page I need in one go", "actions": [{"do": "path", "labels": labels}]},
               {"thought": "done", "actions": [{"do": "done", "summary": "there"}]}]
    seen = []

    async def model(messages, **kwargs):
        seen.append(messages[-1]["content"])
        return replies.pop(0)

    agent = Agent("erp", site, "system", lambda: "job", max_steps=4, guard=guard)
    agent.on_action = on_action
    with patch.object(llm, "chat_json", model):
        outcome = await agent.run()
    return outcome, seen, agent


class PathAction(unittest.IsolatedAsyncioTestCase):
    async def test_a_chain_across_page_loads_runs_in_one_turn(self):
        site = GardenDesk()
        outcome, seen, agent = await run(site, ["Seed catalogue", "Roma", "Packet prices"])
        self.assertEqual(site.clicks, ["Seed catalogue", "Tomato Roma 10734", "Packet prices"])  # a part of a label is fine
        self.assertEqual(site.at, "prices")
        self.assertEqual(outcome["steps"], 2)  # one turn for three clicks, one to finish
        self.assertIn("path: clicked 3 of 3: Seed catalogue > Roma > Packet prices", seen[1])
        self.assertIn("CURRENT PAGE\nurl: http://garden.test/prices", seen[1])  # the page at the end is shown
        self.assertIn("http://garden.test/item-10734", agent.coverage.views)  # pages passed on the way count as seen

    async def test_it_stops_at_a_label_it_cannot_find_and_says_how_far_it_got(self):
        site = GardenDesk()
        _, seen, _ = await run(site, ["Seed catalogue", "Fertiliser", "Packet prices"])
        self.assertEqual(site.clicks, ["Seed catalogue"])
        self.assertIn("path: clicked 1 of 3: Seed catalogue", seen[1])
        self.assertIn('Stopped at "Fertiliser": no control labelled "Fertiliser"', seen[1])

    async def test_an_ambiguous_label_is_not_guessed(self):
        site = GardenDesk(start="prices")
        _, seen, _ = await run(site, ["Bulk"])
        self.assertEqual(site.clicks, [])
        self.assertIn('2 controls are labelled "Bulk" ([1], [2])', seen[1])

    async def test_the_guard_refuses_a_click_of_the_chain(self):
        site = GardenDesk()

        def guard(action):  # refuses any click on the item page's price tab, wherever its id is now
            if action.get("do") == "click" and site.controls().get(str(action.get("id")), ("", ""))[1] == "Packet prices":
                return "refused: not this tab"
            return None

        _, seen, _ = await run(site, ["Seed catalogue", "Tomato Roma 10734", "Packet prices"], guard)
        self.assertEqual(site.clicks, ["Seed catalogue", "Tomato Roma 10734"])
        self.assertIn('"Packet prices" was refused: refused: not this tab', seen[1])

    async def test_at_most_six_clicks_run(self):
        site = GardenDesk(start="calendar")
        _, seen, _ = await run(site, ["Next month"] * 8)
        self.assertEqual(len(site.clicks), worker.PATH_LIMIT)
        self.assertEqual(site.at, "calendar7")
        self.assertIn(f"only the first {worker.PATH_LIMIT} of 8 labels", seen[1])

    async def test_a_message_the_page_announces_stops_the_chain(self):
        site = GardenDesk(alert_on="catalogue")
        _, seen, _ = await run(site, ["Seed catalogue", "Tomato Roma 10734"])
        self.assertEqual(site.clicks, ["Seed catalogue"])
        self.assertIn('After "Seed catalogue" the page says: (alert) Session about to expire', seen[1])

    async def test_each_click_reaches_on_action_with_the_page_it_was_taken_on(self):
        """Walk learning (agent.py WalkRecorder) reads the clicked control's label from that page by its id."""
        site = GardenDesk()
        calls = []

        async def on_action(action, page_before, result):
            label = next(line for line in page_before.splitlines() if line.startswith(f"[{action['id']}] "))
            calls.append((action["do"], label.split('"')[1]))
            return None

        await run(site, ["Seed catalogue", "Tomato Roma 10734"], on_action=on_action)
        self.assertEqual(calls, [("click", "Seed catalogue"), ("click", "Tomato Roma 10734")])

    async def test_bad_input_is_reported_not_crashed_on(self):
        site = GardenDesk()
        _, seen, _ = await run(site, [])
        self.assertIn('path needs "labels"', seen[1])


class WalkLearningThroughAPath(unittest.IsolatedAsyncioTestCase):
    async def test_a_record_walked_with_a_path_is_learned_like_one_walked_click_by_click(self):
        from agent import WalkRecorder
        site = GardenDesk(start="catalogue")
        walks = WalkRecorder()

        async def on_action(action, page_before, result):
            walks.browsed(action, page_before, site.page.url)

        await run(site, ["Tomato Roma 10734", "Packet prices"], on_action=on_action)
        self.assertIsNotNone(walks.walk)  # the record was opened: a walk started
        self.assertEqual(walks.walk["pending"], ["Packet prices"])  # the tab clicked inside it is noted


class PagingInPlaceWithAPath(unittest.IsolatedAsyncioTestCase):
    async def test_pages_a_path_passed_in_place_are_filed_with_the_keep(self):
        """The paged-keep trail: a document paged in place by a path is kept whole, as when paged click by click."""
        from tests.test_paged_keep import DONE, KEEP, items, run_investigator, turn
        filed, asked, _ = await run_investigator([turn({"do": "path", "labels": ["Next page"] * 3}), turn(KEEP), turn(DONE)])
        self.assertIn("path: clicked 3 of 3", asked[1])
        self.assertEqual(items(filed[0][1]), [1, 2, 3, 4])


class Prompts(unittest.TestCase):
    def test_investigators_get_path_and_still_no_form_chain(self):
        self.assertIn('"do": "path"', worker.ACTIONS_HELP_RESEARCH)
        self.assertNotIn('"do": "steps"', worker.ACTIONS_HELP_RESEARCH)
        self.assertNotIn('"do": "path"', worker.ACTIONS_HELP)  # the bid clerk has steps with presses

    def test_the_investigator_prompt_says_to_chain_known_navigation(self):
        import agent
        self.assertIn("ONE turn with path", agent.INVESTIGATOR)


if __name__ == "__main__":
    unittest.main()
