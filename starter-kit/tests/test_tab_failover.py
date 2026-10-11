"""A tab whose browser cannot load its website moves to another browser of the run (browser_env.Tab.renew)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_env import Tab  # noqa: E402


class FakePage:
    def __init__(self, context):
        self.context, self.url = context, "about:blank"

    def on(self, event, handler):
        pass

    async def close(self):
        pass


class FakeContext:
    def __init__(self, name):
        self.name, self.pages = name, []

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page


class Failover(unittest.IsolatedAsyncioTestCase):
    async def test_second_renewal_moves_to_another_browser_and_logs_in_again(self):
        own, other = FakeContext("own"), FakeContext("other")
        tab = Tab(await own.new_page(), {"siteId": "tender-portal", "url": "https://portal.test",
                                          "credentials": {"username": "u", "password": "p"}})
        tab.spares = [other]
        logins = []

        async def goto(url):
            return f"opened {url}"

        async def login():
            logins.append(tab.page.context.name)
            return "entered the credentials"

        with patch.object(tab, "goto", goto), patch.object(tab, "login", login):
            first = await tab.renew()
            self.assertIs(tab.page.context, own)  # a fresh tab in the same browser first
            self.assertNotIn("moved", first)
            second = await tab.renew()
        self.assertIs(tab.page.context, other)
        self.assertIn("moved to another browser", second)
        self.assertEqual(logins, ["other"])  # the other browser has no session yet

    async def test_without_other_browsers_it_keeps_renewing_in_its_own(self):
        own = FakeContext("own")
        tab = Tab(await own.new_page(), {"siteId": "erp", "url": "https://erp.test"})

        async def goto(url):
            return "opened"

        with patch.object(tab, "goto", goto):
            await tab.renew()
            await tab.renew()
        self.assertIs(tab.page.context, own)


if __name__ == "__main__":
    unittest.main()
