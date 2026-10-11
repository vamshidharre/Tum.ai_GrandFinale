# QuoteCraft speed plan — tracking table

Goal: **100 quality in under 5:00 (top 10) at lower cost**, keeping the system agentic (the model chooses where to
go, what to search, what matters; code only helps). Work happens on branch `claude/tier1-speed` in the worktree
`D:\Personal_Projects\QuoteCraft-tier1` (package: `starter-kit/`). One change at a time, one test run each; a run
past 5:00 is cancelled and analysed.

Reference: best scored run `run_a176359f` = 100, **6:15, $0.176** (investigation 219 s, bid form + TAN 155 s).
Leaderboard 06:56 UTC: #1 Plite 0:38 $0.026 · #8 Saltovka 5:28 $0.159 · #9 QuoteCraft 6:15 $0.176.

## Runs

| Run | Commit | Quality | Time | Cost | Investigation | Bid form + TAN | Verdict |
|---|---|---|---|---|---|---|---|
| a176359f (scored) | 2c82a7bf (base) | 100 | 6:15 | $0.176 | 219 s, 155 calls | 155 s, 35 clerk turns | baseline |
| 10def205 (test) | 3fd4d43 | – (looping, cancelled at limit) | >11 min | >$0.37 | 236 s, 364 calls, $0.31 | clerk looped 40+ turns on "Position hinzufügen" | crews + guessed template failed |
| 99f03f3f (test) | e359542 | running | | | | | |

## Changes

Status: ✅ done and verified in a run · 🟨 done, waiting for a run · ❌ tried, made it worse (off) · ⬜ to do

| # | Change | Where | Why (evidence) | Expected gain | Status |
|---|---|---|---|---|---|
| 1 | Click on a slow page load counted as done, never pressed twice (`no_wait_after`) | browser_env.py | a176359f: 78 s login click; local run: every cookie/login click "failed" after 6–30 s | up to 1–2 min in bad runs | 🟨 (local ✅) |
| 2 | Playwright timeouts 6 s action / 15 s navigation; step 20 s; fresh tab at first hard failure | browser_env.py, worker.py | a176359f: 125 s lost on one flaky login | up to 2 min in bad runs | 🟨 |
| 3 | `collect`/`fetch` get 75 s | worker.py | local run: a 4-page collect cut at 20 s | correctness | 🟨 |
| 4 | Verification read reused as next page read | worker.py | one page read per turn saved | ~0.3–1 s/turn | 🟨 |
| 5 | History: no earlier page in full (FULL_TURNS=0), 6 turns, older thoughts capped at 16 | worker.py | uncached input = 88% of cost; 10def205: 7.9k vs 10.6k tokens/call | −25–40% cost | ✅ (tokens/call down) |
| 6 | Sites opened + logged in while the brief is read | agent.py | each agent's first turn was the login | ~5–8 s | 🟨 |
| 7 | Hedged model call (2nd request after 12 s or 4× median) | llm.py | past runs: stuck calls cost 120 s each | up to minutes in bad runs | 🟨 |
| 8 | Clerk `enter_positions` template (code repeats the steps for every position) | submit.py | a176359f: 25 of 35 clerk turns = 5 positions one choice per turn | ~80–100 s | 🟨 (first try ❌, see 9) |
| 9 | Clerk enters the FIRST position by hand, template only for the rest; NOT-saved check after the last press | submit.py | 10def205: template guessed "Position hinzufügen" as the save button → loop | makes 8 safe | 🟨 |
| 10 | Clerk loop breaker: same form fills on same page 3× → refused with a way out | worker.py | 10def205: 40+ identical turns until step cap | prevents 0-score runs | 🟨 |
| 11 | TAN request + ask_mail in one reply; TAN + finalize in one reply | submit.py | 2 clerk turns | ~6 s | 🟨 |
| 12 | Crews: 3 ERP + 2 mail agents in parallel tabs | agent.py | idea: ERP 46 serial turns | — | ❌ 10def205: duplicate work, 2× cost, no time gain → default 1 |
| 13 | End the investigation when no question is open and nothing is being read (don't wait for agents re-checking) | agent.py | 10def205: last question closed ~3:34, end 3:56; tender portal browses docs after done | ~20–40 s | ⬜ |
| 14 | Clerk opens the bid form and fills bidder data DURING the investigation | agent.py, submit.py | a176359f: first 4 clerk turns ~15 s after the bid is ready | ~15 s | ⬜ |
| 15 | Stop re-filing loops: an "already filed" keep twice → agent told to finish | agent.py | 10def205: customer portal re-filed requirements 12× | cost | ⬜ |
| 16 | ERP agent must not search before requirements exist (idle instead) | agent.py prompt | 10def205: 16 turns of "Hydraulikzylinder >20 results" | cost | ⬜ |
| 17 | Read-only pages fetched over HTTP with the browser session instead of full render (model still picks the URL) | browser_env.py | each goto+settle 1–3 s | ~20–40 s | ⬜ (risky on unseen sites) |

## Rules for every change

- No value from the public scenario in code, prompts or notes (scan the diff before each run).
- The model decides navigation, searches and relevance; helpers only execute what it chose.
- Run the unit tests (`uv run python -m unittest discover tests`) before a test run; one test run at a time.
- Record every run in the Runs table with its verdict, and every change's status here.
