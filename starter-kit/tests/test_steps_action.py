"""The "steps" action: a chain of label-addressed form steps across page reloads in one model turn (worker.py).

In every clean sandbox run the bid clerk needed 40-46 turns (3.5-4.5 min) for the configurator, one choice and one
"apply" click per turn, because a page reload drops the rest of a turn's actions. These tests use an invented
cascading form whose element ids change on every reload."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
from worker import Agent, find_control  # noqa: E402


class CascadeForm:
    """Choose a size, apply; choose a finish, apply; type a price; continue. Ids shift with every page."""

    def __init__(self):
        self.base_url = "http://form.test"
        self.page = SimpleNamespace(url=self.base_url, main_frame=SimpleNamespace(evaluate=self._alive))
        self.document, self.document_source = "", ""
        self.stage, self.offset, self.values = 0, 0, {}

    async def _alive(self, script):
        return True

    def take_events(self):
        return []

    def ids(self):
        return {name: str(index + self.offset) for index, name in enumerate(("size", "apply", "finish", "price", "next"))}

    async def observe(self):
        i = self.ids()
        lines = [f"url: {self.base_url}/position", "title: Position 1",
                 f'[{i["size"]}] select "Size" options(* = selected): "S"; "M"; "L"',
                 f'[{i["apply"]}] button "Apply choice"']
        if self.stage >= 1:
            lines.append(f'[{i["finish"]}] select "Finish" options(* = selected): "matt"; "gloss"')
        if self.stage >= 2:
            lines.append(f'[{i["price"]}] input "Unit price" value=""')
            lines.append(f'[{i["next"]}] button "Continue"')
        return "\n".join(lines)

    async def select(self, aid, value):
        i = self.ids()
        key = "size" if str(aid) == i["size"] else "finish" if str(aid) == i["finish"] else None
        if key is None:
            return f"[{aid}] is not a list on this page"
        self.values[key] = value
        return f'selected "{value}"'

    async def click(self, aid):
        i = self.ids()
        if str(aid) == i["apply"]:
            self.stage, self.offset = self.stage + 1, self.offset + 10  # the page reloads: new ids
            return "clicked; the page reloaded"
        if str(aid) == i["next"]:
            self.values["continued"] = True
            return "clicked; next position"
        return f"[{aid}] not found"

    async def fill(self, aid, text, enter=False):
        if str(aid) != self.ids()["price"]:
            return f"[{aid}] not found"
        self.values["price"] = text
        return f"filled [{aid}]"


STEPS = [{"field": "Size", "value": "M"}, {"press": "Apply choice"}, {"field": "Finish", "value": "matt"},
         {"press": "Apply choice"}, {"field": "Unit price", "value": "123,45"}, {"press": "Continue"}]


async def run(form, guard=None):
    replies = [{"thought": "fill position 1 in one go", "actions": [{"do": "steps", "steps": STEPS}]},
               {"thought": "done", "actions": [{"do": "done", "summary": "filled"}]}]
    seen = []

    async def model(messages, **kwargs):
        seen.append(messages[-1]["content"])
        return replies.pop(0)

    with patch.object(llm, "chat_json", model):
        outcome = await Agent("submit", form, "system", lambda: "job", max_steps=4, guard=guard).run()
    return outcome, seen


class StepsAction(unittest.IsolatedAsyncioTestCase):
    async def test_a_whole_cascade_runs_in_one_turn_across_reloads(self):
        form = CascadeForm()
        outcome, seen = await run(form)
        self.assertEqual(form.values, {"size": "M", "finish": "matt", "price": "123,45", "continued": True})
        self.assertEqual(outcome["steps"], 2)  # one turn for the whole position, one to finish
        self.assertIn("ran all 6 steps", seen[1])

    async def test_the_guard_refuses_a_step_like_any_other_action(self):
        form = CascadeForm()

        def guard(action):
            return "refused: price not in the bid" if action.get("do") == "fill" else None

        _, seen = await run(form, guard)
        self.assertNotIn("price", form.values)
        self.assertIn("Step 5 was refused: refused: price not in the bid", seen[1])
        self.assertNotIn("continued", form.values)  # nothing after the refused step ran


class RejectingForm(CascadeForm):
    """Its "Continue" is refused with a message the page announces, and the form shows empty again."""

    async def click(self, aid):
        if str(aid) == self.ids()["next"]:
            self.rejected = True
            return "clicked; the page reloaded"
        return await super().click(aid)

    async def observe(self):
        page = await super().observe()
        return page + ("\n(alert) Lieferzeit fehlt" if getattr(self, "rejected", False) else "")


class StepsStopAtErrors(unittest.IsolatedAsyncioTestCase):
    async def test_an_error_message_after_a_press_stops_the_chain_and_is_reported(self):
        """Run d3ca14df: the form rejected the position, the chain reported success, the clerk looped 95 turns."""
        form = RejectingForm()
        _, seen = await run(form)
        self.assertIn("After step 6 the page says: (alert) Lieferzeit fehlt", seen[1])
        self.assertNotIn("ran all", seen[1])
        self.assertIn("if it confirms what you did, go on", seen[1])  # a notice can be a confirmation too


class FindControl(unittest.TestCase):
    PAGE = ('[4] select "Bore" options: "40"\n[5] button "Apply"\n[6] button "Apply all" (disabled)\n'
            '[7] input "Unit price net" value=""\n[8] button "Open" [9] button "Open"')

    def test_exact_label_wins_over_a_longer_one(self):
        self.assertEqual(find_control(self.PAGE, "Apply", ("button",)), ("5", "button"))

    def test_a_label_part_finds_the_one_control_containing_it(self):
        self.assertEqual(find_control(self.PAGE, "unit price", ("input",)), ("7", "input"))

    def test_two_controls_with_the_same_label_are_not_guessed(self):
        aid, why = find_control(self.PAGE, "Open", ("button",))
        self.assertIsNone(aid)
        self.assertIn("2 controls", why)

    def test_a_control_in_a_collapsed_menu_is_found(self):
        self.assertEqual(find_control('[3] (in collapsed menu) link "Export" -> /x', "Export", ("link",)), ("3", "link"))

    def test_a_disabled_control_is_not_used(self):
        self.assertEqual(find_control(self.PAGE, "Apply all", ("button",))[0], None)


if __name__ == "__main__":
    unittest.main()
