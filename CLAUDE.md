# DeepDive — Agent Arena 2026

A self-verifying research analyst: a multi-agent system that investigates a question
across many sources and produces a **structured, cited report where every claim has been
checked against its source**.

Read [meshapi.md](meshapi.md) before writing any code that calls a model. It holds the
endpoints, the model IDs and the gotchas.

## The hackathon

| | |
|---|---|
| Event | **Agent Arena 2026**, Krish Naik Academy × MeshAPI |
| Portal | https://career.krishnaik.in/dashboard/hackathons?h=agent-arena-2026 (login-gated) |
| Briefing | https://youtu.be/pkmirpBCLAo (17 min, by Divesh Jadhwani) |
| Our track | **DeepDive** — one of three (the others: FinPilot, CreatorOS). Pick ONE; we picked this. |
| Team | Solo or up to 3 |

### Hard rules — breaking any of these voids the submission
1. **Every LLM/AI call routes through MeshAPI.** No direct OpenAI/Anthropic/Google calls.
2. **Backend must be Python** — Flask or FastAPI.
3. **A public GitHub repo IS the submission.**
4. **No hardcoded secrets.** `.env` only. Also: no hardcoded outputs — a human team
   reviews submissions and validates outputs personally.
5. `workflow.yaml` from the portal's Problem Statements tab goes in `.github/workflows/`.
   It runs on push and gates the submission.
6. GitHub repo secrets must contain **`MESH_API_KEY`** and **`SUBMISSION_TOKEN`** (the
   token comes from registering on the portal and is our unique submission ID).

### What judges score
- **Multi-agent orchestration** — distinct agents, not one giant prompt.
- **A real self-correction loop** — the system checks or improves its own work.
- **An auditable decision trail** — *why* did the agent do what it did.
- **Our own test set with measured numbers.**
- Clean code, real data ingestion, reproducible results.

> Deadlines, prize amounts and the exact `workflow.yaml` are only visible on the logged-in
> portal. Ask the user for them rather than guessing — they are not in this file yet.

## The system we are building

The reference workflow shown in the briefing:

```
question
   ↓
Planner        — decomposes into sub-questions, decides what evidence is needed
   ↓
Researcher(s)  — fan out over web search + the uploaded document corpus
   ↓
Drafter        — writes the report, each claim tagged with its source
   ↓
Verifier       — checks every claim against its cited source; accepts, rejects, or
                 re-ranks; LOOPS BACK to Researcher when evidence is thin
   ↓
Synthesizer    — final cited report, exportable as PDF
```

Alongside it, always: a **run trace** (every stage, every token, every MeshAPI request id)
and an **audit trail** (which claims were accepted, which rejected, where the loop-backs
happened and why).

Example question to keep working against:
*"What are the main risks of investing in Indian EV battery startups in 2026?"*

### Non-negotiable product surfaces
- Document upload → ingested into MeshAPI RAG, searchable.
- Live agent-by-agent progress while the run executes.
- Audit trail view: accepted / rejected claims, loop-backs, per-claim confidence.
- Token + cost log per stage, pulled from MeshAPI usage endpoints.
- PDF export of the final report.

## Conventions

- Python backend, FastAPI unless the user says Flask.
- Secrets from `.env` via `os.environ`. The key is `Mesh_AI_API_Key` (note the casing —
  it is already in `.env`). In CI it is the `MESH_API_KEY` repo secret; read both.
- One MeshAPI client wrapper — [backend/mesh.py](backend/mesh.py). Every call through it,
  tagged `{agent, run_id}` so usage events slice by agent, with `X-Request-Id` on the trace
  row. Nothing may bypass it, or the audit trail stops being trustworthy.
- **BYOK is configured** (our own OpenAI key on the Mesh dashboard), so prefer OpenAI models:
  they bill to that key, not the Mesh balance. BYOK fallback is silent — watch
  `byok_fallback_steps` in a run's totals. See [meshapi.md §5](meshapi.md).
- Agent outputs are **typed** — JSON schema via `response_format`, or tool-calling on
  models where `supports_structured_output` is False. Never parse prose.
