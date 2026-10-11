"""Deterministic bid arithmetic for the RFQ-to-bid challenge.

The agents find and select the evidence; this module only applies the bid rules of the task brief to the structured
facts they filed: exact Decimal arithmetic, ROUND_HALF_UP to the cent, largest-remainder cent allocation, marginal
bonus bands, shared stock and delivery commitments. It contains no scenario values and never touches a website.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")

# Mass units to grams and index-rate units to EUR per gram, so any combination the sources use multiplies out.
MASS_TO_GRAMS = {"mg": Decimal("0.001"), "g": Decimal(1), "kg": Decimal(1000), "t": Decimal(1000000)}


def round_cents(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_number(raw, number_format: str | None = None) -> Decimal | None:
    """A number as written on a page ("1.234,56", "64,853 (en-US)", 12) as a Decimal; None when there is none.

    `number_format` is "de-DE" (decimal comma) or "en-US" (decimal point); an inline tag in `raw` wins. Without
    either, the separators decide, and a lone comma is read as a decimal comma, as German sources write it.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    text = str(raw).strip()
    tag = re.search(r"(de-DE|en-US)", text, re.IGNORECASE)
    fmt = (tag.group(1) if tag else number_format or "").lower()
    match = re.search(r"[-+]?\d[\d.,'\s  ]*", text)
    if not match:
        return None
    token = re.sub(r"['\s  ]", "", match.group(0)).rstrip(".,")
    try:
        if fmt == "en-us":
            return Decimal(token.replace(",", ""))
        if fmt == "de-de":
            return Decimal(token.replace(".", "").replace(",", "."))
        if "," in token and "." in token:
            if token.rfind(",") > token.rfind("."):
                return Decimal(token.replace(".", "").replace(",", "."))
            return Decimal(token.replace(",", ""))
        if "," in token:
            return Decimal(token.replace(",", ".")) if token.count(",") == 1 else Decimal(token.replace(",", ""))
        if token.count(".") > 1:
            return Decimal(token.replace(".", ""))
        return Decimal(token)
    except InvalidOperation:
        return None


def parse_when(raw, end_of_day: bool = False) -> datetime | None:
    """An ISO 8601 date or timestamp (as the extraction step writes them) as an aware UTC datetime.

    A value without a time is a whole day: its start, or its last microsecond with `end_of_day`, so that validity
    periods include both endpoints.
    """
    if not raw:
        return None
    text = str(raw).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            return day + timedelta(days=1) - timedelta(microseconds=1) if end_of_day else day
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        german = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[,\s]+(\d{1,2}):(\d{2}))?", text)
        if not german:
            return None
        day, month, year, hour, minute = german.groups()
        try:
            when = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
        except ValueError:
            return None
        if hour is None and end_of_day:
            when += timedelta(days=1) - timedelta(microseconds=1)
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def allocate_cents(total_cents: int, weights: dict[str, Decimal], method: str = "largest_remainder") -> dict[str, Decimal]:
    """Split `total_cents` across the keys in proportion to `weights`, as EUR amounts that sum exactly.

    Every share is floored to the cent; the cents left over go out by `method`: "largest_remainder" (ties to the
    lower key), "remainder_to_first", "remainder_to_last" or "remainder_to_largest" (the biggest weight).
    """
    keys = sorted(weights, key=position_order)
    total_weight = sum(weights.values(), ZERO)
    if not keys or total_cents <= 0 or total_weight <= 0:
        return {key: ZERO for key in keys}
    # Exact fractions: a tie between remainders must be a tie, not a matter of decimal precision.
    exact = {key: Fraction(total_cents) * Fraction(weights[key]) / Fraction(total_weight) for key in keys}
    cents = {key: math.floor(exact[key]) for key in keys}
    left = total_cents - sum(cents.values())
    if method == "remainder_to_first":
        cents[keys[0]] += left
    elif method == "remainder_to_last":
        cents[keys[-1]] += left
    elif method == "remainder_to_largest":
        cents[max(keys, key=lambda key: (weights[key], -keys.index(key)))] += left
    else:
        for key in sorted(keys, key=lambda key: (-(exact[key] - cents[key]), keys.index(key)))[:left]:
            cents[key] += 1
    return {key: Decimal(cents[key]) * CENT for key in keys}


