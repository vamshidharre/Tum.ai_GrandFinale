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
  {"do": "steps", "steps": [{"field": "<label>", "value": "..."}, {"press": "<button label>"}, ...]}
                                                   a sequence across page reloads: each step finds its control again
                                                   by its label on the page as it then is, so a whole chain (choose,
                                                   apply, choose the next, apply, fill, continue) runs in one turn.
                                                   "field" fills a text field, chooses in a list or ticks a box
                                                   (value true/false); "press" clicks a button or link. It stops at
                                                   the first step whose control it cannot find unambiguously and
                                                   tells you which steps ran. Use it for forms whose fields you know.
  {"do": "done", "summary": "<ten words>", "answer": "..."}   you are finished; "answer" only when you were asked for one

Reading the page:
- It is rendered as a screen reader would read it. "(described: ...)" and "(tooltip: ...)" hold text that only
  shows on hover, "[canvas: ...]" what a drawing says, "(in collapsed menu)" an entry of a menu that is closed
  (you can click it all the same), "->" where a link leads, "{data-...}" attributes of a control.
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

# Fix #1 (BEST_RUN_TIMING.md): in run a176359f one flaky tender-portal login cost 128 s (a 78 s click, then a 45 s
# page read, then a failed reload) while a fresh tab loaded at once. A page that cannot be read in READ_TIMEOUT is a
# broken tab, not a slow one: it is replaced at once (Agent._renew).
READ_TIMEOUT = 15  # seconds for one page read (normally 0.3 s)
STEP_TIMEOUT = 20  # seconds for one browser action (a click or goto includes its page load, at most 15 s)
COLLECT_TIMEOUT = 75  # collect follows a list page by page and fetch downloads a file: more than one page load
RENEW_TIMEOUT = 35  # a fresh tab: open it, load the site, and log in again when it moves to another browser
# Signs in an action's result that the tab itself is broken (not a slow page): a fresh tab at the first one.
HARD_FAILURE = ("detached", "ERR_ABORTED", "Target closed", "Target page", "has been closed")
# An announcement after a press that reports a problem rather than a confirmation (Fix #3: enter_positions keeps
# going past "saved" notices and stops at these).
ERROR_ALERT = re.compile(r"fehler|ungültig|unzulässig|nicht möglich|nicht erlaubt|pflicht|erforderlich|fehlt|bitte "
                         r"(?:eine|einen|ein|geben|wählen)|error|invalid|required|missing|not allowed|not possible|"
                         r"must |failed|rejected|abgelehnt", re.I)
# How many past turns stay word for word; older ones keep the reasoning and drop the page text. Each full turn resends
# a page of up to 14,000 characters on every call, and input tokens are most of the run's cost (the provider's prompt
# cache hits only now and then). FULL_TURNS=2 brings back the earlier setting for comparison.
FULL_TURNS = int(os.environ.get("FULL_TURNS", "1"))
# How many past turns are kept at all; older ones shrink to their thought. Every kept turn costs ~250 tokens on every
# later call, and the ERP agent runs 40-60 turns: in run 4876a0ad its calls averaged ~14k input tokens, about half of
# them old history (ERP was 46% of the run's input). HISTORY_TURNS=0 keeps every turn, as before.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "10"))


def _thought(reply: str) -> str:
    """The thought of a stored reply, one line, for the condensed older history."""
    try:
        text = str(json.loads(reply).get("thought") or "")
    except (ValueError, AttributeError):
        text = reply
    return " ".join(text.split())[:200] or "(no thought)"
NAVIGATING = {"click", "goto", "back", "reload", "login", "collect", "fetch", "steps"}
STEPS_LIMIT, STEPS_TIMEOUT = 30, 240  # one "steps" action: at most this many steps, and seconds in all
ALERT = re.compile(r"\((?:alert|status)\)[^\n]*")  # a message the page announces to screen readers (Tab.observe)
CONTROL = re.compile(r'\[(\d+)\] (?:\(in collapsed menu\) )?(select|input(?:\(\w+\))?|button|checkbox|radio|link|clickable|switch) "([^"]*)"([^\[]*)')


