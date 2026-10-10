"""The agent loop: a model reads a browser tab as text, decides what to do next, and acts.

One `Agent` drives one `Tab`. Each turn the model gets its briefing, what its last actions did and the current page,
and answers with a thought and a short list of actions. The loop only carries those out; where to go, what to
search for, which record matters and when the job is finished are the model's calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Awaitable, Callable

import llm
import sanity
from browser_env import NOT_KEPT, Tab
from coverage import Ledger

ACTIONS_HELP = """Reply with ONE JSON object: {"thought": "<one or two short sentences: what the page tells you and what you do next>", "actions": [ ... ]}.
The actions run in order. Once an action loads another page the rest of the list is dropped, because the element
ids change with every page: chain actions only while you stay on the page (fill fields, tick boxes, then click).
Elements you can act on carry an id like [12].

Browser actions:
  {"do": "login"}                                  enter this website's credentials in the login form shown and submit it
  {"do": "click", "id": 12}
  {"do": "fill", "id": 12, "text": "...", "enter": false}     type into a field; "enter": true also presses Enter
  {"do": "select", "id": 12, "value": "<an option's value or label>"}
  {"do": "set", "id": 12, "value": true}           tick/untick a checkbox, choose a radio button, or move a slider to a number
  {"do": "goto", "url": "<an address or path on this website that you have seen>"}
  {"do": "back"}    {"do": "reload"}
  {"do": "collect", "id": 12, "max_pages": 10}     [12] is the next-page or load-more control of a list or paged document you want in full: it is followed until the end and all pages come back as one document
  {"do": "fetch", "id": 12}                        download the file behind link [12] (archive, CSV, PDF, text) and read it as a document
  {"do": "wait"}                                   pause two seconds, for a page that is still loading
  {"do": "done", "summary": "<ten words>", "answer": "..."}   you are finished; "answer" only when you were asked for one

Reading the page:
- It is rendered as a screen reader would read it. "(described: ...)" and "(tooltip: ...)" hold text that only
  shows on hover, "[canvas: ...]" what a drawing says, "(in collapsed menu)" an entry of a menu that is closed
  (you can click it all the same), "->" where a link leads, "{data-...}" attributes of a control.
- The websites can be in German, in English or in both. Read them as they are. Search with the words the website
  and the task brief themselves use, and copy identifiers, references and subject lines exactly: never translate
  them. A label says the same thing in either language; do not skip one because it is not in the language you expect.
- A "LOCKED AREA" is disabled until you pass the check in front of it. A page can hold several checks, one per
  area, also inside embedded frames further down: after passing one, look for the next LOCKED AREA and its own
  controls. A check whose status already reads as unlocked is done; setting it again changes nothing.
- A "toggle" opens or closes a section. If a section you opened stays empty after you unlocked it, click its toggle
  twice (close, then open) to load it again.
- Several controls can carry the same name while only one of them does the job: tell them apart by where they
  lead and what their attributes say.
- Popups, cookie walls, access checks, error pages and expired sessions happen. Deal with them and carry on;
  after a session expires, log in again. Surveys, tools and help pages lead away from your task.
"""

STEP_TIMEOUT = 45  # seconds for one page read or one browser action: a hung page must not freeze the whole run
FULL_TURNS = 2  # how many past turns stay word for word; older ones keep the reasoning and drop the page text
NAVIGATING = {"click", "goto", "back", "reload", "login", "collect", "fetch"}


def state_fingerprint(observation: str) -> str:
    """A stable signature of the model-visible page state.

    ``Tab.observe`` assigns fresh action ids on every read.  They are useful to
    the model, but must not make an unchanged page look like a successful
    action.  Removing only those generated ids keeps visible values, errors,
    dialogs and the URL in the comparison.
    """
    stable = re.sub(r"\[\d+\]", "[]", observation)
    return hashlib.sha1(stable.encode("utf-8", "replace")).hexdigest()


def action_fingerprint(actions: list[dict], observation: str = "") -> str:
    """Use a control's visible label instead of its transient id in repeat checks."""
    stable_actions = []
    for action in actions:
        stable = {key: value for key, value in action.items() if key != "id"}
        if action.get("id") is not None:
            match = re.search(rf"(?m)^\[{re.escape(str(action['id']))}\](.*)$", observation)
            if match:
                stable["target"] = match.group(1).strip()
        stable_actions.append(stable)
    return json.dumps(stable_actions, sort_keys=True)