- Verifier confidence comes from `POST /v1/evaluate` (`typesafe/jev-1.13`), not from
  asking a chat model "how confident are you".
- Never verify a claim with the same model that drafted it.
- Model IDs live in one config module, never inline. Read `GET /v1/models` at startup to
  fail loudly if one was disabled.
- Test set: see [evals/README.md](evals/README.md) for the harness design and
  [evals/golden.json](evals/golden.json) for the cases. Web search runs in **replay mode**
  against frozen fixtures for anything that gates; live mode never gates. Report real
  numbers — judges want measured, not claimed.

## Layout

```
backend/
  config.py        model ids per agent role + runtime switches, all from .env
  schemas.py       Passage, Claim, Run, TraceStep + the strict json_schemas
  mesh.py          the only thing that talks to MeshAPI (stdlib only, so evals can import it)
  agents.py        plan / research / draft / verify / synthesize — one function each
  orchestrator.py  the loop, and audit() — the decision trail judges read
  app.py           FastAPI: / (UI), /api/research, /api/research/stream (SSE), /api/corpus, /api/runs
  selfcheck.py     validates every model id against the live catalogue
frontend/
  index.html       the whole UI: one static file, no build step, no dependencies
evals/             harness design, golden set, poisoned fixture — see evals/README.md
runs/              one JSON per run: full trace + audit (gitignored)
```

## UI

Served at `/` by the backend. Question box, document upload, the five-stage pipeline, and
three tabs: Report, Audit trail (per-claim verdict, confidence, reason, sources) and
Trace & cost (every call with its model, tokens, latency and MeshAPI request id).

**Web search is primary and always runs**; uploaded documents are searched *in addition* to
it, never instead — enforced in `agents.research()`, stated in the UI.

The right-hand **Agent activity** panel shows every agent action live, one entry per MeshAPI
call, colour-coded by agent, with the search query for retrieval steps and a red entry if a
call falls back off the BYOK key. It is driven by a `step` SSE event emitted from
`Mesh._record` — the single choke point every call passes through, which is why nothing can
act without appearing there.

PDF export is `window.print()` against a print stylesheet, deliberately: a server-side
renderer would be a dependency for one button.

## Commands

```bash
pip install -r backend/requirements.txt
python -m backend.selfcheck --offline     # schemas only, no network, no spend
python -m backend.selfcheck               # key, catalogue, balance, one chat + one evaluate
uvicorn backend.app:app --reload

python evals/test_compare.py              # verdict logic, no API, no spend
python evals/test_fixtures.py             # poisoned-fixture invariants
python -m evals.run --mode live --baseline   # first run: records web-search fixtures
python -m evals.run --mode replay            # thereafter -> baselines/candidate.json
python -m evals.compare                      # PASS=0  FAIL=1  REVIEW=2
```

On Windows set `PYTHONIOENCODING=utf-8` before printing a report to the console — reports
contain ₹ and curly quotes, which crash cp1252.

## Status

Pipeline, UI and eval harness all run. First full baseline (8 cases run, 2 skipped, live
web search, no judge):

| | |
|---|---|
| `verify.poison_recall` / `poison_precision` | **1.00** / 0.83 |
| `verify.failure_mode_accuracy` | 0.75 |
| `ground.hallucinated_citation_rate` | **0.00** across 69 claims / 88 citations |
| `ground.unsupported_claim_rate` / `span_resolves_rate` | 0.00 / **1.00** |
| `ground.claim_support_rate` | 0.87 |
| `retrieval.page_content_rate` / `nonempty_rate` | 0.85 / 0.99 |
| `retrieval.recall_at_k` | 0.50 — see below |
| `retrieval.domain_diversity` | 0.69 |
| `verify.abstain_rate` | 0.67 — the Vantrix defect |
| `verify.loopback_rate` | **0.00** — see below |
| `ops.latency` p50 / p95 | 161s / 232s |
| cost | ~$3.90 for the pass ($0.69 of it web search) |