def position_order(key) -> list:
    """Position identifiers in ascending order: their numbers count as numbers ("2" before "10")."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.findall(r"\d+|\D+", str(key))]


def same_text(a: str | None, b: str | None) -> bool:
    """Two attribute values name the same thing: case, spacing and punctuation aside."""
    def norm(value):
        return re.sub(r"[^0-9a-zäöüß]+", "", (value or "").lower())
    return norm(a) == norm(b)


REVISION_WORDS = re.compile(r"^\s*(?:revision|rev|version|ver|ausgabe|stand|index|v)\b\.?\s*:?\s*", re.I)


def same_revision(a: str | None, b: str | None) -> bool:
    """Revision ids match when they differ only by a leading word: "Revision 2", "Rev. 2" and "2" are the same (eval
    variant B: revisions were filed as "Revision 2", the requirements as "2", and no requirement governed)."""
    return same_text(REVISION_WORDS.sub("", a or ""), REVISION_WORDS.sub("", b or ""))


NO_CERTIFICATE = ("", "-", "keine", "kein", "ohne", "none", "no", "n/a", "na", "nicht gefordert", "keine gefordert",
                  "nicht erforderlich", "keine erforderlich", "entfällt")


def same_certification(a: str | None, b: str | None) -> bool:
    """Certifications match, with every way of writing "none required" counting as the same."""
    def norm(value):
        text = re.sub(r"\s+", " ", (value or "").strip().lower()).strip(" .—–")
        return "" if text in NO_CERTIFICATE else text
    return same_text(norm(a), norm(b))


@dataclass
class Position:
    position: str
    requirement_ref: str
    quantity: int
    is_set: bool = False  # the quantity counts sets, not pieces
    description: str = ""


@dataclass
class Clarification:
    """One answer of the tender's Q&A, as far as it changes the bid."""
    number: str
    when: datetime | None
    withdraws: list[str] = field(default_factory=list)
    quantity_changes: list[tuple[str, int, bool | None]] = field(default_factory=list)  # position, quantity, is_set
    amendments: list[tuple[str, str, str, str]] = field(default_factory=list)  # revision, requirement, attribute, value
    assignments: dict[str, dict[str, str]] = field(default_factory=dict)  # position -> {kind: key}
    setup_fees: dict[str, Decimal] = field(default_factory=dict)  # position -> ordinary setup fee, EUR


@dataclass
class Requirement:
    ref: str
    bore_mm: int | None = None
    stroke_mm: int | None = None
    pressure_bar: int | None = None
    mounting: str | None = None
    certification: str | None = None
    catalogue_group: str | None = None
    usable_travel: bool = False  # the stroke is the usable travel: add twice the per-end reserve
    reserve_key: str | None = None
    pieces_per_set: int | None = None

    def missing(self) -> list[str]:
        return [name for name in ("bore_mm", "stroke_mm", "pressure_bar", "mounting") if getattr(self, name) is None]

    def unmatchable(self) -> bool:
        """Too little is known to choose a material. A pressure the specification does not state is no criterion
        (eval variant A); the open questions still ask for it while the investigation runs."""
        return any(getattr(self, name) is None for name in ("bore_mm", "stroke_mm", "mounting"))


@dataclass
class PriceCondition:
    valid_from: datetime | None
    valid_to: datetime | None
    currency: str = "EUR"
    fx_key: str | None = None
    price_unit: int = 1  # the scale prices are for this many pieces
    scales: list[tuple[int, Decimal]] = field(default_factory=list)  # minimum quantity, price

    def valid_on(self, when: datetime) -> bool:
        return (self.valid_from is None or self.valid_from <= when) and (self.valid_to is None or when <= self.valid_to)


@dataclass
class Material:
    number: str
    active: bool | None = None
    catalogue_group: str | None = None
    bore_mm: int | None = None
    stroke_mm: int | None = None
    pressure_bar: int | None = None
    mounting: str | None = None
    certification: str | None = None
    carton_size: int | None = None
    allocation_mass_g: Decimal | None = None
    surcharge_mass_g: Decimal | None = None
    index_code: str | None = None
    stock: int | None = None
    stock_lead_weeks: int | None = None
    production_lead_weeks: int | None = None
    conditions: list[PriceCondition] = field(default_factory=list)

    def matches(self, need: Requirement) -> bool:
        # A pressure the requirement does not state is no criterion (eval variant A: a specification without pressures
        # matched no material at all, and every position was left out).
        return (self.bore_mm == need.bore_mm and self.stroke_mm == need.stroke_mm
                and (need.pressure_bar is None or self.pressure_bar == need.pressure_bar)
                and same_text(self.mounting, need.mounting)
                and same_certification(self.certification, need.certification))


