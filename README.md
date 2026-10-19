# DeepDive

**A self-verifying research analyst.** Ask it a question; it investigates across the live web
and your own documents, drafts findings as individually checkable claims, checks every claim
against the source it cites, sends the weak ones back for more evidence, and only then writes
the report.

Built for **Agent Arena 2026** (Krish Naik Academy × MeshAPI), DeepDive track. Every LLM call
routes through MeshAPI.

---

## The idea

A research agent that sounds confident is easy. One that can tell you *which* of its claims it
could not substantiate — and drops them — is the hard part. DeepDive is built around that:

- **Claims, not prose.** The drafter emits discrete claims, each citing passage ids. A
  paragraph cannot be verified; a claim can.
- **Verification is cheap and deterministic first.** A citation pointing at something that was
  never retrieved is a fabrication — caught in code, for free, before any model is consulted.
  No model's opinion can overturn it.
- **Confidence is calibrated, not self-reported.** Per-claim support comes from MeshAPI's
  `/v1/evaluate` as a probability, not from asking a chat model how sure it feels.
- **The drafter never grades itself.** Drafter and verifier are always different models;
  startup fails loudly if they match.
- **Abstention is a valid answer.** A question with no answerable evidence base returns
  "insufficient evidence" rather than a plausible report.

## The pipeline

```
question
   ↓
Planner        decomposes into sub-questions — or refuses the question outright
   ↓
Researcher     web search (primary, always runs) + your uploaded documents
   ↓
Drafter        discrete claims, each citing passage ids
   ↓
Verifier       deterministic citation checks, then a calibrated probability per claim
   ↓   ↑
   │   └────── loops back when claims are under-evidenced (not when they are simply wrong)
   ↓
Synthesizer    the report, from accepted claims only
```

Rejected claims do not quietly soften into hedged prose. They are dropped, and the audit trail
says which, with what confidence, and why.

## Running it

```bash
pip install -r backend/requirements.txt
cp .env.example .env            # add your MeshAPI rsk_ key
python -m backend.selfcheck     # validates every model id against the live catalogue
uvicorn backend.app:app --reload
```

Open <http://localhost:8000>.

| endpoint | |
|---|---|
| `GET /` | the UI |
| `POST /api/research` | `{question, corpus_file_ids?}` → report + audit |
| `GET /api/research/stream` | the same run as SSE, one event per agent action |
| `POST /api/corpus` | upload a document into MeshAPI RAG |
| `GET /api/runs` · `/api/runs/{id}` | past runs with their full trace |

## The interface

Three tabs — **Report**, **Audit trail** (every claim with verdict, confidence, reason and
sources), **Trace & cost** (every MeshAPI call with model, tokens, latency and request id) —
plus a live **Agent activity** panel showing each agent action as it happens.

That panel is driven from `Mesh._record`, the single function every MeshAPI call passes
through. Nothing in the system can act without appearing there, which is what makes the audit
trail worth trusting.

## Evaluation

`evals/` is a real harness, not a demo. See [evals/README.md](evals/README.md).

The part worth looking at is **`evals/fixtures/poisoned/draft-v1.json`**: a hand-written draft
with ten claims, four of them false, each false in a different way — a figure contradicting its
own source, a citation to a real-but-irrelevant source, a citation to a URL never retrieved,
and a true statement attributed to the wrong source. It is fed straight to the verifier with
the rest of the pipeline bypassed.

Without it, "we have a self-correction loop" is a claim about the code. With it, it is a
number — and a verifier that rubber-stamps everything scores zero on it.

```bash
python evals/test_compare.py         # verdict logic, no API, no spend
python evals/test_fixtures.py        # the poisoned fixture's own invariants
python -m evals.run --mode live --baseline
python -m evals.compare              # PASS=0  FAIL=1  REVIEW=2
```

Web search is non-reproducible, so anything that **gates** runs against frozen fixtures in
replay mode. A live-mode candidate has its gates downgraded to guardrails automatically: the
web moving is not a regression in our code.

## Document questions

Upload a PDF and ask what it says. Two rules the system holds to:

- **A document question is answered from the document, or not at all.** Asked what an
  uploaded report said about lithium imports — a word it never contains — an earlier build
  returned an accurate, well-cited account assembled from sixteen web pages. Nothing in it
  was false and no citation was fabricated, which is precisely why nothing downstream
  objected. It was still the wrong answer. The system now says the document is silent, and
  points out that web sources do cover the topic if that is what you wanted.
- **Web search still runs for general questions**, and document passages simply join the
  evidence pool. Scope is decided per question, not per upload.

Measured over 8 questions on a real 14-page government EV report, each anchored on facts read
off the document by hand:

| | |
|---|---|
| answered correctly | **8 / 8** |
| `ground.claim_support_rate` | 0.979 |
| `ground.hallucinated_citation_rate` | 0.000 |
| `retrieval.corpus_only_rate` | 0.744 |
| `verify.abstain_rate` (the silent-subject case) | 1.000 |
| cost for the suite | $0.11 |

That includes two cases built to be confusable — four bus numbers in one paragraph, and two
adjacent market forecasts with similar shape — and the system kept both straight.

## What does not work yet

Stated plainly, because a research tool that hides its own limitations has missed the point.

- **Entity resolution.** Asked about an invented company whose name resembles a real one, the
  system researches the real one. Every individual claim is correctly supported by its source;
  the failure is that nothing checks the sources are about the entity the question names.
  Our own trap case catches this and it is not yet fixed.
- **Source authority is unmeasured.** Retrieval sometimes returns YouTube and Reddit alongside
  the IEA. Diversity is measured; authority is not.
- **The golden set's must-find domains are unlabelled** — named plausibly, not verified against
  what retrieval actually returns.
- `retrieval.precision_at_k` needs per-result relevance labels, so it is not emitted at all
  rather than estimated.
- **Document retrieval ranking is weak**, and this is upstream of us. Querying the gateway's
  RAG index with an exact phrase from the document can return an unrelated chunk at score
  1.000. We compensate by over-retrieving (`CORPUS_TOP_K=15`) and letting the verifier discard
  what does not support a claim, but the scores themselves cannot be trusted for ranking.
- Replay mode needs `--from-plan` to be robust: when the planner rephrases a sub-query, the
  fixture cache misses.

## Costs, measured

| | |
|---|---|
| Web search (TinyFish with page content) | ~$0.0100 per search |
| A full 10-case eval pass | ~$1.40, of which ~$0.76 is search |

With cheap models, **search is ~95% of spend** — 205 searches cost $2.05 while all tokens cost
$0.07. The cost estimator in the harness was validated against real billing to 0.5%.

Note that a MeshAPI key carries a **spend cap as well as a balance**, and the cap bites first.

## Layout

```
backend/
  config.py        model ids per agent role, all overridable from .env
  schemas.py       Passage, Claim, Run, TraceStep + strict JSON schemas
  mesh.py          the only code that talks to MeshAPI (stdlib only)
  agents.py        plan / research / draft / verify / synthesize
  orchestrator.py  the loop, and audit()
  app.py           FastAPI
  selfcheck.py     validates model ids against the live catalogue
frontend/index.html   the whole UI, one file, no build step
evals/                harness, golden set, poisoned fixture
```
