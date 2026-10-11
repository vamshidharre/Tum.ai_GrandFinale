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
from urllib.parse import unquote_plus, urljoin

from playwright.async_api import async_playwright

import browsers
import llm
import run as contract
from browser_env import Tab
from calculator import calculate_bid
import sanity
import validate
from casefile import SHAPES, CaseFile, brief_intro, brief_rules, read_context, status_sections
from gates import Gates
from worker import ACTIONS_HELP, ACTIONS_HELP_RESEARCH, CONTROL, Agent, brief_for, find_control

RESTART_MIN_SECONDS = 120  # an unfinished agent is restarted only while this much investigation time is left

INVESTIGATOR = """You are one of several research agents preparing a tender bid for a manufacturer's sales
engineering team. Each agent works a different website at the same time; you work the website "{site}" at {url}
in your own browser tab. Together you must find every fact the bid rules need. You only find and file evidence:
you never calculate prices.

{brief}

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
      Files several records of the list in front of you in ONE turn (up to 12 links by id): each record is opened
      and filed as "kind"; "tabs" also opens each named tab of every record, or follows a path of controls clicked
      in order ({{"path": ["<label 1>", "<label 2>"], "kind": "<kind>"}}, labels or a part of them as the record
      shows them), and files the page there with its own kind; "keep_record": false skips filing the record's first
      page. You come back to the list with a report. A record walked by hand costs about ten turns; walk only the
      FIRST one by hand to learn where its facts are, then file the others with file_each. Example: from a list you
      opened [12] "Item Q-71", kept its "Specs" tab as material, clicked "Stock" > "Site North" > "Bin 4" and kept
      that page as material, clicked "Prices" > "Scales" and kept that page as price_conditions. Then, for [15] and
      [18] of a list, one turn: {{"do": "file_each", "links": [15, 18], "kind": "material", "keep_record": false,
      "tabs": [{{"tab": "Specs", "kind": "material"}}, {{"path": ["Stock", "Site North", "Bin 4"], "kind": "material"}},
      {{"path": ["Prices", "Scales"], "kind": "price_conditions"}}]}}. Once you have walked a record, your briefing
      shows this action ready, with the tabs and paths you used.
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
  candidate the question names, with file_each once you have walked one record and know which of its tabs and
  paths hold the facts (also for a list with a single record you need), else with keep. The case file matches them
  against the requirement values, which can still change when amendments or reserves are filed.
- Navigation you can already name (a menu, then a tab, then a link) takes ONE turn with path, not a turn per click.
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


def _bullet(text: str, start: str) -> str:
    """The bullet of `text` that begins with `start`, with its indented continuation lines."""
    begin = text.find("- " + start)
    if begin < 0:
        return ""
    end = re.search(r"\n(?! )", text[begin:])
    return text[begin:begin + end.start()] if end else text[begin:]


# The trap guidance is shown from gate 1 on (gates.py), not in every call; the escalated turn has the full prompt.
TRAP_GUIDANCE = _bullet(INVESTIGATOR, "The websites hold traps")
INVESTIGATOR_LEAN = INVESTIGATOR.replace(TRAP_GUIDANCE + "\n", "") if TRAP_GUIDANCE else INVESTIGATOR
FULL_BRIEF = "TASK BRIEF (the rules are authoritative)\n{brief}\nEND OF BRIEF"
INVESTIGATOR_DROP = ("submission", "abgabe", "einreichung")  # sections of the brief no investigator needs
# Which case-file sections a website's role concerns (the role comes from the context, SITE_ROLES), shown from gate 1
# on next to the sections of the agent's own questions.
ROLE_SECTIONS = {"erp": {"requirements", "materials"}, "mail": {"authorities"}}
# A news item that says an extraction brought nothing usable: trouble (gates.py).
BAD_NEWS = re.compile(r"(?i)extraction failed|no usable records|nothing usable|schema validation rejected")

# A soft budget for the run's input tokens (llm.STATS["prompt_tokens"]). From BUDGET_NEAR of it on, investigators are
# told to file what they have and finish; from all of it on, an investigator whose documents are not being read
# finishes before its next model call. The bid clerk and the courier are exempt: a signed bid must still happen.
# TOKEN_BUDGET=0 turns it off.
BUDGET_NEAR = 0.9
BUDGET_LINE = "TOKEN BUDGET NEARLY USED: file what you have, answer your questions, finish with done (no new searches)."
BUDGET_LOGGED: set[str] = set()  # "near" once logged


def token_budget() -> int:
    try:
        return int(os.environ.get("TOKEN_BUDGET", "1100000"))
    except ValueError:
        return 1_100_000


def budget_used() -> float:
    """The share of the run's input-token budget used so far (0 without a budget)."""
    budget = token_budget()
    return llm.STATS.get("prompt_tokens", 0) / budget if budget > 0 else 0.0


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