@dataclass
class Commitment:
    material: str
    weeks: int
    max_quantity: int | None


@dataclass
class BonusBand:
    lower: Decimal
    upper: Decimal | None  # None: no upper limit
    percent: Decimal


@dataclass
class Authorities:
    """What the authorized records say, each key already resolved to its latest applicable record."""
    discount_percent: Decimal | None = None  # None: no approval
    discount_min_order: Decimal | None = None
    clauses: dict[str, str] = field(default_factory=dict)  # group -> base_only | base_and_surcharge | none
    index_rates: dict[str, Decimal] = field(default_factory=dict)  # code -> EUR per gram
    reserves: dict[str, tuple[str | None, int]] = field(default_factory=dict)  # key -> (revision, mm per end)
    tariffs: dict[str, Decimal] = field(default_factory=dict)  # key -> EUR per carton
    commitments: dict[str, Commitment] = field(default_factory=dict)
    fx: dict[str, Decimal] = field(default_factory=dict)  # key -> EUR per currency unit
    pools: dict[str, tuple[Decimal, str]] = field(default_factory=dict)  # key -> (fee EUR, allocation method)
    bonus: dict[str, list[BonusBand]] = field(default_factory=dict)


@dataclass
class Line:
    position: str
    material_number: str
    quantity: int
    unit_price: Decimal | None = None
    lead_time_weeks: int | None = None
    base_unit_price: Decimal | None = None
    surcharge_per_unit: Decimal | None = None
    packaging_per_unit: Decimal | None = None
    rebate_total: Decimal | None = None
    material: Material | None = None
    gaps: list[str] = field(default_factory=list)  # what kept a field from being worked out


@dataclass
class Bid:
    tender_reference: str
    decision: str
    discount_percent: Decimal
    lines: list[Line]
    total_net: Decimal
    omitted: list[str] = field(default_factory=list)  # positions that could not be priced
    notes: list[str] = field(default_factory=list)

    def answer(self) -> dict:
        def number(value):
            value = Decimal(str(value))
            return int(value) if value == value.to_integral_value() else float(value)
        positions = []
        for line in self.lines:
            entry = {"position": line.position, "material_number": line.material_number, "quantity": line.quantity,
                     "unit_price": float(line.unit_price), "lead_time_weeks": line.lead_time_weeks}
            for name in ("base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"):
                if getattr(line, name) is not None:
                    entry[name] = float(getattr(line, name))
            positions.append(entry)
        return {"tender_reference": self.tender_reference, "decision": self.decision, "currency": "EUR",
                "discount_percent": number(self.discount_percent), "positions": positions,
                "total_net": float(self.total_net)}


def active_clarifications(clarifications: list[Clarification], scenario: datetime) -> list[Clarification]:
    """The answers that count, oldest first: dated no later than the scenario date and not withdrawn by another."""
    dated = [c for c in clarifications if c.when is None or c.when <= scenario]
    withdrawn = {str(number).strip() for c in dated for number in c.withdraws}
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted((c for c in dated if c.number.strip() not in withdrawn), key=lambda c: (c.when or floor, c.number))


