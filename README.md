# QuoteCraft — from RFQ to Bid

> A model-directed, multi-browser agent that turns a public tender into a reviewed and signed commercial bid.

Built at the **[TUM.ai](https://www.tum-ai.com/) European Hackathon League Grand Finale** in Munich, October 10–11, 2026.

## The event

The EHL Grand Finale brought together more than 300 builders for an intensive weekend of building, competing, and shipping. Teams could choose challenges from event partners including [Atira](https://atira.ai/), [BMW Group](https://www.bmwgroup.com/), and [Tacto](https://www.tacto.ai/), while [Entire](https://entire.io/) provided Continuous Memory and traceability for agent-assisted development.

This repository contains our response to **Atira’s “From RFQ to Bid”** challenge.

## The Atira challenge

The task was to build an agent system for a German hydraulics manufacturer that could prepare a tender response without a person manually operating the websites.

At runtime, the system receives a fresh task brief and credentials for four unfamiliar business systems:

1. A tender portal
2. A supplier portal containing the buyer’s specification
3. An ERP system with product, price, and availability information
4. A webmail inbox used for the signing step

The agent must understand the brief, decide where to navigate, find relevant documents, collect evidence, calculate a commercially correct bid, enter it into the tender portal, and sign it with a one-time code from webmail.

The important constraint: the system must remain genuinely agentic. It cannot rely on hard-coded URLs, fixed click paths, selectors, answers, or public-scenario values. The same package is evaluated against private data and different website layouts.

## Our approach

```text
Task brief
    │
    ├──► Four model-directed browser agents
    │       ├── Tender portal
    │       ├── Supplier portal
    │       ├── ERP
    │       └── Webmail
    │
    ├──► Shared evidence case file
    │       └── Typed facts, provenance, open questions, conflict handling
    │
    ├──► Deterministic commercial calculator
    │       └── Exact Decimal arithmetic and rounding
    │
    └──► Submission and pre-submit review
            └── Live form verification, signing, and recovery safeguards
```

### Why it is robust

- **Model-directed navigation:** the model chooses pages, searches, and documents at runtime instead of following a brittle scripted path.
- **Evidence-first extraction:** pages are retained as evidence and converted into typed facts with source provenance; unproven information is not guessed.
- **Exact commercial calculation:** bid calculations use `Decimal` arithmetic and explicit rounding rules rather than LLM arithmetic.
- **Parallel work:** four browsers investigate independent systems concurrently.
- **Verification and recovery:** every browser action is checked for visible state change; silent failures become recovery signals instead of assumed success.
- **Pre-submit gate:** the final bid is checked against the live portal before irreversible confirmation.

## Repository layout

| Path | Purpose |
| --- | --- |
| [`starter-kit/`](starter-kit/) | The submitted Atira agent package |
| [`starter-kit/agent.py`](starter-kit/agent.py) | Orchestrates investigation, calculation, and submission |
| [`starter-kit/worker.py`](starter-kit/worker.py) | Model-directed browser-agent loop |
| [`starter-kit/casefile.py`](starter-kit/casefile.py) | Evidence, typed facts, provenance, and open questions |
| [`starter-kit/calculator.py`](starter-kit/calculator.py) | Deterministic tender-rule and pricing calculations |
| [`starter-kit/submit.py`](starter-kit/submit.py) | Model-directed bid entry and signing |
| [`starter-kit/tests/`](starter-kit/tests/) | Unit, integration, and reliability tests |
| [`output/linkedin/`](output/linkedin/) | Run-time improvement graphic and its reproducible generator |

For a deeper technical walkthrough, see the [`starter-kit` README](starter-kit/README.md).

## Run locally

The package uses Python and [`uv`](https://docs.astral.sh/uv/). From the agent package directory:

```sh
cd starter-kit
uv sync
uv run python -m unittest discover tests
```

Atira provides the temporary credentials, browser endpoints, and model proxy during test and scored runs. To exercise the package in Atira’s public test environment:

```sh
cd starter-kit
atira test-run . --challenge rfq
```

See the [Atira challenge platform](https://ehl-challenge.atira.ai/) for the official task details and fair-play rules.

## Challenge principles

- No scenario-specific values, credentials, prices, or one-time codes are hard-coded.
- An LLM decides navigation targets, searches, and document selection at runtime.
- Deterministic helpers are used where precision matters: parsing, pagination, validation, and commercial arithmetic.
- The sandbox provides up to four browsers; the agent does not launch additional browser processes.
- Ranking prioritizes eligible submissions, then quality, then the cost × time trade-off.

## Development traceability

This project uses [Entire](https://entire.io/) to capture agent sessions and link implementation decisions to Git checkpoints. It gave the team a continuous record of experiments, fixes, and reliability improvements during the hackathon.

---

Made at the TUM.ai EHL Grand Finale • Challenge: [Atira — From RFQ to Bid](https://ehl-challenge.atira.ai/)
