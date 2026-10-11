"""The learned record walk (agent.WalkRecorder): the model walks one record of a list by hand, code notes the controls
it clicked and the pages it kept, and the briefing offers the same walk for the other records as one file_each action.
The model still names the records. Invented website and values only."""
import json
import re
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
from agent import WalkRecorder, file_each  # noqa: E402

BASE = "http://inv.test"
LIST_URL = f"{BASE}/find?q=K-9"
ITEMS = ("PX-4410", "PX-5520", "PX-6630")


def render(path: str) -> str:
    """The invented site: a result list, item records with tabs, a stock drill-down and price scales."""
    nav = '[1] link "TX01" -> /tx01\n'
    if path.startswith("/find"):
        rows = "".join(f'[{3 + n}] link "{item}" -> /item?no={item}\n' for n, item in enumerate(ITEMS))
        return f"url: {BASE}{path}\ntitle: Results\n{nav}{rows}[2] button \"Find\""
    if "?" not in path:
        return f"url: {BASE}{path}\ntitle: Home\n{nav}"
    query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
    item = query["no"]
    tabs = (f'[6] link "Overview" -> /item?no={item}\n[7] link "Specs" -> /item?no={item}&tab=specs\n'
            f'[8] link "Planning" -> /item?no={item}&tab=plan\n[9] link "Stock" -> /stock?no={item}\n')
    prices = f'[11] link "Prices for {item}" -> /prices?no={item}\n'
    if path.startswith("/item"):
        body = {"specs": f"Item {item} specs: bore 70", "plan": f"Item {item} planning: status active"}
        return f"url: {BASE}{path}\ntitle: Item {item}\n{nav}{tabs}{body.get(query.get('tab'), f'Item {item} overview')}"
    if path.startswith("/stock"):
        level = query.get("lvl")
        if level == "bin":
            return f"url: {BASE}{path}\ntitle: Stock {item}\n{nav}free stock 17\n{prices}"
        if level == "loc":
            return (f"url: {BASE}{path}\ntitle: Stock {item}\n{nav}"
                    f'[12] link "Bin 01 · finished goods open" -> /stock?no={item}&lvl=bin\n{prices}')
        return (f"url: {BASE}{path}\ntitle: Stock {item}\n{nav}"
                f'[10] link "Plant 1000 · Main works open" -> /stock?no={item}&lvl=loc\n{prices}')
    if path.startswith("/prices"):
        if query.get("view") == "scales":
            return f"url: {BASE}{path}\ntitle: Prices {item}\n{nav}scales 1 -> 9.10"
        return f'url: {BASE}{path}\ntitle: Prices {item}\n{nav}[13] link "Scales open" -> /prices?no={item}&view=scales'
    return f"url: {BASE}{path}\ntitle: Home\n{nav}"


class FakeErp:
    def __init__(self):
        self.page = SimpleNamespace(url=LIST_URL)
        self.view = render("/find?q=K-9")
        self.document, self.document_source = "", ""

    async def goto(self, url):
        path = url.replace(BASE, "")
        self.view = render(path)
        self.page.url = BASE + path
        return f"opened {self.page.url}"

    async def click(self, aid):
        match = re.search(rf'\[{aid}\] \w+ "[^"]*" -> (\S+)', self.view)
        if match:
            await self.goto(match.group(1))
        return f"clicked [{aid}]"

    async def observe(self):
        self.document, self.document_source = self.view, self.page.url
        return self.view


async def walk_by_hand(site: FakeErp, walks: WalkRecorder, steps: list) -> None:
    """What the agent loop does: every browser action reaches the recorder with the page it was taken on."""
    for step in steps:
        if step[0] == "keep":
            walks.kept(step[1], held=False)
            continue
        page_before = await site.observe()
        action = {"do": step[0], "id": step[1]} if step[0] == "click" else {"do": step[0], "url": step[1]}
        await (site.click(step[1]) if step[0] == "click" else site.goto(step[1]))
        walks.browsed(action, page_before, site.page.url)


FIRST_RECORD = [("click", 3), ("click", 7), ("keep", "material"),
                ("click", 6), ("click", 8), ("keep", "material"),          # a detour through the overview
                ("click", 9), ("click", 10), ("click", 12), ("keep", "material"),
                ("click", 11), ("click", 13), ("keep", "price_conditions"),
                ("goto", LIST_URL)]


def offered_action(tip: str, links: list) -> dict:
    spec = tip.split("\n", 1)[1].replace("<ids of the records you need>", ", ".join(str(i) for i in links))
    return json.loads(spec)


