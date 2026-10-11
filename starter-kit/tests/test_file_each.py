"""The file_each action (agent.file_each): the model names several records of a result list, code opens each one
and the tabs it names and files them with the kinds the model gave. Invented website and values only."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import file_each  # noqa: E402

LIST = ('url: http://erp.test/search?group=G-1\ntitle: Results\n'
        '[3] link "M-100" -> /record?m=M-100\n[4] link "M-200" -> /record?m=M-200\n[5] button "Search"')


def record(number: str, tab: str = "main") -> str:
    address = f"http://erp.test/record?m={number}" + ("" if tab == "main" else f"&tab={tab}")
    return (f"url: {address}\ntitle: Material {number}\n"
            f"Material {number}, {tab} data\n"
            f'[7] link "Technical data" -> /record?m={number}&tab=tech\n[8] button "Prices"')


class FakeSite:
    def __init__(self):
        self.page = SimpleNamespace(url="http://erp.test/search?group=G-1")
        self.document, self.document_source, self.opened = "", "", []
        self.view = LIST

    async def goto(self, url):
        self.opened.append(url)
        path = url.replace("http://erp.test", "")
        if path.startswith("/record"):
            query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
            self.view = record(query["m"], query.get("tab", "main"))
        else:
            self.view = LIST
        self.page.url = self.view.split("\n", 1)[0].removeprefix("url: ")
        return f"opened {self.page.url}"

    async def click(self, aid):
        self.opened.append(f"click {aid}")
        if str(aid) == "8" and "Material" in self.view:  # a tab that is a button: the prices replace the page
            number = self.view.split("m=", 1)[1].split("&", 1)[0]
            self.view = record(number, "prices")
            self.page.url = self.view.split("\n", 1)[0].removeprefix("url: ")
        return f"clicked [{aid}]"

    async def observe(self):
        self.document, self.document_source = self.view, self.page.url
        return self.view


class FileEach(unittest.IsolatedAsyncioTestCase):
    async def run_action(self, action, deadline=None):
        site, filed, seen = FakeSite(), [], []
        result = await file_each(site, action, lambda kind, text, source: filed.append((kind, source)) or "filed",
                                 seen.append, deadline or time.time() + 600)
        return site, filed, seen, result

    async def test_each_record_and_each_named_tab_is_filed_with_its_kind(self):
        site, filed, seen, result = await self.run_action(
            {"do": "file_each", "links": [3, 4], "kind": "material",
             "tabs": [{"tab": "Technical data", "kind": "material"}, {"tab": "Prices", "kind": "price_conditions"}]})
        self.assertEqual(filed, [
            ("material", "http://erp.test/record?m=M-100"), ("material", "http://erp.test/record?m=M-100&tab=tech"),
            ("price_conditions", "http://erp.test/record?m=M-100&tab=prices"),
            ("material", "http://erp.test/record?m=M-200"), ("material", "http://erp.test/record?m=M-200&tab=tech"),
            ("price_conditions", "http://erp.test/record?m=M-200&tab=prices")])
        self.assertEqual(site.page.url, "http://erp.test/search?group=G-1")  # back on the list
        self.assertEqual(len(seen), 6)  # every opened page counts as seen for the coverage gate
        self.assertIn('"M-100"', result)

    async def test_a_path_of_controls_is_followed_and_its_last_page_filed(self):
        # a record whose stock sits behind two clicks: the stock overview, then a storage location
        class DeepSite(FakeSite):
            async def goto(self, url):
                if "tab=stock" in url:
                    number = url.split("m=", 1)[1].split("&", 1)[0]
                    self.view = (f"url: http://erp.test/record?m={number}&tab=stock\ntitle: Stock {number}\n"
                                 f'[2] link "Location 0001 open" -> /record?m={number}&tab=location')
                    self.page.url = f"http://erp.test/record?m={number}&tab=stock"
                    return f"opened {self.page.url}"
                if "tab=location" in url:
                    number = url.split("m=", 1)[1].split("&", 1)[0]
                    self.view = f"url: http://erp.test/record?m={number}&tab=location\nfree stock 31"
                    self.page.url = f"http://erp.test/record?m={number}&tab=location"
                    return f"opened {self.page.url}"
                result = await super().goto(url)
                if "/record" in url and "tab=" not in url:
                    self.view += f'\n[9] link "Stock overview" -> /record?m={url.split("m=", 1)[1]}&tab=stock'
                return result

        site, filed = DeepSite(), []
        result = await file_each(site, {"do": "file_each", "links": [3, 4], "kind": "material",
                                        "tabs": [{"path": ["Stock overview", "Location 0001"], "kind": "material"}]},
                                 lambda kind, text, source: filed.append((kind, source)) or "filed", lambda page: None,
                                 time.time() + 600)
        self.assertEqual(filed, [
            ("material", "http://erp.test/record?m=M-100"), ("material", "http://erp.test/record?m=M-100&tab=location"),
            ("material", "http://erp.test/record?m=M-200"), ("material", "http://erp.test/record?m=M-200&tab=location")])
        self.assertIn('"Stock overview > Location 0001" as material', result)

    async def test_the_ids_are_those_of_the_page_the_model_saw(self):
        # a list that loaded more rows after the model's read numbers them differently on a fresh read
        site, filed, seen = FakeSite(), [], []
        model_saw = 'url: http://erp.test/search?group=G-1\n[3] link "M-200" -> /record?m=M-200'
        await file_each(site, {"do": "file_each", "links": [3], "kind": "material"},
                        lambda kind, text, source: filed.append((kind, source)) or "filed", seen.append,
                        time.time() + 600, model_saw)
        self.assertEqual(filed, [("material", "http://erp.test/record?m=M-200")])

    async def test_an_id_that_is_not_a_link_is_reported_not_guessed(self):
        _, filed, _, result = await self.run_action({"do": "file_each", "links": [9, 5], "kind": "material"})
        self.assertEqual(filed, [])
        self.assertIn("[9] is not a link on this page", result)

    async def test_a_missing_tab_is_reported_and_the_rest_still_filed(self):
        _, filed, _, result = await self.run_action(
            {"do": "file_each", "links": [3], "kind": "material", "tabs": [{"tab": "Stock", "kind": "material"}]})
        self.assertEqual(filed, [("material", "http://erp.test/record?m=M-100")])
        self.assertIn('no control labelled "Stock"', result)

    async def test_without_links_or_kind_nothing_happens(self):
        _, filed, _, result = await self.run_action({"do": "file_each", "links": [3]})
        self.assertEqual(filed, [])
        self.assertIn('needs "links"', result)

    async def test_time_is_bounded(self):
        _, filed, _, result = await self.run_action({"do": "file_each", "links": [3, 4], "kind": "material"},
                                                    deadline=time.time() - 1)
        self.assertEqual(filed, [])
        self.assertIn("time for this action is up", result)


if __name__ == "__main__":
    unittest.main()