# Which kinds of evidence answer which open question (by the part of its id before ":"): an agent that filed one of
# these kinds works on that question, and is not let go while it is open (before_done in `investigator`).
QUESTION_KINDS = {"positions": ("positions",), "revision": ("revisions",), "requirements": ("requirements",),
                  "clarifications": ("clarifications",), "material": ("material",),
                  "data": ("material", "price_conditions")}
DONE_REFUSALS = 2  # refused dones per question and agent
DONE_GATE_SECONDS = 90  # investigation time that must be left for a done to be refused

FILE_EACH_LIMIT = 12  # records per file_each action
FILE_EACH_SECONDS = 150  # and seconds for all of them; the agent continues from the list afterwards


async def file_each(tab, action: dict, file, saw, deadline: float, seen: str = "") -> str:
    """The file_each action: the model names several records on the page in front of it (links by id) and, if each
    record has tabs that matter, the tabs by label; code opens every record and tab and files it with the kind the
    model gave, then goes back to the list. The model chooses the records and kinds, as with keep; this only saves
    one model turn per page opened (the ERP agent needed 58-85 turns per run, each resending its whole prompt)."""
    kind = str(action.get("kind") or "")
    tabs = [t for t in (action.get("tabs") or []) if isinstance(t, dict) and (t.get("tab") or t.get("path"))]
    # "keep_record": false leaves the record's first page unfiled when only its tabs hold facts (one extraction less
    # per record); without tabs the record page is filed all the same, or nothing would be.
    keep_record = not tabs or str(action.get("keep_record", True)).strip().lower() not in ("false", "0", "no")
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
        filed = [f"{kind}: {file(kind, tab.document, tab.document_source)[:120]}"] if keep_record else []
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


ID_PART = re.compile(r"[^\s·•|,;:()\[\]/<>\"']+")


def id_tokens(label: str) -> list[str]:
    """The parts of a control's label that look like a record's number: three characters or more, with a digit."""
    return [part.strip(".-_") for part in ID_PART.findall(label)
            if len(part.strip(".-_")) >= 3 and any(ch.isdigit() for ch in part)]


def id_shape(token: str) -> str:
    """A number's pattern (digits as 0, letters as A): records of one list share it, menu codes rarely do."""
    return re.sub(r"[^\W\d_]", "A", re.sub(r"\d", "0", token))


def _address(url: str) -> str:
    return unquote_plus(url or "").casefold()


def _url_of(page: str) -> str:
    return page.split("\n", 1)[0].removeprefix("url: ").strip() if page.startswith("url: ") else ""