def find_control(page: str, label: str, kinds: tuple[str, ...]) -> tuple[str | None, str]:
    """The id of the one control of one of `kinds` labelled `label` on the rendered page: an exact label first,
    then a label that contains it. (None, why) when there is none or more than one that is not disabled."""
    wanted = " ".join(label.casefold().split())
    found = [(m.group(1), m.group(2), " ".join(m.group(3).casefold().split()), m.group(4)) for m in CONTROL.finditer(page)]
    found = [f for f in found if f[1].split("(")[0] in kinds and "disabled" not in f[3]]
    for match in ([f for f in found if f[2] == wanted], [f for f in found if wanted and wanted in f[2]]):
        ids = list(dict.fromkeys(f[0] for f in match))
        if len(ids) == 1:
            return ids[0], match[0][1]
        if len(ids) > 1:
            return None, f'{len(ids)} controls are labelled "{label}" ({", ".join("[" + i + "]" for i in ids)}): use their ids'
    return None, f'no control labelled "{label}" on the page now'


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

ESCALATION_LIMIT = 2  # stronger turns per agent: they cost reasoning time and an image


class Agent:
    def __init__(self, name: str, tab: Tab, system: str, briefing: Callable[[], str],
                 extra: dict[str, Callable[[dict], Awaitable[str]]] | None = None, deadline: float | None = None,
                 max_steps: int = 80, think: bool = False,
                 idle: Callable[[str], Awaitable[str | None]] | None = None,
                 guard: Callable[[dict], str | None] | None = None, finish_on_repeat: int | None = None, circle_hint: str | None = None,
                 finished_here: Callable[[], bool] | None = None):
        """`briefing()` heads every turn. `extra` adds actions: name -> handler(action) -> result text.
        `idle("done")` lets the caller hold a finished agent until there is news for it (returns the news) or
        let it stop (returns None)."""
        self.name, self.tab, self.system, self.briefing = name, tab, system, briefing
        # True when the agent's own work is done (agent.py: a routed agent with no open question left). It then goes
        # idle without another model turn; run 2e081b8d: the mail agent spent 4 turns (~30 s) "checking for anything
        # missed" after its last question was closed, and the investigation waited for it.
        self.finished_here = finished_here
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
        self._escalate = False
        self.escalations = 0  # stronger turns given (escalation plugin), at most ESCALATION_LIMIT
        self._stuck = False
        self.circle_hint = circle_hint or (
            "You are going in circles: the same steps keep repeating without progress. The page may already show "
            "what you need, including inside embedded documents: read it fully. Otherwise open the link's address "
            "directly with goto, or try a different way. If you already have the answer, finish with done now.")
        self._circle_hint: str | None = None  # a sanity check that may veto a browser action before it runs (sanity.py)
        self.turns: list[dict] = []
        self.opening: list[str] = []  # results to show at the first turn: the site opened, logged in, cookies chosen
        self.page_seen = ""
        self.held: tuple[str, str] | None = None  # a collected or fetched document, until the agent moves on
        self.held_by: str | None = None  # "collect" or "fetch": which action produced `held`
        self.coverage = Ledger()  # what this agent really opened, typed and followed (coverage.py)
        self.steps = 0
        self.started = time.time()

    @property
    def circle_hints(self) -> int:
        """How often this agent was told it is going in circles."""
        return self._circle_hints

    def document(self) -> tuple[str, str]:
        """What the agent is looking at: the document it collected or fetched, else the page as last read."""
        return self.held or (self.tab.document, self.tab.document_source)

    def log(self, text: str) -> None:
        print(f"[{self.name} {self.steps:02d} t+{time.time() - self.started:.0f}s] {text}", flush=True)

    def _messages(self, user: str) -> list[dict]:
        messages = [{"role": "system", "content": self.system}]
        kept = self.turns[-HISTORY_TURNS:] if HISTORY_TURNS > 0 else self.turns
        older = self.turns[:len(self.turns) - len(kept)]
        earlier = ""
        if older:  # one line per older turn: what the agent thought, so it still knows what it tried
            earlier = ("EARLIER IN THIS TASK (your thoughts, oldest first; their pages and results are no longer shown)\n"
                       + "\n".join(f"- {_thought(turn['assistant'])}" for turn in older) + "\n\n")
        for index, turn in enumerate(kept):
            recent = index >= len(kept) - FULL_TURNS
            content = turn["user"] if recent else turn["short"]
            messages.append({"role": "user", "content": (earlier + content) if index == 0 else content})
            messages.append({"role": "assistant", "content": turn["assistant"]})
        messages.append({"role": "user", "content": (earlier + user) if not kept else user})
        return messages

    async def _escalated_turn(self, user: str) -> dict:
        """One stronger turn for an agent that keeps going in circles: reasoning on, and a screenshot of the page,
        which shows what the text view can hide (a slider, a disabled button, a banner over the page, a scanned
        document). The model still decides what to do; nothing about the website is assumed here."""
        text = (user + "\n\nYOU ARE STUCK: your last steps keep repeating without progress. Look at the screenshot of "
                "the page as it really is, think about why your steps did not work, and choose a different way. If "
                "what you need is not reachable from here, finish with done.")
        messages = self._messages(text)
        image = await self.tab.screenshot() if hasattr(self.tab, "screenshot") else None
        if image:
            messages[-1]["content"] = [{"type": "text", "text": text},
                                       {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}]
        self.log(f"escalated: reasoning on{', with a screenshot' if image else ', no screenshot available'}")
        reply = await llm.chat_json(messages, think=True, label=f"{self.name}:escalated")
        return reply or await llm.chat_json(self._messages(user), think=self.think, label=self.name)

    async def _steps(self, steps: list, errors_only: bool = False, limit: int = STEPS_LIMIT) -> str:
        """Runs a chain of label-addressed steps across page reloads (the "steps" action). Every step is turned into
        the plain action it stands for and passes the same guard as a fill, select, set or click the model sends.
        A press that brings a new announcement stops the chain so the model can read it; with `errors_only` (the
        clerk's enter_positions, Fix #3) only an announcement that reads as a problem stops it, not a "saved" one."""
        done: list[str] = []
        # Speed-up #2: the page as last read is reused while the steps only fill fields and make choices on it.
        # Run 20e3c020: every position chain spent 11 s in the browser, mostly re-reading the whole page before each
        # of its 17 steps. A press, a page that moved or lost its control ids, or a control that is gone reads again.
        page: str | None = None
        for number, step in enumerate(steps[:limit], start=1):
            if not isinstance(step, dict):
                continue
            label = str(step.get("press") or step.get("field") or "")
            kinds = (("button", "link", "clickable") if "press" in step
                     else ("select", "input", "checkbox", "radio", "switch"))
            if page is None:
                page = await asyncio.wait_for(self.tab.observe(), READ_TIMEOUT)
            aid, kind = find_control(page, label, kinds)
            if aid is None and "no control labelled" in kind:
                # A configurator can show its next control a moment after the apply that unlocks it: look once more.
                await asyncio.sleep(0.6)
                page = await asyncio.wait_for(self.tab.observe(), READ_TIMEOUT)
                aid, kind = find_control(page, label, kinds)
            if "press" in step:
                action = {"do": "click", "id": aid}
            else:
                value = step.get("value", "")
                if kind == "select":
                    action = {"do": "select", "id": aid, "value": str(value)}
                elif kind in ("checkbox", "radio", "switch"):
                    action = {"do": "set", "id": aid, "value": value if isinstance(value, bool) else str(value).lower() not in ("false", "0", "no", "")}
                else:
                    action = {"do": "fill", "id": aid, "text": str(value)}
            if aid is None:
                return (f"ran {len(done)} of {len(steps)} steps" + (": " + "; ".join(done) if done else "") +
                        f". Stopped at step {number}: {kind}. The page is as it is now; continue from there.")
            veto = self.guard(action) if self.guard else None
            if veto:
                return f"ran {len(done)} of {len(steps)} steps" + (": " + "; ".join(done) if done else "") + f". Step {number} was refused: {veto}"
            alerts_before = set(ALERT.findall(page))
            url = self.tab.page.url
            result = await asyncio.wait_for(self._browser(action), STEP_TIMEOUT)
            if "there is no element" in str(result):  # the reused read was out of date: read again, try once more
                page = await asyncio.wait_for(self.tab.observe(), READ_TIMEOUT)
                fresh_id, _ = find_control(page, label, kinds)
                if fresh_id is not None:
                    action["id"] = fresh_id
                    alerts_before = set(ALERT.findall(page))
                    result = await asyncio.wait_for(self._browser(action), STEP_TIMEOUT)
            done.append(f'{number}. {label}: {str(result)[:80]}')
            if action["do"] != "click":
                if self.tab.page.url != url or not await self._alive():
                    page = None  # the choice reloaded the page: the next step reads it again
            else:  # a press can bring an error message: stop there instead of filling on blindly
                after = await asyncio.wait_for(self.tab.observe(), READ_TIMEOUT)
                page = after  # the next step starts from this read
                new = [a for a in ALERT.findall(after) if a not in alerts_before]
                if errors_only:
                    new = [a for a in new if ERROR_ALERT.search(a)]
                if new:  # an error or a confirmation: the model reads which (run 012496d3: a "saved" notice taken
                    # for an error made the clerk enter the same position again, which the form refused)
                    return (f"ran {len(done)} of {len(steps)} steps: " + "; ".join(done) +
                            f". After step {number} the page says: {' / '.join(new)[:300]}. If that reports an error, "
                            "fix it; if it confirms what you did, go on from the page as it is now.")
            if any(word in str(result) for word in ("has no option", "did not change", "is disabled", "BUT the field")):
                return f"ran {len(done)} of {len(steps)} steps: " + "; ".join(done) + f". Step {number} did not take; check it."
        return f"ran all {len(done)} steps: " + "; ".join(done)

    async def _browser(self, action: dict) -> str:
        do, tab = action.get("do"), self.tab
        if do == "login":
            return await tab.login()
        if do == "click":
            return await tab.click(action.get("id"))
        if do == "steps":
            return await self._steps(action.get("steps") or [])
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

    async def _renew(self, results: list[str]) -> bool:
        """Carry on in a fresh tab (browser_env.Tab.renew); the outcome goes to the model with the turn's results."""
        self.held, self.held_by = None, None
        try:
            renewed = await asyncio.wait_for(self.tab.renew(), RENEW_TIMEOUT)
            results.append("this tab kept failing to load, so it was replaced by a fresh one: " + renewed)
            return True
        except Exception as error:  # noqa: BLE001
            results.append(f"replacing the stuck tab failed: {str(error)[:150]}")
            return False

    async def _alive(self) -> bool:
        try:
            return bool(await asyncio.wait_for(self.tab.page.main_frame.evaluate(
                "() => document.querySelectorAll('[data-aid]').length > 0"), 3))
        except Exception:  # noqa: BLE001  (a page that is being replaced cannot answer: treat it as moved)
            return False

    async def run(self) -> dict:
        results: list[str] = list(self.opening)  # what code did on the site before the first turn (agent.open_site)
        self.opening = []
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
            if self.finished_here is not None and self.idle is not None and self.finished_here():
                self.log("nothing left for this website: idle without another model turn")
                self.briefing()  # takes the news meant for it, so the idle hold waits for new questions instead
                news = await self.idle("done")
                if news is None:
                    outcome.update(done=True, summary=outcome["summary"] or "nothing left for this website")
                    break
                results.append(news)
                continue
            self.steps += 1
            clock = time.time()
            try:
                page = await asyncio.wait_for(self.tab.observe(), READ_TIMEOUT)
            except asyncio.TimeoutError:
                # Fix #1: a page that cannot be read in READ_TIMEOUT is a broken tab (run a176359f: a 45 s read,
                # then a failed reload, then a fresh tab that loaded at once): replace it now and read again.
                page = f"(the page did not finish loading within {READ_TIMEOUT} s; reload it or go elsewhere)"
                if await self._renew(results):
                    try:
                        page = await asyncio.wait_for(self.tab.observe(), READ_TIMEOUT)
                    except Exception:  # noqa: BLE001
                        stuck += 1
                else:
                    stuck += 1
            except Exception as error:  # noqa: BLE001
                page = f"(the page could not be read: {str(error)[:200]})"
            self.coverage.saw(page)
            self.page_seen = page  # the page the model answers to: its ids and link targets (file_each)
            results.extend(self.tab.take_events())
            done_text = "\n".join(f"{n}. {text}" for n, text in enumerate(results, 1)) or "(none yet)"
            left = int(self.deadline - time.time())
            user = (f"{self.briefing()}\n\nTIME LEFT: {left} s\n\nRESULTS OF YOUR LAST ACTIONS\n{done_text}\n\n"
                    f"CURRENT PAGE\n{page}")
            read_s = time.time() - clock
            clock = time.time()
            if self._escalate:
                self._escalate = False
                self.escalations += 1
                reply = await self._escalated_turn(user)
            else:
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
            strict = ((len(recent) >= 3 and len(set(recent[-3:])) == 1)
                      or (len(recent) >= 4 and recent[-1] == recent[-3] and recent[-2] == recent[-4] and recent[-1] != recent[-2]))
            circling = strict or recent[-16:].count(recent[-1]) >= 3
            if circling and (not self.finish_on_repeat or not self._circle_hinted):
                circling = False  # a concrete way out first; only an agent with finish_on_repeat is then forced to finish
                if len(recent) - self._circle_hinted_at >= 6:  # once every 6 turns at most
                    if self._circle_hints >= 2 and self.idle:
                        self._stuck = True  # two hints ignored: an investigator is sent idle (see below)
                    self._circle_hinted, self._circle_hinted_at = True, len(recent)
                    self._circle_hints += 1
                    self._circle_hint = self.circle_hint
                    # The second time: also one stronger turn, reasoning on and with a screenshot (sanity plugin
                    # "escalation"), before the agent is given up on at the third. Investigators only (they have an
                    # idle hook); the bid clerk, which repeats one pattern for every position on purpose (run bb0655a2), only on a
                    # strict loop: the same thought three times or two alternating (run d3ca14df: 95 turns of "fill,
                    # add position, empty again" until its step cap, never signed).
                    if self._circle_hints == 2 and (self.idle or strict) and self.escalations < ESCALATION_LIMIT \
                            and sanity.enabled("escalation"):
                        self._escalate = True
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
            # The previous turn stays in full in the history (its page and results), but without its briefing (case
            # file and open questions): the new message repeats the briefing as it is now. Run 665adb0e: ~2k of
            # the ~9k input tokens of every call were this repeated briefing; uncached input is ~90% of the cost.
            remembered = (f"TIME LEFT: {left} s\n\nRESULTS OF YOUR LAST ACTIONS\n{done_text}\n\nCURRENT PAGE\n{page}"
                          "\n\n(The case file and open questions of that turn are left out: see the latest message.)")
            self.turns.append({"user": remembered, "short": short, "assistant": json.dumps(reply, ensure_ascii=False)})
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
                limit = (STEPS_TIMEOUT if do == "steps" else COLLECT_TIMEOUT if do in ("collect", "fetch")
                         else STEP_TIMEOUT)
                try:
                    result = await asyncio.wait_for(self._browser(action), limit)
                except asyncio.TimeoutError:
                    result = f"{do} took longer than {limit} s and was stopped; the page may be stuck"
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
                unreadable = False
                if do not in ("collect", "fetch"):
                    try:
                        after = await asyncio.wait_for(self.tab.observe(), READ_TIMEOUT)
                        changed = state_fingerprint(page) != state_fingerprint(after)
                        if changed:
                            self.coverage.moved()
                            results.append(f"verification: {do} visibly changed the page state")
                        else:
                            results.append(f"verification: {do} caused no visible page change; inspect the control "
                                           "or try a different recovery path instead of repeating it blindly")
                    except asyncio.TimeoutError:
                        results.append(f"verification: could not re-read the page within {READ_TIMEOUT} s")
                        unreadable = True
                    except Exception as error:  # noqa: BLE001
                        results.append(f"verification: could not re-read the page ({str(error)[:160]})")
                        unreadable = True
                if do in NAVIGATING:
                    failed = any(sign in str(result) for sign in ("Timeout", "timed out", "took longer", "detached", "ERR_ABORTED"))
                    stuck = stuck + 1 if failed else 0
                    # Fix #1: a failed navigation that shows a broken tab (detached frame, aborted load, a page that
                    # cannot be read back) gets a fresh tab now rather than after a second timeout.
                    if failed and (unreadable or any(sign in str(result) for sign in HARD_FAILURE)):
                        stuck = max(stuck, 2)
                if do in ("collect", "fetch"):
                    self.held, self.held_by = (self.tab.document, self.tab.document_source), do
                elif do in NAVIGATING:
                    self.held, self.held_by = None, None
                if do in NAVIGATING or self.tab.page.url != before or not await self._alive():
                    moved = True
            if stuck >= 2:  # the same tab failed twice in a row, or once fatally: carry on in a fresh one
                stuck = 0
                await self._renew(results)
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
