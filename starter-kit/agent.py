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
from worker import ACTIONS_HELP, Agent

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
        clarifications    answers of the tender's bidder Q&A
        revisions         the revision history of the buyer's technical specification
        requirements      the requirement details of one specification revision
        material          ERP master data of materials: technical attributes, status, planning, stock
        price_conditions  ERP price conditions of one material: validity, currency, quantity scales
        mail              an opened mail message
  {{"do": "answer", "question": "<id>", "status": "complete" | "absent", "note": "..."}}
      Close an open question: "complete" when all its evidence is filed (needed for lists that only you know to be
      complete), "absent" when you searched properly and what is asked for does not exist.

How to work
- Every turn starts with the case file and the OPEN QUESTIONS. Work on the questions your website can answer;
  the other agents handle the rest. Facts they file show up in the case file and can open new questions for you.
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
- Follow the search hints a website gives. If a search returns nothing or too much, change the search terms.
- For ERP questions that list free stock or stock lead time as missing, find the material's inventory or storage-stock
  evidence, file it as material, and wait for its extraction result. Check the case file shows the extracted freely
  usable stock quantity (and stock lead time when requested) before finishing. A material page with technical data
  but no stock quantity does not answer a stock question; inspect another relevant stock record and do not guess.
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

    def hook(self, name: str):
        async def hold(why: str) -> str | None:
            version = self.case.version
            before = {qid for qid, _ in self.case.questions()}
            self.idle.add(name)
            try:
                while not self.finished.is_set() and time.time() < self.deadline:
                    changed = await self.case.wait_change(version, 1.0)
                    if self.finished.is_set():
                        break
                    if changed:
                        version = self.case.version
                        fresh = {qid for qid, _ in self.case.questions()} - before
                        if fresh or self.case.news.get(name):
                            return "The case file has news" + (f"; new open questions: {', '.join(sorted(fresh))}" if fresh else "") + "."
                    elif self.idle == self.names and self.case.pending == 0:
                        self.finished.set()
                return None
            finally:
                self.idle.discard(name)
        return hold


def investigator(case: CaseFile, team: Team, tab: Tab, deadline: float) -> Agent:
    name = tab.site_id
    system = INVESTIGATOR.format(site=name, url=tab.base_url, brief=case.brief, actions=ACTIONS_HELP)

    def briefing() -> str:
        questions = case.questions()
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
    agent = Agent(name, tab, system, briefing, extra={"keep": keep, "answer": answer}, deadline=deadline,
                  max_steps=int(os.environ.get("AGENT_MAX_STEPS", "150")), idle=team.hook(name),
                  think=os.environ.get("AGENT_THINK") == "1")
    return agent


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

    opened, tabs = await open_tabs(playwright, sites)
    try:
        working = [tab for name, tab in tabs.items() if not only or name in only]
        team = Team(case, [tab.site_id for tab in working], investigate_until)
        async def bounded(tab):  # an agent stuck past the deadline is stopped so that the others' facts still count
            limit = max(10.0, investigate_until - time.time() + 20)
            try:
                return await asyncio.wait_for(investigator(case, team, tab, investigate_until).run(), limit)
            except asyncio.TimeoutError:
                team.finished.set()
                return RuntimeError(f"stopped after {limit:.0f} s: still busy at the investigation deadline")
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