class WalkRecorder:
    """Learns how the model walked one record by hand, so that the briefing can offer the same walk for the other
    records of a list as one file_each action. Code only replays what the model did; which records to file stays the
    model's choice (it names the links). Sandbox runs 4876a0ad and others: the ERP agent spent ~12 turns per material
    (record, technical tab, keep, planning tab, keep, stock overview, plant, storage location, keep, prices, scales,
    keep) and never used file_each.

    A walk starts when a link whose label carries a number opens a page whose address carries that number (a record
    opened from a list). Every click after it is noted by its control's label; a keep closes a segment: the clicks
    since the last keep become a path filed with the keep's kind. A form entry, a collected document or an address
    typed by hand cannot be replayed and ends the recording (what was learned stays)."""

    def __init__(self):
        self.walk: dict | None = None
        self.learned: dict | None = None  # {"label", "shape", "kind", "keep_record", "tabs", "score"}

    # events -----------------------------------------------------------------------------------------------------
    def browsed(self, action: dict, page_before: str, url_after: str) -> None:
        do = action.get("do")
        url_before = _url_of(page_before)
        if do == "click":
            aid = str(action.get("id", "")).strip("[] ")
            label = next((m.group(3) for m in CONTROL.finditer(page_before) if m.group(1) == aid), None)
            kind = next((m.group(2) for m in CONTROL.finditer(page_before) if m.group(1) == aid), "")
            self._clicked(label, kind, page_before, url_before, url_after)
        elif do == "goto":
            # an address the model took from a link on the page counts as a click on that link
            target = urljoin(url_before, str(action.get("url", ""))).rstrip("/")
            link = next((m for m in CONTROL.finditer(page_before)
                         if (href := re.search(r"->\s*(\S+)", m.group(4)))
                         and urljoin(url_before, href.group(1)).rstrip("/") == target), None)
            if link is not None:
                self._clicked(link.group(3), link.group(2), page_before, url_before, url_after)
            elif self.walk:
                self.walk["over"] = True
        elif do == "back":
            if self.walk and self.walk["pending"]:
                self.walk["pending"].pop()
                if not self.walk["pending"]:
                    self.walk["start"], self.walk["unusable"] = "", False
        elif do in ("fill", "select", "set", "collect", "fetch", "steps") and self.walk:
            self.walk["over"] = True

    def _clicked(self, label: str | None, kind: str, page_before: str, url_before: str, url_after: str) -> None:
        walk = self.walk
        tokens = [t for t in id_tokens(label or "") if t.casefold() in _address(url_after)
                  and t.casefold() not in _address(url_before)] if kind in ("link", "clickable") else []
        # inside a record already being walked (its number in the address), a numbered control is part of the walk
        inside = bool(walk and walk["keeps"] and not walk["over"]
                      and any(t.casefold() in _address(url_before) for t in walk["tokens"]))
        if tokens and not inside and url_after != url_before:
            self.walk = {"label": label, "tokens": tokens, "shape": id_shape(tokens[0]), "pending": [], "start": "",
                         "unusable": False, "segments": [], "main": None, "keeps": 0, "over": False}
            return
        if not walk or walk["over"]:
            return
        if label is None:
            walk["unusable"] = True
            return
        step = self._generic(label)
        if step is None:
            walk["unusable"] = True
            return
        if not walk["pending"]:
            walk["start"] = page_before
        walk["pending"].append(step)

    def _generic(self, label: str) -> str | None:
        """A label without the walked record's own number (that differs from record to record), or None when
        nothing generic is left to find the control by."""
        text = label
        for token in self.walk["tokens"]:
            text = re.sub(re.escape(token), " ", text, flags=re.IGNORECASE)
        if text == label:
            return label.strip() or None
        text = " ".join(text.split()).strip(" ·•|-:,;")
        return text if sum(ch.isalpha() for ch in text) >= 3 else None

    def kept(self, kind: str, held: bool) -> None:
        walk = self.walk
        if not walk or walk["over"]:
            return
        if held:  # a collected or fetched document: no path of clicks leads to it
            walk["over"] = True
            return
        walk["keeps"] += 1
        if not walk["pending"] and not walk["segments"] and walk["main"] is None and not walk["unusable"]:
            walk["main"] = kind
        elif walk["pending"] and not walk["unusable"]:
            path = self._shortest(walk["pending"], walk["start"])
            segment = {"tab": path[0], "kind": kind} if len(path) == 1 else {"path": path, "kind": kind}
            if len(path) <= 6 and segment not in walk["segments"]:
                walk["segments"].append(segment)
        walk["pending"], walk["start"], walk["unusable"] = [], "", False
        score = len(walk["segments"]) + (walk["main"] is not None)
        if score and (self.learned is None or score >= self.learned["score"] or self.learned["label"] == walk["label"]):
            self.learned = {"label": walk["label"], "shape": walk["shape"], "tokens": walk["tokens"],
                            "kind": walk["main"] or walk["segments"][0]["kind"], "keep_record": walk["main"] is not None,
                            "tabs": list(walk["segments"]), "score": score}

    @staticmethod
    def _shortest(path: list[str], start: str) -> list[str]:
        """The path from its last step that was already on the page where it began: a detour through a tab that
        held nothing (opened, then the next tab clicked from there) is not repeated for every record."""
        for index in range(len(path) - 1, 0, -1):
            if find_control(start, path[index], ("link", "button", "clickable"))[0] is not None:
                return path[index:]
        return path

    # the offer --------------------------------------------------------------------------------------------------
    def tip(self, page: str) -> str:
        """The learned walk as a ready file_each action, when the page in front of the agent lists records like the
        one it walked (links whose numbers have the same pattern) and is not that record itself."""
        learned = self.learned
        if not learned or not page:
            return ""
        address = _address(_url_of(page))
        if any(t.casefold() in address for t in learned["tokens"]):
            return ""
        records = [m for m in CONTROL.finditer(page) if m.group(2) in ("link", "clickable")
                   and any(id_shape(t) == learned["shape"] and t.casefold() not in address for t in id_tokens(m.group(3)))]
        if not records:
            return ""
        action = {"do": "file_each", "links": ["<ids>"], "kind": learned["kind"]}
        if learned["tabs"]:
            if not learned["keep_record"]:
                action["keep_record"] = False
            action["tabs"] = learned["tabs"]
        spec = json.dumps(action, ensure_ascii=False).replace('["<ids>"]', "[<ids of the records you need>]")
        return (f'TIP: you walked the record "{learned["label"]}" by hand. File the other records you need from this '
                f"list in ONE turn, with the same tabs and paths:\n{spec}")


