"""One tab at a time passes a website's entrance (browser_env.Entrance, Tab.enter).

Local run with five ERP tabs in one browser session: the session ended, every tab stood in front of the login form
at once, and each tab that loaded the form spoilt the one another tab was filling in ("the form expired" for 55 s).
The website here is invented."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_env  # noqa: E402
from browser_env import AT_LOGIN_JS, Entrance, Tab  # noqa: E402


class Site:
    """A website whose session is shared by its tabs: logged out, every page is the login form."""

    def __init__(self):
        self.logged_in, self.logins, self.at_once, self.worst = False, 0, 0, 0


class Page:
    def __init__(self, site: Site, url: str):
        self.site, self.url = site, url
        self.frames = [SimpleNamespace(evaluate=self.evaluate)]
        self.main_frame = self.frames[0]

    def on(self, event, handler):
        pass

    async def evaluate(self, script, *args):
        return not self.site.logged_in if script == AT_LOGIN_JS else None


class SiteTab(Tab):
    def __init__(self, site: Site, url: str = "http://shop.test/record/7"):
        super().__init__(Page(site, url), {"siteId": "shop", "url": "http://shop.test", "credentials": {"password": "x"}})
        self.site, self.opened = site, []

    async def goto(self, url):
        self.opened.append(url)
        self.page.url = url
        return f"opened {url}"

    async def login(self):
        self.site.at_once += 1
        self.site.worst = max(self.site.worst, self.site.at_once)
        await asyncio.sleep(0.05)
        self.site.logged_in, self.site.logins = True, self.site.logins + 1
        self.site.at_once -= 1
        return "entered the credentials"


class OneTabAtATime(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        browser_env.ENTRANCES.clear()

    async def test_three_tabs_in_front_of_the_login_form_log_in_once(self):
        site = Site()
        first, second, third = SiteTab(site), SiteTab(site), SiteTab(site, "http://shop.test/login?back=%2Fsearch")
        entering = [asyncio.create_task(tab.enter()) for tab in (first, second, third)]
        await asyncio.sleep(0.2)
        self.assertEqual((site.logins, site.worst), (1, 1))
        self.assertEqual([task.done() for task in entering], [True, False, False])  # the others wait for the first
        await first.enter()  # the turns in which the first tab passes what follows the login
        await first.enter()
        await first.enter()
        await asyncio.wait_for(asyncio.gather(*entering), 2)
        self.assertEqual(site.logins, 1)
        self.assertEqual(second.opened, ["http://shop.test/record/7"])  # its page again, under the new session
        self.assertEqual(first.opened, [])

    async def test_a_tab_that_never_comes_back_does_not_hold_the_others_for_ever(self):
        site = Site()
        first, second = SiteTab(site), SiteTab(site)
        await first.enter()
        first.entrance.until = 0  # its time is up
        await asyncio.wait_for(second.enter(), 2)
        self.assertEqual(site.logins, 1)

    async def test_a_clerk_is_not_logged_in_behind_its_back(self):
        site = Site()
        clerk = SiteTab(site)
        await clerk.enter(login=False)
        self.assertEqual(site.logins, 0)

    async def test_tabs_of_different_websites_do_not_wait_for_each_other(self):
        one, other = SiteTab(Site()), SiteTab(Site())
        other.base_url = "http://mail.test"
        await asyncio.wait_for(asyncio.gather(one.enter(), other.enter()), 2)
        self.assertIsNot(one.entrance, other.entrance)
        self.assertIsInstance(one.entrance, Entrance)


if __name__ == "__main__":
    unittest.main()
