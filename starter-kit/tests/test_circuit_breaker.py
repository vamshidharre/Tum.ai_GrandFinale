"""The navigation circuit breaker: a page that does not load is opened once more and then replaced by a fresh tab, by
code and bounded, before the model is asked (worker.Agent._recover); a navigation counts as opened once the server
answered, with one immediate retry (browser_env.Tab.goto). Feedback on 1d0ecea: a 78 s click, 45 s page reads and over
four minutes of mail recovery."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_env  # noqa: E402
import worker  # noqa: E402
from worker import Agent  # noqa: E402


class HangingTab:
    """A tab whose page reads hang until it is opened again (`heals_on="goto"`) or renewed (`heals_on="renew"`)."""

    def __init__(self, heals_on: str):
        self.heals_on, self.healthy, self.calls = heals_on, False, []
        self.base_url = "http://site.test"
        self.page = SimpleNamespace(url="http://site.test/list")
        self.document, self.document_source, self.events = "", "", []

    async def observe(self):
        if not self.healthy:
            await asyncio.sleep(10)
        return "url: http://site.test/list\n[1] link \"Record\" -> /record"

    async def goto(self, url):
        self.calls.append(("goto", url))
        self.healthy = self.heals_on == "goto"
        return f"opened {url}"

    async def renew(self):
        self.calls.append(("renew",))
        self.healthy = self.heals_on == "renew"
        return "fresh tab"


class Recover(unittest.IsolatedAsyncioTestCase):
    async def recover(self, tab):
        agent = Agent("erp", tab, "SYSTEM", lambda: "job")
        with patch.object(worker, "READ_TIMEOUT", 0.2):
            started = time.time()
            page = await agent._recover()
        return page, time.time() - started

    async def test_a_page_that_loads_when_opened_again_needs_no_new_tab(self):
        tab = HangingTab("goto")
        page, _ = await self.recover(tab)
        self.assertEqual(tab.calls, [("goto", "http://site.test/list")])
        self.assertIn("Record", page)
        self.assertIn("opened again", " ".join(tab.events))

    async def test_a_page_that_still_hangs_gets_a_fresh_tab_at_once(self):
        tab = HangingTab("renew")
        page, _ = await self.recover(tab)
        self.assertEqual(tab.calls, [("goto", "http://site.test/list"), ("renew",)])
        self.assertIn("Record", page)
        self.assertIn("replaced by a fresh one", " ".join(tab.events))

    async def test_a_site_that_is_down_is_reported_within_bounds(self):
        tab = HangingTab("never")
        page, seconds = await self.recover(tab)
        self.assertIn("may be down for now", page)
        self.assertLess(seconds, 2)


class FakePage:
    """As much of a Playwright page as Tab.goto touches: the first answer waits fail `misses` times."""

    def __init__(self, misses=0, slow_build=False):
        self.misses, self.slow_build, self.url, self.frames, self.waits = misses, slow_build, "http://site.test/", [], []
        self.main_frame = SimpleNamespace()

    def on(self, *args):
        pass

    async def goto(self, target, wait_until, timeout):
        self.waits.append((wait_until, timeout))
        if self.misses:
            self.misses -= 1
            raise TimeoutError("Timeout exceeded")
        self.url = target

    async def wait_for_load_state(self, state, timeout):
        if self.slow_build:
            raise TimeoutError("still building")


class TwoPhaseGoto(unittest.IsolatedAsyncioTestCase):
    def tab(self, page):
        return browser_env.Tab(page, {"siteId": "erp", "url": "http://site.test", "credentials": {}})

    async def test_a_server_that_answers_late_once_is_asked_again_at_once(self):
        page = FakePage(misses=1)
        result = await self.tab(page).goto("/record")
        self.assertEqual(result, "opened http://site.test/record")
        self.assertEqual([w for w, _ in page.waits], ["commit", "commit"])
        self.assertEqual(page.waits[0][1], browser_env.NAV_COMMIT * 1000)

    async def test_a_server_that_never_answers_fails_after_two_short_waits(self):
        page = FakePage(misses=5)
        result = await self.tab(page).goto("/record")
        self.assertTrue(result.startswith("could not open"))
        self.assertEqual(len(page.waits), 2)

    async def test_a_page_that_answered_but_builds_slowly_counts_as_opened(self):
        tab = self.tab(FakePage(slow_build=True))
        result = await tab.goto("/record")
        self.assertEqual(result, "opened http://site.test/record")
        self.assertIn("still building", " ".join(tab.events))


if __name__ == "__main__":
    unittest.main()
