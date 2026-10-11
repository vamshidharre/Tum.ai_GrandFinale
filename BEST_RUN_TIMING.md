# Best run `run_a176359f` — where the 6:15 goes

Package: `starter-kit/` = exactly the package of this run (2c82a7bf, verified file by file).
Log: `logs (2).jsonl` (same as the API download). Times are on the run page's clock (mm:ss).
Result: quality 100, **6:15, $0.176**, 192 model calls (Qwen, thinking off except the first call).

## 1. Timeline (critical path)

| # | Part | From → to | Time | Model calls | Method (code) | What happened |
|---|---|---|---|---|---|---|
| A | Start + read the brief | 00:00 → 00:08 | **8 s** | 1 | `casefile.read_context`: one Qwen call **with thinking on** | Tender ref, date, senders read from the brief; every agent waits for it |
| B | Investigation (4 agents in parallel, one browser each) | 00:08 → 03:41 | **213 s** | 154 (112 agent calls + 42 extractions) | `agent.solve` → `worker.Agent.run` per site: read page as text → Qwen picks actions → Playwright runs them → page re-read; `keep` → `casefile._extract` (Qwen) | Ends when the LAST agent finishes = the tender portal (see B1) |
| B1 | ↳ tender portal: broken login | 00:08 → 02:16 | **128 s lost** | 2 | `browser_env.Tab.click` (2.5 s click, then `el.click()` fallback with Playwright's 30 s default) → `worker` 45 s page read → reload fails → `Tab.renew` only after the 2nd failure | Login click hung 78 s, next page read hung 45 s; a fresh tab then loaded at once |
| B2 | ↳ tender portal: real work | 02:16 → 02:59 | **43 s** | 11 turns | login, cookie wall, open tender, slider gate, `collect` Q&A (6 pages), `keep`, documents tab, gate, `keep` positions | Normal speed: ~4 s per turn |
| B3 | ↳ tender portal: extra browsing after its facts were filed | 02:59 → 03:40 | **41 s** | 5 turns | model re-opens Positionsübersicht / Leistungsbeschreibung; `Tab.back` 15 s timeout | 23 s `back` timeout + 8 s slow read + re-checks; nothing new filed |
| B4 | ↳ ERP (not on the critical path here, next in line) | 00:08 → 02:48 | 160 s | 45 turns | search catalogue group → open material → planning tab → `keep`; then VK13 prices ×5; then MMBE stock (3 clicks) ×5 | Would be the bottleneck once B1 is fixed |
| B5 | ↳ mail | 00:08 → 01:58 | 110 s | 25 turns | searches, archive paging, slider gates, `keep` | Done early |
| B6 | ↳ customer portal | 00:08 → 01:28 | 80 s | 23 turns | documents register, `collect`, spec revisions, `keep` | Requirements on file at 00:53 |
| C | Calculate the bid | 03:41 | **<1 s** | 0 | `calculator.calculate_bid` (Decimal code) | — |
| D | Bid form: open it, bidder data, bid type | 03:41 → 03:56 | **15 s** | 4 | `submit.submit_bid` clerk agent (`steps`, fill, select, click) | Normal |
| D1 | ↳ form order confusion | 03:56 → 04:02 | 6 s | 2 | model clicks "Zurück" / "Weiter" | Form starts at position 01.04; model thought it must start at 01.01 |
| E | **Bid form: 5 positions** | 04:02 → 05:52 | **110 s** | 23 | clerk, `steps` action: **one configurator choice + "Auswahl übernehmen" per turn** (4 turns), then 1 turn for catalogue no. + amounts + "Weiter" | ≈ 22 s per position. Only the first position used one long chain |
| F | Conditions, declarations, review, request TAN | 05:52 → 06:03 | 11 s | 3 | clerk `steps` / set / click | Normal |
| G | Get the TAN from the mailbox | 06:03 → 06:11 | **8 s** | 2 (+1 clerk) | `ask_mail` → courier agent in a fresh mail tab | Normal |
| H | Enter TAN, check, finalize | 06:11 → 06:16 | 5 s | 2 | `preflight.check` + `finalize` (click in code, receipt read without a model call) | Normal |
| | **Total** | | **6:15** | **192** | | |

## 2. Per agent

| Agent | Turns | Model time | Browser action time | Page read time | Finished at | Main kinds of turns |
|---|---|---|---|---|---|---|
| tender-portal | 18 | 31 s | 122 s | 58 s | 03:40 | login 3× **132 s**, navigate 5× 47 s, gates 4× 14 s |
| erp | 45 | 87 s | 58 s | 14 s | 02:48 | navigate 21× 78 s, keep 17× 53 s |
| mail | 25 | 48 s | 45 s | 10 s | 01:58 | keep 7× 33 s, search 6× 23 s, navigate 5× 18 s |
| customer-portal | 23 | 48 s | 25 s | 7 s | 01:28 | navigate 12× 40 s, collect 4× 15 s |
| submit (clerk) | 35 | 58 s | 86 s | 11 s | 06:16 | **form steps 24× 114 s** (23 of them = the 5 positions) |
| courier | 2 | 3 s | 3 s | 1 s | 06:11 | gate 1×, done 1× |

Model calls: p50 1.7 s, of which ~1.1 s is the platform's queue wait (also with only 1 call in flight), so **every
model turn costs ≥ ~3–4 s of wall time** with its browser action. Time is set by the number of turns in a row.

Cost by caller ($0.176 total; uncached input = 88% of it): erp 466k tokens, submit 361k, mail 290k,
customer-portal 223k, tender-portal 205k, extract 82k. Only the ~4.9k-token system prompt is cached on most calls.

## 3. Where the time is lost (ranked)

| Rank | Waste | Seconds | On critical path? | Cause (method) | Fix (smallest safe change) |
|---|---|---|---|---|---|
| 1 | Tender-portal login hang | **128** | yes | `Tab.click`: after a 2.5 s click that started a slow page load, the `el.click()` fallback waits Playwright's 30 s default; then a 45 s page read; tab renewed only after 2 failures | Count a click that started a page load as done (`no_wait_after`, no second click); 6 s Playwright default timeout; fresh tab at the first hard failure; step timeout 20 s |
| 2 | Bid form positions one choice per turn | **~75** of 110 | yes | clerk sends one cascade choice per `steps` turn (5 turns per position) | Let the clerk do position 1 itself, then code repeats the exact steps that worked for the other 4 (template), with a check that each position was saved |
| 3 | Tender portal browsing after its job | **41** | yes | model re-checks documents; `back` 15 s timeout | End the investigation when every question is closed and nothing is being read; shorter `back` timeout |
| 4 | ERP turns one after another | (160 s total) | becomes critical after #1 | 9 turns per material (search, open, tab, keep, prices, stock drill-down) | Batch: `file_each` over the group's candidates with the tabs that hold the facts; later: parallel ERP tab only for a different material (no shared questions) |
| 5 | Form order confusion | 6 | yes | model expected 01.01 first | Prompt: "enter positions in the order the form asks" |
| 6 | Reading the brief with thinking on | ~4 | yes | `read_context(think=True)` | thinking off, retry with thinking if the reference/date is missing |
| 7 | ERP F4 + `goto` timeout | 22 | no | dead link then 20 s `goto` timeout | covered by #1 timeouts |

**If #1, #2 and #3 are fixed:** investigation ≈ bounded by ERP (~02:48 instead of 03:41, −53 s), positions ≈ 35 s
instead of 110 s (−75 s) → about **4:00–4:20 total**, under the 5:00 target, with the same agentic design.

## 4. Round 2 — three fixes on top of the best package (commit after 95f075b)

Built on the exact best package; everything else stays as it was (history, agents, prompts, calculator, review
checks). Lessons from the failed runs kept OUT: shorter history (ERP looped 121 turns), parallel site agents (2x
cost), repeated-action blocker (stopped the mailbox helper on a re-locking inbox), early investigation end.

| Fix | Bottleneck (run a176359f) | Change | Files | Expected |
|---|---|---|---|---|
| #1 | Tender login hang, 128 s | Element timeout 8 s, navigation 15 s; a click that started a slow page load counts as done and is never pressed twice; login success = the password field is gone (else reported after ≤ 8 s); page read capped at 15 s and a fresh tab at once when it fails; a navigation failure with a broken-tab sign (detached, ERR_ABORTED, page unreadable) renews at the first failure; `collect`/`fetch` keep 75 s | browser_env.py, worker.py | −100 s or more in a run with a flaky page; nothing in a clean run |
| #2 | Tender agent browsing after its job, 41 s | The Q&A question closes by itself when the Q&A list was followed to its end by `collect` (≥ 2 pages, not capped) and is read; routine "document read" notices no longer wake an idle agent (only new questions or a problem notice do) | agent.py, casefile.py, worker.py (`held_by`) | −20–40 s when the tender agent is last; fewer "done" turns (cost) |
| #3 | 5 positions, 110 s (one dropdown per turn) | Clerk enters the FIRST position itself, then ONE `enter_positions` call repeats the steps that worked for every other position, in the form's order; a position still shown in the form's heading after the last press is reported NOT saved; error notices stop it; typing guard and the review checklist unchanged | submit.py, worker.py (`_steps` errors_only) | −60–75 s |

Expected total with all three: about 4:00–4:45 (the investigation is then bounded by the ERP at ~2:50).

| Run | Package | Quality | Time | Cost | Notes |
|---|---|---|---|---|---|
| run_20e3c020 | round 2 (#1+#2+#3) | 100 | **7:38** | $0.248 | brief 11 s; investigation 315 s (ERP 92 calls vs 46; tender and mail first clicks hung 35 s each); bid form + TAN 142 s (positions in one chain each, but 11 s of browser time per chain) |

Tests written for each fix (not run locally, at the user's request): `tests/test_speed_fixes.py`,
`tests/test_enter_positions.py`, `tests/test_browser_env.py` (slow click, login success/failure),
`tests/test_worker.py` (fresh tab at the first hard failure).

## 5. Round 3 — move mechanical steps into code (model still decides)

From run 20e3c020 (7:38): the model was not the slow part; the ERP agent took too many turns and our own code was
slow inside each turn. Allowed by the fair-play rules: code fills forms, logs in, pages and files what the model chose.

| # | Change | Files | Expected |
|---|---|---|---|
| 1 | ERP batching: when ≥ 2 record questions are open the ERP agent's briefing says to work ONE record by hand, then file the other candidates of a result list with one `file_each` (links + the tabs/paths learned); plus a one-time nudge after 3 same-kind one-by-one filings | agent.py | −60–120 s, about half the ERP cost |
| 2 | Faster `steps`: the last page read is reused while steps only fill/choose; a press, a moved page or a vanished control reads again (retry once); bounded modal check (3 s per frame) | worker.py, browser_env.py | ~−30 s on the bid form |
| 3 | Open, log in and answer the cookie notice ("necessary only", else decline) by code on all sites while the brief is read; the agent starts with these notes; a refused login is left to the model | agent.py, browser_env.py | ~−8 s and 8 model calls |
| 4 | Read the brief without reasoning; with reasoning only if reference, date or the discount sender is missing | casefile.py | −5–9 s |

| Run | Package | Quality | Time | Cost | Notes |
|---|---|---|---|---|---|
| (to fill) | round 3 | | | | |

## 6. Round 4 — from the round-3 run's live log (pasted 11 Oct)

Round 3 worked at the start (agents begin at 0:03, already logged in; mail used file_each). Still wasted:

| # | Seen in the log | Change | Files |
|---|---|---|---|
| A | ERP used file_each for two tabs only, then did stock (3 clicks) and prices (2 clicks) by hand for every material (~6 turns each) | ERP hint now asks to repeat ALL clicks of the first record in one file_each, with the stock and price chains as `path`s, and shows the exact shape | agent.py (RECORD_BATCH) |
| B | Supplier-portal agent searched its documents ~25 turns for a mail record; tender agent woken 6x by new ERP questions only to say done | Agents other than ERP/mail no longer see the ERP's and mailbox's questions while the run has such agents, until one is released to all | agent.py (questions_for_site) |
| C | ERP re-opened pages it had just filed ("status did not parse") because their facts were still being read | file_each waits up to 10 s for its own filed pages to be read before the agent's next turn | agent.py (file_each_action) |

| Run | Package | Quality | Time | Cost | Notes |
|---|---|---|---|---|---|
| run_665adb0e (SCORED, sub_5e116351) | **round 3** (started 09:04 UTC, before round 4 at 09:09) | 100 | **4:50** | $0.223 | first run under 5 min; cost up: supplier portal 40 calls/472k tokens (looped pages 1-4 for a mail record), ERP 49/566k (stock and prices by hand), tender 18/251k (9 "done" wake-ups) — all three targeted by round 4 |
| run_ab00e578 (test) | rounds 4 + 5 | – | ~7:00 | investigation $0.136 (was $0.200) | tender 10 turns (was 18), supplier portal 12 (was 40), enter_positions entered 4 positions in one turn; BUT investigation 302 s: ERP 53 turns because 3 of 4 file_each filed no stock/price page (label shown twice; a maintenance notice in front of a record) and the model redid them by hand; model slower (2.9 s/call) |
| run_2e081b8d (test) | round 6 | – | **>6:00** | investigation $0.148 | investigation 280 s. ERP batch got through (35 turns, done 4:27), but it was idle until 1:31 waiting for the requirements (sites slow at start: supplier portal opened 22 s late). The **mail agent was the long pole: 50 turns, 607k input tokens (41% of the run), done last at 4:40**, searching one topic at a time and opening each message by hand, plus 4 model turns after all its questions were already closed. Clerk enter_positions: 4 positions in ~28 s (works) |

## 7. Round 5 — cost: no repeated briefing in the history

| Change | Why | Expected |
|---|---|---|
| The previous turn kept in full in the history no longer repeats its case file and open questions (the new message has them as they are now); its page and results stay | ~2k of the ~9k input tokens per call (run 665adb0e); uncached input ≈ 90% of the cost | −20% input tokens per call, no change in what the model sees now |

Expected with rounds 4 + 5 on top of 665adb0e: about 4:15-4:45 and **$0.12-0.15** (cost × time ~30-35 $·s instead of 64.8).

## 8. Round 6 — make the ERP batch actually get through

| Change | Why (run ab00e578) | Files |
|---|---|---|
| `pick_control`: a label shown twice takes the record's own link (its address carries the record's id), or either when both lead to the same address | "path labels were ambiguous": the price/stock links appear in the menu and on the record | agent.py |
| `pass_notice`: a notice page/dialog (notice word in title/heading, no input field) with one "continue/accept" control is pressed by code during file_each, as the model would | "Wartungshinweis … Akzeptieren und fortfahren" appeared in front of records while code walked them | agent.py |
| file_each waits 3 s (was 10 s) for its pages to be read | 4 × 10 s of ERP time when the model was slow | agent.py |

Expected: ERP ~20 turns instead of 53 → investigation ~120-160 s; total ~3:30-4:15.

## 9. Round 7 — cut the mail agent's turns and the turns after the work is done (from run 2e081b8d)

| # | Seen in the log | Change | Files |
|---|---|---|---|
| A | Mail agent made 4 more model turns after its last question was closed; each turn re-sends ~12k tokens | A routed agent (ERP or mail) that has filed something, has nothing of its own still being read and has no open question for its website goes idle **by code**, without a model turn. It still wakes for new or released questions | worker.py (`finished_here`), agent.py |
| B | Mail agent: 50 turns, one search and one hand-opened message per topic (search, unlock list, open, unlock text, file) | Mail briefing (3+ open questions) asks for one broad reference search per folder, unlock the list once, then ONE file_each over the records with the "original text" control as a path | agent.py (MAIL_BATCH) |
| C | ERP's first record was filed only partly before the batch, so the batch could not repeat stock/price chains | ERP hint: work ONE record ALL the way (status, technical, stock down to free quantity, price scales) before the batch | agent.py (RECORD_BATCH) |

Not changed: quality-relevant logic (RFQ reading, calculator, discount/price rules, reconciliation, pre-submit checks).

Expected: mail ~15-20 turns instead of 50 → investigation ~150-200 s, cost about -30%; total ~4:00-4:45.

| Run | Package | Quality | Time | Cost | Notes |
|---|---|---|---|---|---|
| run_ddfa36f5 (SCORED) | **round 7** (3b3832a3) | **100** | **5:10** | **$0.138** | investigation 200 s / $0.119 (was 280 s / $0.148). Mail 27 turns / 314k (was 50 / 607k), done 2:35 (was 4:40). ERP is now the critical path: 38 turns, done 3:19. Bid 109 s |

## 10. Where the 5:10 of run_ddfa36f5 (round 7) goes

| Phase | Run clock | Time | Turns | Notes |
|---|---|---|---|---|
| Brief read (context) | 0:00-0:04 | 4 s | 1 | |
| Investigation, 4 agents in parallel | 0:04-3:20 | 197 s | 89 + 55 extractions | ends when the ERP ends |
| Bid form (clerk) | 3:20-4:51 | 91 s | 15 | |
| TAN from the mailbox (courier) | 4:51-5:05 | 14 s | 5 | robot checks stay with the model |
| Enter TAN + submit | 5:05-5:10 | 4 s | 1 | |

| Agent | Turns | Working | Idle | Input tokens |
|---|---|---|---|---|
| tender-portal | 11 | 0:04-0:48 | - | 111k |
| customer-portal | 13 | 0:04-0:54 (requirements filed 0:42) | - | 118k |
| mail | 27 | 0:04-2:35 | 0:16-0:42 (no questions yet), 1:57-2:16 | 314k |
| **ERP (critical path)** | 38 | 0:04-3:19 | 0:13-0:42 waiting for requirements | 378k |

ERP critical path:

| Stretch | Time | Turns | Waste |
|---|---|---|---|
| Login + access check | 9 s | 2 | - |
| Idle until the requirements (catalogue groups) are read | 30 s | 0 | dependency; could learn the record layout meanwhile |
| Finding the material search (F4 link dead, then goto) | 11 s | 4 | ~7 s |
| First records by hand: HZ-8405 (blocked) + one record ALL the way | 43 s | 14 | needed once |
| **A second record walked fully by hand, then filed AGAIN with file_each** | 45 s | 11 | **~35 s** |
| 3 searches + 3 file_each (5 records) | 68 s | 6 | file_each browser time 44 s (~9 s per record) |

Bid phase:

| Step | Time | Turns | Waste |
|---|---|---|---|
| Reach the bid form | 11 s | 2 | ~5 s |
| Bidder data + offer type | 6 s | 2 | - |
| **First position by hand** | 31 s | 6 | **~20 s** (round 6 did it in 2 turns) |
| 4 positions with enter_positions | 32 s | 1 | browser time 30 s (~7.4 s per position) |
| Conditions, declarations, check, TAN request | 11 s | 4 | - |
| Courier gets the TAN | 14 s | 5 | - |
| TAN + submit | 4 s | 1 | - |

Next candidates (round 8), in order: ERP second by-hand record (~35 s), ERP idle wait (~30 s), clerk first position (~20 s), browser waits in file_each/enter_positions (part of ~75 s).
