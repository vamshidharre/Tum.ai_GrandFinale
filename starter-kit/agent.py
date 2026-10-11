"""RFQ-to-bid: a team of model-directed agents, one per website, and a deterministic calculator.

1. The model reads the brief: which tender, which date, which senders the rules authorize.
2. One agent per website explores it in its own browser. Each decides where to go, what to search for and which
   pages are evidence, and files those pages. A model call turns every filed page into structured facts in the
   shared case file; the open questions derived from the brief's rules tell all agents what is still missing, so a
   fact found on one website (a catalogue group, a key) steers the search on another.
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

INVESTIGATOR = """You are one of several research agents preparing a tender bid for a manufacturer's sales
engineering team. Each agent works a different website at the same time; you work the website "{site}" at {url}
in your own browser tab. Together you must find every fact the bid rules need. You only find and file evidence:
you never calculate prices.

TASK BRIEF (the rules are authoritative)
{brief}
END OF BRIEF

{actions}
Case-file actions:
  {{"do": "keep", "kind": "<kind>", "note": "<what it is>"}}
      File the document in front of you (the current page, or the document you just collected or fetched) as
      evidence. Its facts are then extracted into the shared case file and reported back to you. Kinds:
        positions         the tender's bill of quantities
        clarifications    answers of the tender's bidder Q&A, and change or amendment notices to the tender
        revisions         the revision history of the buyer's technical specification
        requirements      the requirement details of one specification revision
        material          ERP master data of materials: technical attributes, status, planning, stock
        price_conditions  ERP price conditions of one material: validity, currency, quantity scales
        mail              an opened mail message, or a register of approvals/releases (one record per row)
  {{"do": "file_each", "links": [12, 15, 18], "kind": "<kind>", "tabs": [{{"tab": "<tab label>", "kind": "<kind>"}}]}}
      Open each record you name from the list in front of you (up to 12 links by id) and file it with keep as
      "kind"; "tabs" (optional) also opens and files those tabs of every record, each with its own kind. A tab can
      also be a path of controls clicked in order, the page at its end being filed:
      {{"path": ["<label 1>", "<label 2>", "<label 3>"], "kind": "<kind>"}} (labels or a part of them, as the
      record page shows them). You come back to the list with a report of what was filed. Use it when a result list
      holds several records you need: walk the first record yourself to learn which tabs and paths hold its facts,
      then file the others with one file_each giving the same tabs and paths.
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


class Team:
    """Holds an agent that has nothing to do until the case file brings news, and ends the investigation when every
    agent is idle and no filed document is still being read."""

    def __init__(self, case: CaseFile, names: list[str], deadline: float):
        self.case, self.names, self.deadline = case, set(names), deadline
        self.idle: set[str] = set()
        self.finished = asyncio.Event()
        self.woken: dict[str, float] = {}  # when an agent was last woken for finished reading (at most every 30 s)

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

    def hook(self, name: str):
        async def hold(why: str) -> str | None:
            before = {qid for qid, _ in questions_for_site(name, self.case.questions())}
            self.release(name, before)
            version = self.case.version
            # Documents still being read when the agent went idle: once one of them is done, the agent is woken if
            # questions are still open. Sandbox run 024bd2ac: the ERP agent went idle "until the requirements are
            # read" with its material questions already open; they were never new, so it slept for 400 s.
            reading = self.case.pending
            self.idle.add(name)
            try:
                while not self.finished.is_set() and time.time() < self.deadline:
                    changed = await self.case.wait_change(version, 1.0)
                    if self.finished.is_set():
                        break
                    if changed:
                        self.release(name, before)
                        version = self.case.version
                        fresh = {qid for qid, _ in questions_for_site(name, self.case.questions())} - before
                        if fresh or self.case.news.get(name):
                            return "The case file has news" + (f"; new open questions: {', '.join(sorted(fresh))}" if fresh else "") + "."
                        still_open = {qid for qid, _ in questions_for_site(name, self.case.questions())}
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
BATCH_NUDGE_AFTER = 3  # same-kind pages filed one by one before file_each is suggested once


def batch_hint(counter: dict, kind: str, filed: bool) -> str:
    """Suggest the file_each batch action once an agent files the same kind of page one by one.

    `counter` maps kind -> consecutive filings (mutated). Returns the hint text, or "". Fires
    exactly once per streak so it costs one line, not a nag on every turn. The model still
    decides; nothing here assumes what any website looks like."""
    if not filed or not kind:
        return ""
    counter[kind] = counter.get(kind, 0) + 1
    for other in [key for key in counter if key != kind]:
        counter.pop(other, None)
    if counter[kind] == BATCH_NUDGE_AFTER:
        return ("; you have now filed several pages of this kind one by one — if the list they came "
                "from still shows more records of the same kind, go back to that list and file the "
                "rest together with one file_each (naming the records and the tabs that hold the facts)")
    return ""


