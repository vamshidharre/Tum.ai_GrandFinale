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

from urllib.parse import parse_qs, urlparse

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
# News about an agent's own filed document that needs it back at work (casefile._extract's wording): a failed or
# empty extraction, a conflict with an earlier document, values left out. Other news waits for its next turn.
NEEDS_ATTENTION = re.compile(r"fail|no usable|conflict|again|left out|not found|could not|invalid", re.I)

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
                        # Fix #2: a routine "your document was read" notice does not wake an idle agent for another
                        # model turn (run 99f03f3f: the tender agent was woken 6 times only to say done again); new
                        # questions and notices that report a problem with its documents still do.
                        if fresh or any(NEEDS_ATTENTION.search(item) for item in self.case.news.get(name) or []):
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
# #1: the ERP agent worked every record by hand, one click per model turn: 46 turns in run a176359f, 92 in 20e3c020
# (search, open, a tab, file; then each price view; then each stock breakdown). file_each lets code open and file
# the records the model names, with the tabs and paths it learned on the first one; it was never used.
RECORD_BATCH = ("SAVE TURNS: {count} record questions are open on your website. Work ONE candidate record by hand, "
                "ALL the way: its status and technical views, its stock down to the freely available quantity, and its "
                "price conditions with their quantity scales; file each of its pages on the way (keep first, then the "
                "click that leaves the page, in the SAME reply), so this record needs no second filing; note every control "
                "you clicked from the record page to reach each page you filed. From then on "
                "file every further candidate of a result list with ONE file_each that repeats ALL of them, not only "
                "the plain tabs: a tab is {{\"tab\": \"<label>\", \"kind\": \"material\"}}, and a chain of clicks "
                "(for example down into a stock breakdown to the free stock, or into a price-scale view) is "
                "{{\"path\": [\"<label 1>\", \"<label 2>\", \"<label 3>\"], \"kind\": \"material\" or "
                "\"price_conditions\"}}, using the labels exactly as the record pages show them. Code opens every "
                "record, follows every tab and path, files each page and comes back to the list; do not drill into "
                "stock or prices by hand again. Filed pages are read within seconds: wait for their facts in the "
                "case file instead of opening them again.")
# Run 2e081b8d: the mail agent needed 50 turns (607k input tokens, 41% of the run) and finished last, searching one
# topic at a time and opening each message by hand (search, unlock the list, open, unlock the text, file), while
# in run 665adb0e one broad search per folder and file_each over its list filed several records per turn.
MAIL_BATCH = ("SAVE TURNS: {count} questions are open on your mailbox. Search a folder (inbox, then archive) for the "
              "tender reference so that the list shows several of its records at once, unlock the list once, then "
              "file every record of that list you need with ONE file_each (their links, plus the tab or control that "
              "shows a message's full original text as a path, kind \"mail\"). Code opens and files them and comes "
              "back to the list; do not open them one by one. Search a topic on its own only for what is still open.")
# Run ddfa36f5 (round 7, 5:10): after walking one record down to its price scales, the ERP agent walked a SECOND
# record fully by hand "to learn the path structure" (11 turns, ~35 s on the critical path), then filed it again with
# file_each. Once a price page has been filed by hand, the briefing says the layout is known.
RECORD_LEARNED = ("YOU KNOW THE RECORD LAYOUT: you have walked a record by hand down to its price scales. Do not walk "
                  "another record by hand, not even to check it: search the remaining candidates as you did before and "
                  "file ALL candidates of each result list with ONE file_each, repeating the tabs and click paths you used (labels exactly as "
                  "you clicked them). The filed pages report each record's status, data, stock and prices to the case "
                  "file. A page of the record you walked that is not filed yet goes into such a file_each too.")
# Run ddfa36f5: woken by the record questions, the ERP agent first needed 4 turns (11 s) to find the material search
# (a menu link that did not navigate). Before any record question exists it has nothing else to do.
ERP_PREPARE = ("WHILE YOU WAIT: no record question is open yet because the requirements are still being read. Open "
               "your website's material search page now, so that you can search the moment the questions arrive, then "
               "finish with done. File nothing and do not search yet.")
BATCH_NUDGE_AFTER = 3  # same-kind pages filed one by one before file_each is suggested once
FILED_READ_WAIT = 3  # seconds file_each waits for the pages it filed to be read (10 s cost up to 40 s of ERP time when
# the model was slow, run ab00e578; the result now also says not to open them again)