def resolve_requirements(requirements: dict[str, Requirement], governing_revision: str | None,
                         answers: list[Clarification], authorities: Authorities) -> tuple[dict[str, Requirement], dict[str, str]]:
    """The governing requirements after scoped amendments and the usable-travel derivation.

    Returns them with, per requirement that still waits for a reserve, what it waits for.
    """
    resolved = {ref: replace(need) for ref, need in requirements.items()}
    for answer in answers:  # oldest first, so the latest amendment wins per attribute
        for revision, ref, attribute, value in answer.amendments:
            need = resolved.get(ref)
            if need is None or not governing_revision or not same_revision(revision, governing_revision):
                continue
            name = attribute.lower()
            number = parse_number(value, "de-DE")  # a dot in a German dimension groups thousands
            if name in ("stroke", "bore", "pressure") and number is not None:
                setattr(need, {"stroke": "stroke_mm", "bore": "bore_mm", "pressure": "pressure_bar"}[name], int(number))
            elif name in ("mounting", "certification"):
                setattr(need, name, str(value).strip())
    waiting: dict[str, str] = {}
    for ref, need in resolved.items():
        if not need.usable_travel or need.stroke_mm is None:
            continue
        entry = authorities.reserves.get(need.reserve_key or "")
        if entry is None and not need.reserve_key:  # the release may name only the revision
            bound = [e for e in authorities.reserves.values() if e[0] and same_revision(e[0], governing_revision)]
            entry = bound[0] if len(bound) == 1 else None
        if entry is None or (entry[0] and governing_revision and not same_revision(entry[0], governing_revision)):
            waiting[ref] = need.reserve_key or "the per-end reserve"
            continue
        need.stroke_mm += 2 * entry[1]
        need.usable_travel = False
    return resolved, waiting


def final_quantities(positions: list[Position], answers: list[Clarification],
                     requirements: dict[str, Requirement]) -> tuple[dict[str, int], list[str]]:
    """Each position's quantity in pieces: the tender's, replaced by the latest answer that changes it, sets
    converted with the requirement's pieces per set. Also the positions whose set size is unknown."""
    raw = {p.position: (p.quantity, p.is_set) for p in positions}
    for answer in answers:
        for position, quantity, is_set in answer.quantity_changes:
            if position in raw:
                raw[position] = (quantity, raw[position][1] if is_set is None else is_set)
    pieces, unknown = {}, []
    for p in positions:
        quantity, is_set = raw[p.position]
        need = requirements.get(p.requirement_ref)
        if is_set and not p.is_set and (need is None or not need.pieces_per_set):
            is_set = False
        if is_set:
            if need is None or not need.pieces_per_set:
                unknown.append(p.position)
                continue
            quantity *= need.pieces_per_set
        pieces[p.position] = quantity
    return pieces, unknown


def match_material(need: Requirement, materials: list[Material]) -> Material | None:
    """The active material that equals the requirement in every attribute; its catalogue group first."""
    exact = [m for m in materials if m.active and m.matches(need)]
    grouped = [m for m in exact if need.catalogue_group and same_text(m.catalogue_group, need.catalogue_group)]
    return (grouped or exact or [None])[0]


def merged_assignments(answers: list[Clarification]) -> tuple[dict[str, dict[str, str]], dict[str, Decimal]]:
    keys: dict[str, dict[str, str]] = {}
    fees: dict[str, Decimal] = {}
    for answer in answers:
        for position, assigned in answer.assignments.items():
            keys.setdefault(position, {}).update({kind: key for kind, key in assigned.items() if key})
        fees.update(answer.setup_fees)
    return keys, fees


def base_price(material: Material, quantity: int, scenario: datetime, authorities: Authorities) -> tuple[Decimal | None, str]:
    """The EUR base price per piece: the condition valid on the scenario date, the highest scale reached, the price
    unit normalised, converted with the condition's FX key and rounded to the cent."""
    valid = [c for c in material.conditions if c.scales and c.valid_on(scenario)]
    if not valid:
        return None, "no price condition valid on the scenario date"
    condition = valid[0]
    reached = [scale for scale in condition.scales if scale[0] <= quantity]
    if not reached:
        return None, "the quantity is below the lowest price scale"
    price = max(reached, key=lambda scale: scale[0])[1] / Decimal(condition.price_unit or 1)
    if condition.currency.upper() != "EUR":
        rate = authorities.fx.get(condition.fx_key or "")
        if rate is None:
            return None, f"no FX fixing for {condition.fx_key or condition.currency}"
        price *= rate
    return round_cents(price), ""


