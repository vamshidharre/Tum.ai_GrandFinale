"""The browser layer against local pages: what a model is shown of each trick a business website plays on its
visitors, and whether the tab's actions do what they report. A throwaway server on 127.0.0.1 serves tests/fixtures
(one small hand-written page per trick); a local headless Chromium is driven through browser_env.Tab alone."""
import asyncio
import functools
import html
import inspect
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import NamedTuple
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright  # noqa: E402

import browser_env  # noqa: E402
from browser_env import Tab  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LOGIN = {"username": "pat.example", "password": "not-a-real-secret"}  # invented: only the fixture site knows them
TYPES = {".html": "text/html; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


class FixtureSite:
    """tests/fixtures over HTTP, plus the few things a static file cannot do: a login that sets a session cookie, a
    list paged by query string, rows that arrive on request, answers that take a moment, a form echo, downloads."""

    ORDERS = [(11, "Widget A", 4), (12, "Widget B", 2), (13, "Gasket", 30), (14, "Widget A", 1), (15, "Bolt", 500),
              (16, "Widget B", 8), (17, "Widget A", 4)]
    PER_PAGE = 3
    LATER_MESSAGES = {22: ["Message 23: Invoice sent", "Message 24: Gasket restocked"],
                      24: ["Message 25: Order 17 closed"]}
    DELAY = 0.3  # seconds a "slow" answer takes: a local server answers faster than any real network

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []  # (method, path with query, form fields), in order
        self.sessions: dict[str, str] = {}  # session cookie -> user name
        self.navigation_attempts: dict[str, int] = {}
        self.ticket = secrets.token_hex(4)
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.answer(self, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                site.answer(self, {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]

    def posted(self, path: str) -> dict | None:
        """The fields of the last form posted to `path`."""
        return next((form for method, at, form in reversed(self.requests) if method == "POST" and at == path), None)

    def answer(self, request, form: dict) -> None:
        if request.path == "/favicon.ico":  # the browser's own idea, not a step of the tab
            return self.send(request, 404, "", TYPES[".txt"])
        self.requests.append((request.command, request.path, form))
        url = urlsplit(request.path)
        query = {key: values[-1] for key, values in parse_qs(url.query, keep_blank_values=True).items()}
        cookie = re.search(r"session=(\w+)", request.headers.get("cookie") or "")
        user = self.sessions.get(cookie[1]) if cookie else None
        path = url.path
        if path in ("/recover-on-refresh", "/recover-on-reopen", "/unavailable"):
            attempt = self.navigation_attempts.get(path, 0) + 1
            self.navigation_attempts[path] = attempt
            delayed_attempts = {"/recover-on-refresh": 1, "/recover-on-reopen": 1}
            if path == "/unavailable" or attempt <= delayed_attempts.get(path, 0):
                time.sleep(self.DELAY)
            return self.send(request, 200, "<!doctype html><title>Recovered</title><h1>Recovered page</h1>")
        if path.startswith("/slow/"):
            time.sleep(self.DELAY)
            path = path[len("/slow"):]
        if path.startswith("/private/"):
            if not user:
                return self.send(request, 403, "Sign in first.", TYPES[".txt"])
            path = path[len("/private"):]

        if path == "/login" and request.command == "POST":
            if form == {"ticket": self.ticket, **LOGIN}:
                session = secrets.token_hex(8)
                self.sessions[session] = form["username"]
                return self.send(request, 303, "", TYPES[".txt"],
                                 {"location": "/account", "set-cookie": f"session={session}; Path=/; HttpOnly"})
            return self.send(request, 200, self.page("login.html", ticket=self.ticket,
                                                     message='<p role="alert">Sign-in failed.</p>'))
        if path == "/login":
            return self.send(request, 200, self.page("login.html", ticket=self.ticket, message=""))
        if path == "/account":
            if not user:
                return self.send(request, 302, "", TYPES[".txt"], {"location": "/login"})
            return self.send(request, 200, self.page("account.html", username=html.escape(user)))
        if path == "/orders":
            return self.send(request, 200, self.orders(int(query.get("page") or 1)))
        if path == "/more-messages":
            time.sleep(self.DELAY)
            after = int(query["after"])
            rows = self.LATER_MESSAGES[after]
            following = after + len(rows)
            body = {"rows": rows, "next": f"/more-messages?after={following}" if following in self.LATER_MESSAGES else None}
            return self.send(request, 200, json.dumps(body), "application/json")
        if path == "/echo":
            fields = "".join(f"<dt>{html.escape(key)}</dt><dd>{html.escape(value)}</dd>"
                             for key, value in (form or query).items())
            return self.send(request, 200, f"<!doctype html><title>Received</title><h1>Received</h1><dl>{fields}</dl>")
        if path.startswith("/arrived/"):
            name = html.escape(path[len("/arrived/"):])
            return self.send(request, 200, f"<!doctype html><title>{name}</title><h1>You arrived at {name}</h1>")
        if path == "/bundle.zip":
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                archive.writestr("readme.txt", "Bundle for order 17\n")
                archive.writestr("items.csv", "item;quantity\nWidget A;4\n")
            return self.send(request, 200, data.getvalue(), "application/zip",
                             {"content-disposition": 'attachment; filename="order-17-documents.zip"'})
        file = (FIXTURES / path.lstrip("/")).resolve()
        if FIXTURES in file.parents and file.is_file():
            return self.send(request, 200, file.read_bytes(), TYPES.get(file.suffix, "application/octet-stream"))
        return self.send(request, 404, "Not found.", TYPES[".txt"])

    def page(self, name: str, **values) -> str:
        return Template((FIXTURES / name).read_text(encoding="utf-8")).substitute(values)

    def orders(self, number: int) -> str:
        pages = -(-len(self.ORDERS) // self.PER_PAGE)
        shown = self.ORDERS[(number - 1) * self.PER_PAGE:number * self.PER_PAGE]
        rows = "\n".join(f"  <tr><td>Order {order}</td><td>{item}</td><td>{quantity}</td></tr>"
                         for order, item, quantity in shown)
        previous = f'<a href="/orders?page={number - 1}">Previous page</a>' if number > 1 else ""
        following = f'<a href="/orders?page={number + 1}">Next page</a>' if number < pages else ""
        return self.page("paged_list.html", page=number, pages=pages, rows=rows, previous=previous, next=following)

    @staticmethod
    def send(request, status: int, body, content_type: str = TYPES[".html"], headers: dict | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        request.send_response(status)
        request.send_header("content-type", content_type)
        request.send_header("content-length", str(len(data)))
        request.send_header("cache-control", "no-store")
        for name, value in (headers or {}).items():
            request.send_header(name, value)
        try:
            request.end_headers()
            request.wfile.write(data)
        except ConnectionError:  # the browser closed the tab before the answer was out
            pass


SITE: FixtureSite = None
LOOP: asyncio.AbstractEventLoop = None
PLAYWRIGHT = BROWSER = None
READ: dict[str, str] = {}  # fixture path -> its first observation, for tests that only read


def setUpModule():
    """One server, one event loop and one Chromium for the whole module: a browser per test would take longer than
    the tests. The Playwright driver starts here with the real environment (on Windows it needs SystemRoot, which
    test_kit's entry-point tests clear for the length of a test)."""
    global SITE, LOOP, PLAYWRIGHT, BROWSER
    SITE = FixtureSite()
    LOOP = asyncio.new_event_loop()
    PLAYWRIGHT = LOOP.run_until_complete(async_playwright().start())
    BROWSER = LOOP.run_until_complete(PLAYWRIGHT.chromium.launch(headless=os.environ.get("HEADLESS", "1") != "0"))


def tearDownModule():
    LOOP.run_until_complete(BROWSER.close())
    LOOP.run_until_complete(PLAYWRIGHT.stop())
    LOOP.close()
    SITE.close()
    READ.clear()


def on_the_loop(test):
    @functools.wraps(test)
    def run(self):
        LOOP.run_until_complete(asyncio.wait_for(test(self), 40))  # a hung action fails its test, not the suite
    return run


class Control(NamedTuple):
    """One numbered control of an observation; `text` is everything the observation says about it."""
    id: int
    kind: str
    name: str
    text: str


CONTROL = re.compile(r'\[(\d+)\](?: \(in collapsed menu\))? ([a-z-]+)(?:\([a-z-]+\))? "([^"]*)"')


def controls(observation: str) -> list[Control]:
    found = []
    for line in observation.splitlines():
        marks = list(CONTROL.finditer(line))
        for mark, following in zip(marks, marks[1:] + [None]):
            found.append(Control(int(mark[1]), mark[2], mark[3], line[mark.start():following.start() if following else None]))
    return found


class Case(unittest.TestCase):
    """A test is a coroutine on the module's loop. `open` gives it a tab of its own, with fresh cookies, on a
    fixture; `read` the observation of a fixture nobody acts on."""
    maxDiff = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name, test in list(vars(cls).items()):
            if name.startswith("test") and inspect.iscoroutinefunction(test):
                setattr(cls, name, on_the_loop(test))

    async def open(self, path: str, credentials: dict | None = None) -> Tab:
        context = await BROWSER.new_context()
        self.addCleanup(lambda: LOOP.run_until_complete(context.close()))
        tab = Tab(await context.new_page(), {"siteId": "fixture", "url": SITE.url + "/", "credentials": credentials})
        SITE.requests.clear()
        await tab.goto(path)
        return tab

    async def read(self, path: str) -> str:
        if path not in READ:
            READ[path] = await (await self.open(path)).observe()
        return READ[path]

    def control(self, observation: str, kind: str, name: str) -> Control:
        matches = [control for control in controls(observation) if (control.kind, control.name) == (kind, name)]
        self.assertEqual(len(matches), 1, f'expected exactly one {kind} "{name}" in:\n{observation}')
        return matches[0]

    def line(self, observation: str, text: str) -> str:
        """The one line of the observation that holds `text`: what a reader takes in together with it."""
        lines = [line for line in observation.splitlines() if text in line]
        self.assertEqual(len(lines), 1, f'expected exactly one line with "{text}" in:\n{observation}')
        return lines[0]


class ReadingThePage(Case):
    """observe(): the true content reaches the model, and what is hidden for a reason stays out of its way."""

    async def test_a_name_cut_up_by_invisible_characters_reads_as_its_label(self):
        tab = await self.open("/obfuscated_text.html")
        page = await tab.observe()
        self.assertEqual([hex(ord(char)) for char in page if unicodedata.category(char) == "Cf"], [])
        link = self.control(page, "link", "Order 17")
        self.assertIn("/arrived/order-17", link.text)
        self.assertIn("/arrived/order-17", await tab.click(link.id))
        self.assertIn("You arrived at order-17", await tab.observe())

    async def test_text_cut_up_by_invisible_characters_reads_whole(self):
        self.assertIn("Widget A is in stock", await self.read("/obfuscated_text.html"))

    async def test_text_cut_up_by_zero_width_no_break_spaces_reads_whole(self):
        self.assertIn("Gasket is sold out", await self.read("/obfuscated_text.html"))

    async def test_a_status_that_only_a_hidden_tooltip_holds_is_read_with_its_button(self):
        page = await self.read("/tooltip_canvas.html")
        self.assertIn("Delayed until Friday", self.control(page, "button", "Show").text)
        self.assertEqual(page.count("Delayed until Friday"), 1)

    async def test_a_hidden_tooltip_on_a_table_cell_is_read_with_its_row(self):
        page = await self.read("/tooltip_canvas.html")
        self.assertIn("Held at customs", self.line(page, "Parcel 1"))

    async def test_a_code_drawn_on_a_canvas_is_read(self):
        self.assertIn("Pickup code: 4821", await self.read("/tooltip_canvas.html"))

    async def test_labels_drawn_by_the_stylesheet_name_their_fields(self):
        tab = await self.open("/pseudo_labels.html")
        page = await tab.observe()
        quantity, colour = self.control(page, "input", "Quantity"), self.control(page, "input", "Colour")
        await tab.fill(quantity.id, "40")
        await tab.fill(colour.id, "blue")
        self.assertEqual(await tab.page.input_value("#quantity"), "40")
        self.assertEqual(await tab.page.input_value("#colour"), "blue")

    async def test_column_names_drawn_in_front_of_table_cells_are_read(self):
        page = await self.read("/pseudo_labels.html")
        self.assertRegex(self.line(page, "12 kg"), r"Weight\W*12 kg\b.*Length\W*80 cm")

    async def test_a_drawn_label_with_a_separator_after_it_is_read(self):
        self.assertRegex(self.line(await self.read("/pseudo_labels.html"), "steel"), r"Material\W*steel")

    async def test_a_collapsed_section_is_read_and_its_link_works(self):
        tab = await self.open("/details.html")
        page = await tab.observe()
        self.assertLess(page.index("Technical data"), page.index("Weight: 12 kg"))
        sheet = self.control(page, "link", "Data sheet")
        self.assertIn("/arrived/data-sheet", await tab.click(sheet.id))

    async def test_a_collapsed_section_that_loads_when_opened_is_read(self):
        self.assertIn("Deliver to gate 3, mornings only.", await self.read("/details.html"))

    async def test_a_document_embedded_in_an_embedded_document_is_read_and_worked(self):
        tab = await self.open("/frames_outer.html")
        page = await tab.observe()
        order = ["Outer page", "Middle document", "Innermost: Widget B costs 12.50", "After the frame"]
        self.assertEqual(sorted(order, key=page.index), order)
        note, save = self.control(page, "input", "Note"), self.control(page, "button", "Save note")
        await tab.fill(note.id, "call first")
        await tab.click(save.id)
        page = await tab.observe()
        self.assertIn("Saved: call first", page)
        # a link inside the innermost document moves that document only
        await tab.click(self.control(page, "link", "Second inner page").id)
        page = await tab.observe()
        order = ["Outer page", "Middle document", "You arrived at inner-two", "After the frame"]
        self.assertEqual(sorted(order, key=page.index), order)
        self.assertTrue(tab.page.url.endswith("/frames_outer.html"), tab.page.url)

    async def test_buttons_that_show_the_same_sign_are_told_apart_by_their_labels(self):
        page = await self.read("/icon_buttons.html")
        self.assertIn("Dismiss this notice", self.line(page, "Export is ready"))
        self.assertIn("Delete order 17", self.line(page, "4 × Widget A"))

    async def test_a_time_shows_its_machine_value_next_to_what_is_displayed(self):
        page = await self.read("/time_pre.html")
        self.assertIn("2031-05-06T14:30:00Z", self.line(page, "6 May 2031, 16:30"))

    async def test_csv_in_a_preformatted_block_keeps_one_record_per_line(self):
        lines = (await self.read("/time_pre.html")).splitlines()
        for record in ("item;quantity;unit;remark", "Widget A;40;pcs;", "Widget B;15;pcs;fragile",
                       'Gasket, small;200;pcs;"keep dry; keep flat"'):
            self.assertIn(record, lines)

    async def test_tab_separated_records_keep_their_empty_fields(self):
        page = await self.read("/time_pre.html")
        fields = [field.strip() for field in re.split(r"\t|\||;", self.line(page, "Bolt M8"))]
        self.assertEqual(fields, ["Bolt M8", "", "on order"])

    async def test_a_menu_that_only_opens_under_the_pointer_lists_its_entries_and_they_work(self):
        tab = await self.open("/hover_menu.html")
        page = await tab.observe()
        self.assertIn("/arrived/invoices", self.control(page, "link", "Invoices").text)
        archive = self.control(page, "link", "Archive")
        self.assertIn("/arrived/archive", archive.text)
        self.assertIn("/arrived/archive", await tab.click(archive.id))
        self.assertIn("You arrived at archive", await tab.observe())

    async def test_of_three_controls_with_one_name_only_the_working_one_is_offered(self):
        tab = await self.open("/decoy_triplet.html")
        page = await tab.observe()
        named = [control for control in controls(page) if control.name == "Open invoice"]
        offered = [control for control in named if "disabled" not in control.text]
        self.assertEqual(len(offered), 1, page)
        self.assertNotIn("wrong-invoice", page)
        for dead in named:
            if dead != offered[0]:
                await tab.click(dead.id)
        self.assertTrue(tab.page.url.endswith("/decoy_triplet.html"), tab.page.url)
        await tab.click(offered[0].id)
        self.assertTrue(tab.page.url.endswith("/arrived/invoice-17"), tab.page.url)
        self.assertNotIn("/arrived/wrong-invoice", SITE.paths)

    async def test_lookalike_controls_show_what_tells_them_apart(self):
        page = await self.read("/decoy_triplet.html")
        named = [control for control in controls(page) if control.name == "Open delivery note"]
        self.assertEqual([control.kind for control in named], ["button", "link", "link"], page)
        self.assertIn("data-placeholder", named[0].text)
        self.assertIn("-> /arrived/help", named[1].text)
        self.assertIn("-> /arrived/delivery-note-17", named[2].text)


class GettingPastObstacles(Case):
    """Popups, access checks and browser questions: the model hears of them and the tab's actions get it through."""

    @staticmethod
    async def popup_open(tab: Tab) -> bool:
        return await tab.page.evaluate("document.getElementById('popup').open")

    @staticmethod
    async def popup_opens(tab: Tab) -> None:
        await tab.page.wait_for_function("document.getElementById('popup').open")

    async def close_popup(self, tab: Tab, page: str) -> None:
        """What a model does about a popup the page still shows: press its close button, the first one listed."""
        if await self.popup_open(tab):
            close = [control for control in controls(page) if control.kind == "button" and control.name in ("×", "Close")]
            self.assertTrue(close, f"the popup is open, but no button closes it in:\n{page}")
            await tab.click(close[0].id)
        self.assertFalse(await self.popup_open(tab), f"the popup is still open after:\n{page}")

    async def add_to_basket(self, tab: Tab) -> None:
        """The page behind the popup works again."""
        page = await tab.observe()
        self.assertNotIn("sign me up", page)
        self.assertNotIn("Send my answer", page)
        await tab.click(self.control(page, "button", "Add Widget A").id)
        self.assertIn("Basket: 1 Widget A", await tab.observe())

    async def test_a_popup_that_opened_by_itself_is_reported_and_gone_without_following_its_link(self):
        tab = await self.open("/timed_dialog.html")
        await self.popup_opens(tab)
        page = await tab.observe()
        self.assertIn("Newsletter", page + "\n".join(tab.take_events()))
        await self.close_popup(tab, page)
        await self.add_to_basket(tab)
        self.assertTrue(tab.page.url.endswith("/timed_dialog.html"), tab.page.url)
        self.assertNotIn("/arrived/newsletter", SITE.paths)

    async def test_a_popup_that_asks_something_is_shown_and_its_close_button_closes_it(self):
        tab = await self.open("/timed_dialog_question.html")
        await self.popup_opens(tab)
        page = await tab.observe()
        self.assertIn("Quick question", page + "\n".join(tab.take_events()))
        await self.close_popup(tab, page)
        await self.add_to_basket(tab)
        self.assertTrue(tab.page.url.endswith("/timed_dialog_question.html"), tab.page.url)
        self.assertNotIn("/arrived/survey", SITE.paths)

    async def test_a_popup_that_opens_after_the_page_was_read_does_not_swallow_the_next_actions_unnoticed(self):
        tab = await self.open("/timed_dialog.html?after=1500")
        page = await tab.observe()
        self.assertFalse(await self.popup_open(tab), "test setup: the popup opened before the page was read")
        remark, add = self.control(page, "input", "Remark"), self.control(page, "button", "Add Widget A")
        await self.popup_opens(tab)

        said = await tab.fill(remark.id, "two boxes")
        held = await tab.page.input_value("#remark")
        self.assertTrue(held == "two boxes" or not said.startswith("filled"),
                        f'fill answered "{said}", but the field holds "{held}"')
        said = await tab.click(add.id)
        basket = await tab.page.inner_text("#basket")
        self.assertTrue(basket == "Basket: 1 Widget A" or not said.startswith("clicked"),
                        f'click answered "{said}", but the page says "{basket}"')

        page = await tab.observe()
        self.assertIn("Newsletter", page + "\n".join(tab.take_events()))
        await self.close_popup(tab, page)
        self.assertNotIn("/arrived/newsletter", SITE.paths)

    async def test_a_locked_area_opens_once_its_slider_and_checkbox_are_set(self):
        tab = await self.open("/gate.html")
        page = await tab.observe()
        slider = self.control(page, "slider", "Slide all the way to the right")
        person = self.control(page, "checkbox", "I am a person")
        item, warehouse = self.control(page, "input", "Item"), self.control(page, "select", "Warehouse")
        check, report = self.control(page, "button", "Check stock"), self.control(page, "link", "Stock report")
        self.assertIn("max=100", slider.text)
        self.assertIn("unchecked", person.text)
        self.assertRegex(page, r"\bLocked\b")
        for locked in (item, warehouse, check):
            self.assertIn("disabled", locked.text)

        await tab.click(check.id)  # locked: nothing may happen
        await tab.click(report.id)
        self.assertEqual(await tab.page.inner_text("#answer"), "")
        self.assertTrue(tab.page.url.endswith("/gate.html"), tab.page.url)

        # one reading of the page, then the whole chain: the ids stay valid while the page stays
        await tab.set(slider.id, 100)
        await tab.set(person.id, True)
        await tab.fill(item.id, "Widget A")
        await tab.select(warehouse.id, "s")
        await tab.click(check.id)
        page = await tab.observe()
        self.assertIn("Unlocked", page)
        self.assertNotRegex(page, r"\bLocked\b")
        self.assertIn("Widget A (s): 40 in stock", page)
        for unlocked in ("Item", "Check stock"):
            self.assertNotIn("disabled", next(c for c in controls(page) if c.name == unlocked).text)
        await tab.click(self.control(page, "link", "Stock report").id)
        self.assertTrue(tab.page.url.endswith("/arrived/stock-report"), tab.page.url)

    async def test_a_submit_that_asks_are_you_sure_goes_through_and_the_question_is_reported_once(self):
        tab = await self.open("/confirm_submit.html")
        page = await tab.observe()
        tab.take_events()
        await tab.fill(self.control(page, "input", "Remark").id, "leave at the gate")
        said = await tab.click(self.control(page, "button", "Send order").id)
        self.assertEqual(SITE.posted("/echo"),
                         {"order": "17", "confirmed": "yes", "remark": "leave at the gate", "action": "send"})
        self.assertIn("/echo", said)
        events = tab.take_events()
        self.assertEqual(len([event for event in events if "Send order 17 now?" in event and "accept" in event]), 1,
                         events)
        self.assertEqual(tab.take_events(), [])
        self.assertIn("leave at the gate", await tab.observe())


class Acting(Case):
    """click, fill, select and set change the page as they say; login, collect and fetch do their whole job."""

    async def test_fill_select_set_and_click_put_into_a_form_what_they_were_told(self):
        tab = await self.open("/form.html")
        page = await tab.observe()
        await tab.fill(self.control(page, "input", "Customer").id, "Example Works")
        await tab.fill(self.control(page, "input", "Remark").id, "ring twice")
        await tab.select(self.control(page, "select", "Item").id, "wb")  # by value
        await tab.select(self.control(page, "select", "Unit").id, "Boxes")  # by label
        await tab.set(self.control(page, "radio", "Express").id, True)
        await tab.set(self.control(page, "checkbox", "Gift wrap").id, True)
        await tab.set(self.control(page, "checkbox", "Send me updates").id, False)
        await tab.set(self.control(page, "slider", "Priority").id, 4)

        page = await tab.observe()  # the model sees what it did
        self.assertIn('"Example Works"', self.control(page, "input", "Customer").text)
        self.assertIn('"ring twice"', self.control(page, "input", "Remark").text)
        self.assertIn('*"wb"', self.control(page, "select", "Item").text)
        self.assertIn('*"box"', self.control(page, "select", "Unit").text)
        self.assertIn("[checked]", self.control(page, "radio", "Express").text)
        self.assertIn("[unchecked]", self.control(page, "radio", "Standard").text)
        self.assertIn("[checked]", self.control(page, "checkbox", "Gift wrap").text)
        self.assertIn("[unchecked]", self.control(page, "checkbox", "Send me updates").text)
        self.assertIn("value=4", self.control(page, "slider", "Priority").text)

        self.assertIn("/echo", await tab.click(self.control(page, "button", "Place order").id))
        self.assertEqual(SITE.posted("/echo"), {"customer": "Example Works", "remark": "ring twice", "item": "wb",
                                                "unit": "box", "delivery": "express", "gift": "yes", "priority": "4"})
        self.assertIn("Example Works", await tab.observe())

    async def test_fill_with_enter_sends_a_form_that_has_no_button(self):
        tab = await self.open("/form.html")
        page = await tab.observe()
        await tab.fill(self.control(page, "input", "Find an order").id, "order 17", enter=True)
        self.assertTrue(tab.page.url.endswith("/echo?q=order+17"), tab.page.url)
        self.assertIn("order 17", await tab.observe())

    async def test_an_element_that_is_gone_is_reported_instead_of_guessed(self):
        tab = await self.open("/form.html")
        await tab.observe()
        for said in (await tab.click(999), await tab.fill(999, "x"), await tab.select(999, "x"), await tab.set(999, 1),
                     await tab.collect(999), await tab.fetch(999)):
            self.assertIn("no element [999]", said)
        self.assertEqual(SITE.paths, ["/form.html"])

    async def test_goto_back_and_reload_move_the_tab_but_never_off_the_site(self):
        tab = await self.open("/details.html")
        self.assertIn("/arrived/one", await tab.goto("/arrived/one"))
        self.assertIn("not on this website", await tab.goto("http://elsewhere.invalid/"))
        self.assertTrue(tab.page.url.endswith("/arrived/one"), tab.page.url)
        self.assertIn("/details.html", await tab.back())
        self.assertEqual(SITE.paths.count("/details.html"), 2)  # nothing may be cached: back asks the server again
        await tab.reload()
        self.assertEqual(SITE.paths.count("/details.html"), 3)
        self.assertIn("Weight: 12 kg", await tab.observe())

    async def test_navigation_recovery_can_refresh_the_current_page(self):
        tab = await self.open("/arrived/recovery-target")
        result = await tab._recover_navigation(
            tab.page.url, SITE.url + "/arrived/previous", "navigation", RuntimeError("timed out"))
        self.assertIn("refreshed successfully", result)
        self.assertIn("You arrived at recovery-target", await tab.observe())

    async def test_slow_navigation_reopens_the_tab_when_refresh_also_times_out(self):
        tab = await self.open("/details.html")
        original_page = tab.page
        SITE.requests.clear()
        with patch.object(browser_env, "PAGE_LOAD_TIMEOUT_MS", 500), patch.object(FixtureSite, "DELAY", 0.8):
            result = await tab.goto("/recover-on-reopen")
        self.assertIn("reopened the tab", result)
        self.assertIsNot(tab.page, original_page)
        self.assertTrue(original_page.is_closed())
        self.assertIn("Recovered page", await tab.observe())
        self.assertEqual(SITE.paths.count("/recover-on-reopen"), 2)

    async def test_unreachable_page_returns_a_readable_failure_after_bounded_recovery(self):
        tab = await self.open("/details.html")
        SITE.requests.clear()
        with patch.object(browser_env, "PAGE_LOAD_TIMEOUT_MS", 500), patch.object(FixtureSite, "DELAY", 0.8):
            result = await tab.goto("/unavailable")
            observation = await tab.observe()
        self.assertIn("refresh and reopen both failed", result)
        self.assertIn("did not load after refresh/reopen", observation)
        self.assertGreaterEqual(SITE.paths.count("/unavailable"), 2)
        self.assertLessEqual(SITE.paths.count("/unavailable"), 3)

    async def test_login_signs_in_with_the_credentials_of_the_site(self):
        tab = await self.open("/account", LOGIN)
        self.assertTrue(tab.page.url.endswith("/login"), tab.page.url)
        said = await tab.login()
        self.assertEqual(SITE.posted("/login"), {"ticket": SITE.ticket, **LOGIN})
        self.assertTrue(tab.page.url.endswith("/account"), tab.page.url)
        self.assertIn("/account", said)
        page = await tab.observe()
        self.assertIn("Signed in as pat.example", page)
        self.assertNotIn(LOGIN["password"], said + page)
        self.assertEqual([path for path in SITE.paths if path.startswith("/echo")], [])  # the search box was left alone

    async def test_login_says_when_it_has_no_credentials_or_no_form(self):
        self.assertIn("no credentials", await (await self.open("/login")).login())
        self.assertIn("no password field", await (await self.open("/form.html", LOGIN)).login())
        self.assertEqual(SITE.paths, ["/form.html"])

    async def test_collect_follows_next_to_the_last_page_and_returns_every_row_once(self):
        tab = await self.open("/orders")
        page = await tab.observe()
        said = await tab.collect(self.control(page, "link", "Next page").id)
        for number, item, quantity in SITE.ORDERS:
            self.assertEqual(said.count(f"Order {number} | {item} | {quantity}"), 1, said)
            self.assertEqual(tab.document.count(f"Order {number} |"), 1, tab.document)
        self.assertEqual(SITE.paths, ["/orders", "/orders?page=2", "/orders?page=3"])
        self.assertEqual(controls(said), [], "ids of pages that are gone invite a click on the wrong element")

    async def test_collect_stops_at_the_number_of_pages_it_was_given(self):
        tab = await self.open("/orders")
        page = await tab.observe()
        said = await tab.collect(self.control(page, "link", "Next page").id, max_pages=2)
        self.assertEqual([number for number, _, _ in SITE.ORDERS if f"Order {number} |" in said], [11, 12, 13, 14, 15, 16])
        self.assertEqual(SITE.paths, ["/orders", "/orders?page=2"])

    async def every_message_once(self, path: str) -> None:
        tab = await self.open(path)
        page = await tab.observe()
        said = await tab.collect(self.control(page, "button", "Load more").id)
        self.assertEqual(await tab.page.locator("#list li").count(), 5, said)
        self.assertEqual(await tab.page.locator("#more").count(), 0, said)
        # the answer may point at the page instead of repeating it: the model reads the page next, `keep` files .document
        for text in (tab.document, await tab.observe()):
            for number in range(21, 26):
                self.assertEqual(text.count(f"Message {number}:"), 1, f"{said}\n{text}")

    async def test_collect_presses_load_more_until_the_list_is_complete(self):
        await self.every_message_once("/load_more.html")

    async def test_collect_waits_for_rows_that_load_more_fetches_from_the_server(self):
        await self.every_message_once("/load_more_remote.html")

    async def test_fetch_returns_the_file_behind_a_link_and_leaves_the_tab_where_it_is(self):
        tab = await self.open("/downloads.html")
        page = await tab.observe()
        said = await tab.fetch(self.control(page, "link", "Price list (CSV)").id)
        for record in (FIXTURES / "price_list.csv").read_text(encoding="utf-8").splitlines():
            self.assertIn(record, said.splitlines())
            self.assertIn(record, tab.document.splitlines())
        self.assertIn("price_list.csv", said)
        self.assertTrue(tab.document_source.endswith("/price_list.csv"), tab.document_source)
        self.assertTrue(tab.page.url.endswith("/downloads.html"), tab.page.url)

    async def test_fetch_opens_an_archive_and_reads_the_files_in_it(self):
        tab = await self.open("/downloads.html")
        page = await tab.observe()
        said = await tab.fetch(self.control(page, "link", "All documents (ZIP)").id)
        for part in ("order-17-documents.zip", "readme.txt", "Bundle for order 17", "items.csv", "Widget A;4"):
            self.assertIn(part, said)

    async def test_fetch_reads_a_web_page_without_its_markup(self):
        tab = await self.open("/downloads.html")
        page = await tab.observe()
        said = await tab.fetch(self.control(page, "link", "Printable order").id)
        self.assertRegex(said, r"Widget A\W+4\b")
        self.assertIn("Ship to: Dock 2", said)
        self.assertNotRegex(said, r"<\w+|padding")
        self.assertIn("no address", await tab.fetch(self.control(page, "button", "Refresh").id))

    async def test_fetch_downloads_with_the_session_of_the_tab(self):
        tab = await self.open("/login", LOGIN)
        await tab.login()
        page = await tab.observe()
        said = await tab.fetch(self.control(page, "link", "Price list (CSV)").id)
        self.assertIn("Widget B;7.25;EUR", said)
        self.assertIn("Bundle for order 17", await tab.fetch(self.control(page, "link", "All documents (ZIP)").id))
        stranger = await self.open("/downloads.html")
        self.assertIn("opened", await stranger.goto("/private/price_list.csv"))
        self.assertIn("Sign in first.", await stranger.observe())


if __name__ == "__main__":
    unittest.main()