def investigator(case: CaseFile, team: Team, tab: Tab, deadline: float) -> Agent:
    name = tab.site_id
    gates = Gates()
    full_system = INVESTIGATOR.format(site=name, url=tab.base_url, actions=ACTIONS_HELP_RESEARCH,
                                      brief=FULL_BRIEF.format(brief=brief_for(case.brief, drop_sections=INVESTIGATOR_DROP)))
    # The task card instead of the whole brief (gates on): what the model read from the brief at the start, and the
    # brief's own task paragraph. Without a card (the context has no tender reference) the whole brief, as before.
    card = ""
    if isinstance(case, CaseFile):
        card = case.task_card(brief_intro(brief_for(case.brief)), {role: site for role, site in SITE_ROLES.items() if site},
                              mail=site_role(name) == "mail")
    rules_brief = brief_for(case.brief, drop_sections=INVESTIGATOR_DROP + ("access", "zugang"))
    system = full_system if gates.full() else INVESTIGATOR_LEAN.format(
        site=name, url=tab.base_url, actions=ACTIONS_HELP_RESEARCH,
        brief=card or FULL_BRIEF.format(brief=brief_for(case.brief, drop_sections=INVESTIGATOR_DROP)))

    def case_view(sections: set[str] | None) -> str:
        return case.status(sections) if isinstance(case, CaseFile) else case.status()

    def briefing(full: bool = False) -> str:
        questions = questions_for_site(name, case.questions())
        news = case.take_news(name)
        if any(BAD_NEWS.search(item) for item in news):
            gates.trouble("an extraction found nothing")
        gate = 2 if full or gates.full() else gates.gate
        ids = [qid for qid, _ in questions]
        sections = (None if gate >= 2 else set() if gate == 0
                    else status_sections(ids, filed_kinds) | ROLE_SECTIONS.get(site_role(name) or "", set()))
        text = f"CASE FILE\n{case_view(sections)}\n\nOPEN QUESTIONS\n"
        # The mailbox agent sees every record it still needs at once, with the brief's subject word and sender,
        # so that it can plan its searches (sandbox runs 9ac58b69, 472732d2, 63f3be2e: 25-46 turns of guessing).
        checklist = case.mail_checklist(questions) if site_role(name) == "mail" else ""
        if checklist:
            text += checklist + "\n\n"
        text += "\n".join(f"- [{qid}] {question}" for qid, question in questions) or "(none)"
        if news:
            text += "\n\nNEWS FROM YOUR FILED DOCUMENTS\n" + "\n".join(f"- {item}" for item in news)
        own_pending = case.pending_for(name)
        if own_pending:
            text += f"\n\n({own_pending} filed document(s) are still being read.)"
        elif case.pending:
            text += f"\n\n({case.pending} document(s) filed by other agents are still being read.)"
        tip = walks.tip(agent.page_seen) if questions else ""
        if tip:
            text += "\n\n" + tip
        if gate == 1:  # trouble: what helps to judge a record, and the brief's rules on these questions
            if TRAP_GUIDANCE:
                text += "\n\nWHEN YOU CHECK A RECORD\n" + TRAP_GUIDANCE[2:]
            rules = brief_rules(rules_brief, ids) if ids else ""
            if rules:
                text += "\n\nTHE BRIEF'S RULES ON YOUR QUESTIONS\n" + rules
        if budget_used() >= BUDGET_NEAR:
            text += "\n\n" + BUDGET_LINE
            if "near" not in BUDGET_LOGGED:
                BUDGET_LOGGED.add("near")
                print(f"[{name}] token budget: {llm.STATS.get('prompt_tokens', 0)} of {token_budget()} input tokens "
                      "used; investigators are told to finish", flush=True)
        return text

    agent: Agent
    walks = WALKS.setdefault(name, WalkRecorder())  # what was learned outlives a restart; the walk in progress not
    walks.walk = None

    filed_kinds: set[str] = set()  # kinds of evidence this agent filed: the questions it works on (before_done)
    refused: dict[str, int] = {}  # question -> times a done was refused over it

    def filing(kind: str, text: str, source: str) -> str:
        res = case.file(kind, text, source, name)
        if res.startswith("filed"):
            filed_kinds.add(kind)
        return res

    async def keep(action: dict) -> str:
        document, source = agent.document()
        kind = str(action.get("kind", ""))
        pages = [] if agent.held else agent.trail_pages()
        paged = len(pages)
        res = filing(kind, document, source)
        if not res.startswith("filed"):
            gates.trouble("a keep was refused")
        if kind in SHAPES:
            walks.kept(kind, agent.held is not None)
        if res.startswith("this document is already filed"):
            res += "; do not file this same page again — check OPEN QUESTIONS for what is missing on another page, or finish with done"
        if res.startswith(("filed", "this document is already filed")):
            agent.forget_trail(filed=pages or [agent.tab.document])
            if paged > 1:
                res = (f"you paged through this document in place, so the {paged} pages you read of it since your last "
                       "keep were filed together: " + res)
        return res

    async def file_each_action(action: dict) -> str:
        return await file_each(tab, action, filing, agent.coverage.saw, deadline, agent.page_seen)

    async def before_done(action: dict) -> str | None:
        """A done while a question this agent filed evidence for is still open: the agent is told what is missing
        instead of going idle (sandbox run dafda06b: the customer-portal agent read 4 requirements in its thoughts,
        filed one page, and finished; the requirements question stayed open and nobody came back to it). At most
        DONE_REFUSALS times per question, never while its own documents are being read or near the deadline."""
        if not sanity.enabled("done_gate") or case.pending_for(name) or deadline - time.time() < DONE_GATE_SECONDS:
            return None
        mine = [(qid, text) for qid, text in questions_for_site(name, case.questions())
                if filed_kinds & set(QUESTION_KINDS.get(qid.split(":", 1)[0], ())) and refused.get(qid, 0) < DONE_REFUSALS]
        if not mine:
            return None
        for qid, _ in mine:
            refused[qid] = refused.get(qid, 0) + 1
        return ("not finished: you filed evidence for these questions and they are still open:\n"
                + "\n".join(f"- [{qid}] {text}" for qid, text in mine)
                + "\nThe case file does not have what they name yet. If you read it on pages you did not file, go back "
                  "to them and file them: a paged document with collect on its next-page control, or page through it "
                  "and keep (every page of it you read since your last keep is filed with it). If it is on another "
                  "website, or does not exist, answer it absent where the question allows that, or say so with done again.")

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
                  before_done=before_done,
                  max_steps=int(os.environ.get("AGENT_MAX_STEPS", "150")), idle=team.hook(name),
                  think=os.environ.get("AGENT_THINK") == "1",
                  circle_hint="You are going in circles: the same thought keeps coming back without progress. If a "
                              "link or control does nothing, do not click it again: open its address directly with "
                              "goto, or use the search form on the page. If the "
                              "page in front of you is a candidate record, file it with keep now, also when it seems "
                              "not to match: the case file does the matching, and requirement values can still change "
                              "when amendments or reserves are filed. If you need facts the other agents have not "
                              "filed yet, finish with done; you are called again when the case file has news.")

    async def note_walk(action: dict, page_before: str, result: str) -> None:
        walks.browsed(action, page_before, tab.page.url)
        return None

    agent.on_action = note_walk
    gates.log = agent.log
    agent.gates = gates
    agent.full_system = full_system
    agent.full_briefing = lambda: briefing(full=True)
    agent.stop_when = lambda: ("token budget used up" if token_budget() > 0 and budget_used() >= 1
                               and not case.pending_for(name) else None)
    return agent