class LearnedWalk(unittest.IsolatedAsyncioTestCase):
    async def test_the_path_is_learned_from_one_record_walked_by_hand(self):
        site, walks = FakeErp(), WalkRecorder()
        await walk_by_hand(site, walks, FIRST_RECORD)
        self.assertEqual(walks.learned["tabs"], [
            {"tab": "Specs", "kind": "material"},
            {"tab": "Planning", "kind": "material"},  # the overview detour is not repeated
            {"path": ["Stock", "Plant 1000 · Main works open", "Bin 01 · finished goods open"], "kind": "material"},
            {"path": ["Prices for", "Scales open"], "kind": "price_conditions"}])  # without the record's own number
        self.assertFalse(walks.learned["keep_record"])  # the record's first page was never kept

    async def test_the_tip_offers_the_learned_tabs_on_a_list_and_not_on_the_record(self):
        site, walks = FakeErp(), WalkRecorder()
        self.assertEqual(walks.tip(await site.observe()), "")  # nothing walked yet
        await walk_by_hand(site, walks, FIRST_RECORD)
        tip = walks.tip(await site.observe())
        self.assertIn('"do": "file_each"', tip)
        self.assertIn("PX-4410", tip)
        action = offered_action(tip, [4, 5])
        self.assertEqual(action["tabs"], walks.learned["tabs"])
        self.assertEqual(action["kind"], "material")
        self.assertIs(action["keep_record"], False)
        await site.goto(f"{BASE}/item?no=PX-4410&tab=specs")
        self.assertEqual(walks.tip(await site.observe()), "")  # on the walked record itself: no offer
        await site.goto(f"{BASE}/home")
        self.assertEqual(walks.tip(await site.observe()), "")  # a page without records like it: no offer

    async def test_file_each_with_the_learned_tabs_files_the_other_records(self):
        site, walks = FakeErp(), WalkRecorder()
        await walk_by_hand(site, walks, FIRST_RECORD)
        page = await site.observe()
        action = offered_action(walks.tip(page), [4, 5])
        filed = []
        result = await file_each(site, action, lambda kind, text, source: filed.append((kind, source)) or "filed",
                                 lambda seen: None, time.time() + 600, page)
        expected = []
        for item in ("PX-5520", "PX-6630"):
            expected += [("material", f"{BASE}/item?no={item}&tab=specs"), ("material", f"{BASE}/item?no={item}&tab=plan"),
                         ("material", f"{BASE}/stock?no={item}&lvl=bin"),
                         ("price_conditions", f"{BASE}/prices?no={item}&view=scales")]
        self.assertEqual(filed, expected, result)  # the record pages themselves are not filed (keep_record false)
        self.assertEqual(site.page.url, LIST_URL)

    async def test_a_form_entry_ends_the_recording_and_keeps_what_was_learned(self):
        site, walks = FakeErp(), WalkRecorder()
        await walk_by_hand(site, walks, FIRST_RECORD[:3])
        walks.browsed({"do": "fill", "id": 2, "text": "x"}, await site.observe(), site.page.url)
        await walk_by_hand(site, walks, [("click", 8), ("keep", "material")])
        self.assertEqual(walks.learned["tabs"], [{"tab": "Specs", "kind": "material"}])

    async def test_a_menu_code_opened_first_does_not_spoil_the_record_walk(self):
        site, walks = FakeErp(), WalkRecorder()
        await site.goto(f"{BASE}/home")
        await walk_by_hand(site, walks, [("click", 1)])  # the menu code "TX01" opens /tx01: it looks like a record
        await site.goto(LIST_URL)
        self.assertEqual(walks.tip(await site.observe()), "")  # nothing kept there: nothing learned, no offer
        await walk_by_hand(site, walks, FIRST_RECORD)
        self.assertEqual(walks.learned["label"], "PX-4410")
        self.assertEqual(len(walks.learned["tabs"]), 4)

    async def test_a_record_page_kept_itself_is_the_main_kind(self):
        site, walks = FakeErp(), WalkRecorder()
        await walk_by_hand(site, walks, [("click", 4), ("keep", "material"), ("click", 7), ("keep", "material"),
                                         ("goto", LIST_URL)])
        self.assertTrue(walks.learned["keep_record"])
        action = offered_action(walks.tip(await site.observe()), [3])
        self.assertNotIn("keep_record", action)
        self.assertEqual(action["tabs"], [{"tab": "Specs", "kind": "material"}])


class InvestigatorWiring(unittest.IsolatedAsyncioTestCase):
    async def test_the_briefing_shows_the_tip_after_a_walk_through_the_agent_hooks(self):
        site = FakeErp()
        site.site_id, site.base_url = "inv-erp", BASE
        filed = []
        case = SimpleNamespace(
            brief="# Task\nInvented.", questions=lambda: [("material:Q1", "find the items")], take_news=lambda name: [],
            status=lambda: "(empty)", pending_for=lambda name: 0, pending=0,
            file=lambda kind, text, source, name: filed.append((kind, source)) or "filed")
        team = SimpleNamespace(hook=lambda name: None)
        agent.WALKS.clear()
        investigator = agent.investigator(case, team, site, time.time() + 600)
        for step in FIRST_RECORD:
            if step[0] == "keep":
                investigator.page_seen = await site.observe()
                await investigator.extra["keep"]({"do": "keep", "kind": step[1]})
                continue
            page_before = await site.observe()
            action = {"do": step[0], "id": step[1]} if step[0] == "click" else {"do": step[0], "url": step[1]}
            await (site.click(step[1]) if step[0] == "click" else site.goto(step[1]))
            await investigator.on_action(action, page_before, "")
        investigator.page_seen = await site.observe()
        text = investigator.briefing()
        self.assertIn("TIP: you walked the record", text)
        self.assertIn('"path": ["Stock"', text)


if __name__ == "__main__":
    unittest.main()
