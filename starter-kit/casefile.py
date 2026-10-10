"""The shared case file: what the site agents have filed, as structured facts.

An agent files a page it judged relevant (`keep`); a model call turns that page into records of one of the kinds
below; this module merges them, works out from the brief's rules what is still unknown (the open questions every
agent sees) and hands the calculator its inputs. It selects among records only by the brief's own criteria: exact
tender reference, authorized sender, latest record no later than the scenario date.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import llm
from browser_env import PAGE_BREAK
from calculator import (Authorities, BonusBand, Clarification, Commitment, MASS_TO_GRAMS, Material, Position,
                        PriceCondition, Requirement, active_clarifications, base_price, final_quantities,
                        match_material, merged_assignments, parse_number, parse_when, resolve_requirements, same_text)

TOPICS = ("discount_approval", "condition_rules", "price_indices", "tech_release", "packaging_tariffs",
          "delivery_release", "fx_fixing", "setup_pools", "project_bonus")
ASSIGNMENT_KINDS = ("condition_group", "packaging_tariff", "delivery_commitment", "setup_pool", "project_bonus")

EXTRACT_SYSTEM = """You turn one piece of evidence from a business website into structured records for a tender bid.
You get the task brief (its rules say what matters), the kind of record wanted, the JSON shape, and the evidence text
as an agent read it from the page.

How to extract
- Copy identifiers, keys, references and names exactly as written.
- Amounts, prices, rates, masses and fees are STRINGS copied exactly as written, with their separators and any
  format hint that stands next to them. Give `number_format` wherever the shape asks: "en-US" if the source says the
  decimal sign is a point, "de-DE" if it is a comma (the usual case in German sources).
- Counts (quantities, weeks, millimetres, bar, pieces) are JSON integers.
- Dates and times are ISO 8601. Prefer an explicit UTC/ISO timestamp when the evidence shows one. A local time with
  a named timezone keeps its offset (2030-05-01T10:00:00+03:00). A date alone stays YYYY-MM-DD. Mind stated
  day/month orders.
- State only what the evidence says. Leave out every key the evidence says nothing about (no nulls, no empty
  lists). Never invent, convert or calculate.
- Evidence can contain drafts, previews, templates, examples, look-alike records for other references and
  superseded content. Extract the authoritative original and set the flags that tell them apart.
Reply with one JSON object of the requested shape and nothing else."""

SHAPES = {
    "positions": """The tender's bill of quantities.
{"positions": [{"position": "<position number>", "description": "<short text>", "quantity": <integer as published>,
  "unit": "<unit as written>", "unit_is_set": <true if the unit counts sets/kits of several pieces, false for pieces>,
  "requirement_ref": "<the specification requirement this position refers to>"}]}""",
    "clarifications": """Answers of the tender's bidder Q&A. One entry per answer; an answer that changes nothing still gets an entry.
{"answers": [{"number": "<answer number>", "timestamp": "<when it was issued>",
  "withdraws": ["<number of an earlier answer this one withdraws>"],
  "quantity_changes": [{"position": "<position>", "quantity": <new binding quantity>, "unit_is_set": <true|false|null if the unit is not restated>}],
  "tech_amendments": [{"revision": "<specification revision it names>", "requirement_ref": "<requirement>",
     "attribute": "stroke|bore|pressure|mounting|certification", "value": "<new value, number without unit for the first three>"}],
  "assignments": [{"position": "<position>", "condition_group": "<key or null>", "packaging_tariff": "<key or null>",
     "delivery_commitment": "<key or null>", "setup_pool": "<key or null>", "project_bonus": "<key or null>",
     "ordinary_setup_fee": "<one-off setup fee for this position as written, or null>", "number_format": "de-DE|en-US"}]}]}