def batch_hint(counter: dict, kind: str, filed: bool) -> str:
    """Suggest file_each once an agent has filed the same kind of page one by one BATCH_NUDGE_AFTER times. `counter`
    maps kind -> consecutive filings (mutated). Fires once per streak; the model still decides."""
    if not filed or not kind:
        return ""
    counter[kind] = counter.get(kind, 0) + 1
    for other in [key for key in counter if key != kind]:
        counter.pop(other, None)
    if counter[kind] == BATCH_NUDGE_AFTER:
        return ("; you have now filed several pages of this kind one by one: if the list they came from still shows "
                "more records of the same kind, go back to that list and file the rest together with one file_each "
                "(naming the records and the tabs or paths that hold the facts)")
    return ""


# Run ab00e578: three of four ERP file_each actions filed no stock or price page, so the model clicked them all again
# by hand (~8 turns per material, ERP 53 turns, investigation 302 s). Two causes, both handled in code here:
# 1. a label shown twice on a record page (once in the menu, once for the record): pick_control takes the one whose
#    address belongs to the record, or either one when both lead to the same address;
# 2. a notice page or dialog put in front of the record ("maintenance note ... accept and continue"): pass_notice
#    presses its single way forward, as the model does in its own turns, but only on a page without input fields.
NOTICE_PAGE = re.compile(r"hinweis|wartung|maintenance|notice|meldung|achtung|attention|information|bestätig|confirm",
                         re.I)
NOTICE_GO = re.compile(r"akzeptier|fortfahren|weiter|continue|accept|proceed|verstanden|got it|schließen|close|"
                       r"\bok\b|bestätigen|confirm", re.I)


def pick_control(page: str, label: str, kinds: tuple[str, ...], url: str) -> tuple[str | None, str]:
    """find_control, and when the label is ambiguous: the controls if they all lead to the same address, else the
    one control whose address carries a value of the current page's address (the record's own link)."""
    aid, why = find_control(page, label, kinds)
    if aid is not None or "controls are labelled" not in why:
        return aid, why
    listed = re.search(r"\(((?:\[\d+\](?:, )?)+)\): use their ids$", why)
    ids = re.findall(r"\[(\d+)\]", listed.group(1)) if listed else []
    targets = {m.group(1): (re.search(r"->\s*(\S+)", m.group(4)) or [None, None])[1]
               for m in CONTROL.finditer(page) if m.group(1) in ids}
    addresses = set(targets.values())
    if len(addresses) == 1 and None not in addresses:
        return ids[0], why
    query = urlparse(url)
    tokens = [value for values in parse_qs(query.query).values() for value in values if len(value) >= 3]
    tokens += [part for part in query.path.split("/") if len(part) >= 4]
    scoped = [aid for aid, target in targets.items() if target and any(token in target for token in tokens)]
    if len(scoped) == 1:
        return scoped[0], why
    return None, why


async def pass_notice(tab, page: str) -> str | None:
    """A notice in front of the page (a dialog, or a short page that announces something) with a single way forward
    and no input field: press it and say what was pressed; None leaves the page as it is."""
    modal = "!! A MODAL DIALOG" in page
    area = page.split("!! END OF MODAL DIALOG")[0] if modal else page
    # Without a dialog, only a short page whose title or heading announces a notice counts (a record page that
    # merely mentions a "Hinweis" somewhere and has a "Weiter" link must not be left).
    head = "\n".join(line for line in page.splitlines()[:8] if line.startswith(("title:", "#")))
    if not modal and (len(page) > 2500 or not NOTICE_PAGE.search(head)):
        return None
    controls = [(m.group(1), m.group(2), m.group(3)) for m in CONTROL.finditer(area)]
    if any(kind.split("(")[0] in ("input", "select", "checkbox", "radio", "switch") for _, kind, _ in controls):
        return None  # a check to pass or a form to fill is the model's
    forward = list(dict.fromkeys(aid for aid, kind, label in controls
                                 if kind in ("button", "link", "clickable") and NOTICE_GO.search(label)))
    if len(forward) != 1:
        return None
    label = next(label for aid, _, label in controls if aid == forward[0])
    await tab.click(forward[0])
    return label


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
        passed = await pass_notice(tab, record)
        if passed:  # a notice came first: the record is behind it
            opened += f'; passed a notice ("{passed}")'
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
                kinds = ("link", "button", "clickable")
                now = await tab.observe()
                control, why = pick_control(now, step, kinds, tab.page.url)
                if control is None and await pass_notice(tab, now):  # a notice in the way: past it, look again
                    now = await tab.observe()
                    control, why = pick_control(now, step, kinds, tab.page.url)
                if control is None and number == 0 and tab.page.url != record_url:
                    await tab.goto(record_url)
                    now = await tab.observe()
                    control, why = pick_control(now, step, kinds, tab.page.url)
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


