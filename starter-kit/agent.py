"""RFQ-to-bid: a team of model-directed agents, one per website, and a deterministic calculator.

1. The model reads the brief: which tender, which date, which senders the rules authorize.
2. One agent per website explores it in its own browser. Each decides where to go, what to search for and which
   pages are evidence, and files those pages. A model call turns every filed page into structured facts in the
   shared case file; the open questions derived from the brief's rules tell all agents what is still missing, so a
   fact found on one website (a catalogue group, a key) steers the search on another. The ERP and the mailbox hold
   many independent records (one material per requirement, one message per kind of rule), so each of them gets
   several agents, every one in a tab of its own in that website's browser, with the open questions shared out
   among them: the records are then read side by side instead of one after another.
3. `calculator.py` applies the bid rules to the facts: arithmetic only.
4. An agent enters the bid in the tender portal, has the mail agent fetch the signing TAN, and submits.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

from playwright.async_api import async_playwright

import browsers
import llm
import run as contract
from browser_env import Tab
from calculator import calculate_bid
import sanity
import validate
from casefile import SHAPES, CaseFile, read_context
from worker import ACTIONS_HELP, CONTROL, Agent, find_control

RESTART_MIN_SECONDS = 120  # an unfinished agent is restarted only while this much investigation time is left
PING_EVERY = 12  # seconds between the requests that keep an idle agent's website session in use

# Everything up to the last line is the same for every investigator, so the model's prompt cache holds it once for all
# of them; the website an agent works stands at the very end (YOUR_SITE).
INVESTIGATOR = """You are one of several research agents preparing a tender bid for a manufacturer's sales
engineering team. The agents work the run's websites at the same time, each in a browser tab of its own; a website
can have several agents, each with its own open questions. Which website is yours stands at the end of these
instructions. Together you must find every fact the bid rules need. You only find and file evidence: you never
calculate prices.

TASK BRIEF (the rules are authoritative)
{brief}
END OF BRIEF

{actions}
Case-file actions:
  {{"do": "keep", "kind": "<kind>", "note": "<what it is>"}}
      File the document in front of you (the current page, or the document you just collected or fetched) as
      evidence. Its facts are then extracted into the shared case file and reported back to you. Kinds:
        positions         the tender's bill of quantities
        clarifications    answers of the tender's bidder Q&A
        revisions         the revision history of the buyer's technical specification
        requirements      the requirement details of one specification revision
        material          ERP master data of materials: technical attributes, status, planning, stock
        price_conditions  ERP price conditions of one material: validity, currency, quantity scales
        mail              an opened mail message
  {{"do": "file_each", "links": [12, 15, 18], "kind": "<kind>", "tabs": [{{"tab": "<tab label>", "kind": "<kind>"}}]}}
      Open each record you name from the list in front of you (up to 12 links by id) and file it with keep as
      "kind"; "tabs" (optional) also opens and files those tabs of every record, each with its own kind. You come
      back to the list with a report of what was filed. Use it when a result list holds several records you need:
      it saves a turn per record.
  {{"do": "answer", "question": "<id>", "status": "complete" | "absent", "note": "..."}}
      Close an open question: "complete" when all its evidence is filed (needed for lists that only you know to be
      complete), "absent" when you searched properly and what is asked for does not exist.

How to work
- Every turn starts with the case file and the OPEN QUESTIONS. Work on the questions your website can answer;
  the other agents handle the rest. Facts they file show up in the case file and can open new questions for you.
- The OPEN QUESTIONS shown to you are the work assigned to your website. Do not search for another website's
  records. If none are open, finish with done; the coordinator will call you again when your website gets new work.
- You remember only the page in front of you: file a relevant page with keep BEFORE you leave it. keep files the
  whole document, also what was cut from your view. File a document once, with the right kind. Facts on a page only
  enter the CASE FILE when you call keep on that page; reading a value in your thought without calling keep leaves
  the case file unchanged.
- Turns are what takes time, so do several things in one reply. keep files the page that is open at that moment:
  [keep, click] files the page in front of you and goes on; [click, keep] opens a link and files the page it leads
  to, for a link whose label already tells you it is a record you need. An address shown after "->" can be opened
  with goto, and several goto and keep can follow one another: [goto A, keep, goto B, keep] opens and files two
  records in one reply. A reply that only files a page, or only closes a question, wastes a turn.