# Which website plays which role in this run, as the model read it from the brief (solve). The private dataset uses
# other site names, so routing goes by role first and falls back to the public site names only without one.
SITE_ROLES: dict[str, str] = {}
# Routed questions their own agent left open after it finished, with nothing still being read: offered to every agent
# from then on. The routing assumes the public split of facts (materials in the ERP, keys and approvals in the mail);
# the private dataset may split them differently, and a routed question must not end up with nobody to answer it.
RELEASED: set[str] = set()
# Per website: the record walk its agent learned (WalkRecorder), offered as a file_each action in its briefing.
WALKS: dict[str, WalkRecorder] = {}


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
        prefixes = ("material:", "data:")
        return [(qid, text) for qid, text in questions if qid.startswith(prefixes) or qid in RELEASED]
    if plays("mail"):
        prefixes = ("condition_group:", "packaging_tariff:", "delivery_commitment:", "setup_pool:",
                    "project_bonus:", "index:", "fx:", "reserve:")
        return [(qid, text) for qid, text in questions
                if qid in ("mailbox", "discount") or qid.startswith(prefixes) or qid in RELEASED]
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


async def solve(task: dict, playwright) -> dict | None:
    started = time.time()
    sites = [site for site in task.get("access") or [] if isinstance(site, dict) and site.get("siteId") and site.get("url")]
    only = {name for name in os.environ.get("AGENT_SITES", "").split(",") if name}
    if not sites or not task.get("brief"):
        return None
    brief = task["brief"]
    listing = "\n".join(f"- {site['siteId']}: {site['url']}" for site in sites)
    context = await read_context(brief + f"\n\nThe websites of this run:\n{listing}")
    SITE_ROLES.clear()
    RELEASED.clear()
    WALKS.clear()
    BUDGET_LOGGED.clear()
    SITE_ROLES.update({role: str(context.get(f"{role}_site") or "") for role in ("erp", "mail")
                       if str(context.get(f"{role}_site") or "") in {site["siteId"] for site in sites}})
    case = CaseFile(brief, context)
    print(f"Sanity checks on: {', '.join(sanity.active()) or 'none'}", flush=True)
    print(f"Context: tender {case.reference}, scenario {case.scenario.isoformat()}, company {case.company}, "
          f"senders {case.senders}, subjects {case.subjects}", flush=True)
    print(f"Context gates {'on' if Gates().enabled else 'off (full context every turn)'}; token budget "
          f"{token_budget() or 'off'}", flush=True)

    left = contract.seconds_left(task)
    budget = min(left, 20 * 60) if left != float("inf") else 20 * 60  # a scored run has 20 minutes
    hard_stop = started + budget - 45
    # The bid form takes ~2 min and the signing code another 1-2 min; a signed partial bid scores, an unsigned
    # complete one does not. So the investigation ends with 8 minutes left (sandbox test run 54c78c38: 330 s was
    # too little and the run ended unsigned).
    submit_reserve = min(480, budget * 0.4)
    investigate_until = hard_stop - submit_reserve

    opened, tabs = await open_tabs(playwright, sites)
    try:
        working = [tab for name, tab in tabs.items() if not only or name in only]
        team = Team(case, [tab.site_id for tab in working], investigate_until)
        async def bounded(tab):  # an agent stuck past the deadline is stopped so that the others' facts still count
            restarted = False
            while True:
                limit = max(10.0, investigate_until - time.time() + 20)
                try:
                    outcome = await asyncio.wait_for(investigator(case, team, tab, investigate_until).run(), limit)
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