def investigator(case: CaseFile, team: Team, tab: Tab, deadline: float) -> Agent:
    name = tab.site_id
    system = INVESTIGATOR.format(site=name, url=tab.base_url, brief=case.brief, actions=ACTIONS_HELP)

    def briefing() -> str:
        questions = questions_for_site(name, case.questions())
        news = case.take_news(name)
        text = f"CASE FILE\n{case.status()}\n\nOPEN QUESTIONS\n"
        text += "\n".join(f"- [{qid}] {question}" for qid, question in questions) or "(none)"
        if site_role(name) == "mail" and len(questions) >= 3:
            text += "\n\n" + MAIL_BATCH.format(count=len(questions))
        records = [qid for qid, _ in questions if qid.startswith(("material:", "data:"))]
        if site_role(name) == "erp" and records and learned[0]:
            text += "\n\n" + RECORD_LEARNED
        elif site_role(name) == "erp" and len(records) >= 2:
            text += "\n\n" + RECORD_BATCH.format(count=len(records))
        elif site_role(name) == "erp" and not questions and not worked[0]:
            text += "\n\n" + ERP_PREPARE
        if news:
            text += "\n\nNEWS FROM YOUR FILED DOCUMENTS\n" + "\n".join(f"- {item}" for item in news)
        own_pending = case.pending_for(name)
        if own_pending:
            text += f"\n\n({own_pending} filed document(s) are still being read.)"
        elif case.pending:
            text += f"\n\n({case.pending} document(s) filed by other agents are still being read.)"
        return text

    agent: Agent
    batched: dict = {}  # kind -> consecutive one-by-one filings, for the file_each nudge (#1)
    worked = [False]  # has filed something: from then on it may go idle by code once its questions are answered
    learned = [False]  # has filed a price page reached through a record: the record layout is known (RECORD_LEARNED)

    def finished_here() -> bool:
        """A routed (ERP or mail) agent that has worked and whose website has no open question left and nothing of
        its own still being read: its work is done (its questions are exactly its website's, so nothing is missed)."""
        return (site_role(name) is not None and worked[0] and not case.pending_for(name)
                and not questions_for_site(name, case.questions()))

    async def keep(action: dict) -> str:
        kind = str(action.get("kind", ""))
        document, source = agent.document()
        res = case.file(kind, document, source, name)
        worked[0] = worked[0] or res.startswith("filed")
        if res.startswith("this document is already filed"):
            res += "; do not file this same page again — check OPEN QUESTIONS for what is missing on another page, or finish with done"
        else:
            res += batch_hint(batched, kind, res.startswith("filed"))
        if kind == "price_conditions" and res.startswith("filed") and not learned[0]:
            learned[0] = True
            res += ("; you now know the record layout: file every further candidate with file_each from its result "
                    "list, repeating your tabs and click paths, and do not walk another record by hand")
        # Fix #2: the bidder Q&A filed as a list that `collect` followed to its own end (over more than one page) is
        # the whole Q&A, so its question closes by itself once it is read (casefile.CaseFile.listed_in_full)
        # instead of keeping the agent browsing until it closes it (run a176359f: 41 s of re-opened documents).
        run = getattr(tab, "last_collect", None)
        if (kind == "clarifications" and res.startswith("filed") and agent.held_by == "collect" and run
                and not run.get("capped") and run.get("pages", 0) >= 2):
            case.listed_in_full["clarifications"] = name
            res += "; this is the Q&A list followed to its end, so the clarifications question closes once it is read"
        return res

    async def file_each_action(action: dict) -> str:
        said = await file_each(tab, action, lambda kind, text, source: case.file(kind, text, source, name),
                               agent.coverage.saw, deadline, agent.page_seen)
        worked[0] = worked[0] or "filed" in said
        if "filed" in said and any(isinstance(tab, dict) and tab.get("kind") == "price_conditions"
                                   for tab in action.get("tabs") or []):
            learned[0] = True
        # The pages it filed are read in a few seconds. Round-3 run: the ERP agent took a status still being read
        # for "did not parse" and opened the same pages again (3 extra turns); its next turn now sees the facts.
        until = min(deadline, time.time() + FILED_READ_WAIT)
        while case.pending_for(name) and time.time() < until:
            await case.wait_change(case.version, min(1.0, max(0.0, until - time.time())))
        return said + ("\nThe filed pages have been read: their facts are in the case file above."
                       if not case.pending_for(name) else
                       "\nSome filed pages are still being read; their facts follow as news. Do not open them again.")

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
                  finished_here=finished_here,
                  max_steps=int(os.environ.get("AGENT_MAX_STEPS", "150")), idle=team.hook(name),
                  think=os.environ.get("AGENT_THINK") == "1",
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


def site_role(site_id: str) -> str | None:
    """"erp" or "mail" for the agent whose questions are routed, None for one that sees every question."""
    site = site_id.casefold()
    for role in ("erp", "mail"):
        named = (SITE_ROLES.get(role) or "").casefold()
        if (site == named) if named else (role in site):
            return role
    return None