- File pages that state the facts themselves (an opened document, record or message), not menus or hit lists.
  For a paged list or document that matters, use collect so that no page is missed, then keep it. For stock
  availability, drill down from plant/physical summaries into storage locations to view freely available
  (unrestricted) stock excluding blocked/inspection stock, and file that page with keep.
- The websites hold traps: superseded revisions, look-alike records for other tender references, drafts and
  previews, blocked materials, future-dated records, values in the wrong unit or number format. Check dates,
  references, senders and status marks before you rely on a record, and follow the brief's rules on which one
  counts. When in doubt, file both and let the facts decide.
- Do not reject a candidate record yourself (for example a material whose stroke seems to differ): file every
  candidate the question names, with file_each when a list shows several of them (after you have seen one record
  and know which of its tabs hold the facts), else with keep. The case file matches them against the requirement values, which can still
  change when amendments or reserves are filed.
- Follow the search hints a website gives. If a search returns nothing or too much, change the search terms.
- When a question asks for every item in a list, inspect its page controls and use collect on the control that
  advances that list before filing it. Check the resulting page count or end state; do not mark a list complete after
  seeing only its first page. If the list is inside an embedded document, use the controls belonging to that document.
- For mailbox questions, search relevant folders separately when mailbox search covers only the current folder;
  include the archive when looking for historical or latest records. If a query is too broad or returns nothing,
  refine the terms and try again. Open candidate messages and file the message itself, not only its hit row.
- If the news says extraction failed or found no usable records, do not mark that question complete. Reopen the
  evidence page, check that it contains the records requested, and file it again with the right kind.
- When no open question is left that your website can answer right now, finish with done, also when you need
  facts the other agents have not filed yet. You are called again as soon as the case file brings new questions.
  Never idle with wait and never browse around to pass the time.

