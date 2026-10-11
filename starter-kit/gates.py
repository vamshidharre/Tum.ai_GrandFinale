"""Escalation gates for an investigator's context: the cheapest prompt by default, more context only while the agent
has trouble, and back down as soon as it makes progress again.

Input tokens are most of a run's cost (runs of 1.6-2.1 M input tokens, ~9-10k per investigator call, of which the
whole brief, the whole case file and ten turns of history were sent on every call). Most calls are plain navigation
and need none of that.

Gate 0  (default) the task card, the agent's own open questions, the one-line case-file header, the page, the last
        GATE0_HISTORY turns (older ones as one-line thoughts) and the action list.
Gate 1  after ONE turn with trouble (an action failed or changed nothing, the page did not load or the agent waited,
        a search found nothing or was repeated, the same thought or actions as the turn before, a keep or answer or
        done refused, an extraction found nothing): also the case-file lines its questions concern, the trap
        guidance, the brief's rules on its question topics and the longer history.
Gate 2  still in trouble STUCK_TURNS turns after reaching gate 1, or the circle detection fires: the escalated turn
        (worker.Agent._escalated_turn: reasoning on, a screenshot, the full brief and the whole case file).
Back to gate 0 after progress (a document filed, a question closed, a new page without trouble) or CALM_TURNS calm
turns. CONTEXT_GATES=0 turns the gates off: every turn then carries the full context, as before.
"""
from __future__ import annotations

import os
import re
from typing import Callable

GATE0_HISTORY = 3  # past turns kept at gate 0; gate 1 and 2 keep worker.HISTORY_TURNS
CALM_TURNS = 2  # turns without trouble after which a raised gate goes back to 0
STUCK_TURNS = 2  # turns with trouble at gate 1 after which the next turn is escalated (gate 2)

# What an action's result says when it did not work, and how the log names it. First match wins.
TROUBLE = (
    ("caused no visible page change", "an action changed nothing"),
    ("took longer than", "an action timed out"),
    ("did not load", "the page did not load"),
    ("could not ", "an action failed"),
    (" failed", "an action failed"),
    ("there is no element", "a stale element id"),
    ("is disabled", "a control is disabled"),
    ("did not change", "a control did not change"),
    ("did not take", "a step did not take"),
    ("has no option", "a choice is not offered"),
    ("BUT the field", "a field did not keep its value"),
    ("no control labelled", "a control was not found"),
    ("is not on this website", "an address off the website"),
    ("is not a link on this page", "a record link was not found"),
    ("not closed", "an answer was refused"),
    ("is not an open question", "an answer was refused"),
    ("not finished:", "a done was refused"),
    ("this document is already filed", "a keep was refused"),
    ("there is no document to file", "a keep was refused"),
    ("unknown kind", "a keep was refused"),
    ("still being extracted", "waiting for an extraction"),
    ("You have sent the same actions", "the same actions again"),
    ("You are going in circles", "going in circles"),
)
# What a result says when the agent made progress: a document filed, a question closed.
PROGRESS = re.compile(r"(?m)(?:^filed \d|: filed \d|pages you read of it since your last keep were filed|"
                      r"question \S+ closed as)")
# A page that answers a search with nothing (in English or German). Only looked at right after the agent typed text.
NO_HITS = re.compile(r"(?i)\b(?:no|0|keine|null)\s+(?:matching\s+|passenden\s+)?(?:results?|hits?|treffer|"
                     r"ergebnisse?|messages|nachrichten|e-?mails|entries|einträge|records|datensätze|materials?|"
                     r"materialien)\b|nothing (?:was )?found|nichts gefunden|no match|kein(?:e)? (?:treffer|ergebnis)")


def enabled_by_env() -> bool:
    return os.environ.get("CONTEXT_GATES", "1").strip() != "0"