async def file_each(tab, action: dict, file, saw, deadline: float, seen: str = "") -> str:
    """The file_each action: the model names several records on the page in front of it (links by id) and, if each
    record has tabs that matter, the tabs by label; code opens every record and tab and files it with the kind the
    model gave, then goes back to the list. The model chooses the records and kinds, as with keep; this only saves
    one model turn per page opened (the ERP agent needed 58-85 turns per run, each resending its whole prompt)."""
    kind = str(action.get("kind") or "")
    tabs = [t for t in (action.get("tabs") or []) if isinstance(t, dict) and (t.get("tab") or t.get("path"))]
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
            # A tab, or a path of controls clicked one after another (a stock overview, then a plant, then a
            # storage location); the page at its end is filed. Every control is looked up by its label on the page
            # as it is then (ids change with every page). If the first one is not on the page, back to the record.
            path = [str(step) for step in (extra.get("path") or [extra.get("tab")]) if step][:6]
            name = " > ".join(path)
            missing = None
            for number, step in enumerate(path):
                now = await tab.observe()
                control, why = find_control(now, step, ("link", "button", "clickable"))
                if control is None and number == 0 and tab.page.url != record_url:
                    await tab.goto(record_url)
                    now = await tab.observe()
                    control, why = find_control(now, step, ("link", "button", "clickable"))
                if control is None:
                    missing = f'"{name}": {why}'
                    break
                href_tab = next((m.group(4) for m in CONTROL.finditer(now) if m.group(1) == control), "")
                target = re.search(r"->\s*(\S+)", href_tab or "")
                await (tab.goto(target.group(1)) if target else tab.click(control))
            if missing:
                filed.append(missing)
                continue
            saw(await tab.observe())
            tab_kind = str(extra.get("kind") or kind)
            filed.append(f'"{name}" as {tab_kind}: {file(tab_kind, tab.document, tab.document_source)[:120]}')
        report.append(f'[{aid}] "{label}" ({opened[:60]}): ' + "; ".join(filed))
    await tab.goto(home)
    return f"file_each opened {len(links)} record(s) and is back on the list:\n" + "\n".join(report)


CREW_NOTE = """
You are one of {count} agents on this website, each in its own tab and logged in to the same session; the OPEN
QUESTIONS shown to you are your share of its work. Do not work on the others' questions: they are being handled.
"""


def investigator(case: CaseFile, team: Team, tab: Tab, deadline: float, name: str | None = None) -> Agent:
    name = name or tab.site_id
    system = INVESTIGATOR.format(site=tab.site_id, url=tab.base_url, brief=case.brief, actions=ACTIONS_HELP)
    if len(CREW.get(tab.site_id) or []) > 1:
        system += CREW_NOTE.format(count=len(CREW[tab.site_id]))

    def briefing() -> str:
        questions = questions_for_site(name, case.questions())
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
    batched: dict = {}  # kind -> consecutive one-by-one filings, for the file_each nudge

    async def keep(action: dict) -> str:
        kind = str(action.get("kind", ""))
        document, source = agent.document()
        res = case.file(kind, document, source, name)
        if res.startswith("this document is already filed"):
            res += "; do not file this same page again — check OPEN QUESTIONS for what is missing on another page, or finish with done"
        else:
            res += batch_hint(batched, kind, not res.startswith("there is no document"))
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
                  circle_hint="You are going in circles: the same thought keeps coming back without progress. If a "
                              "link or control does nothing, do not click it again: open its address directly with "
                              "goto, or use the search form on the page. If the "
                              "page in front of you is a candidate record, file it with keep now, also when it seems "
                              "not to match: the case file does the matching, and requirement values can still change "
                              "when amendments or reserves are filed. If you need facts the other agents have not "
                              "filed yet, finish with done; you are called again when the case file has news.")
    AGENTS[name] = agent
    return agent


# Which website plays which role in this run, as the model read it from the brief (solve). The private dataset uses
# other site names, so routing goes by role first and falls back to the public site names only without one.
SITE_ROLES: dict[str, str] = {}
# Routed questions their own agent left open after it finished, with nothing still being read: offered to every agent
# from then on. The routing assumes the public split of facts (materials in the ERP, keys and approvals in the mail);
# the private dataset may split them differently, and a routed question must not end up with nobody to answer it.
RELEASED: set[str] = set()
# Several agents on one website, each in its own tab of the same browser session, split its open questions between
# them. Scored run a176359f: the ERP agent worked 46 turns one after another (search a group, open a candidate, its
# planning tab, file it; then every price condition; then every stock) and was the last to finish. Rule 10 of the
# brief says sites and positions can be investigated in parallel, and tabs are not limited. By role: CREW_SIZE["erp"]
# agents on the ERP, CREW_SIZE["mail"] on the webmail (1 turns it off).
# Off by default: test run 10def205 (3 ERP + 2 mail agents) took 236 s and $0.31 for the investigation against 219 s
# and $0.14 with one agent per site (a176359f): the agents redid each other's materials (154 ERP calls instead of 46).
CREW_SIZE = {"erp": int(os.environ.get("ERP_AGENTS", "1")), "mail": int(os.environ.get("MAIL_AGENTS", "1"))}
HELPER_MARK = "~"  # a helper agent is named "<site>~<n>"
CREW: dict[str, list[str]] = {}  # site -> the names of its agents, the site's own first
ASSIGNED: dict[str, str] = {}  # question group -> the agent of its site's crew that works on it
AGENTS: dict[str, Agent] = {}  # name -> the investigator, so a crew can wait for its site's agent to be under way


