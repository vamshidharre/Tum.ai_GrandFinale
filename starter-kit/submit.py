"""The last stage: an agent enters the calculated bid in the tender portal, signs it and reads the receipt.

The calculator has fixed every number. `submit_bid` hands them to a model as a bid sheet and lets it drive whatever
bid form the portal has: where the form is, what each step wants, which notation a field takes, how a position is
left out and when the signing code has to be requested are all read from the pages. A second agent, on the webmail,
fetches that code when the first one asks for it. Nothing here knows a field name or the order of the steps.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from decimal import Decimal

import llm
import sanity
from browser_env import Tab
from calculator import Bid
from preflight import Report, check as check_review
from worker import ACTIONS_HELP, CONTROL, Agent, brief_for

WRAP_UP = 150  # seconds before the deadline at which entering stops and signing starts: the code takes a while
MAIL_BUDGET = 120  # seconds one request to the mailbox may take
CLERK_STEPS, COURIER_STEPS = 120, 25
TURNS_RESERVE = 25  # clerk turns kept for leaving out what is not entered, signing and submitting (run 15a3aaf9)
# The clerk's hint when it goes in circles (run d3ca14df: 95 turns of "fill the position, add it, empty again" until
# its step cap, never signed). The brief's rule 9: every bid form lets a position be left out explicitly, and a
# signed bid with one position left out keeps the others' points; an unsigned one scores nothing.
# Run 8a2e1426: the same loop on one position, then the clerk left out that one AND every position after it. So the
# hint first sends it back to the start of the stuck position (a configurator that got out of step is reset by doing
# it again from its first choice, as in every clean run), and leaving out is for that one position only.
CLERK_CIRCLE_HINT = (
    "You are going in circles: the same steps keep repeating without progress. Read the page for an error or hint "
    "next to the fields and fix exactly that. If the form seems out of step (fields empty or locked, \"not possible "
    "in this step\"), start this position over: reload the page or go back to its first choice, then redo every "
    "choice with its apply button and all amounts in one \"steps\" action, as for the positions before. Only if that "
    "fails too, leave out THIS ONE position with the form's explicit way of omitting a position. Every position "
    "after it has its values on the bid sheet: enter each of them normally, then sign. A signed bid keeps the points "
    "of every position it has; an unsigned one scores nothing.")
# Leaving out a priced position costs its points, so the clerk may do it only for a position it is stuck on: once at
# the start, and once more each time it is caught going in circles again (run 012496d3: a single allowance for the
# whole run made a clerk stuck on a second position loop on the refused omit). The positions the calculator left out
# and a short time are always allowed. Matched on the omit control's label in a few languages.
OMIT_CONTROL = re.compile(r"auslass|weglass|omit|leave out|skip position|nicht anbieten", re.I)
# How often the final step is held back because the page does not show the bid (preflight "unproven"). The wait is
# bounded: a signed bid scores, an unsigned one does not.
UNPROVEN_REFUSALS = 2
# Entering a bid is reading a form and copying values: without the model's long reasoning a turn takes about half
# the time, and the reply's "thought" still leaves it room to check a conversion.
CLERK_THINKS, COURIER_THINKS = False, False

# The clerk needs the brief's part on how to submit, not its bid rules (the bid sheet carries their result): the
# sections whose heading names the submission, plus the rule lines on leaving positions out. A brief without such a
# heading is kept whole (credentials removed either way). CONTEXT_GATES=0 brings back the whole brief.
SUBMISSION_HEADING = re.compile(r"(?im)^(#{1,6})[^\n]*(?:submission|abgabe|einreichung)[^\n]*$")
OMIT_RULE = re.compile(r"(?i)partial submission|teilabgabe|teilpunkte|\bomit|auslass|weglass")


def clerk_brief(brief: str) -> str:
    text = brief_for(brief)
    if os.environ.get("CONTEXT_GATES", "1").strip() == "0":
        return text
    spans: list[tuple[int, int]] = []
    for heading in SUBMISSION_HEADING.finditer(text):
        if any(start <= heading.start() < end for start, end in spans):
            continue  # a sub-heading of a section already taken
        level = len(heading.group(1))
        after = re.compile(rf"(?m)^#{{1,{level}}}\s").search(text, heading.end())
        spans.append((heading.start(), after.start() if after else len(text)))
    if not spans:
        return text
    taken = "\n\n".join(text[start:end].strip() for start, end in spans)
    rules = [line.strip() for line in text.splitlines() if OMIT_RULE.search(line) and line.strip() not in taken]
    return taken + ("\n\nRules on leaving positions out:\n" + "\n".join(rules) if rules else "")

CLERK = """You are the bid clerk of a manufacturer's sales engineering team. Your colleagues have researched a
public tender and a calculator has priced it: the BID SHEET below is the final result. You work the tender portal
"{site}" at {url} in your own browser tab, and your whole job is to get exactly this bid into the portal's bid
form, sign it, submit it and read the confirmation. You do not research and you do not recalculate.