class Gates:
    """The context gate of one agent. `trouble` raises it at once (from 0 to 1) so the turn being built already
    carries more context; `end_turn` reads what the turn's actions did and decides the next turn's gate."""

    def __init__(self, enabled: bool | None = None, log: Callable[[str], None] | None = None):
        self.enabled = enabled_by_env() if enabled is None else enabled
        self.gate = 0
        self.log = log or (lambda text: print(text, flush=True))
        self.calm = 0  # turns in a row without trouble
        self.stuck = 0  # turns with trouble since gate 1 was reached
        self.turns = {0: 0, 1: 0, 2: 0}  # turns spent at each gate (for the tester's count)
        self._reasons: list[str] = []
        self._start = 0  # the gate the current turn started at
        self._searched = False  # the last turn typed text: the next page may answer it with nothing
        self._typed: set[tuple[str, str]] = set()  # (address, text) typed so far

    # ---- for the prompt --------------------------------------------------------------------------------------
    def full(self) -> bool:
        """Every turn carries the full context (gates off)."""
        return not self.enabled

    def history(self, default: int) -> int:
        """How many past turns the next call keeps word for word or condensed (worker.Agent._messages)."""
        if not self.enabled or self.gate > 0:
            return default
        return GATE0_HISTORY if default <= 0 else min(default, GATE0_HISTORY)

    # ---- events ----------------------------------------------------------------------------------------------
    def _set(self, gate: int, why: str) -> None:
        if gate != self.gate:
            self.log(f"gate {self.gate}->{gate}: {why}")
            self.gate = gate

    def trouble(self, why: str) -> None:
        """Something went wrong in this turn: from gate 0 straight to gate 1, for the turn being built."""
        if not self.enabled:
            return
        self._reasons.append(why)
        if self.gate == 0:
            self._set(1, why)

    def escalate(self, why: str) -> None:
        """The next turn is the escalated one (the circle detection decided so)."""
        if self.enabled:
            self._set(2, why)

    def start_turn(self, page: str = "") -> None:
        """A new turn: the page in front of the agent may already show trouble (a search without hits, an
        unreadable page)."""
        if not self.enabled:
            return
        self._start = self.gate
        if page.startswith("(the page could not"):
            self.trouble("the page could not be read")
        elif self._searched and NO_HITS.search(page or ""):
            self.trouble("the search found nothing")
        self._searched = False

    def end_turn(self, actions: list, results: list, *, url_before: str = "", url_after: str = "",
                 repeated: bool = False, can_escalate: bool = True) -> bool:
        """What the turn's actions did decides the next turn's gate. True: the next turn is to be escalated."""
        if not self.enabled:
            return False
        self.turns[self.gate] = self.turns.get(self.gate, 0) + 1
        before, self._reasons = self._reasons, []  # raised while the turn was built (page, news, keep)
        text = "\n".join(str(result) for result in results)
        reasons = [why for marker, why in TROUBLE if marker in text][:1]
        for action in actions:
            if not isinstance(action, dict):
                continue
            if action.get("do") == "wait":
                reasons.append("waiting for the page")
            typed = " ".join(str(action.get("text") or "").casefold().split())
            if action.get("do") == "fill" and typed:
                self._searched = True
                if (url_before, typed) in self._typed:
                    reasons.append("the same search again")
                self._typed.add((url_before, typed))
        if repeated:
            reasons.append("the same thought or actions as the turn before")
        filed = bool(PROGRESS.search(text))
        moved = bool(url_after) and url_after != url_before and not reasons
        if self.gate == 2 and self._start != 2:  # escalated during this turn: the next turn is the escalated one
            return False
        if self.gate == 2:  # the escalated turn is over: back to gate 1 unless it made progress
            self._set(1, "after the escalated turn")
            self.stuck = 0
        if filed or moved:
            self.calm, self.stuck = 0, 0
            self._set(0, "a document filed or a question closed" if filed else "a new page")
            return False
        reasons = before + reasons
        if reasons:
            self.calm = 0
            if self._start == 0 or self.gate == 0:  # this turn raised the gate: it does not count as stuck yet
                self._set(1, reasons[0])
                return False
            self.stuck += 1
            if self.stuck >= STUCK_TURNS and can_escalate:
                self.stuck = 0
                self._set(2, f"still in trouble ({reasons[0]})")
                return True
            return False
        self.calm += 1
        if self.gate and self.calm >= CALM_TURNS:
            self.stuck = 0
            self._set(0, f"{CALM_TURNS} calm turns")
        return False