**Three findings from it:**

1. **The loop never fires.** `loopback_rate` is 0.00 across every case. `_needs_more()` only
   triggers on a `needs_evidence` verdict, which `verify()` only assigns for
   `irrelevant_source` / `wrong_attribution` — and real drafts produce neither. The
   self-correction loop is wired and tested but has never run on live data. This is the
   headline judging criterion, so it needs widening before submission.
2. **`recall_at_k 0.50` is a labelling artefact, not a retrieval failure.** Both misses are
   `reuters.com`, which never appears in any result. The seeds named it plausibly, not
   verifiably. What *does* come back includes youtube.com, reddit.com and facebook.com — so
   source authority is a real gap that `domain_diversity` does not capture.
3. **The Vantrix trap resolves to a different real company** (cellworks.life, crunchbase,
   biospace). Entity resolution, as described below.

Eval harness runs. `evals/run.py` measures every metric in [evals/README.md](evals/README.md);
`evals/compare.py` gives the PASS/REVIEW/FAIL verdict and downgrades gates to guardrails on a
live-mode candidate.

**Known defect the harness found:** `trp-invented-company` does not abstain. "Vantrix" is a
real company (Montreal, media software), so the researcher found it and the drafter wrote a
report on its 2010 Series C — correctly noting it is not a Pune battery startup, but writing
the report anyway. Claim-level verification cannot catch this: every claim *is* supported by
its source. The failure is at the question level — nothing checks that the retrieved sources
are about the entity the question names. Needs an entity-grounding step, and the fixture needs
a name with no real homonym.

### Cheap-model run, 2026-10-16 — the loop fires, but quality fell

`loopbacks 1` on all four research and conflict cases (capped at 1 for budget). The
self-correction loop went from never running to running every time.

The cost shows up in acceptance:

| | frontier | cheap |
|---|---|---|
| claims accepted | ~95% (96/99) | **56% (20/36)** |
| claims per case | 10–24 | 8–10 |
| searches per case | 12–15 | 13–19 |

Two causes are confounded here and one run cannot separate them: the cheap drafter writes
weaker claims, *and* the loop now re-drafts, so the final verify judges a second draft. Hold
one constant before concluding which.

**There is currently no valid baseline.** `baselines/baseline.json` still holds the
all-abstained garbage from the killed gpt-4o-mini run. The cheap run that would have replaced
it died on its last case.

**Why it died: the key has a $5.00 spend cap, and total spend hit $5.0075** — HTTP 402
`spend_limit_exceeded`. That is the binding limit, not the balance (which still showed $1.18).
Raise the cap on the Mesh dashboard before the next run. Nothing will run until you do.

Where the $5.01 went, from the dashboard:

| | cost | recurs? |
|---|---|---|
| `gpt-5.5` (old drafter + synthesizer) | $2.544 | no |
| `tinyfish-search`, 205 searches @ ~$0.0100 | $2.053 | **yes** |
| `gpt-5.6-terra` (old planner) | $0.336 | no |
| everything on the cheap set combined | **$0.073** | yes |

**With cheap models, web search is ~95% of cost.** A full 10-case live pass is ~76 searches
≈ $0.76 against ~$0.06 of tokens, so the levers are replay mode and `MAX_SUBQUESTIONS` — not
the model picker. 191 fixtures are now recorded, which is what makes replay runs nearly free.

`ops.websearch.cost_usd_est` is validated: it predicted $2.063 against $2.053 billed, 0.5%
error. Trust it.

The harness now survives this: a `MeshError` on one case records that case as skipped and the
pass still writes a snapshot, and a 402 stops cleanly with the remaining cases marked. It used
to lose every completed case.

Not done: golden seeds unlabelled, corpus fixture missing,
`retrieval.precision_at_k` needs relevance labels, `--from-report`/`--from-plan` unimplemented,
not registered on the portal, `workflow.yaml` not downloaded, `cost_usd` comes back `None` from
chat responses so real cost needs `GET /v1/usage/events` (we estimate web-search cost from the
documented rates instead).
