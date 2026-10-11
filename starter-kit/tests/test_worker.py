"""The generic action verifier must expose silent portal failures to the model."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
from worker import Agent, action_fingerprint, state_fingerprint  # noqa: E402


class FakeTab:
    def __init__(self, change_on_fill: bool):
        self.base_url = "http://portal.test"
        self.page = SimpleNamespace(url=self.base_url, main_frame=SimpleNamespace(evaluate=self._alive))
        self.document, self.document_source = "", ""
        self.change_on_fill = change_on_fill
        self.view = 'url: http://portal.test\ntitle: Form\n[1] input "Name" value=""'

    async def _alive(self, script):
        return True

    async def observe(self):
        return self.view

    def take_events(self):
        return []

    async def fill(self, aid, text, enter=False):
        if self.change_on_fill:
            self.view = f'url: http://portal.test\ntitle: Form\n[1] input "Name" value="{text}"'
        return f"filled [{aid}]"


class ActionVerification(unittest.IsolatedAsyncioTestCase):
    async def run_agent(self, change_on_fill: bool):
        replies = [
            {"thought": "", "actions": [{"do": "fill", "id": 1, "text": "Ada"}]},
            {"thought": "", "actions": [{"do": "done", "summary": "checked"}]},
        ]
        asked = []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("test", FakeTab(change_on_fill), "system", lambda: "job", max_steps=4).run()
        return outcome, asked

    async def test_changed_form_state_is_reported(self):
        outcome, asked = await self.run_agent(True)
        self.assertTrue(outcome["done"])
        self.assertIn("verification: fill visibly changed the page state", asked[1])

    async def test_silent_noop_is_reported_with_a_recovery_hint(self):
        outcome, asked = await self.run_agent(False)
        self.assertTrue(outcome["done"])
        self.assertIn("verification: fill caused no visible page change", asked[1])
        self.assertIn("instead of repeating it blindly", asked[1])

    def test_generated_action_ids_do_not_hide_a_repeated_control(self):
        first = 'url: http://portal.test\n[1] button "Continue"'
        reread = 'url: http://portal.test\n[48] button "Continue"'
        self.assertEqual(action_fingerprint([{"do": "click", "id": 1}], first),
                         action_fingerprint([{"do": "click", "id": 48}], reread))

    def test_generated_action_ids_do_not_count_as_a_state_change(self):
        first = 'url: http://portal.test\n[1] button "Continue"'
        reread = 'url: http://portal.test\n[48] button "Continue"'
        self.assertEqual(state_fingerprint(first), state_fingerprint(reread))


class LoopDetectionAndChaining(unittest.IsolatedAsyncioTestCase):
    """Tests for repeated-action loop warnings, stale-ID chaining protection, and stuck-tab renewal."""

    async def test_repeating_same_action_on_unchanged_page_injects_loop_warning_on_third_repeat(self):
        tab = FakeTab(change_on_fill=False)
        replies = [
            {"thought": "try 1", "actions": [{"do": "fill", "id": 1, "text": "Same"}]},
            {"thought": "try 2", "actions": [{"do": "fill", "id": 1, "text": "Same"}]},
            {"thought": "try 3", "actions": [{"do": "fill", "id": 1, "text": "Same"}]},
            {"thought": "stop", "actions": [{"do": "done", "summary": "stopped"}]},
        ]
        asked = []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("loop-test", tab, "system", lambda: "job", max_steps=6).run()
        self.assertTrue(outcome["done"])
        self.assertNotIn("You have sent the same actions three times", asked[2])
        self.assertIn("You have sent the same actions three times with the same result", asked[3])

    async def test_actions_after_a_navigating_action_are_skipped_because_ids_are_stale(self):
        tab = FakeTab(change_on_fill=True)

        async def click(aid):
            tab.view = 'url: http://portal.test/step2\ntitle: Step 2\n[9] button "Next"'
            tab.page.url = "http://portal.test/step2"
            return f"clicked [{aid}]"

        tab.click = click
        replies = [
            {"thought": "click then fill", "actions": [
                {"do": "click", "id": 1},
                {"do": "fill", "id": 9, "text": "stale"},
            ]},
            {"thought": "done", "actions": [{"do": "done", "summary": "ok"}]},
        ]
        asked = []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("chain-test", tab, "system", lambda: "job", max_steps=4).run()
        self.assertTrue(outcome["done"])
        self.assertIn("the page changed, its ids are stale", asked[1])

    async def test_a_navigation_failure_that_shows_a_broken_tab_renews_it_at_once(self):
        # Scored run a176359f: a detached frame cost 125 s (a 30 s click, a 45 s read, then a failed reload) before
        # the second failure renewed the tab; a fresh tab loaded at once.
        tab = FakeTab(change_on_fill=False)
        renew_calls, clicks = [], []

        async def failing_click(aid):
            clicks.append(aid)
            if len(clicks) > 1:
                return f"clicked [{aid}]"
            return f"click [{aid}] failed: Page.click: net::ERR_ABORTED; maybe frame was detached?"

        async def renew():
            renew_calls.append(True)
            tab.view = 'url: http://portal.test\ntitle: Fresh\n[1] button "Retry"'
            return tab.view

        tab.click = failing_click
        tab.renew = renew
        replies = [
            {"thought": "click 1", "actions": [{"do": "click", "id": 1}]},
            {"thought": "click 2", "actions": [{"do": "click", "id": 1}]},
            {"thought": "done", "actions": [{"do": "done", "summary": "recovered"}]},
        ]
        asked = []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("renew-test", tab, "system", lambda: "job", max_steps=5).run()
        self.assertTrue(outcome["done"])
        self.assertEqual(len(renew_calls), 1)
        self.assertIn("this tab kept failing to load, so it was replaced by a fresh one", asked[1])

    async def test_two_consecutive_navigation_failures_trigger_tab_renew(self):
        tab = FakeTab(change_on_fill=False)
        renew_calls = []

        async def failing_click(aid):  # a timeout alone may be a slow page: the tab is replaced at the second one
            return f"click [{aid}] failed: Page.click: Timeout 6000ms exceeded."

        async def renew():
            renew_calls.append(True)
            tab.view = 'url: http://portal.test\ntitle: Fresh\n[1] button "Retry"'
            return tab.view

        tab.click = failing_click
        tab.renew = renew
        replies = [
            {"thought": "click 1", "actions": [{"do": "click", "id": 1}]},
            {"thought": "click 2", "actions": [{"do": "click", "id": 1}]},
            {"thought": "done", "actions": [{"do": "done", "summary": "recovered"}]},
        ]
        asked = []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("renew-test", tab, "system", lambda: "job", max_steps=5).run()
        self.assertTrue(outcome["done"])
        self.assertEqual(len(renew_calls), 1)
        self.assertIn("this tab kept failing to load, so it was replaced by a fresh one", asked[2])


class TokenCutOffAndTeamWakeup(unittest.IsolatedAsyncioTestCase):
    """Tests for LLM token-cutoff retry doubling and Agent idle/wakeup lifecycle."""

    async def test_chat_json_doubles_max_tokens_when_finish_reason_is_length(self):
        requested_tokens = []

        class FakeCompletions:
            async def create(self, **kwargs):
                requested_tokens.append(kwargs.get("max_tokens"))
                if len(requested_tokens) == 1:
                    choice = SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(content='{"answers": [{"number": "Q-1"'),
                    )
                else:
                    choice = SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content='{"answers": [{"number": "Q-1"}]}'),
                    )
                return SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, prompt_tokens_details=None),
                    choices=[choice],
                )

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        gate = llm.asyncio.Semaphore(4)

        async def fast_sleep(_):
            return None

        with patch.object(llm, "_session", return_value=(fake_client, gate)), \
             patch.object(llm.asyncio, "sleep", fast_sleep):
            result = await llm.chat_json([{"role": "user", "content": "extract"}], max_tokens=8000, label="extract:clarifications")
        self.assertEqual(result, {"answers": [{"number": "Q-1"}]})
        self.assertEqual(requested_tokens, [8000, 16000])

    async def test_idle_callback_holds_finished_agent_and_resumes_when_news_arrives(self):
        tab = FakeTab(change_on_fill=True)
        idle_responses = ["Open questions remain: data:MAT-101", None]
        replies = [
            {"thought": "pause until requirements arrive", "actions": [{"do": "done", "summary": "waiting"}]},
            {"thought": "now finish", "actions": [{"do": "done", "summary": "all done"}]},
        ]
        asked = []

        async def idle_hook(reason: str):
            return idle_responses.pop(0)

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("erp-test", tab, "system", lambda: "job", max_steps=5, idle=idle_hook).run()
        self.assertTrue(outcome["done"])
        self.assertEqual(outcome["summary"], "all done")
        self.assertEqual(outcome["steps"], 2)
        self.assertIn("Open questions remain: data:MAT-101", asked[1])

    async def test_multi_page_navigation_cycle_triggers_loop_warning_on_third_pass(self):
        pages = [
            "url: https://portal.example/q?page=1\n[1] link \"Next\"",
            "url: https://portal.example/q?page=2\n[1] link \"Prev\"",
            "url: https://portal.example/q?page=1\n[1] link \"Next\"",
            "url: https://portal.example/q?page=2\n[1] link \"Prev\"",
            "url: https://portal.example/q?page=1\n[1] link \"Next\"",
            "url: https://portal.example/q?page=2\n[1] link \"Prev\"",
        ]
        idx = [0]

        class CyclingTab(FakeTab):
            async def observe(self) -> str:
                return pages[min(idx[0], len(pages) - 1)]

            async def click(self, aid) -> str:
                idx[0] += 1
                self.page.url = f"https://portal.example/q?page={(idx[0] % 2) + 1}"
                return f"clicked [{aid}]"

        tab = CyclingTab(pages[0])
        replies = [
            {"thought": "go p2", "actions": [{"do": "click", "id": 1}]},
            {"thought": "go p1", "actions": [{"do": "click", "id": 1}]},
            {"thought": "go p2", "actions": [{"do": "click", "id": 1}]},
            {"thought": "go p1", "actions": [{"do": "click", "id": 1}]},
            {"thought": "go p2", "actions": [{"do": "click", "id": 1}]},
            {"thought": "stop", "actions": [{"do": "done", "summary": "escaped cycle"}]},
        ]
        seen_prompts: list[str] = []

        async def model(messages, **kwargs):
            seen_prompts.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("cycle-test", tab, "system", lambda: "job", max_steps=8).run()
        self.assertTrue(outcome["done"])
        self.assertTrue(any("same actions three times" in p for p in seen_prompts))


if __name__ == "__main__":
    unittest.main()



class CircleDetection(unittest.IsolatedAsyncioTestCase):
    """Sandbox run e3804980: the ERP agent rejected the same material every 6 turns for 46 turns, which neither the
    back-to-back nor the alternating repeat check caught."""

    async def run_cycle(self, **options):
        cycle = ["search the group", "open the first hit", "read the stroke", "go back", "open it again",
                 "this material has the wrong stroke"]
        replies = [{"thought": cycle[i % 6], "actions": [{"do": "fill", "id": 1, "text": str(i)}]} for i in range(20)]
        replies.append({"thought": "stop", "actions": [{"do": "done", "summary": "stopped"}]})
        asked = []

        async def model(messages, **kwargs):
            asked.append(messages[-1]["content"])
            return replies.pop(0)

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("test", FakeTab(True), "system", lambda: "job", max_steps=30, **options).run()
        return outcome, asked

    async def test_thought_repeated_three_times_in_twelve_turns_gets_the_agents_own_hint(self):
        outcome, asked = await self.run_cycle(circle_hint="file the candidate with keep")
        hinted = [i for i, text in enumerate(asked) if "file the candidate with keep" in text]
        self.assertEqual(hinted, [13, 19])  # third pass of the cycle, then again at most every 6 turns
        self.assertTrue(outcome["done"])  # an investigator is hinted, never forced to finish

    async def test_investigator_repeating_an_unchanged_page_is_sent_idle(self):
        """Sandbox run 74b22ceb: the tender-portal agent opened the same unchanged page 122 times."""
        replies = [{"thought": "check the remaining documents", "actions": [{"do": "fill", "id": 1, "text": "Same"}]}
                   for _ in range(20)]
        idled = []

        async def model(messages, **kwargs):
            return replies.pop(0)

        async def idle(state):
            idled.append(len(replies))
            return None  # no news: the investigation ends

        with patch.object(llm, "chat_json", model):
            outcome = await Agent("test", FakeTab(False), "system", lambda: "job", max_steps=30, idle=idle).run()
        self.assertTrue(outcome["done"])
        self.assertEqual(idled, [14])  # sent idle after six identical rounds, not after the 30-step cap


class TeamWakeup(unittest.IsolatedAsyncioTestCase):
    """Sandbox run 024bd2ac: the ERP agent went idle while documents were still being read, with its material
    questions already open. They were never new, so nothing woke it for 400 s."""

    class FakeCase:
        def __init__(self):
            self.version, self.pending, self.news = 0, 1, {}
            self.open = [("material:A-1", "the ERP material for requirement A-1")]

        def questions(self):
            return self.open

        async def wait_change(self, version, timeout):
            await asyncio.sleep(0.01)
            return self.version != version

    async def test_idle_agent_is_woken_when_the_documents_it_waited_for_are_read(self):
        from agent import Team
        case = self.FakeCase()
        team = Team(case, ["erp", "mail"], deadline=time.time() + 5)
        hold = asyncio.create_task(team.hook("erp")("done"))
        await asyncio.sleep(0.05)
        self.assertFalse(hold.done())  # still waiting: nothing has been read yet
        case.pending, case.version = 0, 1  # the requirements extraction is done; the question was open before
        news = await asyncio.wait_for(hold, 2)
        self.assertIn("material:A-1", news or "")