class Agent:
    def __init__(self, name: str, tab: Tab, system: str, briefing: Callable[[], str],
                 extra: dict[str, Callable[[dict], Awaitable[str]]] | None = None, deadline: float | None = None,
                 max_steps: int = 80, think: bool = False,
                 idle: Callable[[str], Awaitable[str | None]] | None = None,
                 guard: Callable[[dict], str | None] | None = None, finish_on_repeat: int | None = None, circle_hint: str | None = None):
        """`briefing()` heads every turn. `extra` adds actions: name -> handler(action) -> result text.
        `idle("done")` lets the caller hold a finished agent until there is news for it (returns the news) or
        let it stop (returns None)."""
        self.name, self.tab, self.system, self.briefing = name, tab, system, briefing
        self.extra = extra or {}
        self.deadline = deadline or time.time() + 3600
        self.max_steps, self.think, self.idle = max_steps, think, idle
        self.guard = guard
        # An agent that states the same thought this many turns in a row has its answer and is only "verifying":
        # it gets one forced final turn instead of running out of time (stress test: the courier read the right code,
        # then re-verified it for 14 turns and ended with no answer).
        self.finish_on_repeat = finish_on_repeat
        self._thoughts: list[str] = []
        self._circle_hinted = False
        self._circle_hinted_at = -99
        self._circle_hints = 0
        self._stuck = False
        self.circle_hint = circle_hint or (
            "You are going in circles: the same steps keep repeating without progress. The page may already show "
            "what you need, including inside embedded documents: read it fully. Otherwise open the link's address "
            "directly with goto, or try a different way. If you already have the answer, finish with done now.")
        self._circle_hint: str | None = None  # a sanity check that may veto a browser action before it runs (sanity.py)
        self.turns: list[dict] = []
        self.held: tuple[str, str] | None = None  # a collected or fetched document, until the agent moves on
        self.coverage = Ledger()  # what this agent really opened, typed and followed (coverage.py)
        self.steps = 0
        self.started = time.time()

    def document(self) -> tuple[str, str]:
        """What the agent is looking at: the document it collected or fetched, else the page as last read."""
        return self.held or (self.tab.document, self.tab.document_source)

    def log(self, text: str) -> None:
        print(f"[{self.name} {self.steps:02d} t+{time.time() - self.started:.0f}s] {text}", flush=True)

    def _messages(self, user: str) -> list[dict]:
        messages = [{"role": "system", "content": self.system}]
        for index, turn in enumerate(self.turns):
            recent = index >= len(self.turns) - FULL_TURNS
            messages.append({"role": "user", "content": turn["user"] if recent else turn["short"]})
            messages.append({"role": "assistant", "content": turn["assistant"]})
        messages.append({"role": "user", "content": user})
        return messages

    async def _browser(self, action: dict) -> str:
        do, tab = action.get("do"), self.tab
        if do == "login":
            return await tab.login()
        if do == "click":
            return await tab.click(action.get("id"))
        if do == "fill":
            return await tab.fill(action.get("id"), action.get("text", ""), bool(action.get("enter")))
        if do == "select":
            return await tab.select(action.get("id"), action.get("value", ""))
        if do == "set":
            return await tab.set(action.get("id"), action.get("value", True))
        if do == "goto":
            return await tab.goto(action.get("url", ""))
        if do == "back":
            return await tab.back()
        if do == "reload":
            return await tab.reload()
        if do == "collect":
            return await tab.collect(action.get("id"), action.get("max_pages") or 10)
        if do == "fetch":
            return await tab.fetch(action.get("id"))
        return f'unknown action "{do}"'

    def _may_close(self, action: dict) -> str | None:
        """An action that closes a question ({"question": ..., "status": "complete" | "absent"}) is checked against
        what the agent really did on its website, not against what it says it did. None lets the action run."""
        status = str(action.get("status", "")).strip().lower()
        if "question" not in action or status not in ("complete", "absent") or not sanity.enabled("coverage_gate"):
            return None
        return self.coverage.gate(str(action.get("question", "")).strip("[] "), status)

    async def _alive(self) -> bool:
        try:
            return bool(await self.tab.page.main_frame.evaluate("() => document.querySelectorAll('[data-aid]').length > 0"))
        except Exception:  # noqa: BLE001
            return False

    async def run(self) -> dict:
        results: list[str] = []
        outcome = {"done": False, "summary": "", "answer": None, "steps": 0}
        self.log(f"starting at {self.tab.base_url}")
        if self.tab.page.url in ("", "about:blank"):
            try:
                results.append(await asyncio.wait_for(self.tab.goto(self.tab.base_url + "/"), STEP_TIMEOUT))
            except asyncio.TimeoutError:
                results.append(f"opening {self.tab.base_url} took longer than {STEP_TIMEOUT} s; reload or try again")
        repeats, last_signature, failures = 0, "", 0
        seen_signatures: dict[str, int] = {}
        stuck = 0  # page reads and navigations in a row that timed out or failed: the tab may be broken
        while self.steps < self.max_steps and time.time() < self.deadline:
            self.steps += 1
            clock = time.time()
            try:
                page = await asyncio.wait_for(self.tab.observe(), STEP_TIMEOUT)
            except asyncio.TimeoutError:
                stuck += 1
                page = f"(the page did not finish loading within {STEP_TIMEOUT} s; reload it or go elsewhere)"
            except Exception as error:  # noqa: BLE001
                page = f"(the page could not be read: {str(error)[:200]})"
            self.coverage.saw(page)
            results.extend(self.tab.take_events())
            done_text = "\n".join(f"{n}. {text}" for n, text in enumerate(results, 1)) or "(none yet)"
            left = int(self.deadline - time.time())
            user = (f"{self.briefing()}\n\nTIME LEFT: {left} s\n\nRESULTS OF YOUR LAST ACTIONS\n{done_text}\n\n"
                    f"CURRENT PAGE\n{page}")
            read_s = time.time() - clock
            clock = time.time()
            reply = await llm.chat_json(self._messages(user), think=self.think, label=self.name)
            model_s = time.time() - clock
            clock = time.time()
            actions = reply.get("actions")
            if isinstance(actions, dict):
                actions = [actions]
            if not isinstance(actions, list) or not actions:
                if llm._warm.get("expired"):
                    self.log("run token expired (submission completed); stopping")
                    break
                failures += 1
                if failures >= 4:
                    self.log("the model gave no usable actions four times; stopping")
                    break
                results = ['your reply had no "actions" list; reply with {"thought": ..., "actions": [...]}']
                continue
            failures = 0
            thought = str(reply.get("thought", ""))[:300]
            self._thoughts.append(" ".join(thought.lower().split())[:160])
            recent = self._thoughts
            # Going in circles: the same thought three times (A A A), two thoughts alternating (A B A B), as in the
            # courier stress test where "unlock the list" and "open the newest message" alternated for 19 turns, or
            # the same thought three times within the last 16 turns, as in sandbox run e3804980 where the ERP agent
            # rejected the same material every 6 turns for 46 turns.
            circling = (
                (len(recent) >= 3 and len(set(recent[-3:])) == 1)
                or (len(recent) >= 4 and recent[-1] == recent[-3] and recent[-2] == recent[-4] and recent[-1] != recent[-2])
                or recent[-16:].count(recent[-1]) >= 3)
            if circling and (not self.finish_on_repeat or not self._circle_hinted):
                circling = False  # a concrete way out first; only an agent with finish_on_repeat is then forced to finish
                if len(recent) - self._circle_hinted_at >= 6:  # once every 6 turns at most
                    if self._circle_hints >= 2 and self.idle:
                        self._stuck = True  # two hints ignored: an investigator is sent idle (see below)
                    self._circle_hinted, self._circle_hinted_at = True, len(recent)
                    self._circle_hints += 1
                    self._circle_hint = self.circle_hint
            if circling:
                final = await llm.chat_json(self._messages(
                    user + "\n\nYou have reached the same conclusion several times in a row. Stop checking and finish "
                    'NOW with exactly one action: {"do": "done", "summary": "...", "answer": "..."}, the answer being '
                    "the value you found (or no answer if you truly found none)."), think=False, label=self.name)
                done = next((a for a in (final.get("actions") or []) if isinstance(a, dict) and a.get("do") == "done"), None)
                done = done or (final if final.get("do") == "done" else None)
                if done is not None:
                    self.log(f"forced to finish after going in circles: {json.dumps(done, ensure_ascii=False)[:200]}")
                    outcome.update(done=True, summary=str(done.get("summary", "")), answer=done.get("answer"))
                    break
            head = page.split("\n", 2)
            short = f"RESULTS OF YOUR LAST ACTIONS\n{done_text[:700]}\n\nPAGE (text dropped)\n" + "\n".join(head[:2])
            self.turns.append({"user": user, "short": short, "assistant": json.dumps(reply, ensure_ascii=False)})
            if os.environ.get("AGENT_TRACE"):  # what the agent saw and answered, one file per turn
                os.makedirs(os.environ["AGENT_TRACE"], exist_ok=True)
                with open(os.path.join(os.environ["AGENT_TRACE"], f"{self.name}_{self.steps:03d}.txt"), "w", encoding="utf-8") as handle:
                    handle.write(user + "\n\n===== REPLY =====\n" + json.dumps(reply, ensure_ascii=False, indent=1))

            # A repeated action is a loop only if the model also sees the same
            # page.  A form may keep its URL while legitimately changing state.
            signature = action_fingerprint(actions, page) + state_fingerprint(page)
            seen_signatures[signature] = seen_signatures.get(signature, 0) + 1
            repeats = repeats + 1 if signature == last_signature else max(0, seen_signatures[signature] - 1)
            last_signature = signature

            results, finished, moved = [], None, False
            if self._circle_hint:  # shown with the results of this turn's actions, at the top of the next turn
                results.append(self._circle_hint)
                self._circle_hint = None
            for action in actions[:8]:
                if not isinstance(action, dict):
                    continue
                do = action.get("do")
                if do == "done":
                    finished = action
                    break
                if do == "wait":
                    await asyncio.sleep(2)
                    results.append("waited two seconds")
                    continue
                if do in self.extra:
                    refusal = self._may_close(action)
                    if refusal:
                        results.append(refusal)
                        continue
                    try:
                        results.append(await self.extra[do](action))
                    except Exception as error:  # noqa: BLE001
                        results.append(f"{do} failed: {str(error)[:200]}")
                    continue
                if moved:
                    results.append(f'skipped {json.dumps(action, ensure_ascii=False)[:120]}: the page changed, its ids are stale')
                    continue
                veto = self.guard(action) if self.guard else None
                if veto:
                    results.append(veto)
                    continue
                before = self.tab.page.url
                try:
                    result = await asyncio.wait_for(self._browser(action), STEP_TIMEOUT)
                except asyncio.TimeoutError:
                    result = f"{do} took longer than {STEP_TIMEOUT} s and was stopped; the page may be stuck"
                except Exception as error:  # noqa: BLE001
                    result = f"{do} failed: {str(error)[:200]}"
                results.append(result)
                if do == "fill" and NOT_KEPT not in str(result):  # a text the field refused was never searched
                    self.coverage.typed(before, action.get("text", ""))
                elif do == "collect":
                    self.coverage.followed(getattr(self.tab, "last_collect", None))
                # Do not trust a successful browser call by itself: enterprise
                # portals often swallow a click or reject a field silently.
                # The next model turn gets a compact, actionable state-delta
                # report rather than assuming the dispatch took effect.
                if do not in ("collect", "fetch"):
                    try:
                        after = await asyncio.wait_for(self.tab.observe(), STEP_TIMEOUT)
                        changed = state_fingerprint(page) != state_fingerprint(after)
                        if changed:
                            self.coverage.moved()
                            results.append(f"verification: {do} visibly changed the page state")
                        else:
                            results.append(f"verification: {do} caused no visible page change; inspect the control "
                                           "or try a different recovery path instead of repeating it blindly")
                    except asyncio.TimeoutError:
                        results.append(f"verification: could not re-read the page within {STEP_TIMEOUT} s")
                    except Exception as error:  # noqa: BLE001
                        results.append(f"verification: could not re-read the page ({str(error)[:160]})")
                if do in NAVIGATING:
                    failed = any(sign in str(result) for sign in ("Timeout", "timed out", "took longer", "detached", "ERR_ABORTED"))
                    stuck = stuck + 1 if failed else 0
                if do in ("collect", "fetch"):
                    self.held = (self.tab.document, self.tab.document_source)
                elif do in NAVIGATING:
                    self.held = None
                if do in NAVIGATING or self.tab.page.url != before or not await self._alive():
                    moved = True
            if stuck >= 2:  # the same tab failed twice in a row: carry on in a fresh one (browser_env.Tab.renew)
                stuck = 0
                try:
                    renewed = await asyncio.wait_for(self.tab.renew(), STEP_TIMEOUT)
                    results.append("this tab kept failing to load, so it was replaced by a fresh one: " + renewed)
                except Exception as error:  # noqa: BLE001
                    results.append(f"replacing the stuck tab failed: {str(error)[:150]}")
                self.held = None
            self.log(f"(read {read_s:.1f}s, model {model_s:.1f}s, act {time.time() - clock:.1f}s) {thought} | "
                     f"{json.dumps(actions, ensure_ascii=False)[:400]} | " + " ; ".join(r[:160] for r in results))
            if repeats >= 2:
                results.append("You have sent the same actions three times with the same result: they do not work "
                               "here. Read the page again from the top for another way (a further check, a toggle, a "
                               "different link, a search), or finish with done.")
            # An agent that can be held idle (an investigator) and is stuck is sent idle as if it had finished: the
            # same actions six times on an unchanged page, or still circling after two hints. Sandbox run 74b22ceb:
            # the tender-portal agent opened the same unchanged page 122 times (~8 min), and the investigation
            # waited for it until its time limit. Idle, it is woken again when the case file has news for it.
            if finished is None and self.idle and (repeats >= 5 or self._stuck):
                self.log("stuck: repeating itself without progress; sent idle until the case file has news")
                finished = {"do": "done", "summary": "stopped: repeated the same steps without progress"}
                self._stuck, self._circle_hints, self._thoughts = False, 0, []
                seen_signatures.clear()
                last_signature, repeats = "", 0
                results.append("You were sent idle because you kept repeating the same steps without progress. "
                               "Work on what the case file news asks for, by a different way than before.")
            if finished is not None:
                outcome.update(summary=str(finished.get("summary", "")), answer=finished.get("answer"))
                news = await self.idle("done") if self.idle else None
                if news is None:
                    outcome["done"] = True
                    break
                results.append(news)
        outcome["steps"] = self.steps
        return outcome