A key the answer says does not apply ("no pool", "no bonus") is null.""",
    "revisions": """The revision history of the buyer's technical specification.
{"document": "<document name or number>", "revisions": [{"revision": "<revision id>", "date": "<date>",
  "status": "current|superseded|draft|unknown", "status_text": "<the status as written>"}]}""",
    "requirements": """Requirements of ONE revision of the technical specification.
{"revision": "<the revision this text belongs to>", "requirements": [{"requirement_ref": "<id>", "bore_mm": <int>,
  "stroke_mm": <int, the stroke figure as stated>,
  "stroke_is_usable_travel": <true if the figure is a usable travel to which an end reserve must be added, false if it is the nominal stroke>,
  "reserve_key": "<key of the end reserve / technical release it points to, or null>",
  "pressure_bar": <int>, "mounting": "<mounting type>", "certification": "<required certificate, or null if none is required>",
  "catalogue_group": "<catalogue group named for the ERP search>", "pieces_per_set": <int or null>}]}""",
    "material": """ERP master data of materials. A page may show one part of one material, or a list of several.
{"materials": [{"material_number": "<number>", "description": "<short text>", "catalogue_group": "<group or null>",
  "is_active": <true if the status allows sales, false if blocked/locked/discontinued, null if not shown>,
  "status_text": "<status code and meaning as written>", "bore_mm": <int|null>, "stroke_mm": <int|null>,
  "pressure_bar": <int|null>, "mounting": "<or null>", "certification": "<certificate as written; null if not shown>",
  "carton_size": <pieces per carton, int|null>,
  "allocation_mass": "<mass used to allocate shared setup cost, as written, or null>", "allocation_mass_unit": "mg|g|kg|t",
  "surcharge_mass": "<mass the metal surcharge is charged on, as written, or null>", "surcharge_mass_unit": "mg|g|kg|t",
  "metal_index_code": "<price index code or null>", "number_format": "de-DE|en-US",
  "free_stock": <total freely available stock in pieces across storage locations; prefer a stated total; int|null>,
  "stock_lead_weeks": <delivery time from stock in weeks, int|null>,
  "production_lead_weeks": <production lead time in weeks, int|null>}]}""",
    "price_conditions": """Price conditions (condition records with validity and quantity scales) of ONE material.
{"material_number": "<number>", "conditions": [{"valid_from": "<date>", "valid_to": "<date>", "currency": "<ISO code>",
  "fx_key": "<key of the FX fixing it names, or null>", "price_unit_pieces": <the number of pieces the prices are quoted for, int>,
  "number_format": "de-DE|en-US", "scales": [{"min_quantity": "<from quantity as written>", "price": "<price as written>"}]}]}""",
    "mail": """Messages of the sales mailbox. One entry per message whose text is shown.
{"mailbox_address": "<the mailbox the user is logged in to, if the page shows it, else null>",
 "messages": [{"sender": "<sender address>", "timestamp": "<sent/received>", "subject": "<subject>",
  "tender_reference": "<the reference this message is about, exactly as written, with any suffix>",
  "topic": "discount_approval|condition_rules|price_indices|tech_release|packaging_tariffs|delivery_release|fx_fixing|setup_pools|project_bonus|other",
  "number_format": "de-DE|en-US",
  "discount": {"approves": <true only if the message itself grants a discount>, "percent": "<as written>",
               "min_order_value": "<minimum net order value the approval is conditional on, or null>"},
  "condition_rules": [{"group": "<key>", "discount_applies_to": "base_only|base_and_surcharge|none"}],
  "price_indices": [{"code": "<index code>", "rate": "<as written>", "unit": "<e.g. EUR/kg>", "valid_from": "<date or null>", "valid_to": "<date or null>"}],
  "tech_release": [{"reserve_key": "<key or null>", "revision": "<specification revision it is for>", "reserve_mm_per_end": <int>}],
  "packaging_tariffs": [{"key": "<tariff key>", "eur_per_carton": "<as written>"}],
  "delivery_release": [{"key": "<commitment key>", "material_number": "<material it is for>", "weeks": <int>, "max_quantity": <inclusive quantity ceiling, int|null>}],
  "fx_fixing": [{"key": "<FX key>", "currency": "<foreign currency>", "eur_per_unit": "<EUR for one unit of that currency, as written>"}],
  "setup_pools": [{"key": "<pool key>", "fee": "<the pool's one fee, as written>",
                   "cent_allocation": "largest_remainder|remainder_to_first|remainder_to_last|remainder_to_largest"}],
  "project_bonus": [{"key": "<plan key>", "bands": [{"from": "<lower bound>", "to": "<upper bound or null if open-ended>", "percent": "<rate>"}]}]}]}
Fill only the blocks the message's original text really contains; leave the others out. Take values from the
message as sent, never from a draft, preview or quoted template shown around it. One price index code can list
several rates with validity windows: give each as its own entry.""",
}

CONTEXT_SHAPE = """From the task brief, as JSON:
{"tender_reference": "<the tender's reference>", "scenario_date": "<ISO timestamp>", "company_name": "<the bidder's company name>",
 "vat_id": "<the bidder's VAT id or null>",
 "mail_site": "<the id, from the list of this run's websites, of the webmail that holds the sales mailbox>",
 "authorized_senders": {"discount_approval": "<mailbox local part the brief authorizes for discount approvals>",
   "condition_rules": "...", "price_indices": "...", "tech_release": "...", "packaging_tariffs": "...",
   "delivery_release": "...", "fx_fixing": "...", "setup_pools": "...", "project_bonus": "..."}}