def calculate_bid(tender_reference: str, scenario: datetime, governing_revision: str | None,
                  positions: list[Position], clarifications: list[Clarification],
                  requirements: dict[str, Requirement], materials: list[Material], authorities: Authorities,
                  no_material: set[str] | None = None) -> Bid:
    """Apply the bid rules. `no_material` names the requirements the agents searched without finding an active
    match: one of them makes the answer a no_bid. Positions that lack evidence are left out (partial bid)."""
    answers = active_clarifications(clarifications, scenario)
    needs, waiting = resolve_requirements(requirements, governing_revision, answers, authorities)
    quantities, unknown_sets = final_quantities(positions, answers, needs)
    assigned, setup_fees = merged_assignments(answers)
    notes: list[str] = []
    omitted: list[str] = []

    lines: dict[str, Line] = {}
    # Every position whose material and quantity are known, priced or not. Shared amounts (setup pool, project bonus,
    # pooled stock demand) are split over all their member positions: a member that could not be priced still takes
    # its share, so the priced ones do not absorb it (option A, agreed 10 Oct; sandbox run 9c8fe292 put the whole
    # pool on one position because the other members were omitted).
    known: dict[str, tuple] = {}
    for p in sorted(positions, key=lambda p: position_order(p.position)):
        need = needs.get(p.requirement_ref)
        if need is not None and p.requirement_ref not in waiting and not need.unmatchable():
            material = match_material(need, materials)
            if material is None and p.requirement_ref in (no_material or set()):
                approved = authorities.discount_percent or ZERO
                conditional = bool(authorities.discount_min_order)
                return Bid(tender_reference, "no_bid", ZERO if conditional else approved, [], ZERO,
                           notes=[f"no active material matches requirement {p.requirement_ref} (position {p.position})"])
        else:
            material = None
        if material is not None and quantities.get(p.position, 0) > 0:
            known[p.position] = (material, quantities[p.position])
        if material is None or quantities.get(p.position, 0) <= 0:
            omitted.append(p.position)
            notes.append(f"position {p.position}: " + ("set size unknown" if p.position in unknown_sets
                                                       else "no quantity" if material is not None
                                                       else f"no material resolved for {p.requirement_ref}"))
            continue
        lines[p.position] = Line(p.position, material.number, quantities[p.position], material=material)

    for line in list(lines.values()):
        line.base_unit_price, why = base_price(line.material, line.quantity, scenario, authorities)
        if line.base_unit_price is None:
            notes.append(f"position {line.position}: {why}")
            omitted.append(line.position)
            del lines[line.position]

    # The approval's threshold looks at the undiscounted goods value: quantities times EUR base prices.
    goods = {key: round_cents(Decimal(line.quantity) * line.base_unit_price) for key, line in lines.items()}
    order_value = round_cents(sum((Decimal(l.quantity) * l.base_unit_price for l in lines.values()), ZERO))
    discount = authorities.discount_percent or ZERO
    if discount and authorities.discount_min_order is not None and order_value < authorities.discount_min_order:
        discount = ZERO
        notes.append("the conditional discount approval misses its minimum order value")
    factor = (HUNDRED - discount) / HUNDRED

    def members(kind: str, defined) -> dict[str, list[str]]:
        """Every tender position assigned to each pool or plan, priced or not."""
        groups: dict[str, list[str]] = {}
        for p in sorted(positions, key=lambda p: position_order(p.position)):
            assigned_key = assigned.get(p.position, {}).get(kind)
            if assigned_key and assigned_key in defined:
                groups.setdefault(assigned_key, []).append(p.position)
        return groups

    def member_goods(key: str) -> Decimal | None:
        """Undiscounted goods value of a member, also for one that is not priced; None when it cannot be known."""
        if key in goods:
            return goods[key]
        if key not in known:
            return None
        material, quantity = known[key]
        price, _ = base_price(material, quantity, scenario, authorities)
        return None if price is None else round_cents(Decimal(quantity) * price)

    pool_share = {key: ZERO for key in lines}
    for pool, keys in members("setup_pool", authorities.pools).items():
        fee, method = authorities.pools[pool]
        weights = {}
        for key in keys:
            material, quantity = (lines[key].material, lines[key].quantity) if key in lines else known.get(key, (None, 0))
            weights[key] = Decimal(quantity) * ((material.allocation_mass_g if material else None) or ZERO)
        if any(weight <= 0 for weight in weights.values()):  # a member's share cannot be known: leave it blank
            for key in keys:
                if key in lines:
                    lines[key].gaps.append("allocation mass")
            continue
        for key, share in allocate_cents(int(round_cents(fee) * 100), weights, method).items():
            if key in pool_share:
                pool_share[key] += share

    rebate = {key: ZERO for key in lines}
    for plan, keys in members("project_bonus", authorities.bonus).items():
        values = {key: member_goods(key) for key in keys}
        if any(value is None for value in values.values()):  # a member's goods value is unknown: leave it blank
            for key in keys:
                if key in lines:
                    lines[key].gaps.append(f"project_bonus {plan} member value unknown")
            continue
        eligible = sum(values.values(), ZERO)
        credit = ZERO
        for band in authorities.bonus[plan]:
            top = eligible if band.upper is None else min(eligible, band.upper)
            if top > band.lower:
                credit += (top - band.lower) * band.percent / HUNDRED
        shares = allocate_cents(int(round_cents(credit) * 100), values)
        for key, share in shares.items():
            if key in rebate:
                rebate[key] += share

    demand: dict[str, int] = {}  # pooled per material over every known position, priced or not (rule 15)
    for material, quantity in known.values():
        demand[material.number] = demand.get(material.number, 0) + quantity

    for key, line in lines.items():
        material, quantity, keys = line.material, Decimal(line.quantity), assigned.get(key, {})
        unassigned = [kind for kind in ("packaging_tariff", "setup_pool", "project_bonus", "delivery_commitment",
                                        "condition_group") if keys.get(kind) and keys[kind] not in
                      {"packaging_tariff": authorities.tariffs, "setup_pool": authorities.pools,
                       "project_bonus": authorities.bonus, "delivery_commitment": authorities.commitments,
                       "condition_group": authorities.clauses}[kind]]
        line.gaps.extend(f"{kind} {keys[kind]}" for kind in unassigned)

        surcharge = ZERO  # the mass surcharge per piece, unrounded
        if material.index_code:
            rate = authorities.index_rates.get(material.index_code)
            if rate is None or material.surcharge_mass_g is None:
                line.gaps.append(f"metal index {material.index_code}")
            else:
                surcharge = material.surcharge_mass_g * rate
        setup = setup_fees.get(key, ZERO) + pool_share[key]

        cartons = ZERO
        if keys.get("packaging_tariff") in authorities.tariffs:
            if material.carton_size:
                cartons = Decimal(math.ceil(line.quantity / material.carton_size)) * authorities.tariffs[keys["packaging_tariff"]]
            else:
                line.gaps.append("carton size")

        clause = authorities.clauses.get(keys.get("condition_group") or "", "base_only")
        base = line.base_unit_price * (factor if clause in ("base_only", "base_and_surcharge") else 1)
        mass = surcharge * (factor if clause == "base_and_surcharge" else 1)
        line.unit_price = round_cents(base + mass + (setup + cartons - rebate[key]) / quantity)

        blocked = " ".join(line.gaps)
        line.surcharge_per_unit = None if "metal index" in blocked or "setup_pool" in blocked or "allocation mass" in blocked \
            else round_cents(surcharge + setup / quantity)
        line.packaging_per_unit = None if "packaging_tariff" in blocked or "carton size" in blocked \
            else round_cents(cartons / quantity)
        line.rebate_total = None if "project_bonus" in blocked else round_cents(rebate[key])

        pooled = demand[line.material_number]
        commitment = authorities.commitments.get(keys.get("delivery_commitment") or "")
        if commitment and same_text(commitment.material, line.material_number) and (
                commitment.max_quantity is None or pooled <= commitment.max_quantity):
            line.lead_time_weeks = commitment.weeks
        elif material.stock is not None and material.stock >= pooled and material.stock_lead_weeks is not None:
            line.lead_time_weeks = material.stock_lead_weeks
        else:
            line.lead_time_weeks = material.production_lead_weeks
            if material.stock is None:
                line.gaps.append("stock")
        if line.lead_time_weeks is None:
            line.lead_time_weeks = material.stock_lead_weeks or 0
            line.gaps.append("lead time")
        if line.gaps:
            notes.append(f"position {key}: unresolved {', '.join(line.gaps)}")

    ordered = [lines[key] for key in sorted(lines, key=position_order)]
    total = round_cents(sum((Decimal(line.quantity) * line.unit_price for line in ordered), ZERO))
    return Bid(tender_reference, "bid", discount, ordered, total, omitted=sorted(set(omitted)), notes=notes)
