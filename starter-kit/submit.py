"""The last stage: an agent enters the calculated bid in the tender portal, signs it and reads the receipt.

The calculator has fixed every number. `submit_bid` hands them to a model as a bid sheet and lets it drive whatever
bid form the portal has: where the form is, what each step wants, which notation a field takes, how a position is
left out and when the signing code has to be requested are all read from the pages. A second agent, on the webmail,
fetches that code when the first one asks for it. Nothing here knows a field name or the order of the steps.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from decimal import Decimal

import sanity
from browser_env import Tab
from calculator import Bid
from preflight import Report, check as check_review
from worker import ACTIONS_HELP, Agent

WRAP_UP = 150  # seconds before the deadline at which entering stops and signing starts: the code takes a while
MAIL_BUDGET = 120  # seconds one request to the mailbox may take
CLERK_STEPS, COURIER_STEPS = 120, 25
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
    omits = {"count": 0, "hints": 0}  # omit controls pressed, and the clerk's circle hints at the last one

    def _label_of(aid) -> str | None:
        user = clerk.turns[-1]["user"] if clerk.turns else ""
        match = re.search(rf'\[{re.escape(str(aid))}\] [a-z ]*?"([^"]+)"', user)
        return match.group(1).strip() if match else None

    def clerk_guard(action: dict) -> str | None:
        veto = guard(action) if guard is not None else None
        if veto:
            return veto
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
                    and deadline - time.time() >= WRAP_UP):
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
        if codes:
            text += "\nCodes from the mailbox so far, oldest first: " + ", ".join(codes)
        if left < WRAP_UP:
            text += (f"\nTIME IS SHORT ({left} s). Enter no further positions: leave out the ones not entered yet if "
                     "the form allows it, go straight to the signature, fetch the code and submit what is entered.")
        return text

    bidder = {"company": context.get("company"), "vat_id": context.get("vat_id"), "email": context.get("email")}
    sheet = bid_sheet(bid)
    system = CLERK.format(site=tender_tab.site_id, url=tender_tab.base_url, brief=brief, reference=reference,
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
                  extra={"ask_mail": ask_mail, "check_submission": check_submission, "finalize": finalize}, deadline=deadline,
                  max_steps=CLERK_STEPS, think=CLERK_THINKS, guard=clerk_guard,
                  circle_hint=CLERK_CIRCLE_HINT)
    outcome = await clerk.run()
    receipt = str(outcome.get("answer") or "").strip()
    notes = str(outcome.get("summary") or "")
    if final["clicked"]:  # submitted: the confirmation was read in code, no model turn after the click
        receipt = receipt or final["page"][:1500]
        return {"submitted": True, "receipt": receipt, "notes": notes or "submitted; confirmation read without the model"}
    if not outcome.get("done"):
        notes = (notes + " " if notes else "") + f"stopped after {outcome.get('steps', 0)} steps without a confirmation"
    return {"submitted": bool(outcome.get("done") and receipt), "receipt": receipt, "notes": notes}
