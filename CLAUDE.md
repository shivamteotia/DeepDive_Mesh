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

**Valid baseline recorded 2026-10-19** (`evals/baselines/baseline.json`), live web search, no
judge, 8 cases run / 2 skipped, models: planner+drafter+synthesizer `gpt-4.1-mini`, verifier
`gpt-4o-mini`, scoring `jev-1.13`. Cost $0.83.

| metric | value | vs frontier run |
|---|---|---|
| `verify.poison_recall` | **1.00** | = |
| `verify.poison_precision` | 0.83 | = |
| `verify.failure_mode_accuracy` | 0.75 | = |
| `ground.hallucinated_citation_rate` | **0.00** | = |
| `ground.unsupported_claim_rate` | 0.00 | = |
| `ground.span_resolves_rate` | **1.00** | = |
| `ground.claim_support_rate` | 0.92 | +0.05 |
| `retrieval.page_content_rate` | 0.89 | +0.04 |
| `retrieval.nonempty_rate` | **1.00** | +0.01 |
| `retrieval.domain_diversity` | 0.76 | +0.07 |
| `verify.loopback_rate` | 0.29 | +0.29 (loop now fires) |
| `verify.abstain_rate` | **0.33** | **-0.34 REGRESSION** |
| `retrieval.recall_at_k` | 0.00 | -0.50 (labelling artefact) |
| `ops.latency` p50/p95 | 153s / 271s | slower |
| claims accepted | 43/44 = 98% | nano was 56% |

**The model question is settled:** `gpt-4.1-mini` beats `gpt-4.1-nano` decisively (98% vs 56%
acceptance, same verifier) at a twelfth of `gpt-5.5`'s price.

### The one serious open defect: question-level grounding

`abstain_rate` fell to 0.33 — **two of three traps now produce confident, fully cited reports
about things that do not exist**:

- `trp-invented-company` asserts "Vantrix Cellworks, a Pune-based battery startup, raised
  $15.5 million in a Series C on June 30, 2020." The company is invented. Earlier runs at
  least noted the mismatch; this one stitches real funding data onto the invented name.
- `trp-unanswerable-counterfactual` asks whether *battery* cell prices would have fallen
  faster without PLI. It returns a report on **solar** cells — wrong domain, and it answers a
  factual question in place of the counterfactual.

**Why per-claim verification cannot catch either:** every claim genuinely is supported by the
passage it cites, so `hallucinated_citation_rate` reads a perfect 0.00 while the report as a
whole is fabricated. The failure is at the question level — nothing checks that the retrieved
evidence is about the entity, domain and question actually asked.

**The fix** is one `/v1/evaluate` call after research and before drafting: "do these passages
describe the entity and question asked?" Below a threshold, abstain. Cheap, uses a primitive
already in `mesh.py`, and it is the highest-value change left — `abstain_rate` is a gate and
"knowing when to stop" is a judged criterion.

Secondary: `recall_at_k 0.00` is the known labelling artefact. The misses are reuters.com (x3),
niti.gov.in and nrel.gov — domains the seeds named plausibly that retrieval never returns.
Relabel from what actually comes back, or drop the metric from the gate set.

### Submission state

- Public repo: https://github.com/shivamteotia/DeepDive_Mesh — pushed, `.env` excluded, verified
  no key-shaped strings in the tree.
- Registered: `Submission_Token` is in `.env`.
- **STILL NEEDED:** `workflow.yaml` from the portal into `.github/workflows/`, plus repo
  secrets `MESH_API_KEY` and `SUBMISSION_TOKEN`. Without it the submission is never collected.
- Document upload is built but has never been run with a real file. `doc202685944701.pdf`
  ("Roads Reimagined: The Rise of Electric Vehicles Ecosystem", Aug 2026) sits untracked in the
  project root and would serve as both the corpus fixture and the upload test.

### Cost, measured

| | cost | recurs? |
|---|---|---|
| this baseline pass (8 cases) | $0.83 | yes |
| of which web search, 80 searches | $0.826 | yes |
| of which all tokens (914k prompt) | ~$0.37 | yes |

Search remains the dominant cost. `ops.websearch.cost_usd_est` is validated against real
billing to 0.5%. A MeshAPI key carries a **spend cap as well as a balance** and the cap bites
first — ours silently blocked everything at $5.00 while the balance read $1.18.

### Known gaps

Replay mode needs `--from-plan`: the planner rephrases sub-queries between runs, so the fixture
cache misses and replay cannot currently gate. Golden seeds unlabelled.
`retrieval.precision_at_k` needs relevance labels. Corpus fixture missing. `cost_usd` is `None`
on chat responses, so token cost needs `GET /v1/usage/events`.
