"""What an agent really did on its website, and the gate that reads it before a question is closed.

A model that says "I searched everywhere" proves nothing. The agent loop writes this record itself, from the actions
it carried out: the pages it opened, the text it typed into search and filter fields, the lists it followed and how
each of them ended. Closing a question is then checked against the record:

- a list that stopped at its page limit while its paging control was still there is incomplete, so no question is
  closed on this website until the rest is read;
- "absent" is the one claim the case file cannot check, so it is answered with the record first: a search without
  hits covers that search term in that place and nothing more.

Every refusal is bounded. After `LIMIT` of them a question closes anyway: the gate may cost an agent a few turns,
it must never keep a run from finishing. Nothing here knows a website; where to look next stays the model's call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

LIMIT = 2  # refusals per question
SHOWN = 12  # entries of each kind in the record an agent is shown


@dataclass
class ListRun:
    where: str  # the address the list was followed from
    control: str  # the label of the paging control
    pages: int
    capped: bool  # it stopped at the page limit and the control was still there


def _short(address: str) -> str:
    """An address without its host: the agent works one website."""
    parts = urlparse(address)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")


class Ledger:
    def __init__(self):
        self.views: dict[str, str] = {}  # address -> title, in the order of the first visit
        self.queries: list[tuple[str, str]] = []  # (address, text typed), each once
        self.lists: list[ListRun] = []
        self.moves = 0  # browser actions that visibly changed the page
        self.refused: dict[str, tuple[int, int]] = {}  # question -> (refusals so far, size of the record at the last)

    # ---- written by the agent loop ---------------------------------------------------------------------------

    def saw(self, page: str) -> None:
        """A page as the agent read it: its first lines carry the address and the title."""
        address = re.match(r"url: (\S+)", page or "")
        if address and address.group(1) not in self.views:
            title = re.search(r"^title: (.*)$", page, re.M)
            self.views[address.group(1)] = title.group(1).strip()[:60] if title else ""

    def typed(self, where: str, text) -> None:
        text = " ".join(str(text or "").split())
        if text and (where, text.casefold()) not in {(place, query.casefold()) for place, query in self.queries}:
            self.queries.append((where, text))

    def followed(self, run: dict | None) -> None:
        """How `Tab.collect` ended (its `last_collect`)."""
        if run:
            self.lists.append(ListRun(str(run.get("where", "")), str(run.get("control", "")),
                                      int(run.get("pages") or 0), bool(run.get("capped"))))

    def moved(self) -> None:
        self.moves += 1

    # ---- read by the gate ------------------------------------------------------------------------------------

    def size(self) -> int:
        return len(self.views) + len(self.queries) + len(self.lists) + self.moves

    def incomplete(self) -> list[ListRun]:
        """Lists whose last run stopped at the page limit. Following the same control to its end clears it."""
        latest: dict[str, ListRun] = {}
        for run in self.lists:
            latest[run.control] = run
        return [run for run in latest.values() if run.capped]

    def report(self) -> str:
        pages = [f'"{title}" {_short(address)}' if title else _short(address) for address, title in self.views.items()]
        typed = [f'"{text[:60]}" at {_short(where)}' for where, text in self.queries]
        lists = [f'"{run.control}" from {_short(run.where)}: {run.pages} page(s), '
                 + ("STOPPED AT THE PAGE LIMIT" if run.capped else "to the end") for run in self.lists]
        return "\n".join(f"- {label} ({len(items)}): " + ("; ".join(items[-SHOWN:]) or "none") for label, items in (
            ("pages opened", pages), ("text typed into search and other fields", typed),
            ("lists followed page by page", lists)))

    def gate(self, question: str, status: str) -> str | None:
        """None when the question may be closed as `status`, else what is missing."""
        times, size = self.refused.get(question, (0, -1))
        if times >= LIMIT:
            return None
        cut_short = self.incomplete()
        if cut_short:
            self.refused[question] = (times + 1, self.size())
            return ("not closed: " + "; ".join(f'the list you followed with "{run.control}" from {_short(run.where)} '
                                               f"stopped at its limit of {run.pages} page(s) with more pages left"
                                               for run in cut_short)
                    + ". An incomplete list proves neither that everything is filed nor that something is absent. "
                      "Collect the rest (collect continues from the page you are on; a higher max_pages reads "
                      "further), file it with keep, then answer again.")
        if status != "absent" or (times and self.size() > size):
            return None
        self.refused[question] = (times + 1, self.size())
        lead = "not closed: nothing new was searched since the last check. " if times else "not closed yet. "
        return (lead + "Absent needs a full search, and this is what you actually did on this website:\n"
                + self.report()
                + "\nA search without hits covers only that search term in that place. Before you answer absent "
                  "again: open every section, folder or list of this website that could hold it and is not in the "
                  "record, search with another term from the case file (an identifier, a shorter form, a "
                  "describing word), and follow paged lists to their end. If the record already covers all of "
                  "that, make one more different search to confirm, then answer again.")