"""
YOUR_SITE = 'YOUR WEBSITE: "{site}" at {url}.'
SHARED_SITE = (" Other agents work this same website in tabs of their own and share its login, so you are usually "
               "logged in already. The OPEN QUESTIONS shown to you are your share; the rest is theirs.")


class Team:
    """Holds an agent that has nothing to do until the case file brings news, and ends the investigation when every
    agent is idle and no filed document is still being read."""

    def __init__(self, case: CaseFile, names: list[str], deadline: float):
        self.case, self.names, self.deadline = case, set(names), deadline
        self.idle: set[str] = set()
        self.finished = asyncio.Event()
        self.woken: dict[str, float] = {}  # when an agent was last woken for finished reading (at most every 30 s)
        self.pings: dict = {}  # agent -> a request that keeps its website's session in use while it is held idle

    def release(self, name: str, candidates: set[str]) -> None:
        """A routed agent (ERP or mail) went idle leaving `candidates` open and nothing is being read any more: those
        go to every agent (RELEASED), and the others are woken by a case-file change. Questions that turn up while it
        is idle are its own work (it is woken for them), so only the ones open when it went idle are candidates."""
        if self.case.pending or site_role(name) is None:
            return
        still = {qid for qid, _ in self.case.questions()} & candidates
        fresh = still - RELEASED
        if fresh:
            RELEASED.update(fresh)
            print(f"[{name}] left open, now offered to every agent: {', '.join(sorted(fresh))}", flush=True)
            bump = getattr(self.case, "_bump", None)
            if bump:
                bump()

    async def watch(self) -> None:
        """Ends the investigation once no question is open and nothing is still being read: every fact the calculator
        needs is then on file. Before, it ended only when each agent had said so in a turn of its own (sandbox run
        9902e32b: 9 s for the ERP agent's closing turn after its last record was filed)."""
        quiet = 0
        while not self.finished.is_set() and time.time() < self.deadline:
            await asyncio.sleep(0.25)
            quiet = quiet + 1 if not self.case.pending and not self.case.questions() else 0
            if quiet >= 2:
                print("[team] no open question is left and nothing is being read: the investigation ends", flush=True)
                self.finished.set()

    async def wait_for_work(self, name: str, ready=lambda: True) -> bool:
        """Holds a further agent of a website until a share of the open questions is its own; no model call before
        that. False when the investigation ended first."""
        self.idle.add(name)
        try:
            while not self.finished.is_set() and time.time() < self.deadline:
                if ready() and questions_for_site(name, open_work(self.case)):
                    return True
                changed = await self.case.wait_change(self.case.version, 0.5)
                if not changed and self.idle == self.names and self.case.pending == 0:
                    self.finished.set()
            return False
        finally:
            self.idle.discard(name)

    def hook(self, name: str):
        async def hold(why: str) -> str | None:
            before = {qid for qid, _ in questions_for_site(name, open_work(self.case))}
            self.release(name, before)
            version = self.case.version
            # Documents still being read when the agent went idle: once one of them is done, the agent is woken if
            # questions are still open. Sandbox run 024bd2ac: the ERP agent went idle "until the requirements are
            # read" with its material questions already open; they were never new, so it slept for 400 s.
            reading = self.case.pending
            self.idle.add(name)
            pinged = time.time()
            try:
                while not self.finished.is_set() and time.time() < self.deadline:
                    if name in self.pings and time.time() - pinged >= PING_EVERY:
                        pinged = time.time()
                        asyncio.ensure_future(self.pings[name]())
                    changed = await self.case.wait_change(version, 1.0)
                    if self.finished.is_set():
                        break
                    if changed:
                        self.release(name, before)
                        version = self.case.version
                        fresh = {qid for qid, _ in questions_for_site(name, open_work(self.case))} - before
                        if fresh or self.case.news.get(name):
                            return "The case file has news" + (f"; new open questions: {', '.join(sorted(fresh))}" if fresh else "") + "."
                        still_open = {qid for qid, _ in questions_for_site(name, open_work(self.case))}
                        rested = time.time() - self.woken.get(name, 0.0) >= 30
                        if reading and self.case.pending < reading and still_open and rested:
                            self.woken[name] = time.time()
                            return ("Documents that were still being read are now in the case file. Open questions: "
                                    f"{', '.join(sorted(still_open))}. Work on those your website can answer.")
                    elif self.idle == self.names and self.case.pending == 0:
                        self.finished.set()
                return None
            finally:
                self.idle.discard(name)
        return hold


FILE_EACH_LIMIT = 12  # records per file_each action
FILE_EACH_SECONDS = 150  # and seconds for all of them; the agent continues from the list afterwards


async def file_each(tab, action: dict, file, saw, deadline: float, seen: str = "") -> str:
    """The file_each action: the model names several records on the page in front of it (links by id) and, if each
    record has tabs that matter, the tabs by label; code opens every record and tab and files it with the kind the
    model gave, then goes back to the list. The model chooses the records and kinds, as with keep; this only saves
    one model turn per page opened (the ERP agent needed 58-85 turns per run, each resending its whole prompt)."""
    kind = str(action.get("kind") or "")
    tabs = [t for t in (action.get("tabs") or []) if isinstance(t, dict) and t.get("tab")]
    links = [str(i).strip("[] ") for i in (action.get("links") or [])][:FILE_EACH_LIMIT]
    if not links or not kind:
        return 'file_each needs "links" (ids of the records on this page) and "kind"'
    home = tab.page.url
    # The ids and link targets of the page the model saw when it chose (`seen`); a fresh read could number the
    # elements differently (a list that loads or reorders), and the wrong record would be filed.
    page = seen or await tab.observe()
    targets = {m.group(1): (m.group(3), (re.search(r"->\s*(\S+)", m.group(4)) or [None, None])[1])
               for m in CONTROL.finditer(page) if m.group(2) in ("link", "clickable")}
    first = True
    stop_at = min(deadline, time.time() + FILE_EACH_SECONDS)
    report = []
    for aid in links:
        if time.time() > stop_at:
            report.append(f"[{aid}] not opened: time for this action is up; file the rest with another file_each")
            continue
        label, href = targets.get(aid, (None, None))
        if label is None:
            report.append(f"[{aid}] is not a link on this page")
            continue
        if not href and not first:  # a link without an address: find it again by its label on the list as it is now
            if tab.page.url != home:
                await tab.goto(home)
            aid_now, why = find_control(await tab.observe(), label, ("link", "clickable"))
            if aid_now is None:
                report.append(f'[{aid}] "{label}" not opened: {why}')
                continue
            aid = aid_now
        first = False
        opened = await (tab.goto(href) if href else tab.click(aid))
        record = await tab.observe()
        saw(record)
        if tab.page.url == home:  # nothing opened (a control that filters or sorts the list): nothing to file
            report.append(f'[{aid}] "{label}" did not open a record ({opened[:60]})')
            continue
        filed = [f"{kind}: {file(kind, tab.document, tab.document_source)[:120]}"]
        record_url = tab.page.url
        for extra in tabs:
            # Looked up on the page as it is now (ids change with every page); if the previous tab replaced the
            # record and this tab is not on it, back to the record first.
            now = await tab.observe()
            control, why = find_control(now, str(extra["tab"]), ("link", "button", "clickable"))
            if control is None and tab.page.url != record_url:
                await tab.goto(record_url)
                now = await tab.observe()
                control, why = find_control(now, str(extra["tab"]), ("link", "button", "clickable"))
            if control is None:
                filed.append(f'tab "{extra["tab"]}": {why}')
                continue
            href_tab = next((m.group(4) for m in CONTROL.finditer(now) if m.group(1) == control), "")
            target = re.search(r"->\s*(\S+)", href_tab or "")
            await (tab.goto(target.group(1)) if target else tab.click(control))
            saw(await tab.observe())
            tab_kind = str(extra.get("kind") or kind)
            filed.append(f'tab "{extra["tab"]}" as {tab_kind}: {file(tab_kind, tab.document, tab.document_source)[:120]}')
        report.append(f'[{aid}] "{label}" ({opened[:60]}): ' + "; ".join(filed))
    await tab.goto(home)
    return f"file_each opened {len(links)} record(s) and is back on the list:\n" + "\n".join(report)


def investigator(case: CaseFile, team: Team, tab: Tab, deadline: float, name: str | None = None) -> Agent:
    name = name or tab.site_id
    shared = len(POOLS.get(site_role(name) or "") or []) > 1
    system = (INVESTIGATOR.format(brief=case.brief, actions=ACTIONS_HELP)
              + YOUR_SITE.format(site=tab.site_id, url=tab.base_url) + (SHARED_SITE if shared else ""))
    LOOKING_AT[name] = lambda: f"{tab.page.url}\n{tab.document[:30000]}"
    team.pings[name] = tab.ping

    def briefing() -> str:
        questions = questions_for_site(name, open_work(case))
        news = case.take_news(name)
        text = f"CASE FILE\n{case.status()}\n\nOPEN QUESTIONS\n"
        text += "\n".join(f"- [{qid}] {question}" for qid, question in questions) or "(none)"
        if news:
            text += "\n\nNEWS FROM YOUR FILED DOCUMENTS\n" + "\n".join(f"- {item}" for item in news)
        own_pending = case.pending_for(name)
        if own_pending:
            text += f"\n\n({own_pending} filed document(s) are still being read.)"
        elif case.pending:
            text += f"\n\n({case.pending} document(s) filed by other agents are still being read.)"
        return text

    agent: Agent

    async def keep(action: dict) -> str:
        document, source = agent.document()
        res = case.file(str(action.get("kind", "")), document, source, name)
        if res.startswith("this document is already filed"):
            res += "; do not file this same page again — check OPEN QUESTIONS for what is missing on another page, or finish with done"
        return res

    async def file_each_action(action: dict) -> str:
        return await file_each(tab, action, lambda kind, text, source: case.file(kind, text, source, name),
                               agent.coverage.saw, deadline, agent.page_seen)

    async def answer(action: dict) -> str:
        # A completion action can arrive in the same turn as `keep`. Wait for this agent's own
        # extraction result (up to 45 s) instead of blocking on slow extractions from other websites.
        wait_until = min(deadline, time.time() + 45.0)
        while case.pending_for(name) and time.time() < wait_until:
            version = case.version
            if case.pending_for(name):
                await case.wait_change(version, min(1.0, max(0.0, wait_until - time.time())))
        if case.pending_for(name):
            return "Evidence extraction is still pending and the investigation deadline is near; do not repeat this answer action."
        return case.answer(str(action.get("question", "")).strip("[] "), str(action.get("status", "")), str(action.get("note", "")), agent=name)

    # Navigation runs without the model's thinking: about twice as fast. The facts themselves are read by the
    # extraction calls in casefile.py, which keep thinking on. AGENT_THINK=1 turns it back on for comparison.
    agent = Agent(name, tab, system, briefing, extra={"keep": keep, "file_each": file_each_action, "answer": answer}, deadline=deadline,
                  max_steps=int(os.environ.get("AGENT_MAX_STEPS", "150")), idle=team.hook(name),
                  think=os.environ.get("AGENT_THINK") == "1",
                  work=lambda: bool(questions_for_site(name, open_work(case))), relogin=True,
                  circle_hint="You are going in circles: the same thought keeps coming back without progress. If a "
                              "link or control does nothing, do not click it again: open its address directly with "
                              "goto, or use the search form on the page. If the "
                              "page in front of you is a candidate record, file it with keep now, also when it seems "
                              "not to match: the case file does the matching, and requirement values can still change "
                              "when amendments or reserves are filed. If you need facts the other agents have not "
                              "filed yet, finish with done; you are called again when the case file has news.")
    return agent


# Which website plays which role in this run, as the model read it from the brief (solve). The private dataset uses
# other site names, so routing goes by role first and falls back to the public site names only without one.
SITE_ROLES: dict[str, str] = {}
# Routed questions their own agent left open after it finished, with nothing still being read: offered to every agent
# from then on. The routing assumes the public split of facts (materials in the ERP, keys and approvals in the mail);
# the private dataset may split them differently, and a routed question must not end up with nobody to answer it.
RELEASED: set[str] = set()


# Several agents on one website (see the module text): how many tabs the ERP and the mailbox get, the agents of each
# role in this run (the website's own agent first, the others named "<site>#2", ...), which agent took which share
# of the open questions, and what each agent's tab shows (a question about a record goes to the agent looking at it).
POOL_SIZE = {"erp": int(os.environ.get("ERP_TABS", "5")), "mail": int(os.environ.get("MAIL_TABS", "5"))}
POOLS: dict[str, list[str]] = {}
CLAIMS: dict[str, str] = {}
LOOKING_AT: dict = {}
ERP_KINDS = ("material:", "data:")
MAIL_KINDS = ("condition_group:", "packaging_tariff:", "delivery_commitment:", "setup_pool:", "project_bonus:",
              "index:", "fx:", "reserve:", "topic:")
# One mail message defines every key of its kind, so all questions about a kind are one share of work.
MAIL_SHARE = {"condition_group": "condition_rules", "packaging_tariff": "packaging_tariffs",
              "delivery_commitment": "delivery_release", "setup_pool": "setup_pools", "project_bonus": "project_bonus",
              "index": "price_indices", "fx": "fx_fixing", "reserve": "tech_release"}


def open_work(case) -> list[tuple[str, str]]:
    """What the agents are shown: the open questions, and the mail records that can be fetched ahead of them."""
    ahead = getattr(case, "prefetch", None)
    return case.questions() + (ahead() if ahead else [])


def site_of(name: str) -> str:
    return name.split("#", 1)[0]


def routed_role(qid: str) -> str | None:
    """The role whose agents a question is routed to, None for one every website's agent sees."""
    if qid.startswith(ERP_KINDS):
        return "erp"
    if qid in ("mailbox", "discount") or qid.startswith(MAIL_KINDS):
        return "mail"
    return None


def work_share(qid: str) -> str:
    kind, _, key = qid.partition(":")
    if kind == "topic":
        return f"mail:{key}"
    return f"mail:{MAIL_SHARE[kind]}" if kind in MAIL_SHARE else qid


def share_out(role: str, qids: list[str]) -> None:
    """Gives every share of work nobody has yet to one agent of the role: the one whose tab shows the record it is
    about (the agent that filed a material gets the question about its stock and prices), else the one with the
    least open work. A share stays with its agent, so two agents never work the same record."""
    pool = POOLS.get(role) or []
    shares = list(dict.fromkeys(work_share(qid) for qid in qids))
    load = {name: sum(1 for share in shares if CLAIMS.get(share) == name) for name in pool}
    for share in shares:
        if CLAIMS.get(share) in load:
            continue
        key = share.split(":", 1)[-1]
        near = [name for name in pool if key and key in (LOOKING_AT[name]() if name in LOOKING_AT else "")]
        owner = min(near or pool, key=lambda name: (load[name], pool.index(name)))
        CLAIMS[share] = owner
        load[owner] += 1


def retire(name: str) -> None:
    """An agent that has ended for good: its shares go back to the others of its role."""
    for pool in POOLS.values():
        if name in pool and len(pool) > 1:
            pool.remove(name)
    for share in [share for share, owner in CLAIMS.items() if owner == name]:
        del CLAIMS[share]
    LOOKING_AT.pop(name, None)


def site_role(site_id: str) -> str | None:
    """"erp" or "mail" for the agent whose questions are routed, None for one that sees every question."""
    site = site_of(site_id).casefold()
    for role in ("erp", "mail"):
        named = (SITE_ROLES.get(role) or "").casefold()
        if (site == named) if named else (role in site):
            return role
    return None


def questions_for_site(site_id: str, questions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep specialized mail and ERP agents on questions their portal can resolve (from Vamshidhar's v1 package,
    sandbox run 44a626ef: 100 in 9.6 min, every agent finished by itself)."""
    role = site_role(site_id)
    if role is None:
        # A website without a routed role sees what no agent of this run is routed for. Sandbox run 9902e32b: shown
        # the mailbox's questions too, the supplier-portal agent searched its own website for them for 17 turns.
        # A routed question its agents gave up on is RELEASED and shown to everyone.
        present = {routed for routed, named in SITE_ROLES.items() if named}
        return [(qid, text) for qid, text in questions if routed_role(qid) not in present or qid in RELEASED]
    mine = [(qid, text) for qid, text in questions if routed_role(qid) == role or qid in RELEASED]
    pool = POOLS.get(role) or []
    if len(pool) < 2 or site_id not in pool:
        return mine
    share_out(role, [qid for qid, _ in mine if qid not in RELEASED])
    return [(qid, text) for qid, text in mine if qid in RELEASED or CLAIMS.get(work_share(qid)) == site_id]


async def open_tabs(playwright, sites: list[dict]) -> tuple[list, dict[str, Tab]]:
    """One tab per website: a browser each while the run has enough of them, else further tabs in the last one."""
    count = max(1, len(browsers.cdp_urls()) or min(len(sites), 4))
    opened, tabs, pages = [], {}, []
    for number in range(min(count, len(sites))):
        browser, page = await browsers.connect(playwright, number)
        opened.append(browser)
        pages.append(page)
    for index, site in enumerate(sites):
        page = pages[index] if index < len(pages) else await pages[-1].context.new_page()
        tabs[site["siteId"]] = Tab(page, site)
    for tab in tabs.values():  # where a tab may move when its own browser cannot load its website (Tab.renew)
        tab.spares = [page.context for page in pages if page.context is not tab.page.context]
    return opened, tabs


async def further_tab(tab: Tab) -> Tab | None:
    """Another tab on `tab`'s website, in the same browser and session, for a further agent of that website. It
    opens as a window of its own where the browser allows it: a tab behind another one is throttled by the browser
    (timers, drawing), and these agents work at the same time. None when the browser gives no further page."""
    context, page = tab.page.context, None
    try:
        browser = context.browser
        session = await (browser.new_browser_cdp_session() if browser else context.new_cdp_session(tab.page))
        try:
            async with context.expect_page(timeout=4000) as opening:
                await session.send("Target.createTarget", {"url": "about:blank", "newWindow": True,
                                                           "left": 0, "top": 0, "width": 1280, "height": 800})
            page = await opening.value
        finally:
            await session.detach()
    except Exception:  # noqa: BLE001  no window of its own: an ordinary further tab
        page = None
    try:
        page = page or await context.new_page()
    except Exception as error:  # noqa: BLE001
        print(f"[{tab.site_id}] no further tab: {str(error)[:150]}", flush=True)
        return None
    further = Tab(page, {"siteId": tab.site_id, "url": tab.base_url, "credentials": tab.credentials})
    further.spares = tab.spares
    return further


async def arrive(tab: Tab) -> None:
    """Opens the website and, if it starts with its login form, enters this run's login: no decision is in that, so
    it happens while the model still reads the brief. Everything after it is the agent's."""
    try:
        await asyncio.wait_for(tab.goto(tab.base_url + "/"), 25)
        await asyncio.wait_for(tab.enter(), 15)
    except Exception as error:  # noqa: BLE001  the agent opens and logs in itself then
        print(f"[{tab.site_id}] not opened ahead: {str(error)[:150]}", flush=True)


async def solve(task: dict, playwright) -> dict | None:
    started = time.time()
    sites = [site for site in task.get("access") or [] if isinstance(site, dict) and site.get("siteId") and site.get("url")]
    only = {name for name in os.environ.get("AGENT_SITES", "").split(",") if name}
    if not sites or not task.get("brief"):
        return None
    brief = task["brief"]
    listing = "\n".join(f"- {site['siteId']}: {site['url']}" for site in sites)
    reading = asyncio.ensure_future(read_context(brief + f"\n\nThe websites of this run:\n{listing}"))
    try:
        opened, tabs = await open_tabs(playwright, sites)
    except BaseException:
        reading.cancel()
        raise
    await asyncio.gather(*(arrive(tab) for name, tab in tabs.items() if not only or name in only))
    context = await reading
    SITE_ROLES.clear()
    RELEASED.clear()
    POOLS.clear()
    CLAIMS.clear()
    LOOKING_AT.clear()
    SITE_ROLES.update({role: str(context.get(f"{role}_site") or "") for role in ("erp", "mail")
                       if str(context.get(f"{role}_site") or "") in {site["siteId"] for site in sites}})
    case = CaseFile(brief, context)
    print(f"Sanity checks on: {', '.join(sanity.active()) or 'none'}", flush=True)
    print(f"Context: tender {case.reference}, scenario {case.scenario.isoformat()}, company {case.company}, "
          f"senders {case.senders}", flush=True)

    left = contract.seconds_left(task)
    budget = min(left, 20 * 60) if left != float("inf") else 20 * 60  # a scored run has 20 minutes
    hard_stop = started + budget - 45
    # The bid form takes ~2 min and the signing code another 1-2 min; a signed partial bid scores, an unsigned
    # complete one does not. So the investigation ends with 8 minutes left (sandbox test run 54c78c38: 330 s was
    # too little and the run ended unsigned).
    submit_reserve = min(480, budget * 0.4)
    investigate_until = hard_stop - submit_reserve

    try:
        working = [tab for name, tab in tabs.items() if not only or name in only]
        members: list[tuple[str, Tab]] = [(tab.site_id, tab) for tab in working]
        for role in ("erp", "mail"):  # further agents for the websites with many independent records
            lead = next((tab for tab in working if site_role(tab.site_id) == role), None)
            if lead is None:
                continue
            extra = await asyncio.gather(*(further_tab(lead) for _ in range(max(0, POOL_SIZE[role] - 1))))
            names = [lead.site_id]
            for further in extra:
                if further is not None:
                    names.append(f"{lead.site_id}#{len(names) + 1}")
                    members.append((names[-1], further))
            POOLS[role] = names
        print("Agents: " + ", ".join(name for name, _ in members), flush=True)
        team = Team(case, [name for name, _ in members], investigate_until)
        running: dict[str, Agent] = {}

        async def bounded(name, tab):  # an agent stuck past the deadline is stopped so that the others' facts still count
            restarted = False
            own = site_of(name)
            try:
                if name != own:  # a further agent: no model call until a share of the work is its own and the
                    # website's first agent is past the entrance (its first turn: a consent page, a check)
                    since = time.time()
                    if not await team.wait_for_work(name, lambda: (own in running and running[own].steps >= 2)
                                                    or own in team.idle or time.time() - since > 20):
                        return {"done": True, "summary": "no work came up for this agent", "answer": None, "steps": 0}
                while True:
                    limit = max(10.0, investigate_until - time.time() + 20)
                    running[name] = investigator(case, team, tab, investigate_until, name)
                    try:
                        outcome = await asyncio.wait_for(running[name].run(), limit)
                    except asyncio.TimeoutError:
                        team.finished.set()
                        return RuntimeError(f"stopped after {limit:.0f} s: still busy at the investigation deadline")
                    # Weak sandbox runs (10, 22.8, 27.8, 29.1) had agents end with done: false while time was left. One
                    # fresh start, with a clean conversation in a new tab, instead of giving the site up (sanity.py).
                    if (sanity.enabled("agent_restart") and not restarted and not outcome.get("done")
                            and not team.finished.is_set()
                            and investigate_until - time.time() > RESTART_MIN_SECONDS and case.questions()):
                        restarted = True
                        print(f"[{name}] stopped unfinished after {outcome.get('steps')} steps; restarting once "
                              "in a fresh tab", flush=True)
                        try:
                            await asyncio.wait_for(tab.renew(), 45)
                        except Exception as error:  # noqa: BLE001
                            print(f"[{name}] could not renew the tab: {str(error)[:150]}", flush=True)
                        continue
                    return outcome
            finally:  # ended for good: the others may finish without it, and its share of the work is theirs
                team.names.discard(name)
                retire(name)
                bump = getattr(case, "_bump", None)
                if bump:
                    bump()

        jobs = [asyncio.ensure_future(bounded(name, tab)) for name, tab in members]
        watching = asyncio.ensure_future(team.watch())
        ended = asyncio.ensure_future(team.finished.wait())
        while not team.finished.is_set() and any(not job.done() for job in jobs):
            await asyncio.wait([ended, *(job for job in jobs if not job.done())], return_when=asyncio.FIRST_COMPLETED)
        # The investigation is over. Agents held idle return by themselves; one still in a turn would only finish a
        # page nobody needs any more, so it is stopped instead of waited for.
        if jobs:
            await asyncio.wait(jobs, timeout=1.5)
        for job in (*jobs, watching, ended):
            if not job.done():
                job.cancel()
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.gather(watching, ended, return_exceptions=True)
        for (name, _), outcome in zip(members, outcomes):
            print(f"[{name}] finished: {outcome if isinstance(outcome, BaseException) else json.dumps(outcome, ensure_ascii=False)[:300]}", flush=True)
        for _ in range(40):  # facts of the last filed documents
            if not case.pending:
                break
            await asyncio.sleep(0.5)

        facts = case.inputs()
        bid = calculate_bid(case.reference, case.scenario, facts.governing, facts.positions, facts.clarifications,
                            facts.requirements, facts.materials, facts.authorities, facts.no_material)
        issues = []
        if sanity.enabled("validator"):
            bid, issues = validate.check(bid, {p.position for p in facts.positions} or None)
        if issues:
            print("VALIDATOR: " + " | ".join(issues), flush=True)
        print(f"Investigation took {time.time() - started:.0f} s. {llm.summary()}", flush=True)
        print("CASE FILE\n" + case.status(), flush=True)
        print("OPEN QUESTIONS LEFT: " + (", ".join(qid for qid, _ in case.questions()) or "none"), flush=True)
        print("BID: " + json.dumps(bid.answer(), ensure_ascii=False) + f" omitted={bid.omitted} notes={bid.notes}", flush=True)
        if os.environ.get("AGENT_DUMP"):
            with open(os.environ["AGENT_DUMP"], "w", encoding="utf-8") as handle:
                handle.write(case.dump())
        if os.environ.get("AGENT_SKIP_SUBMIT") == "1":
            return bid.answer()

        submission = task.get("submission") or {}
        tender_tab = tabs.get(submission.get("siteId")) or next(
            (tab for tab in tabs.values() if submission.get("url", "").startswith(tab.base_url)), None)
        mail_tab = tabs.get(str(context.get("mail_site") or ""))
        if submission.get("kind") == "site" and tender_tab is not None:
            import submit
            receipt = await submit.submit_bid(
                tender_tab, mail_tab, bid,
                {"tender_reference": case.reference, "scenario_date": case.scenario.isoformat(), "company": case.company,
                 "vat_id": case.vat_id, "email": case.mailbox or ""}, brief, hard_stop)
            print(f"Submission: {json.dumps(receipt, ensure_ascii=False)[:600]}. Total {time.time() - started:.0f} s. {llm.summary()}", flush=True)
        return bid.answer()
    finally:
        for browser in opened:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass


async def main() -> int:
    task, token = contract.load_run()
    print(f"Run {task.get('run_id')}: {len(task.get('access') or [])} site(s), {contract.seconds_left(task):.0f}s left.", flush=True)
    async with async_playwright() as playwright:
        answer = await solve(task, playwright)
    if not answer:
        print("No answer to submit; exiting cleanly.")
    elif (task.get("submission") or {}).get("kind") == "site":
        print(f"Answer worked out for the site: {json.dumps(answer)}")
    elif not token:
        print(f"Answer (local run, not submitted): {json.dumps(answer)}")
    else:
        status, body = contract.submit_answer(task, token, answer)
        print(f"Submitted answer -> HTTP {status}: {body}")
        return 0 if status < 300 else 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