The local part is what stands before the @; null for a topic the brief names no sender for."""


def _int(value) -> int | None:
    number = parse_number(value)
    return int(number) if number is not None else None


def _text(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if text and text.lower() not in ("null", "none", "n/a", "-", "—") else None


RECORD_LISTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "positions": ("positions", ("position",)),
    "clarifications": ("answers", ("number",)),
    "revisions": ("revisions", ("revision",)),
    "requirements": ("requirements", ("requirement_ref",)),
    "material": ("materials", ("material_number",)),
    "price_conditions": ("conditions", ()),
    "mail": ("messages", ("sender", "timestamp", "subject")),
}


def validate_extraction(kind: str, data) -> tuple[dict | None, str]:
    """Keep only typed records that can be safely merged without guessing.

    The model remains responsible for understanding the selected evidence.  This
    deterministic boundary only verifies the response contract before records
    reach the calculator: a malformed envelope is rejected, while one malformed
    record cannot poison its well-formed neighbours.  It intentionally does not
    infer missing values or impose public-portal labels.
    """
    if not isinstance(data, dict):
        return None, "schema validation rejected a non-object extraction"
    root, identifiers = RECORD_LISTS[kind]
    rows = data.get(root)
    if not isinstance(rows, list):
        return None, f'schema validation rejected {kind}: "{root}" must be a list'
    valid: list[dict] = []
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        # Condition rows identify themselves by their scale list; every other
        # record has stable source identifiers that the merge layer relies on.
        identified = isinstance(row.get("scales"), list) if kind == "price_conditions" \
            else all(_text(row.get(name)) for name in identifiers)
        if identified:
            valid.append(row)
        else:
            rejected += 1
    if kind == "requirements" and valid and not _text(data.get("revision")):
        return None, "schema validation rejected requirements without a revision identifier"
    if kind == "price_conditions" and valid and not _text(data.get("material_number")):
        return None, "schema validation rejected price conditions without a material number"
    clean = dict(data)
    clean[root] = valid
    note = "" if not rejected else f"schema validation dropped {rejected} malformed {root} record(s)"
    return clean, note


def _merge(store: dict, key, record: dict) -> None:
    old = store.setdefault(key, {})
    for name, value in record.items():
        if value is not None and value != "" and value != []:
            old[name] = value


class CaseFile:
    def __init__(self, brief: str, context: dict):
        self.brief = brief
        self.reference = _text(context.get("tender_reference")) or ""
        self.scenario = parse_when(context.get("scenario_date")) or datetime.now(timezone.utc)
        self.company = _text(context.get("company_name")) or ""
        self.vat_id = _text(context.get("vat_id")) or ""
        senders = context.get("authorized_senders") or {}
        self.senders = {topic: (_text(senders.get(topic)) or "").split("@")[0].lower() for topic in TOPICS}
        self.mailbox: str | None = None
        self.positions: dict[str, dict] = {}
        self.clarifications: dict[str, dict] = {}
        self.revisions: dict[str, dict] = {}
        self.requirements: dict[tuple[str, str], dict] = {}
        self.materials: dict[str, dict] = {}
        self.conditions: dict[str, dict[tuple, dict]] = {}
        self.messages: dict[tuple, dict] = {}
        self.evidence: list[dict] = []  # extracted records, retained with the page and agent that supplied them
        self.answers: dict[str, tuple[str, str]] = {}  # question id -> (complete | absent, note)
        self.filed: set[str] = set()
        self.news: dict[str, list[str]] = {}
        self.pending = 0
        self.version = 0
        self._changed = asyncio.Event()

    # ---- filing evidence -------------------------------------------------------------------------------------

    def _bump(self) -> None:
        self.version += 1
        self._changed.set()

    async def wait_change(self, seen: int, timeout: float) -> bool:
        """True once the case file has moved past version `seen`."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while self.version == seen:
            self._changed.clear()
            left = end - loop.time()
            if left <= 0:
                return False
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=left)
            except asyncio.TimeoutError:
                return False
        return True

    def take_news(self, agent: str) -> list[str]:
        return self.news.pop(agent, [])

    def file(self, kind: str, text: str, source: str, agent: str) -> str:
        """File a document an agent chose; the extraction runs in the background and reports back as news."""
        if kind not in SHAPES:
            return f'unknown kind "{kind}"; the kinds are: {", ".join(SHAPES)}'
        if len(text.strip()) < 20:
            return "there is no document to file"
        # The pages of a collected list are read side by side: shorter replies, sooner.
        pages = [page for page in re.split(re.escape(PAGE_BREAK) + r" \d+ =====\n", text) if len(page.strip()) >= 20]
        folder = os.environ.get("AGENT_DOCS_DIR")
        if folder:
            os.makedirs(folder, exist_ok=True)
        fresh = 0
        for page in pages:
            digest = hashlib.sha1((kind + "\n" + page).encode("utf-8", "replace")).hexdigest()
            if digest in self.filed:
                continue
            self.filed.add(digest)
            self.pending += 1
            fresh += 1
            if folder:  # local tuning only: preserve the exact evidence that was filed, one page per file
                filename = f"{len(self.filed):03d}-{kind}-{digest[:8]}.json"
                with open(os.path.join(folder, filename), "w", encoding="utf-8") as out:
                    json.dump({"kind": kind, "source": source, "agent": agent, "text": page}, out,
                              ensure_ascii=False)
            asyncio.ensure_future(self._extract(kind, page, source, agent, digest))
        if not fresh:
            return f"this document is already filed as {kind}"
        return (f"filed {len(text)} characters ({fresh} page(s)) from {source} as {kind}; the facts in it will be "
                "reported to you")

    async def _extract(self, kind: str, text: str, source: str, agent: str, digest: str = "") -> None:
        try:
            prompt = (f"TASK BRIEF\n{self.brief}\n\nThe tender reference is {self.reference}; the scenario date is "
                      f"{self.scenario.isoformat()}.\n\nKIND OF RECORD: {kind}\nSHAPE\n{SHAPES[kind]}\n\n"
                      f"EVIDENCE (from {source})\n{text[:60000]}")
            raw = await llm.chat_json([{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": prompt}],
                                      think=True, max_tokens=8000, label=f"extract:{kind}",
                                      provider=os.environ.get("EXTRACT_PROVIDER", "qwen"))
            data, validation = validate_extraction(kind, raw)
            summary = self.merge(kind, data) if data else (validation or "the extraction failed; file the document again")
            if data and summary == "nothing usable was found in it":
                data = None
                summary = "no usable records were extracted; check the evidence and file it again"
            elif validation and data:
                summary = validation + "; " + summary
            if data:
                self.evidence.append({"id": digest, "kind": kind, "source": source, "agent": agent,
                                      "summary": summary, "validation": validation, "extracted": data})
            print(f"[extract {kind}] {source} -> {json.dumps(data, ensure_ascii=False)[:1800]}", flush=True)
        except Exception as error:  # noqa: BLE001
            data = None
            summary = f"the extraction failed ({str(error)[:120]}); file the document again"
        if not data:
            self.filed.discard(digest)  # so that filing it again is accepted rather than refused as a duplicate
        self.news.setdefault(agent, []).append(f"From the {kind} document you filed ({source}): {summary}")
        self.pending -= 1
        self._bump()

    def merge(self, kind: str, data: dict) -> str:
        """Merge extracted records; returns a one-line account of what they said."""
        told: list[str] = []
        if kind == "positions":
            for row in data.get("positions") or []:
                key = _text(row.get("position"))
                if key:
                    _merge(self.positions, key, row)
                    told.append(f"{key}: {row.get('quantity')} {row.get('unit') or ''} -> {row.get('requirement_ref')}")
        elif kind == "clarifications":
            for row in data.get("answers") or []:
                key = _text(row.get("number"))
                if key:
                    self.clarifications[key] = row
                    told.append(f"answer {key} ({row.get('timestamp')})")
        elif kind == "revisions":
            for row in data.get("revisions") or []:
                key = _text(row.get("revision"))
                if key:
                    _merge(self.revisions, key, row)
                    told.append(f"{key}: {row.get('status')} ({row.get('date')})")
        elif kind == "requirements":
            revision = _text(data.get("revision")) or ""
            for row in data.get("requirements") or []:
                key = _text(row.get("requirement_ref"))
                if key:
                    _merge(self.requirements, (revision, key), row)
                    told.append(f"{key} in revision {revision or '?'}: bore {row.get('bore_mm')}, stroke {row.get('stroke_mm')}"
                                f"{' (usable travel)' if row.get('stroke_is_usable_travel') else ''}, {row.get('pressure_bar')} bar, "
                                f"{row.get('mounting')}, certification {row.get('certification')}, group {row.get('catalogue_group')}")
        elif kind == "material":
            for row in data.get("materials") or []:
                key = _text(row.get("material_number"))
                if key:
                    _merge(self.materials, key, row)
                    told.append(f"{key}: " + ", ".join(f"{name}={value}" for name, value in row.items()
                                                       if value is not None and name not in ("material_number", "description", "number_format")))
        elif kind == "price_conditions":
            key = _text(data.get("material_number"))
            for row in data.get("conditions") or []:
                if key and row.get("scales"):
                    self.conditions.setdefault(key, {})[(str(row.get("valid_from")), str(row.get("valid_to")))] = row
                    told.append(f"{key}: {row.get('valid_from')}..{row.get('valid_to')} {row.get('currency')} "
                                f"per {row.get('price_unit_pieces')} pcs, {len(row['scales'])} scales")
        elif kind == "mail":
            if _text(data.get("mailbox_address")) and "@" in data["mailbox_address"]:
                self.mailbox = data["mailbox_address"].strip()
                told.append("mailbox address")
            for row in data.get("messages") or []:
                key = (str(row.get("sender")), str(row.get("timestamp")), str(row.get("subject")))
                self.messages[key] = row
                blocks = [topic for topic in ("discount",) + TOPICS if row.get(topic)]
                told.append(f"{row.get('subject')} from {row.get('sender')} at {row.get('timestamp')} about "
                            f"{row.get('tender_reference')}: {row.get('topic')} ({', '.join(blocks) or 'no usable data'})")
        self._bump()
        return "; ".join(told)[:1500] if told else "nothing usable was found in it"

    # ---- from records to calculator inputs -------------------------------------------------------------------

    def governing_revision(self) -> str | None:
        current = []
        for key, row in self.revisions.items():
            when = parse_when(row.get("date"))
            if row.get("status") == "current" and (when is None or when <= self.scenario):
                current.append((when or datetime.min.replace(tzinfo=timezone.utc), key))
        return max(current)[1] if current else None

    def _authorized(self, row: dict, topic: str) -> bool:
        """The brief's test for a record: exact tender reference, authorized sender, not after the scenario date."""
        sender = (_text(row.get("sender")) or "").lower()
        local, _, domain = sender.partition("@")
        if self.senders.get(topic) and local != self.senders[topic]:
            return False
        if self.mailbox and domain and domain != self.mailbox.lower().partition("@")[2]:
            return False
        if (_text(row.get("tender_reference")) or "").upper() != self.reference.upper():
            return False
        when = parse_when(row.get("timestamp"))
        return when is None or when <= self.scenario

    def authorities(self) -> Authorities:
        result = Authorities()
        floor = datetime.min.replace(tzinfo=timezone.utc)
        ordered = sorted(self.messages.values(), key=lambda row: parse_when(row.get("timestamp")) or floor)
        day = self.scenario.date()
        for row in ordered:  # oldest first: a newer record replaces the older one, key by key
            fmt = row.get("number_format")
            discount = row.get("discount") or {}
            if discount.get("approves") and self._authorized(row, "discount_approval"):
                percent = parse_number(discount.get("percent"), fmt)
                if percent is not None:
                    result.discount_percent = percent
                    result.discount_min_order = parse_number(discount.get("min_order_value"), fmt)
            if row.get("condition_rules") and self._authorized(row, "condition_rules"):
                for rule in row["condition_rules"]:
                    if _text(rule.get("group")) and rule.get("discount_applies_to") in ("base_only", "base_and_surcharge", "none"):
                        result.clauses[rule["group"].strip()] = rule["discount_applies_to"]
            if row.get("price_indices") and self._authorized(row, "price_indices"):
                by_code: dict[str, list[dict]] = {}
                for entry in row["price_indices"]:
                    if _text(entry.get("code")):
                        by_code.setdefault(entry["code"].strip(), []).append(entry)
                for code, entries in by_code.items():
                    for entry in entries:
                        start, end = parse_when(entry.get("valid_from")), parse_when(entry.get("valid_to"), end_of_day=True)
                        if (start and start.date() > day) or (end and end.date() < day):
                            continue
                        rate = parse_number(entry.get("rate"), fmt)
                        unit = re.search(r"/\s*(mg|g|kg|t)\b", str(entry.get("unit") or "EUR/kg"))
                        if rate is not None:
                            result.index_rates[code] = rate / MASS_TO_GRAMS[unit.group(1) if unit else "kg"]
                            break
            if row.get("tech_release") and self._authorized(row, "tech_release"):
                for entry in row["tech_release"]:
                    reserve = _int(entry.get("reserve_mm_per_end"))
                    if reserve is not None:
                        key = _text(entry.get("reserve_key")) or f"revision {entry.get('revision')}"
                        result.reserves[key] = (_text(entry.get("revision")), reserve)
            if row.get("packaging_tariffs") and self._authorized(row, "packaging_tariffs"):
                for entry in row["packaging_tariffs"]:
                    rate = parse_number(entry.get("eur_per_carton"), fmt)
                    if _text(entry.get("key")) and rate is not None:
                        result.tariffs[entry["key"].strip()] = rate
            if row.get("delivery_release") and self._authorized(row, "delivery_release"):
                for entry in row["delivery_release"]:
                    weeks = _int(entry.get("weeks"))
                    if _text(entry.get("key")) and weeks is not None:
                        result.commitments[entry["key"].strip()] = Commitment(
                            _text(entry.get("material_number")) or "", weeks, _int(entry.get("max_quantity")))
            if row.get("fx_fixing") and self._authorized(row, "fx_fixing"):
                for entry in row["fx_fixing"]:
                    rate = parse_number(entry.get("eur_per_unit"), fmt)
                    if _text(entry.get("key")) and rate is not None:
                        result.fx[entry["key"].strip()] = rate
            if row.get("setup_pools") and self._authorized(row, "setup_pools"):
                for entry in row["setup_pools"]:
                    fee = parse_number(entry.get("fee"), fmt)
                    if _text(entry.get("key")) and fee is not None:
                        result.pools[entry["key"].strip()] = (fee, entry.get("cent_allocation") or "largest_remainder")
            if row.get("project_bonus") and self._authorized(row, "project_bonus"):
                for entry in row["project_bonus"]:
                    bands = []
                    for band in entry.get("bands") or []:
                        lower, percent = parse_number(band.get("from"), fmt), parse_number(band.get("percent"), fmt)
                        if percent is not None:
                            bands.append(BonusBand(lower or Decimal(0), parse_number(band.get("to"), fmt), percent))
                    if _text(entry.get("key")) and bands:
                        result.bonus[entry["key"].strip()] = bands
        return result

    def inputs(self) -> SimpleNamespace:
        governing = self.governing_revision()
        positions = [Position(key, _text(row.get("requirement_ref")) or "", _int(row.get("quantity")) or 0,
                              bool(row.get("unit_is_set")), _text(row.get("description")) or "")
                     for key, row in sorted(self.positions.items())]
        clarifications = []
        for key, row in self.clarifications.items():
            entry = Clarification(key, parse_when(row.get("timestamp")),
                                  withdraws=[str(n).strip() for n in row.get("withdraws") or [] if _text(n)])
            for change in row.get("quantity_changes") or []:
                quantity = _int(change.get("quantity"))
                if _text(change.get("position")) and quantity is not None:
                    entry.quantity_changes.append((change["position"].strip(), quantity, change.get("unit_is_set")))
            for change in row.get("tech_amendments") or []:
                if _text(change.get("requirement_ref")) and _text(change.get("attribute")) and change.get("value") is not None:
                    entry.amendments.append((_text(change.get("revision")) or "", change["requirement_ref"].strip(),
                                             change["attribute"].strip(), str(change["value"])))
            for assigned in row.get("assignments") or []:
                position = _text(assigned.get("position"))
                if not position:
                    continue
                keys = {kind: _text(assigned.get(kind)) for kind in ASSIGNMENT_KINDS if _text(assigned.get(kind))}
                if keys:
                    entry.assignments.setdefault(position, {}).update(keys)
                fee = parse_number(assigned.get("ordinary_setup_fee"), assigned.get("number_format"))
                if fee is not None:
                    entry.setup_fees[position] = fee
            clarifications.append(entry)
        requirements = {}
        for (revision, ref), row in self.requirements.items():
            if governing and revision and not same_text(revision, governing):
                continue
            if not governing and revision:
                continue
            requirements[ref] = Requirement(
                ref, _int(row.get("bore_mm")), _int(row.get("stroke_mm")), _int(row.get("pressure_bar")),
                _text(row.get("mounting")), _text(row.get("certification")), _text(row.get("catalogue_group")),
                bool(row.get("stroke_is_usable_travel")), _text(row.get("reserve_key")), _int(row.get("pieces_per_set")))
        materials = []
        for number, row in self.materials.items():
            def grams(name):
                mass = parse_number(row.get(name), row.get("number_format"))
                return None if mass is None else mass * MASS_TO_GRAMS.get(str(row.get(name + "_unit") or "g").lower(), Decimal(1))
            conditions = []
            for record in (self.conditions.get(number) or {}).values():
                fmt = record.get("number_format")
                scales = [(int(parse_number(s.get("min_quantity"), fmt) or 0), parse_number(s.get("price"), fmt))
                          for s in record.get("scales") or [] if parse_number(s.get("price"), fmt) is not None]
                conditions.append(PriceCondition(parse_when(record.get("valid_from")), parse_when(record.get("valid_to"), end_of_day=True),
                                                 (_text(record.get("currency")) or "EUR").upper(), _text(record.get("fx_key")),
                                                 _int(record.get("price_unit_pieces")) or 1, scales))
            materials.append(Material(
                number, row.get("is_active"), _text(row.get("catalogue_group")), _int(row.get("bore_mm")),
                _int(row.get("stroke_mm")), _int(row.get("pressure_bar")), _text(row.get("mounting")),
                _text(row.get("certification")), _int(row.get("carton_size")), grams("allocation_mass"),
                grams("surcharge_mass"), _text(row.get("metal_index_code")), _int(row.get("free_stock")),
                _int(row.get("stock_lead_weeks")), _int(row.get("production_lead_weeks")), conditions))
        return SimpleNamespace(governing=governing, positions=positions, clarifications=clarifications,
                               requirements=requirements, materials=materials, authorities=self.authorities(),
                               no_material={qid.split(":", 1)[1] for qid, (status, _) in self.answers.items()
                                            if qid.startswith("material:") and status == "absent"})

    # ---- what is still unknown -------------------------------------------------------------------------------

    def questions(self) -> list[tuple[str, str]]:
        """The open questions, derived from the brief's rules and the facts on file: id and what to find."""
        facts = self.inputs()
        found: list[tuple[str, str]] = []

        def ask(qid: str, text: str) -> None:
            if qid not in self.answers:
                found.append((qid, text))

        if not facts.positions:
            ask("positions", "The tender's bill of quantities: every position with its number, quantity, unit and "
                             "requirement reference.")
        ask("clarifications", "Every answer in the tender's bidder Q&A, on all of its pages: quantity changes, "
                              "withdrawn answers, technical amendments and the keys an answer assigns to a position. "
                              "Answer this question complete once every page of the Q&A is filed.")
        if not self.mailbox:
            ask("mailbox", "The address of the sales mailbox (shown in the webmail).")
        if not facts.governing:
            ask("revision", "The revision history of the buyer's technical specification, to see which revision is "
                            "marked current.")
        refs = sorted({p.requirement_ref for p in facts.positions if p.requirement_ref})
        answers = active_clarifications(facts.clarifications, self.scenario)
        needs, waiting = resolve_requirements(facts.requirements, facts.governing, answers, facts.authorities)
        if facts.governing:
            lacking = [ref for ref in refs if ref not in needs or needs[ref].missing() or not needs[ref].catalogue_group]
            sets = {p.requirement_ref for p in facts.positions if p.is_set}
            lacking += [ref for ref in sets if ref in needs and not needs[ref].pieces_per_set and ref not in lacking]
            if lacking:
                ask("requirements", f"In specification revision {facts.governing} (the current one), the requirements "
                                    f"{', '.join(lacking)} in full: bore, stroke (nominal or usable travel, with its reserve key), "
                                    "pressure, mounting, certification, catalogue group, pieces per set.")
        for ref, key in waiting.items():
            ask(f"reserve:{ref}", f"The technical release for specification revision {facts.governing} that gives the "
                                  f"per-end reserve ({key}): requirement {ref} states a usable travel.")
        assigned, _ = merged_assignments(answers)
        quantities, _ = final_quantities(facts.positions, answers, needs)
        matched: dict[str, Material] = {}
        for ref in refs:
            need = needs.get(ref)
            if need is None or ref in waiting or need.missing():
                continue
            material = match_material(need, facts.materials)
            if material is None:
                seen = [f"{m.number} ({'active' if m.active else 'not active' if m.active is False else 'status unknown'}, "
                        f"{m.bore_mm}/{m.stroke_mm} mm, {m.pressure_bar} bar, {m.mounting}, {m.certification})"
                        for m in facts.materials if need.catalogue_group and same_text(m.catalogue_group, need.catalogue_group)]
                ask(f"material:{ref}", f"The ERP material for requirement {ref}: active, in catalogue group "
                                       f"{need.catalogue_group}, bore {need.bore_mm} mm, stroke {need.stroke_mm} mm, "
                                       f"{need.pressure_bar} bar, mounting {need.mounting}, certification "
                                       f"{need.certification or 'none'}. File each candidate's technical data and status. "
                                       f"On file for this group: {'; '.join(seen) or 'nothing yet'}. Answer absent only "
                                       "when every material of the group is on file and none matches.")
                continue
            matched[ref] = material
            gaps = [label for label, value in (("stock quantity", material.stock), ("lead time from stock", material.stock_lead_weeks),
                                               ("production lead time", material.production_lead_weeks),
                                               ("carton size", material.carton_size), ("allocation mass", material.allocation_mass_g),
                                               ("surcharge mass and metal index code", material.surcharge_mass_g)) if value is None]
            if not any(c.scales and c.valid_on(self.scenario) for c in material.conditions):
                gaps.append("the price condition valid on the scenario date with all its quantity scales")
            if gaps:
                ask(f"data:{material.number}", f"ERP material {material.number} (it matches requirement {ref}) still lacks: "
                                               f"{'; '.join(gaps)}.")
        if facts.authorities.discount_percent is None:
            ask("discount", f"The sales director's most recent discount approval for exactly {self.reference} (inbox and "
                            "archive). File the message; answer absent if no approval exists.")
        labels = {"condition_group": ("the condition rules message", facts.authorities.clauses),
                  "packaging_tariff": ("the packaging tariffs message", facts.authorities.tariffs),
                  "delivery_commitment": ("the delivery release message", facts.authorities.commitments),
                  "setup_pool": ("the setup pools message", facts.authorities.pools),
                  "project_bonus": ("the project bonus message", facts.authorities.bonus)}
        for position in sorted(assigned):
            for kind, key in assigned[position].items():
                label, known = labels[kind]
                if key not in known:
                    extra = ""
                    need = needs.get(next((p.requirement_ref for p in facts.positions if p.position == position), ""))
                    if kind == "delivery_commitment" and need is not None and need.ref in matched:
                        extra = f" (the position's material is {matched[need.ref].number})"
                    ask(f"{kind}:{key}", f"For {self.reference}: {label} that defines {key}, assigned to position {position}{extra}.")
        for material in matched.values():
            if material.index_code and material.index_code not in facts.authorities.index_rates:
                ask(f"index:{material.index_code}", f"For {self.reference}: the latest price index message with the rate of "
                                                    f"{material.index_code} (material {material.number}).")
            for condition in material.conditions:
                if condition.valid_on(self.scenario) and condition.currency != "EUR" and (condition.fx_key or "") not in facts.authorities.fx:
                    ask(f"fx:{condition.fx_key}", f"For {self.reference}: the FX fixing message with the rate of "
                                                  f"{condition.fx_key} ({condition.currency}, material {material.number}).")
        return found

    def answer(self, question: str, status: str, note: str) -> str:
        """An agent closes a question: its evidence is complete, or the thing does not exist."""
        open_ids = {qid for qid, _ in self.questions()}
        if question not in open_ids:
            return f'"{question}" is not an open question'
        if status not in ("complete", "absent"):
            return 'status must be "complete" or "absent"'
        if self.pending:
            return "facts from documents you filed are still being extracted; wait for them, then answer"
        if question == "clarifications" and status == "complete" and not self.clarifications:
            return "no Q&A answers have been extracted yet; reopen the Q&A evidence, file it as clarifications, and wait for the extraction result"
        if question.startswith("material:") and status == "absent":
            facts = self.inputs()
            answers = active_clarifications(facts.clarifications, self.scenario)
            needs, _ = resolve_requirements(facts.requirements, facts.governing, answers, facts.authorities)
            need = needs.get(question.split(":", 1)[1])
            group = [m for m in facts.materials if need and need.catalogue_group and same_text(m.catalogue_group, need.catalogue_group)]
            unknown = [m.number for m in group if m.active is None or None in (m.bore_mm, m.stroke_mm, m.pressure_bar, m.mounting)]
            if not group:
                return "no material of that catalogue group is on file yet: search the group and file its materials first"
            if unknown:
                return f"the technical data or status of {', '.join(unknown)} is not on file yet: file it before answering absent"
        if status == "complete" and question.split(":")[0] in (
                "positions", "revision", "requirements", "reserve", "material", "data", "mailbox", "discount",
                "index", "fx", "condition_group", "packaging_tariff", "delivery_commitment", "setup_pool",
                "project_bonus"):
            return "this question closes by itself once the facts are on file; file the missing evidence instead"
        self.answers[question] = (status, note)
        self._bump()
        return f"question {question} closed as {status}"

    def status(self) -> str:
        """What is on file, short enough to head every agent turn."""
        facts = self.inputs()
        answers = active_clarifications(facts.clarifications, self.scenario)
        needs, _ = resolve_requirements(facts.requirements, facts.governing, answers, facts.authorities)
        quantities, _ = final_quantities(facts.positions, answers, needs)
        assigned, fees = merged_assignments(answers)
        lines = [f"Tender {self.reference}, scenario date {self.scenario.isoformat()}, mailbox {self.mailbox or 'unknown'}."]
        for p in facts.positions:
            keys = ", ".join(f"{kind} {key}" for kind, key in assigned.get(p.position, {}).items())
            lines.append(f"Position {p.position}: tender quantity {p.quantity}{' sets' if p.is_set else ''}, final "
                         f"{quantities.get(p.position, '?')} pieces, requirement {p.requirement_ref}" + (f"; {keys}" if keys else ""))
        lines.append(f"Q&A answers on file: {', '.join(sorted(self.clarifications)) or 'none'}.")
        lines.append(f"Specification: current revision {facts.governing or 'unknown'}; revisions on file: "
                     + (", ".join(f"{key} {row.get('status')}" for key, row in self.revisions.items()) or "none") + ".")
        for ref, need in sorted(needs.items()):
            lines.append(f"Requirement {ref}: bore {need.bore_mm}, stroke {need.stroke_mm}{' usable travel' if need.usable_travel else ''}, "
                         f"{need.pressure_bar} bar, {need.mounting}, certification {need.certification or 'none'}, group {need.catalogue_group}")
        for m in facts.materials:
            valid = [c for c in m.conditions if c.valid_on(self.scenario)]
            lines.append(f"Material {m.number}: {'active' if m.active else 'NOT active' if m.active is False else 'status ?'}, group "
                         f"{m.catalogue_group}, {m.bore_mm}/{m.stroke_mm} mm, {m.pressure_bar} bar, {m.mounting}, {m.certification}; "
                         f"stock {m.stock}, lead {m.stock_lead_weeks}/{m.production_lead_weeks} wk, carton {m.carton_size}, "
                         f"index {m.index_code}, conditions on file {len(m.conditions)} ({len(valid)} valid on the scenario date"
                         + (f", {valid[0].currency}" + (f" {valid[0].fx_key}" if valid[0].fx_key else "") if valid else "") + ")")
        a = facts.authorities
        lines.append("Authorized mail records: discount "
                     + ("none on file" if a.discount_percent is None else f"{a.discount_percent} %" + (f" if order value >= {a.discount_min_order}" if a.discount_min_order else ""))
                     + "; " + "; ".join(f"{name}: {', '.join(sorted(store)) or '-'}" for name, store in (
                         ("condition groups", a.clauses), ("tariffs", a.tariffs), ("commitments", a.commitments), ("pools", a.pools),
                         ("bonus plans", a.bonus), ("FX", a.fx), ("indices", a.index_rates), ("reserves", a.reserves))))
        return "\n".join(lines)

    def dump(self) -> str:
        return json.dumps({"positions": self.positions, "clarifications": self.clarifications, "revisions": self.revisions,
                           "requirements": {f"{rev}|{ref}": row for (rev, ref), row in self.requirements.items()},
                           "materials": self.materials,
                           "conditions": {key: list(rows.values()) for key, rows in self.conditions.items()},
                           "messages": list(self.messages.values()), "answers": self.answers, "mailbox": self.mailbox,
                           "evidence": self.evidence},
                          ensure_ascii=False, default=str)


async def read_context(brief: str) -> dict:
    """The tender's identity and the brief's sender rules, read from the brief by the model."""
    return await llm.chat_json([{"role": "system", "content": "You read a task brief and return facts from it as JSON."},
                                {"role": "user", "content": f"{CONTEXT_SHAPE}\n\nTASK BRIEF\n{brief}"}],
                               think=True, max_tokens=1500, label="context")