def questions_for_site(site_id: str, questions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep specialized mail and ERP agents on questions their portal can resolve (from Vamshidhar's v1 package,
    sandbox run 44a626ef: 100 in 9.6 min, every agent finished by itself)."""
    site = site_id.casefold()

    def plays(role: str) -> bool:
        named = (SITE_ROLES.get(role) or "").casefold()
        return site == named if named else role in site

    if plays("erp"):
        return [(qid, text) for qid, text in questions if _erp_question(qid) or qid in RELEASED]
    if plays("mail"):
        return [(qid, text) for qid, text in questions if _mail_question(qid) or qid in RELEASED]
    # Every other agent leaves the ERP's and the mailbox's questions to them while this run has such an agent, until
    # one is left open and RELEASED to all. Round-3 run: the supplier-portal agent searched its own documents for a
    # mail record for ~25 turns, and the tender agent was woken six times by new ERP questions only to say done.
    return [(qid, text) for qid, text in questions
            if qid in RELEASED or not ((SITE_ROLES.get("erp") and _erp_question(qid))
                                       or (SITE_ROLES.get("mail") and _mail_question(qid)))]


def _erp_question(qid: str) -> bool:
    return qid.startswith(("material:", "data:"))


def _mail_question(qid: str) -> bool:
    return qid in ("mailbox", "discount") or qid.startswith(
        ("condition_group:", "packaging_tariff:", "delivery_commitment:", "setup_pool:", "project_bonus:", "index:",
         "fx:", "reserve:"))


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


async def open_site(tab: Tab) -> list[str]:
    """#3: open the website from the run's access list, log in with the run's credentials and choose the cookie
    notice's "necessary only", as every agent's first two turns did by hand (run a176359f: 2 turns, ~5-8 s per
    agent), while the model still reads the brief. Anything else (access checks, menus, searches) is the model's.
    Bounded; on any trouble the agent simply starts from the page as it is."""
    notes: list[str] = []
    try:
        notes.append(await asyncio.wait_for(tab.goto(tab.base_url + "/"), 20))
        for _ in range(3):  # a login and a cookie notice, in whichever order the site shows them
            if tab.credentials.get("password") and await tab.password_field_shown(0):
                said = await asyncio.wait_for(tab.login(), 25)
                notes.append(said)
                if "still shown" in said:
                    break  # a refused login is the model's to read: no second try by code
                continue
            chosen = await asyncio.wait_for(tab.choose_necessary_cookies(), 15)
            if not chosen:
                break
            notes.append(chosen)
    except Exception as error:  # noqa: BLE001
        notes.append(f"opening the website did not finish ({str(error)[:120] or 'timed out'}); look at the page")
    return [f"(before your first turn, by code) {note}" for note in notes]


async def solve(task: dict, playwright) -> dict | None:
    started = time.time()
    sites = [site for site in task.get("access") or [] if isinstance(site, dict) and site.get("siteId") and site.get("url")]
    only = {name for name in os.environ.get("AGENT_SITES", "").split(",") if name}
    if not sites or not task.get("brief"):
        return None
    brief = task["brief"]
    listing = "\n".join(f"- {site['siteId']}: {site['url']}" for site in sites)
    # #3: the browsers open every website, log in and choose the cookie notice while the model reads the brief.
    context_call = asyncio.ensure_future(read_context(brief + f"\n\nThe websites of this run:\n{listing}"))
    opened, tabs = await open_tabs(playwright, sites)
    openings = {name: asyncio.ensure_future(open_site(tab)) for name, tab in tabs.items()}
    try:
        context = await context_call
    except BaseException:
        for browser in opened:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        raise
    SITE_ROLES.clear()
    RELEASED.clear()
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
        team = Team(case, [tab.site_id for tab in working], investigate_until)
        async def bounded(tab):  # an agent stuck past the deadline is stopped so that the others' facts still count
            restarted = False
            try:  # what code already did on the site (open_site); the agent starts from there. A late opening is
                # cancelled rather than left running beside the agent on the same tab.
                opening = await asyncio.wait_for(openings[tab.site_id], 45)
            except Exception:  # noqa: BLE001
                opening = []
            while True:
                limit = max(10.0, investigate_until - time.time() + 20)
                agent = investigator(case, team, tab, investigate_until)
                agent.opening, opening = list(opening), []
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
                    print(f"[{tab.site_id}] stopped unfinished after {outcome.get('steps')} steps; restarting once "
                          "in a fresh tab", flush=True)
                    try:
                        await asyncio.wait_for(tab.renew(), 45)
                    except Exception as error:  # noqa: BLE001
                        print(f"[{tab.site_id}] could not renew the tab: {str(error)[:150]}", flush=True)
                    continue
                return outcome
        outcomes = await asyncio.gather(*(bounded(tab) for tab in working), return_exceptions=True)
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
