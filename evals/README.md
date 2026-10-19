# DeepDive eval harness — design

Our own test set with measured numbers. This is one of the five things judges score, and the
only one we can't fake.

Structurally descended from the Disha HR harness (snapshot → baseline → verdict, gate /
guardrail / info), but the subject is different, so almost everything inside changed:

| Disha | DeepDive | Why |
|---|---|---|
| unit of truth = one **answer** | unit of truth = one **claim** | a report has ~30 claims; failures are per claim, not per question |
| gates protect against **PII leakage** | gates protect against **fabrication** | our failure mode is a confident unsourced claim, not a data leak |
| `truth_sql` against a local DB | **citation resolves to a retrieved passage** | our free, never-flaky deterministic check |
| `det / quality / safety / ops` | `retrieval / ground / verify / report / ops` | five things can break independently |
| routes: policy, data, safety, scope | kinds: research, corpus, conflict, trap, poisoned | what varies in research questions |
| judge on Groq | **judge on MeshAPI** | hackathon rule: every LLM call routes through MeshAPI |

## The one structural decision: replay vs live

The live web is not reproducible. A score drop means the web changed, our query changed, or
our code changed — and we can't tell which. So web search runs in two modes.

**`WEBSEARCH=replay`** — every `/v1/web/search` response is cached to
`fixtures/websearch/<sha256(query+params)>.json`, `page_content` included. Replays
forever. **A cache miss in replay mode is a hard failure**, never a fallthrough to live:
otherwise a drifted query silently goes online and we lose determinism without noticing.
This mode gates. CI runs it. It is free and fast.

**`WEBSEARCH=live`** — nightly, and once before submission. Everything it produces is
`guardrail` or `info`, never a gate. It answers the one thing replay can't: does this still
work against today's web.

The planner generates the sub-queries, so planner drift misses every cache entry. Planner
output is frozen as a fixture too, and `--from-plan` scores retrieval against it.

## Case kinds

Each golden case declares a `kind`; the kind decides which metrics run and which fields are
required. See `golden.json`.

| kind | What it tests | Required fields |
|---|---|---|
| `research` | the normal path end to end | `question`, `must_find_domains`, `must_contain_claims`, `forbidden_claims` |
| `corpus` | answerable **only** from uploaded documents — must not drift to the web | `question`, `corpus_files`, `must_contain_claims` |
| `conflict` | sources genuinely disagree — the report must surface the disagreement, not silently pick one | `question`, `conflicting_on`, `must_find_domains` |
| `trap` | no good evidence exists (future figures, invented entity) — correct output is **"insufficient evidence"** | `question`, `abstain_expected: true` |
| `poisoned` | the verifier in isolation: a hand-written draft with known-false claims | `draft`, `false_claim_ids` |

`poisoned` is the important one. It is not a question — it feeds a fixed draft straight to the
verifier. Without it, a verifier that accepts everything scores perfectly on citation support.
It is the only case kind that measures whether the self-correction loop does anything.

## Metrics

Flat dotted ids, same as Disha, so the compare step stays dumb.

### `retrieval.*` — did we find the sources
| id | kind | tol | notes |
|---|---|---|---|
| `retrieval.recall_at_k` | gate (replay) | 0 | vs `must_find_domains`. **Match on domain, not URL** — URLs rot, domains don't. Strongest signal, cheapest to label |
| `retrieval.domain_hit_rate` | info | — | mean *fraction* of must-find domains hit: the softer signal behind the pass/fail gate |
| `retrieval.precision_at_k` | guardrail | 0.05 | **not emitted yet** — needs per-result relevance labels the golden set does not carry. A made-up number would be worse than none |
| `retrieval.nonempty_rate` | gate | 0 | catches total silent failure |
| `retrieval.page_content_rate` | gate | 0 | **the TinyFish trap** — if this drops we are reasoning on snippets, not pages, and nothing errors. See meshapi.md §4 |
| `retrieval.truncated_rate` | info | — | `page_content_truncated` share |
| `retrieval.domain_diversity` | guardrail | 0.05 | distinct domains / results. Five citations off one blog is weak even if each is "relevant" |
| `retrieval.corpus_only_rate` | gate | 0 | `corpus` cases: share of citations from the uploaded files rather than the web |

### `ground.*` — do claims trace to sources (deterministic, free, never flaky)
| id | kind | tol | notes |
|---|---|---|---|
| `ground.hallucinated_citation_rate` | gate | 0 | cited URL was never in the retrieval set. Worst failure mode, costs nothing to detect. **Lower is better** |
| `ground.unsupported_claim_rate` | gate | 0 | claims carrying no citation at all. Lower is better |
| `ground.span_resolves_rate` | gate | 0 | every citation resolves to a real passage span in the retrieved text — our `truth_sql` equivalent |
| `ground.claim_support_rate` | guardrail | 0.05 | does the cited passage *actually* support the claim. `/v1/evaluate`, `noul` question, thresholded |
| `ground.forbidden_claim_rate` | gate | 0 | share of cases whose report asserts one of the case's `forbidden_claims`. `/v1/evaluate`, so mentioning one in order to reject it does not count. **Lower is better** |

