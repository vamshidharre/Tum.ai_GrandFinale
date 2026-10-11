"""Several agents on one website (agent.py `crew` and `share`), the start of every website before the first model
turn (`open_site`), and the second request for a model call that hangs (llm.py `chat_json`)."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import llm  # noqa: E402
from browser_env import Tab  # noqa: E402


class FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page


class FakePage:
    def __init__(self, context, login=False):
        self.context, self.url, self.frames, self.login = context, "about:blank", [], login
        self.main_frame = self

    def on(self, *args):
        pass

    async def evaluate(self, script, *args):
        if "LOGIN" in str(script) or "password" in str(script) and args:
            return "no password field on this page"
        return self.login

    async def goto(self, url, **kwargs):
        self.url = url

    async def title(self):
        return ""


def questions(*ids):
    return [(qid, f"find {qid}") for qid in ids]


class Sharing(unittest.TestCase):
    def setUp(self):
        for store in (agent.CREW, agent.ASSIGNED, agent.SITE_ROLES):
            store.clear()
        agent.RELEASED.clear()
        agent.SITE_ROLES.update(erp="inventory", mail="inbox")  # invented site names: the roles come from the brief

    def test_one_agent_on_a_website_sees_all_of_its_questions(self):
        routed = agent.questions_for_site("inventory", questions("material:A-1", "material:A-2", "discount"))
        self.assertEqual([qid for qid, _ in routed], ["material:A-1", "material:A-2"])

    def test_a_crew_shares_the_questions_out_evenly_and_keeps_them_stable(self):
        agent.CREW["inventory"] = ["inventory", "inventory~2", "inventory~3"]
        open_ = questions("material:A-1", "material:A-2", "material:A-3", "material:A-4", "material:A-5")
        shares = {name: [qid for qid, _ in agent.questions_for_site(name, open_)] for name in agent.CREW["inventory"]}
        self.assertEqual(sorted(sum(shares.values(), [])), [qid for qid, _ in open_])
        self.assertEqual(sorted(len(share) for share in shares.values()), [1, 2, 2])
        # Once a question is answered, the others stay where they are; a new one goes to the least busy agent.
        later = [q for q in open_ if q[0] != shares["inventory~2"][0]] + questions("data:M-9")
        again = {name: [qid for qid, _ in agent.questions_for_site(name, later)] for name in agent.CREW["inventory"]}
        for name in again:
            self.assertTrue(set(again[name]) - {"data:M-9"} <= set(shares[name]))
        self.assertIn("data:M-9", again["inventory~2"])

    def test_on_the_webmail_one_kind_of_record_stays_with_one_agent(self):
        agent.CREW["inbox"] = ["inbox", "inbox~2"]
        open_ = questions("mailbox", "discount", "delivery_commitment:D-1", "delivery_commitment:D-2", "fx:F-1")
        shares = {name: [qid for qid, _ in agent.questions_for_site(name, open_)] for name in agent.CREW["inbox"]}
        holder = next(name for name, share in shares.items() if "delivery_commitment:D-1" in share)
        self.assertIn("delivery_commitment:D-2", shares[holder])
        self.assertEqual(sorted(sum(shares.values(), [])), sorted(qid for qid, _ in open_))

    def test_a_released_question_is_offered_to_every_agent(self):
        agent.CREW["inventory"] = ["inventory", "inventory~2"]
        agent.RELEASED.add("discount")
        for name in agent.CREW["inventory"]:
            self.assertIn("discount", [qid for qid, _ in agent.questions_for_site(name, questions("discount"))])

    def test_a_helper_plays_the_role_of_its_website(self):
        self.assertEqual(agent.base_site("inventory~3"), "inventory")
        self.assertEqual(agent.site_role("inventory~3"), "erp")
        self.assertEqual(agent.site_role("inbox~2"), "mail")


class Spawning(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for store in (agent.CREW, agent.ASSIGNED, agent.SITE_ROLES, agent.AGENTS):
            store.clear()
        agent.SITE_ROLES.update(erp="inventory")

    def site(self, login=False) -> Tab:
        context = FakeContext()
        return Tab(FakePage(context, login), {"siteId": "inventory", "url": "http://inventory.test",
                                              "credentials": {"username": "u", "password": "p"}})

    async def test_helpers_start_once_the_sites_agent_is_under_way_and_there_is_work_to_share(self):
        tab = self.site()
        case = SimpleNamespace(questions=lambda: questions("material:A-1", "material:A-2", "material:A-3"))
        team = agent.Team(case, ["inventory"], time.time() + 600)
        agent.AGENTS["inventory"] = SimpleNamespace(steps=3)
        started = []

        async def run(helper, name, opening):
            started.append((name, helper.site_id, helper.page.url, opening[0]))
            return {"done": True}

        outcomes = await agent.crew(case, team, tab, 3, time.time() + 600, run)
        self.assertEqual([name for name, *_ in started], ["inventory~2", "inventory~3"])
        self.assertTrue(all(url == "http://inventory.test/" for _, _, url, _ in started))
        self.assertEqual(agent.CREW["inventory"], ["inventory", "inventory~2", "inventory~3"])
        self.assertEqual(team.names, {"inventory", "inventory~2", "inventory~3"})
        self.assertEqual(len(tab.page.context.pages), 2)  # new tabs of the same browser session
        self.assertEqual(outcomes, [{"done": True}, {"done": True}])

    async def test_no_helpers_while_the_site_is_still_at_its_login_or_has_one_question(self):
        tab = self.site(login=True)
        case = SimpleNamespace(questions=lambda: questions("material:A-1", "material:A-2"))
        team = agent.Team(case, ["inventory"], time.time() + 600)
        agent.AGENTS["inventory"] = SimpleNamespace(steps=3)

        async def run(*args):
            raise AssertionError("no helper may start")

        waiting = asyncio.ensure_future(agent.crew(case, team, tab, 3, time.time() + 600, run))
        await asyncio.sleep(1.5)
        team.finished.set()
        self.assertEqual(await waiting, [])
        self.assertEqual(agent.CREW, {})

    async def test_no_helpers_when_too_little_time_is_left(self):
        case = SimpleNamespace(questions=lambda: questions("material:A-1", "material:A-2"))
        team = agent.Team(case, ["inventory"], time.time() + 60)
        self.assertEqual(await agent.crew(case, team, self.site(), 3, time.time() + 60, None), [])


class OpeningASite(unittest.IsolatedAsyncioTestCase):
    async def test_the_site_is_opened_and_its_login_form_filled_before_the_first_turn(self):
        class LoginTab:
            credentials = {"password": "p"}
            base_url = "http://shop.test"

            async def goto(self, url):
                return f"opened {url}"

            async def login(self):
                return "entered the credentials and submitted the login form; now at http://shop.test/home"

        notes = await agent.open_site(LoginTab())
        self.assertEqual(notes[0], "(before your first turn) opened http://shop.test/")
        self.assertIn("logged in with this run's credentials", notes[1])

    async def test_a_site_without_a_login_form_is_only_opened(self):
        class OpenTab:
            credentials = {"password": "p"}
            base_url = "http://shop.test"

            async def goto(self, url):
                return f"opened {url}"

            async def login(self):
                return "no password field on this page"

        self.assertEqual(len(await agent.open_site(OpenTab())), 1)


class SecondRequest(unittest.IsolatedAsyncioTestCase):
    async def test_a_hanging_call_is_asked_a_second_time_and_the_first_reply_wins(self):
        calls = []

        class Completions:
            async def create(self, **kwargs):
                calls.append(time.monotonic())
                if len(calls) == 1:
                    await asyncio.sleep(30)  # the first request hangs
                usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None)
                return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content='{"ok": true}'))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        with patch.object(llm, "_session", return_value=(client, asyncio.Semaphore(4))), \
                patch.object(llm, "HEDGE_AFTER", 0.2), patch.dict(llm._warm, {"done": True}):
            started = time.monotonic()
            reply = await llm.chat_json([{"role": "user", "content": "x"}], think=False, label="hedge-test")
        self.assertEqual(reply, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertLess(time.monotonic() - started, 5)

    async def test_a_thinking_call_is_not_doubled(self):
        calls = []

        class Completions:
            async def create(self, **kwargs):
                calls.append(1)
                await asyncio.sleep(0.4)
                usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None)
                return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content='{"ok": 1}'))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        with patch.object(llm, "_session", return_value=(client, asyncio.Semaphore(4))), \
                patch.object(llm, "HEDGE_AFTER", 0.1), patch.dict(llm._warm, {"done": True}):
            self.assertEqual(await llm.chat_json([{"role": "user", "content": "x"}], think=True), {"ok": 1})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