TASK BRIEF (background on what a valid submission is; the values to enter come from the bid sheet)
{brief}
END OF BRIEF

BIDDER
{bidder}

BID SHEET
{sheet}

Reading the bid sheet
- "decision" is "bid" or "no_bid". A no_bid has no positions and a total of zero: take the form's way of
  declining, and still sign and submit it.
- "positions" has one entry per tender position that is offered. Enter each under its own position number.
- "material" describes the offered product, for forms that make you pick or configure it: choose what equals
  these values.
- Amounts are net EUR, written here with a decimal point and without thousands separators. A field may expect
  another notation (a decimal comma, a unit, whole numbers for weeks and pieces): follow its hints, placeholder
  and error messages. Change the notation only, never the value.
- null means the value is not known: leave that field empty. 0 or 0.00 is a known zero: enter it.
- "omitted_positions" could not be priced. Never invent values for them: use the form's explicit way of leaving
  a position out, if it has one.
- Save turns: once you have seen how one step of the form works (which fields, which button applies a choice or
  moves on), do a whole step, for example one position with all its choices and amounts, in one "steps" action,
  using the labels exactly as the page shows them. If it stops part-way, read the page and carry on from there.
  This includes a configurator where each choice must be applied before the next one appears: put every choice
  and its apply button, in order, into the same "steps" action, then the amounts and the button that moves on.
  You know every value from the bid; you do not need to see each list first.
- Enter the first position with one "steps" action: every choice with its apply button, all amounts and the
  control that moves on to the next position. When the form moves on, code enters the remaining positions with
  the same steps and their own values from the bid sheet, and tells you what it entered.
