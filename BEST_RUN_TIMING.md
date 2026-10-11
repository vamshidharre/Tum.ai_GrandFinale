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

## 4. Order to apply (one change per test run)

| Step | Change | Expected | Run id | Result |
|---|---|---|---|---|
| 1 | #1 click/timeout/fresh-tab fix only | removes the 1–2 min hangs on flaky pages | | |
| 2 | #2 positions template after the first position | −~80 s | | |
| 3 | #3 end investigation when complete | −~20–40 s | | |
| 4 | #6 read the brief without thinking | −~4 s (more when the model is busy) | | |
| 5 | #4 ERP batching | −~30–60 s once ERP is the bottleneck | | |