def base_site(name: str) -> str:
    """The website an agent works on: "erp" for "erp" and for its helper "erp~2"."""
    return name.split(HELPER_MARK, 1)[0]


def share(name: str, routed: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """This agent's share of its website's open questions when several agents work that website. A question group
    stays with the agent it was given to while it is open; a new one goes to the agent with the fewest open groups.
    On the ERP each question is a group (one per requirement or material); on the webmail one kind of record is (all
    delivery commitments come from the same message)."""
    site = base_site(name)
    crew = CREW.get(site) or []
    if len(crew) < 2:
        return routed

    def group(qid: str) -> str:
        return qid.split(":", 1)[0] if site_role(site) == "mail" else qid

    groups = sorted({group(qid) for qid, _ in routed if qid not in RELEASED})
    load = {member: 0 for member in crew}
    for key in groups:
        if ASSIGNED.get(key) in load:
            load[ASSIGNED[key]] += 1
    for key in groups:
        if ASSIGNED.get(key) not in load:
            ASSIGNED[key] = min(crew, key=lambda member: (load[member], crew.index(member)))
            load[ASSIGNED[key]] += 1
    return [(qid, text) for qid, text in routed if qid in RELEASED or ASSIGNED.get(group(qid)) == name]


def site_role(site_id: str) -> str | None:
    """"erp" or "mail" for the agent whose questions are routed, None for one that sees every question."""
    site = base_site(site_id).casefold()
    for role in ("erp", "mail"):
        named = (SITE_ROLES.get(role) or "").casefold()
        if (site == named) if named else (role in site):
            return role
    return None


def questions_for_site(site_id: str, questions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep specialized mail and ERP agents on questions their portal can resolve (from Vamshidhar's v1 package,
    sandbox run 44a626ef: 100 in 9.6 min, every agent finished by itself). Several agents on one website share its
    questions (`share`)."""
    site = base_site(site_id).casefold()

    def plays(role: str) -> bool:
        named = (SITE_ROLES.get(role) or "").casefold()
        return site == named if named else role in site

    if plays("erp"):
        prefixes = ("material:", "data:")
        return share(site_id, [(qid, text) for qid, text in questions if qid.startswith(prefixes) or qid in RELEASED])
    if plays("mail"):
        prefixes = ("condition_group:", "packaging_tariff:", "delivery_commitment:", "setup_pool:",
                    "project_bonus:", "index:", "fx:", "reserve:")
        return share(site_id, [(qid, text) for qid, text in questions
                               if qid in ("mailbox", "discount") or qid.startswith(prefixes) or qid in RELEASED])
    return questions


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


CREW_MIN_SECONDS = 90  # helpers start only while this much investigation time is left


async def open_site(tab: Tab) -> list[str]:
    """Before the agent's first turn, while the model still reads the brief: open the website from the run's access
    list and, if it shows a login form, log in with the run's credentials, as every agent's first turn did (two
    turns and ~5 s per agent in run a176359f). Pages after the login (cookie walls, access checks, menus) are left
    to the model."""
    notes = []
    try:
        notes.append(await asyncio.wait_for(tab.goto(tab.base_url + "/"), 25))
        if tab.credentials.get("password"):
            outcome = await asyncio.wait_for(tab.login(), 20)
            if not outcome.startswith("no password field"):
                notes.append("logged in with this run's credentials: " + outcome)
    except Exception as error:  # noqa: BLE001
        notes.append(f"opening the website did not finish ({str(error)[:120] or 'timed out'}); look at the page")
    return [f"(before your first turn) {note}" for note in notes]


async def on_login_page(tab: Tab) -> bool:
    try:
        return bool(await tab.page.main_frame.evaluate(
            "() => [...document.querySelectorAll('input[type=password]')].some(e => e.offsetParent !== null)"))
    except Exception:  # noqa: BLE001
        return True


async def crew(case: CaseFile, team: Team, tab: Tab, size: int, until: float, run) -> list:
    """Helper agents for the website of `tab`: once its own agent is under way (logged in, past its first turns) and
    the website has at least two open questions, `size - 1` more agents start in new tabs of the same browser session
    and the site's questions are shared out (`share`). Returns the helpers' outcomes."""
    site = tab.site_id
    while True:
        if team.finished.is_set() or until - time.time() < CREW_MIN_SECONDS:
            return []
        agent = AGENTS.get(site)
        if (agent is not None and agent.steps >= 2 and len(questions_for_site(site, case.questions())) >= 2
                and not await on_login_page(tab)):
            break
        await asyncio.sleep(1.0)
    helpers = []
    for number in range(2, size + 1):
        try:
            page = await asyncio.wait_for(tab.page.context.new_page(), 15)
        except Exception as error:  # noqa: BLE001
            print(f"[{site}] could not open a tab for a helper: {str(error)[:150]}", flush=True)
            break
        helper = Tab(page, {"siteId": site, "url": tab.base_url, "credentials": tab.credentials})
        helper.spares = tab.spares
        helpers.append((f"{site}{HELPER_MARK}{number}", helper))
    if not helpers or team.finished.is_set():
        return []
    CREW[site] = [site] + [name for name, _ in helpers]
    for key in [key for key, member in ASSIGNED.items() if base_site(member) == site]:
        ASSIGNED.pop(key)  # share the open questions out again, among all of them
    team.names.update(name for name, _ in helpers)
    print(f"[{site}] {len(helpers)} more agent(s) on this website: {', '.join(name for name, _ in helpers)}", flush=True)

    async def start(name: str, helper: Tab):
        try:
            note = await asyncio.wait_for(helper.goto(helper.base_url + "/"), 20)
        except Exception as error:  # noqa: BLE001
            note = f"opening the website did not finish ({str(error)[:120] or 'timed out'})"
        outcome = await run(helper, name, [f"(before your first turn) {note}"])
        print(f"[{name}] finished: {outcome if isinstance(outcome, Exception) else json.dumps(outcome, ensure_ascii=False)[:300]}",
              flush=True)
        return outcome

    return await asyncio.gather(*(start(name, helper) for name, helper in helpers), return_exceptions=True)


async def solve(task: dict, playwright) -> dict | None:
    started = time.time()
    sites = [site for site in task.get("access") or [] if isinstance(site, dict) and site.get("siteId") and site.get("url")]
    only = {name for name in os.environ.get("AGENT_SITES", "").split(",") if name}
    if not sites or not task.get("brief"):
        return None
    brief = task["brief"]
    listing = "\n".join(f"- {site['siteId']}: {site['url']}" for site in sites)
    # The model reads the brief while the browsers open the websites and log in (open_site).
    context_call = asyncio.ensure_future(read_context(brief + f"\n\nThe websites of this run:\n{listing}"))
    opened, tabs = await open_tabs(playwright, sites)
    try:
        openings = {name: asyncio.ensure_future(open_site(tab)) for name, tab in tabs.items()}
        context = await context_call
        SITE_ROLES.clear()
        RELEASED.clear()
        CREW.clear()
        ASSIGNED.clear()
        AGENTS.clear()
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

        working = [tab for name, tab in tabs.items() if not only or name in only]
        team = Team(case, [tab.site_id for tab in working], investigate_until)

        async def bounded(tab, name=None, opening=None):  # an agent stuck past the deadline is stopped so that the others' facts still count
            name = name or tab.site_id
            if opening is None and name in openings:
                opening = await openings[name]
            restarted = False
            while True:
                limit = max(10.0, investigate_until - time.time() + 20)
                agent = investigator(case, team, tab, investigate_until, name)
                agent.opening, opening = list(opening or []), None
                try:
                    outcome = await asyncio.wait_for(agent.run(), limit)
                except asyncio.TimeoutError:
                    team.finished.set()
                    return RuntimeError(f"stopped after {limit:.0f} s: still busy at the investigation deadline")
                # Weak sandbox runs (10, 22.8, 27.8, 29.1) had agents end with done: false while time was left. One
                # fresh start, with a clean conversation in a new tab, instead of giving the site up (sanity.py).
                if (sanity.enabled("agent_restart") and not restarted and not outcome.get("done")
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
        crews = [crew(case, team, tab, CREW_SIZE.get(site_role(tab.site_id) or "", 1), investigate_until, bounded)
                 for tab in working if CREW_SIZE.get(site_role(tab.site_id) or "", 1) > 1]
        outcomes = await asyncio.gather(*(bounded(tab) for tab in working), *crews, return_exceptions=True)
        for tab, outcome in zip(working, outcomes):
            print(f"[{tab.site_id}] finished: {outcome if isinstance(outcome, Exception) else json.dumps(outcome, ensure_ascii=False)[:300]}", flush=True)
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