### `verify.*` — does the self-correction loop work
| id | kind | tol | notes |
|---|---|---|---|
| `verify.poison_recall` | gate | 0 | share of known-false claims the verifier rejected. **The headline number for this track** |
| `verify.poison_precision` | guardrail | 0.05 | true claims it did *not* wrongly reject — a verifier that rejects everything scores 1.0 on recall |
| `verify.failure_mode_accuracy` | guardrail | 0.05 | of the false claims it caught, how many it labelled with the *right* failure mode |
| `verify.abstain_rate` | gate | 0 | `trap` cases only. A system that never abstains is the #1 research-agent failure |
| `verify.conflict_surfaced_rate` | guardrail | 0.05 | `conflict` cases: did the report state that sources disagree |
| `verify.loopback_rate` | info | — | how often the verifier sent work back. Not better high or low; it explains the other numbers |

### `report.*` — judged quality of the final report (DeepEval, judge on MeshAPI)
| id | kind | tol | notes |
|---|---|---|---|
| `report.faithfulness.avg_score` | guardrail | 0.05 | |
| `report.answer_relevancy.avg_score` | guardrail | 0.05 | |
| `report.contextual_precision.avg_score` | guardrail | 0.05 | |
| `report.contextual_recall.avg_score` | guardrail | 0.05 | |
| `report.coverage_geval.avg_score` | guardrail | 0.05 | share of `must_contain_claims` addressed |
| `report.toxicity.avg_score` | gate | 0.02 | every case. 1 = clean |
| `report.*.n` | info | — | case counts |

### `ops.*`
| id | kind | tol | notes |
|---|---|---|---|
| `ops.websearch.calls_per_run` | guardrail | 25% rel | cost discipline is a judging criterion |
| `ops.websearch.cost_usd_est` | guardrail | 25% rel | **computed, not reported**: $0.005 per search on the pinned engine plus $0.001 per returned result carrying page text. **Validated against real billing on 2026-10-17: predicted $2.063 across 205 searches, dashboard said $2.053 — 0.5% error.** Chat responses carry no cost field, so token cost still needs `GET /v1/usage/events` |
| `ops.tokens.prompt_total` / `.completion_total` | guardrail | 25% rel | token volume, in place of a token cost we cannot yet price |
| `ops.byok_fallback_steps` | guardrail | 0 | steps where MeshAPI fell back off our OpenAI key onto its own credentials. Any rise means we started spending Mesh balance. **Lower is better** |
| `ops.latency.p95_ms` | guardrail | 25% rel | |
| `ops.latency.p50_ms` | info | — | |
| `ops.reliability.success_rate` | guardrail | 0 | |
| `ops.websearch.engine_mix` | info | — | the `provider` field. A shift means failover is firing and per-call cost moved $0.005 → $0.008 |

Note the direction flip: `ground.hallucinated_citation_rate`, `ground.unsupported_claim_rate`,
`retrieval.truncated_rate` and every `ops.*cost*` / latency metric are **lower is better**.
The rule table must carry direction per metric, not infer it from the namespace.

## Gates vs guardrails

**Gates** (a regression blocks, `FAIL`) are all and only the fabrication metrics plus
toxicity: a DeepDive that invents a citation is worthless, however good its prose.
**Guardrails** (`REVIEW`, a human decides) are quality, cost and latency.
**Info** is never a verdict input.

Only replay-mode runs may set a gate. `compare.compare()` enforces this: it reads
`metadata.websearch_mode` off the candidate and **downgrades every gate to a guardrail**
when the candidate did not run in replay, so a live run can REVIEW but never FAIL. The web
moving is not our regression.

## Workflow

```bash
pip install -r evals/requirements.txt             # only needed for the judged report.* metrics
python -m evals.run --mode live --baseline        # first run: records the web-search fixtures
python -m evals.run --mode replay                # thereafter -> baselines/candidate.json
python -m evals.compare                           # PASS=0  FAIL=1  REVIEW=2
python -m evals.run --no-judge                    # deterministic + calibrated + ops only
python -m evals.run --repeat 5 --baseline         # mean of 5 + baselines/spread.json
python -m evals.run --only poi-draft-v1           # one case, writes no snapshot
python -m evals.run --kind trap                   # one kind
python evals/test_compare.py                      # verdict logic only, no API, no spend
python evals/test_fixtures.py                     # poisoned-fixture invariants
```

On Windows set `PYTHONIOENCODING=utf-8` first — reports contain `₹` and curly quotes, which
crash cp1252 on print.

`--from-report` and `--from-plan` are in the design but not implemented yet.

Baseline = **mean of 5 runs**, with per-metric `spread.json`. Tolerances come from that
spread, not from guesses — if a metric's run-to-run spread exceeds its tolerance, the
tolerance is wrong and CI will flap.

CI runs `--no-judge` in replay mode on every push: deterministic and ops rows gate, judged
rows are local only.

## Judge

DeepEval, with a `DeepEvalBaseLLM` subclass pointed at `https://api.meshapi.ai/v1`
(`Mesh_AI_API_Key`). Hackathon rule 1 covers the judge too — it is an LLM call in our repo.

Per-claim verdicts use `POST /v1/evaluate` (`typesafe/jev-1.13`) rather than a chat model:
it returns a calibrated probability, output tokens are free, and there is nothing to parse.
Remember `noul` is a probability, not a boolean — threshold it. See meshapi.md §4.

**Never judge with the model that drafted.** Record `judge_model` in the snapshot metadata;
if it differs between baseline and candidate the judged rows are not comparable and the
compare step says so.

## Labelling plan

Do not label hundreds of results. Target, achievable in about a day:

- **15–20 `research` cases**, 2–4 must-find domains each
- **3–4 `corpus` cases** over a small fixed document set
- **2–3 `conflict` cases**
- **3–5 `trap` cases**
- **1 `poisoned` draft**, ~10 claims, 4 of them false

Then freeze the web-search fixtures and the numbers become reproducible.