- If that did not happen (the report says so), enter the next position yourself, then let code enter the others:
  {{"do": "fill_positions", "done": ["<position you entered>"], "template": [<the steps that worked for it, every
  value replaced by a placeholder>]}}. Placeholders are the bid sheet's names in braces: {{quantity}},
  {{unit_price|de}} (decimal comma), {{lead_time_weeks}}, {{material_number}}, {{bore_mm}}, {{stroke_mm}},
  {{mounting}}, {{certification|none=<the form's option for no certificate>}} and so on; end the template with
  the control that moves on to the next position. It enters every further position the form shows, each with its
  own values, and stops where the form does not accept one: then finish that position yourself.
- A position is entered once. When the form saves it, it may list it, show a short confirmation and empty its
  fields for a further line: then go on with the control that moves to the next position or step, and do not
  enter the same position again.

{actions}
One more action:
  {{"do": "ask_mail", "task": "<what you need and how to recognise it>"}}
      A colleague who has the bidder's mailbox open in another browser looks for it and answers. This is how you
      get the signing code (TAN) the portal sends by mail. Ask only after you have triggered the code in the
      portal, and pass on what you know about it (the tender reference, that you requested it just now, anything
      the portal displayed about it): the mailbox also holds older codes that no longer work.
  {{"do": "check_submission"}}
      Read the current review page and produce a no-model checklist of visible errors, unfilled required controls,
      tender reference, offered positions and total. Run this immediately before the irreversible final step.
      BLOCKER is something the page shows to be wrong. NOT SHOWN is something a review must show and this page
      does not: a page that does not show the bid proves nothing about what the portal holds.
  {{"do": "finalize", "id": 12}}
      Press the final submit/confirm control only after check_submission reported READY. Never use ordinary click for
      the irreversible final submit control.

How to work
- Start from the page you are on and find the bid form of tender {reference}. Log in again if the portal asks.
- A bid form asks, in an order of its own, for things like the bidder's details, bid or no bid, the positions,
  the commercial terms, declarations, a review and a signature. Read each page and give it what it asks for,
  from the bidder data and the bid sheet. Fill every field the sheet has a value for, optional ones included.
- Read the form's own instructions: when entries are saved, what going back does, which fields are required.
  Do the fields of a page in one reply, then the control that saves them or moves on.
- After each step look at the result. If the form reports an error or did not keep a value, correct that field
  before you move on.
- Declarations: read what each one says. Give the confirmations a valid bid needs; leave unticked, or untick,
  options that would alter the bid or have nothing to do with it.
- On the review page compare what the form lists with the bid sheet: reference, decision, every position, the
  discount and the total. Go back and correct differences while there is time.
- Signing: trigger the code in the portal, get it with ask_mail, enter it and submit. A submission is final.
  If the portal rejects a code, request a fresh one instead of trying the same again.
- On the final review page, call check_submission. If it is BLOCKED, correct the reported issue and check again.
  Use finalize for the final irreversible confirmation only after it reports READY.
- You are finished only when the portal confirms that the bid was received. Then finish with done and put the
  confirmation (its receipt number or code, else its wording) in "answer". If you cannot submit, finish with
  done without "answer" and say why in "summary".
- Watch the time. A bid that is entered but not submitted scores nothing; a submitted bid with positions left
  out keeps the points of the positions it holds. When the head of a turn says time is short, do as it says.
"""

COURIER = """You have the sales mailbox of a bidding company open in the webmail "{site}" at {url}, in your own
browser tab. A colleague is entering the company's bid for tender {reference} in the tender portal right now and
asks you for something from the mailbox, usually the one-time signing code (TAN) the portal has just mailed.

{actions}

How to work
- The request heads every turn, together with the answers you gave earlier in this run.
- Codes expire. The mailbox can hold older codes, codes for other tenders whose references look alike, and
  unrelated mail with similar subjects. The one that counts is the newest message received for exactly this request.
  Open the message itself and check its reference, sender and time before you answer: a row in a list is not enough.
- Judge a message by where it is and by its header (folder, sender, date, subject, request or sequence number), NOT by
  labels inside its text. Messages can contain decoy sections, such as a local preview, a draft copy or a quoted
  block marked "not sent", that say nothing about the message itself: a code mail in the inbox from the portal is a
  received message even if such a section inside it says otherwise. When several code mails match, the one with the
  highest request or sequence number is the newest; take its code without waiting for a "better" one.
- Lists can be paged, sorted either way and split over folders, and new mail may only show after a reload. If the
  message is not there yet, reload and look again a few times.
- Log in again if the webmail asks.
- Finish with done and put ONLY the value asked for in "answer": a code exactly as written, nothing around it.
  If you cannot find it, finish with done without "answer" and say in "summary" what you saw instead.
"""


PLACEHOLDER = re.compile(r"\{(\w+)(?:\|([^}]*))?\}")


def fill_template(template: list, line: dict) -> list:
    """One position's steps from the template the clerk wrote after entering a position by hand (fill_positions): every
    {name} becomes that bid-sheet line's value (its own fields and its material's), "{name|de}" with a decimal comma,
    "{name|none=<text>}" with <text> when the value is null. A field step whose value is null and has no none= text
    is left out: an unknown value leaves its field empty, as the bid sheet says."""
    values = {**(line.get("material") or {}), **{k: v for k, v in line.items() if k != "material"}}
    steps = []
    for step in template:
        if not isinstance(step, dict):
            continue
        if "press" in step:
            steps.append({"press": PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), str(step["press"]))})
            continue
        raw, skip = step.get("value", ""), False

        def put(match):
            nonlocal skip
            name, option = match.group(1), match.group(2) or ""
            if name not in values:
                return match.group(0)
            value = values[name]
            if value is None:
                if option.startswith("none="):
                    return option[len("none="):]
                skip = True
                return ""
            text = str(value)
            return text.replace(".", ",") if option == "de" else text

        value = PLACEHOLDER.sub(put, raw) if isinstance(raw, str) else raw
        if not skip:
            steps.append({**step, "value": value})
    return steps


def as_steps(action: dict, page: str) -> list:
    """A browser action as label-addressed steps (for the automatic form replay): a choice or a field by the label
    its id has on the page it was taken on, a click by its control's label, a steps action as it is."""
    do = action.get("do")
    if do == "steps":
        return [step for step in action.get("steps") or [] if isinstance(step, dict)]
    aid = str(action.get("id", "")).strip("[] ")
    label = next((m.group(3) for m in CONTROL.finditer(page) if m.group(1) == aid), None)
    if not label:
        return []
    if do == "click":
        return [{"press": label}]
    if do == "fill":
        return [{"field": label, "value": str(action.get("text", ""))}]
    if do in ("select", "set"):
        return [{"field": label, "value": action.get("value")}]
    return []


def _number_forms(value) -> set[str]:
    """How a bid-sheet value may have been typed: as it is, with a decimal comma, with German thousands separators."""
    text = str(value)
    forms = {text}
    try:
        number = Decimal(text)
    except Exception:  # noqa: BLE001  not a number
        return forms
    if "." in text:
        forms.add(text.replace(".", ","))
        forms.add(f"{number:,.2f}".replace(",", " ").replace(".", ",").replace(" ", "."))
    return forms


def templatize(steps: list, line: dict) -> tuple[list, list[tuple[int, str, list[str]]]]:
    """The template of one entered position (automatic form replay): every field value that equals one of this
    position's bid-sheet values becomes its placeholder ({quantity}, {unit_price|de}, ...). The one value the sheet
    does not have, typed for a position whose certification is null, is the form's word for "no certificate".
    Returns the template and the ambiguous fields (index, label, candidate placeholders): a value that equals two
    fields (0.00 for surcharge and rebate) is not guessed here (resolve_template asks the model)."""
    values = {**(line.get("material") or {}), **{k: v for k, v in line.items() if k not in ("material", "position")}}
    forms: dict[str, set[str]] = {}
    for key, value in values.items():
        if value is None:
            continue
        for form in _number_forms(value):
            forms.setdefault(form.strip().lower(), set()).add("{" + key + ("|de" if "," in form else "") + "}")
    template, ambiguous, unmatched = [], [], []
    for step in steps:
        if not isinstance(step, dict):
            continue
        value = step.get("value")
        if "press" in step or not isinstance(value, str):
            template.append(step)
            continue
        found = sorted(forms.get(value.strip().lower(), set()))
        keys = {h.replace("|de", "") for h in found}
        if len(keys) > 1:
            ambiguous.append((len(template), str(step.get("field", "")), found))
            template.append(step)
        elif keys:
            template.append({**step, "value": found[0]})
        else:
            unmatched.append(len(template))
            template.append(step)
    if len(unmatched) == 1 and "certification" in values and values["certification"] is None:
        index = unmatched[0]
        template[index] = {**template[index], "value": "{certification|none=" + template[index]["value"] + "}"}
    return template, ambiguous


async def resolve_template(template: list, ambiguous: list[tuple[int, str, list[str]]]) -> list | None:
    """Fields whose value matched several bid-sheet fields: the model names, from the field's label, which one it
    takes (one short call). None when it cannot."""
    if not ambiguous:
        return template
    fields = [{"field": label, "candidates": candidates} for _, label, candidates in ambiguous]
    reply = await llm.chat_json([
        {"role": "system", "content": "You map bid form fields to bid sheet values. Answer only with JSON."},
        {"role": "user", "content": "For each form field, choose the one bid sheet placeholder it takes, from its "
                                    "candidates, by what the field's label says. Answer "
                                    '{"choices": [{"field": "<label>", "placeholder": "<one candidate>"}]}\n'
                                    + json.dumps(fields, ensure_ascii=False)}], think=False, max_tokens=800,
        label="submit:template")
    chosen = {str(c.get("field")): str(c.get("placeholder")) for c in reply.get("choices") or [] if isinstance(c, dict)}
    resolved = list(template)
    for index, label, candidates in ambiguous:
        if chosen.get(label) not in candidates:
            return None
        resolved[index] = {**resolved[index], "value": chosen[label]}
    return resolved


def shown_position(page: str, candidates: list[str], every: list[str] | None = None) -> str | None:
    """The bid position the form shows now, if it is one of `candidates`: the position named in a heading, else the
    one named first next to the word "Position"/"Pos.". A heading naming a position that is not a candidate (already
    entered) means none: never another position's values into that one's form. A number inside another word (an
    option like "M-2") is not a position."""
    every = every or candidates

    def first(text: str, labelled: bool) -> str | None:
        prefix = r"(?:position|pos\.?)\s*" if labelled else r"(?<![\w.\-/])"
        found = [(m.start(), p) for p in every
                 for m in [re.search(prefix + rf"{re.escape(p)}(?![\w.\-/])", text, re.I)] if m]
        return min(found)[1] if found else None
    headings = "\n".join(line for line in page.splitlines() if line.lstrip().startswith("#") or "title:" in line)
    named = first(headings, True) or first(page, True) or first(headings, False)  # "Schritt 3 von 8" is no position
    return named if named in candidates else None


def _amount(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def bid_sheet(bid: Bid) -> dict:
    """The bid as the clerk gets it: the answer's fields with exact amounts as text, the positions left out, and per
    position the offered material's attributes for forms that have the bidder configure the product."""
    positions = []
    for line in bid.lines:
        material = line.material
        positions.append({
            "position": line.position, "material_number": line.material_number,
            "material": None if material is None else {
                "bore_mm": material.bore_mm, "stroke_mm": material.stroke_mm, "pressure_bar": material.pressure_bar,
                "mounting": material.mounting, "certification": material.certification},
            "quantity": line.quantity, "unit_price": _amount(line.unit_price),
            "lead_time_weeks": line.lead_time_weeks, "base_unit_price": _amount(line.base_unit_price),
            "surcharge_per_unit": _amount(line.surcharge_per_unit),
            "packaging_per_unit": _amount(line.packaging_per_unit), "rebate_total": _amount(line.rebate_total)})
    discount = Decimal(bid.discount_percent)
    return {"tender_reference": bid.tender_reference, "decision": bid.decision, "currency": "EUR",
            "discount_percent": int(discount) if discount == discount.to_integral_value() else float(discount),
            "positions": positions, "omitted_positions": list(bid.omitted), "total_net": _amount(bid.total_net)}


async def submit_bid(tender_tab: Tab, mail_tab: Tab | None, bid: Bid, context: dict, brief: str,
                     deadline: float) -> dict:
    """Have an agent enter, sign and submit `bid` in the tender portal before `deadline` (a time.time() value).

    `context` holds tender_reference, scenario_date, company, vat_id and email. Returns {"submitted", "receipt",
    "notes"}: `receipt` is the portal's confirmation as the agent read it, empty when there was none.
    """
    reference = str(context.get("tender_reference") or bid.tender_reference)
    guard = sanity.TypingGuard() if sanity.enabled("typing_guard") else None
    codes: list[str] = []
    final = {"clicked": False, "page": ""}  # set by finalize once the binding submit button was pressed  # what the mailbox handed out so far: a used code must not be handed out again
    review: Report | None = None
    held_back = 0  # final steps refused because the page did not show the bid

    # The courier gets a fresh tab in the mailbox's browser (same session), opened now in the background while the
    # bid form is filled: the investigators' tab can be left half-navigated by the deadline that stopped them, and
    # a cold mailbox costs minutes when the signing code is waiting (sandbox test run 54c78c38).
    courier_tab: Tab | None = None
    warming = None
    if mail_tab is not None:
        try:
            page = await mail_tab.page.context.new_page()
            courier_tab = Tab(page, {"siteId": mail_tab.site_id, "url": mail_tab.base_url,
                                     "credentials": mail_tab.credentials})
            warming = asyncio.ensure_future(courier_tab.goto(courier_tab.base_url + "/"))
        except Exception:  # noqa: BLE001  no second tab: fall back to the investigators' one
            courier_tab = mail_tab

    # The control that requested the signing code: clicking it again before a code came back voids the code that is
    # on its way (sandbox run a85be021 lost its signature this way). Recorded when the clerk first asks the mailbox.
    code_request = {"label": None, "searches": 0, "last_click": None}
    replayed: set[str] = set()  # positions fill_positions entered
    omits = {"count": 0, "hints": 0}  # omit controls pressed, and the clerk's circle hints at the last one

    def _label_of(aid) -> str | None:
        user = clerk.turns[-1]["user"] if clerk.turns else ""
        match = re.search(rf'\[{re.escape(str(aid))}\] [a-z ]*?"([^"]+)"', user)
        return match.group(1).strip() if match else None

    def clerk_guard(action: dict) -> str | None:
        veto = guard(action) if guard is not None else None
        if veto:
            return veto
        pressed = None
        if action.get("do") == "click":  # its label on the page as last read (the ids of this turn)
            pressed = next((m.group(3) for m in CONTROL.finditer(str(getattr(tender_tab, "document", "") or ""))
                            if m.group(1) == str(action.get("id", "")).strip("[] ")), None)
        if pressed and refused.get(pressed, 0) >= 2:
            # Run 63f3be2e: "Position hinzufügen" pressed 10 times, refused each time ("not possible in this step").
            return (f'blocked: the form refused "{pressed}" twice in this step. It is not the way on here: use the '
                    "control that saves this step and moves on to the next one.")
        # Bounded: after two searches that found nothing the code may really be lost, and a fresh request is allowed.
        label = _label_of(action.get("id")) if action.get("do") == "click" else None
        if (sanity.enabled("single_code_request") and label and code_request["label"]
                and not codes and code_request["searches"] < 2 and label == code_request["label"]):
            return (f'blocked: "{code_request["label"]}" was already used and no code has come back yet. A new '
                    "request makes the code already on its way invalid. Ask the mailbox colleague again (ask_mail) "
                    "and say exactly what to look for: the newest code mail for this tender.")
        if label and bid.decision == "bid" and OMIT_CONTROL.search(label):
            stuck_again = clerk.circle_hints > omits["hints"]  # caught going in circles since the last omit
            # A position the form adds that is not on the bid sheet (run 9ac58b69: an extra form position) is left
            # out as it should be: the cap is only for a page that shows one of the bid's priced positions.
            page = str(getattr(tender_tab, "document", "") or "")
            priced_here = any(re.search(rf"(?<![\w.]){re.escape(str(line.position))}(?![\w.])", page)
                              for line in bid.lines)
            if (priced_here and omits["count"] >= len(bid.omitted) + 1 and not stuck_again
                    and deadline - time.time() >= WRAP_UP and not turns_short()):
                return (f'blocked: "{label}" would leave out a position that has its values on the bid sheet. Only '
                        "the one position you were stuck on may be left out. Enter this position with its values "
                        "from the bid sheet (all choices with their apply buttons and the amounts in one \"steps\" "
                        "action), then move on.")
            omits["count"] += 1
            omits["hints"] = clerk.circle_hints
        if label:
            code_request["last_click"] = label  # read from the page the clerk acted on, before the click happens
        return None

    async def ask_mail(action: dict) -> str:
        if code_request["label"] is None:
            code_request["label"] = code_request["last_click"]  # the control that asked for the code
        code_request["searches"] += 1
        task = str(action.get("task") or "").strip()
        if not task:
            return 'ask_mail needs a "task": say what you need from the mailbox and how to recognise it'
        if courier_tab is None:
            return "there is no mailbox in this run, so nothing can be fetched by mail"
        if warming is not None and not warming.done():
            try:
                await asyncio.wait_for(asyncio.shield(warming), 20)
            except Exception:  # noqa: BLE001  the courier opens the page itself then
                pass

        def request() -> str:
            text = f"REQUEST FROM YOUR COLLEAGUE AT THE TENDER PORTAL\n{task}"
            if codes:
                text += ("\n\nANSWERS YOU GAVE EARLIER IN THIS RUN (they are used up: a new request needs a newer "
                         "message)\n" + "\n".join(f"- {code}" for code in codes))
            return text

        system = COURIER.format(site=courier_tab.site_id, url=courier_tab.base_url, reference=reference,
                                actions=ACTIONS_HELP)
        courier = Agent("courier", courier_tab, system, request, deadline=min(deadline, time.time() + MAIL_BUDGET),
                        max_steps=COURIER_STEPS, think=COURIER_THINKS, finish_on_repeat=3)
        outcome = await courier.run()
        answer = str(outcome.get("answer") or "").strip()
        if not answer:
            return ("the mailbox colleague found nothing: " + (outcome.get("summary") or "no result in the time given")
                    + ". Do NOT request a new code yet: a new request makes the current one invalid and costs minutes. "
                    "Ask the colleague again first, saying what to look for (the newest code mail for this tender).")
        codes.append(answer)
        if guard is not None:
            guard.allow(answer)  # read from the mailbox during the run: the clerk may type it
        return f'the mailbox colleague answers: "{answer}"' + (f' ({outcome["summary"]})' if outcome.get("summary") else "")

    async def fill_positions(action: dict) -> str:
        """Replays the template of a position the clerk entered by hand for every position the form shows next, with
        that position's values from the bid sheet (runs 15a3aaf9, 49f7be51: 35-100 clerk turns on the positions, one
        choice or field per turn, and loops). Stops at the first chain that does not run cleanly."""
        template = action.get("template") or []
        if not isinstance(template, list) or not template:
            return 'fill_positions needs "template": the steps of one position, with {placeholders} for its values'
        lines = {str(line["position"]): line for line in sheet["positions"]}
        replayed.update(str(p).strip() for p in action.get("done") or [])  # entered by hand: never again
        report = []
        for _ in range(len(lines)):
            if time.time() > deadline - WRAP_UP:
                report.append("stopped: time is short, sign what is entered")
                break
            remaining = [p for p in lines if p not in replayed]
            position = shown_position(await tender_tab.observe(), remaining, list(lines))
            if position is None:
                report.append("no further bid position is shown on the form: continue with the form's next step")
                break
            result = await clerk._steps(fill_template(template, lines[position]))
            report.append(f"position {position}: {result[:300]}")
            # Progress is the form moving on to another position (a notice after the last press may be a
            # confirmation); still on the same position means it was not accepted.
            now = shown_position(await tender_tab.observe(), list(lines), list(lines))
            if now == position or not (result.startswith("ran all") or "page says" in result):
                report.append("stopped there: the form did not move on; read the page and finish this position yourself")
                break
            replayed.add(position)
        return "fill_positions:\n" + "\n".join(report)

    async def look() -> Report:
        report = check_review(await tender_tab.observe(), bid, reference)
        if report.unproven and not sanity.enabled("summary_check"):  # switched off: missing evidence only warns
            report = Report(not report.blockers, report.blockers, report.warnings + report.unproven, report.checks)
        return report

    async def check_submission(action: dict) -> str:
        nonlocal review
        review = await look()
        return review.text()

    async def finalize(action: dict) -> str:
        nonlocal review, held_back
        # Navigation, expiry and server validation can invalidate an earlier review, so inspect it again.
        review = await look()
        if review.blockers:
            return review.text() + "\nFinal submission was not pressed."
        if review.unproven:
            # The page does not show the bid, so pressing here would confirm something nobody compared. The clerk
            # is sent to a page that does show it, a bounded number of times and never when time is short.
            short = deadline - time.time() < WRAP_UP
            if held_back < UNPROVEN_REFUSALS and not short:
                held_back += 1
                return review.text() + "\nFinal submission was not pressed."
            print("[submit] final step pressed without full review evidence ("
                  + ("time is short" if short else f"held back {held_back} time(s)") + "): "
                  + " | ".join(review.unproven), flush=True)
        result = await tender_tab.click(action.get("id"))
        # Submitting ends the run and with it the run token: every later model call fails ("expired/unknown run
        # token" in sandbox runs 6da03229, 8184b178, 3c3568bb). So the confirmation is read here, in code, and the
        # clerk stops without another model turn.
        try:
            final["page"] = await asyncio.wait_for(tender_tab.observe(), 20)
        except Exception:  # noqa: BLE001
            final["page"] = ""
        final["clicked"] = True
        clerk.deadline = 0  # ends the agent loop before its next model call
        return result

    def heading() -> str:
        left = int(deadline - time.time())
        text = f"YOUR JOB: enter, sign and submit the bid for tender {reference}, then read the confirmation."
        # The position the form shows now, with its own values (run 63f3be2e: the clerk assumed the form starts with
        # the first position of the sheet and typed another position's values into it).
        every = [str(line["position"]) for line in sheet["positions"]]
        shown = shown_position(str(getattr(tender_tab, "document", "") or ""), every, every)
        if shown is not None:
            line = next(l for l in sheet["positions"] if str(l["position"]) == shown)
            text += (f"\nTHE FORM SHOWS POSITION {shown} NOW. Its values from the bid sheet: "
                     + json.dumps(line, ensure_ascii=False))
        if codes:
            text += "\nCodes from the mailbox so far, oldest first: " + ", ".join(codes)
        if left < WRAP_UP:
            text += (f"\nTIME IS SHORT ({left} s). Enter no further positions: leave out the ones not entered yet if "
                     "the form allows it, go straight to the signature, fetch the code and submit what is entered.")
        elif turns_short():
            # Run 15a3aaf9: a four-step cycle on one position (choose, fill, add, "not possible", continue, "complete
            # the configuration") ran the clerk into its step cap before the time ran short: unsigned, 0 points.
            text += (f"\nYOUR TURNS ARE RUNNING OUT ({CLERK_STEPS - clerk.steps} left). Enter no further positions: "
                     "leave out the position you are on and the ones not entered yet if the form allows it, go "
                     "straight to the signature, fetch the code and submit what is entered.")
        return text

    def turns_short() -> bool:
        return CLERK_STEPS - clerk.steps <= TURNS_RESERVE

    bidder = {"company": context.get("company"), "vat_id": context.get("vat_id"), "email": context.get("email")}
    sheet = bid_sheet(bid)
    system = CLERK.format(site=tender_tab.site_id, url=tender_tab.base_url, brief=clerk_brief(brief), reference=reference,
                          bidder=json.dumps(bidder, ensure_ascii=False, indent=1),
                          sheet=json.dumps(sheet, ensure_ascii=False, indent=1), actions=ACTIONS_HELP)
    if sanity.enabled("no_guessed_mailbox") and not bidder.get("email"):
        system += ("\nTHE BIDDER'S E-MAIL ADDRESS IS NOT KNOWN YET. If the form asks for it, get it with ask_mail (the "
                   "mailbox colleague can read the address of the mailbox it has open) and type exactly that. Never "
                   "make up an address, not even a plausible one.\n")
    if guard is not None:
        guard.allow(sheet)
        guard.allow(bidder)
        guard.allow(reference)
    clerk = Agent("submit", tender_tab, system, heading,
                  extra={"ask_mail": ask_mail, "check_submission": check_submission, "finalize": finalize,
                         "fill_positions": fill_positions}, deadline=deadline,
                  max_steps=CLERK_STEPS, think=CLERK_THINKS, guard=clerk_guard,
                  circle_hint=CLERK_CIRCLE_HINT)
    chains: dict[str, list] = {}  # steps the clerk ran while the form showed each position (automatic replay)
    refused: dict[str, int] = {}  # button label -> times the form answered it with a new alert, in this step
    form_step = {"now": None}  # (page title, shown position) of the step the refusals belong to

    async def on_action(action: dict, before: str, result: str) -> str | None:
        """Automatic form replay, whatever the clerk's style (run 472732d2: single selects and clicks, no steps):
        every action taken while the form shows a bid position is recorded as a label-addressed step; when the form
        moves on to another bid position, that position's steps become the template (templatize; the model resolves
        only ambiguous fields) and code enters every further position (fill_positions)."""
        every = [str(line["position"]) for line in sheet["positions"]]
        # the page after a click (this runs before the worker's own re-read); other actions do not change the step
        after = await tender_tab.observe() if action.get("do") == "click" else str(getattr(tender_tab, "document", "") or "")

        def step_of(page: str) -> tuple:
            heading = next((l.strip() for l in page.splitlines() if l.lstrip().startswith("#")), "")
            return heading, shown_position(page, every, every)

        step_before, step_now = step_of(before), step_of(after)
        if step_before != form_step["now"]:  # refusals belong to the step they happened in
            form_step["now"] = step_before
            refused.clear()
        if action.get("do") == "click" and step_now == step_before and "(alert)" in after:
            # pressed, still on the same step, and the page announces an alert: the form refused it
            label = next((s["press"] for s in as_steps(action, before) if "press" in s), None)
            if label:
                refused[label] = refused.get(label, 0) + 1
        elif step_now != step_before:  # the form moved on
            form_step["now"] = step_now
            refused.clear()
        position = shown_position(before, [p for p in every if p not in replayed], every)
        if position is None or time.time() > deadline - WRAP_UP:
            return None
        chains.setdefault(position, []).extend(as_steps(action, before))
        if action.get("do") not in ("click", "steps"):
            return None
        following = [p for p in every if p not in replayed and p != position]
        if shown_position(await tender_tab.observe(), following, every) is None:
            return None  # still on this position (or past the positions): nothing to replay yet
        replayed.add(position)
        template, ambiguous = templatize(chains[position], next(l for l in sheet["positions"] if str(l["position"]) == position))
        template = await resolve_template(template, ambiguous)
        if template is None:
            return ("The next positions are not entered automatically (a field could not be matched to the bid "
                    "sheet): enter the next one, then use fill_positions.")
        line = json.dumps(template, ensure_ascii=False)[:400]
        print(f"[submit] automatic form replay after position {position}: {line}", flush=True)
        report = await fill_positions({"template": template})
        return (f"Position {position} is in. The following positions were entered automatically with the same steps "
                "and their own values:\n" + report)

    clerk.on_action = on_action
    outcome = await clerk.run()
    receipt = str(outcome.get("answer") or "").strip()
    notes = str(outcome.get("summary") or "")
    if final["clicked"]:  # submitted: the confirmation was read in code, no model turn after the click
        receipt = receipt or final["page"][:1500]
        return {"submitted": True, "receipt": receipt, "notes": notes or "submitted; confirmation read without the model"}
    if not outcome.get("done"):
        notes = (notes + " " if notes else "") + f"stopped after {outcome.get('steps', 0)} steps without a confirmation"
    return {"submitted": bool(outcome.get("done") and receipt), "receipt": receipt, "notes": notes}
