"""DeepDive eval harness: runs golden.json through the pipeline and measures it.

    python -m evals.run                      # full run -> baselines/candidate.json
    python -m evals.run --no-judge           # deterministic + ops only: free, never flaky
    python -m evals.run --baseline           # bless this run as baselines/baseline.json
    python -m evals.run --repeat 5           # mean of 5 + per-metric spread.json
    python -m evals.run --only res-ev-battery-risks
    python -m evals.run --mode replay        # force web-search replay (gates need this)
    python -m evals.compare                  # candidate vs baseline -> PASS / REVIEW / FAIL

Four families of score:

  deterministic  a citation that is not in the retrieval set is a fabrication. No model is
                 consulted and no model's opinion could overturn it. Free, never flaky.
  calibrated     /v1/evaluate returns a probability, not prose: claim support, whether a
                 report surfaced a conflict, whether it asserted a forbidden claim.
  judged         DeepEval (judge routed through MeshAPI) on the final report: the RAG triad
                 plus coverage and toxicity.
  operational    latency, search count, token volume.

The harness imports backend.orchestrator rather than talking to the API over HTTP: the
orchestrator *is* the system, and backend.mesh is stdlib-only so this pulls in no web stack.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from backend import agents, config                      # noqa: E402
from backend.mesh import Mesh, MeshError                 # noqa: E402
from backend.orchestrator import audit, run_research, totals  # noqa: E402
from backend.schemas import Claim, Passage, Run          # noqa: E402

BASELINES = HERE / "baselines"
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "openai/gpt-5.6-terra")
SUPPORT_THRESHOLD = 0.7   # for the calibrated yes/no questions below
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")


_UPLOADED = {}  # fixture path -> file_id, so one pass uploads each fixture once


# ---------------- executing one case ----------------
def execute(case, mesh):
    """Run one golden case. Returns (Run, extra) where extra holds kind-specific scoring."""
    kind = case["kind"]

    if kind == "poisoned":
        fx = json.loads((HERE / case["draft"]).read_text(encoding="utf-8"))
        inp = fx["input"]
        run = Run(question=inp["question"])
        run.passages = [Passage(id=p["id"], url=p["url"], title=p["title"], text=p["text"])
                        for p in inp["retrieval_set"]]
        run.claims = [Claim(id=c["id"], text=c["text"], citations=c["citations"])
                      for c in inp["claims"]]
        # same guard as the research path: this case must not be able to kill the run
        try:
            agents.verify(Mesh(run=run), run)
            run.status = "done"
        except MeshError as e:
            run.status = "failed"
            run.report = f"[{type(e).__name__}] {e}"
        return run, {"expected": fx["expected"]}

    file_ids = None
    if kind == "corpus":
        file_ids = []
        for rel in case["corpus_files"]:
            path = HERE / rel
            if not path.exists():
                return None, {"skipped": f"missing corpus fixture {rel}"}
            # Upload each fixture once per pass, not once per case. Eight corpus cases over
            # one 1.2MB PDF is eight uploads, eight embedding waits and eight copies in the
            # RAG store, all of the same bytes.
            if rel not in _UPLOADED:
                status = mesh.upload_file(path.name, path.read_bytes(), _mime(path))
                _UPLOADED[rel] = status["file_id"]
                print(f"    uploaded {path.name} -> {status['file_id'][:8]} "
                      f"({status.get('total_tokens')} tokens embedded)")
            file_ids.append(_UPLOADED[rel])

    t0 = time.perf_counter()
    try:
        run = run_research(case["question"], corpus_file_ids=file_ids)
    except MeshError as e:
        run = Run(question=case["question"])
        run.status = "failed"
        run.report = f"[{type(e).__name__}] {e}"
    return run, {"wall_ms": round((time.perf_counter() - t0) * 1000)}


def _mime(path):
    return {".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain",
            ".csv": "text/csv", ".json": "application/json"}.get(path.suffix.lower(), "text/plain")


# ---------------- deterministic + calibrated scoring ----------------
def score_case(case, run, extra, mesh, judge_cheap=True):
    """Everything measurable without DeepEval. Mutates and returns `extra`."""
    kind = case["kind"]
    a = audit(run)
    extra["counts"] = a["counts"]
    extra["loopbacks"] = run.loopbacks
    extra["abstained"] = run.abstained or not run.accepted()
    extra["status"] = run.status

    # --- retrieval, from the trace's own search records ---
    searches = [s for s in run.trace if s.action == "researcher.web_search"]
    extra["searches"] = len(searches)
    extra["results_total"] = sum(s.detail.get("n_results", 0) for s in searches)
    extra["results_with_text"] = sum(s.detail.get("n_with_page_content", 0) for s in searches)
    extra["searches_nonempty"] = sum(1 for s in searches if s.detail.get("n_results", 0) > 0)
    extra["engines"] = [s.detail.get("engine") for s in searches if s.detail.get("engine")]

    domains = [_domain(p.url) for p in run.passages if p.source == "web"]
    extra["domain_diversity"] = (len(set(domains)) / len(domains)) if domains else None
    extra["truncated"] = sum(1 for p in run.passages if p.truncated)
    extra["passages"] = len(run.passages)

    if case.get("must_find_domains"):
        want = {d.lower().lstrip(".") for d in case["must_find_domains"]}
        found = {w for w in want if any(w == d or d.endswith("." + w) for d in domains)}
        extra["recall_hit"] = len(found) == len(want)      # the documented pass/fail
        extra["recall_fraction"] = len(found) / len(want)  # info: softer signal
        extra["recall_missing"] = sorted(want - found)

    if kind == "corpus":
        cited = [p for p in run.passages
                 if any(p.id in c.citations for c in run.accepted())]
        extra["corpus_citations"] = sum(1 for p in cited if p.source == "corpus")
        extra["total_citations"] = len(cited)

    # --- grounding, per claim ---
    claims = run.claims
    extra["claims"] = len(claims)
    extra["dangling"] = sum(1 for c in claims if c.failure_mode == "dangling_citation")
    extra["unsupported"] = sum(1 for c in claims if c.failure_mode == "unsupported")
    known = {p.id: p for p in run.passages}
    cites = [(c, cid) for c in claims for cid in c.citations]
    extra["citations"] = len(cites)
    extra["spans_resolve"] = sum(1 for _, cid in cites if known.get(cid) and known[cid].text.strip())
    supports = [c.support for c in claims if c.support is not None]
    extra["support_mean"] = statistics.fmean(supports) if supports else None

    # --- poisoned: the verifier measured against the answer key ---
    if kind == "poisoned":
        key = extra["expected"]
        tp = fp = tn = fn = 0
        modes_right = 0
        for c in claims:
            want = key[c.id]["verdict"]
            got = "accept" if c.verdict == "accepted" else "reject"
            tp += want == "reject" and got == "reject"
            fn += want == "reject" and got == "accept"
            tn += want == "accept" and got == "accept"
            fp += want == "accept" and got == "reject"
            if want == "reject" and c.failure_mode == key[c.id].get("failure_mode"):
                modes_right += 1
        extra["poison"] = {"tp": tp, "fp": fp, "tn": tn, "fn": fn,
                           "modes_right": modes_right, "n_false": tp + fn}
        extra.pop("expected")  # the key must not reach the report file as if it were a result
        return extra

    if kind == "trap" or case.get("abstain_expected"):
        extra["abstain_ok"] = bool(extra["abstained"])

    # --- calibrated checks on the final report (cheap: /v1/evaluate, output free) ---
    if judge_cheap and run.report and run.status != "failed":
        if kind == "conflict":
            extra["conflict_surfaced"] = _noul(
                mesh, {"question": case["question"], "report": run.report[:20000]},
                "Probability that the report explicitly states that its sources disagree with "
                "each other on this question, and attributes more than one estimate.") >= SUPPORT_THRESHOLD
        if case.get("forbidden_claims"):
            asserted = [f for f in case["forbidden_claims"]
                        if _noul(mesh, {"report": run.report[:20000], "statement": f},
                                 "Probability that the REPORT asserts the STATEMENT as true. "
                                 "Mentioning it in order to reject it is not asserting it."
                                 ) >= SUPPORT_THRESHOLD]
            extra["forbidden_asserted"] = asserted
    return extra


def _noul(mesh, state, instructions):
    answers = mesh.evaluate(state, {"q": {"type": "noul", "instructions": instructions}},
                            action="eval.noul")
    return float(answers.get("q", {}).get("noul", 0.0))


def _domain(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---------------- judged scoring (DeepEval, judge on MeshAPI) ----------------
def mesh_judge():
    """DeepEval judge routed through MeshAPI. Rule 1 covers the judge: it is an LLM call in
    this repo, so it goes through the gateway like everything else."""
    from deepeval.models import DeepEvalBaseLLM
    from openai import AsyncOpenAI, OpenAI

    class MeshJudge(DeepEvalBaseLLM):
        def __init__(self, model):
            kw = {"api_key": config.MESH_API_KEY, "base_url": config.MESH_BASE_URL,
                  "max_retries": 6}
            self.sync, self.async_ = OpenAI(**kw), AsyncOpenAI(**kw)
            super().__init__(model)

        def load_model(self):
            return self.sync

        def _args(self, prompt, schema):
            return {"model": self.name, "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                    **({"response_format": {"type": "json_object"}} if schema else {})}

        def generate(self, prompt, schema=None, **_):
            return self.sync.chat.completions.create(**self._args(prompt, schema)).choices[0].message.content

        async def a_generate(self, prompt, schema=None, **_):
            r = await self.async_.chat.completions.create(**self._args(prompt, schema))
            return r.choices[0].message.content

        def get_model_name(self):
            return self.name

    return MeshJudge(JUDGE_MODEL)


def judge(rows):
    """Score each report with the metrics its kind calls for. One metric at a time, so one
    failure cannot lose the rest."""
    from deepeval.metrics import (AnswerRelevancyMetric, ContextualPrecisionMetric,
                                  ContextualRecallMetric, FaithfulnessMetric, GEval,
                                  ToxicityMetric)
    try:
        from deepeval.test_case import SingleTurnParams as Params
    except ImportError:
        from deepeval.test_case import LLMTestCaseParams as Params
    from deepeval.test_case import LLMTestCase

    j = mesh_judge()
    coverage = GEval(name="coverage", model=j,
                     evaluation_params=[Params.INPUT, Params.ACTUAL_OUTPUT, Params.EXPECTED_OUTPUT],
                     criteria=("The expected output lists the key points the report must address. "
                               "Judge coverage only: how many does the report address? Do not "
                               "penalise extra detail and do not judge factual correctness."))
    suites = {
        "research": [FaithfulnessMetric(model=j), AnswerRelevancyMetric(model=j),
                     ContextualPrecisionMetric(model=j), ContextualRecallMetric(model=j), coverage],
        "corpus": [FaithfulnessMetric(model=j), coverage],
        "conflict": [FaithfulnessMetric(model=j)],
        None: [ToxicityMetric(model=j)],
    }

    scored = []
    for row in rows:
        case, run = row["case"], row["run"]
        if run is None or case["kind"] == "poisoned" or not run.get("report"):
            continue
        metrics = suites.get(case["kind"], []) + suites[None]
        ctx = [p["text"][:4000] for p in run["passages"]] or ["(nothing retrieved)"]
        tc = LLMTestCase(input=case.get("question", ""), actual_output=run["report"],
                         retrieval_context=ctx,
                         expected_output="\n".join(case.get("must_contain_claims", [])) or None)
        out = {"id": case["id"], "kind": case["kind"]}
        for metric in metrics:
            name = type(metric).__name__.replace("Metric", "").lower()
            name = getattr(metric, "name", name) or name
            key = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")
            try:
                metric.measure(tc)
                out[key] = round(float(metric.score), 3)
                out[f"{key}_why"] = str(metric.reason)[:400]
            except Exception as e:  # noqa: BLE001
                out[key] = None
                out[f"{key}_error"] = f"{type(e).__name__}: {str(e)[:140]}"
        print("    " + f"{case['id']:28} " +
              "  ".join(f"{k}={v:.2f}" for k, v in out.items() if isinstance(v, float)))
        scored.append(out)
    return scored


# ---------------- snapshot ----------------
def snapshot(rows, scored, label, mode):
    """Flatten one run into the dotted metric ids compare.py knows about."""
    m = {}
    by = lambda k: [r["extra"] for r in rows
                    if r["case"]["kind"] == k and r["extra"] and not r["extra"].get("skipped")]
    # `ex` deliberately EXCLUDES the poisoned fixture. Its fabrications are planted on
    # purpose, so counting them would put a permanent non-zero floor under
    # ground.hallucinated_citation_rate -- a gate whose whole job is to read zero. The
    # fixture is scored only through verify.poison_*, via by("poisoned") below.
    ex = [r["extra"] for r in rows
          if r["case"]["kind"] != "poisoned" and r["extra"] and not r["extra"].get("skipped")]
    ratio = lambda num, den: round(num / den, 4) if den else None

    def put(mid, value):
        if value is not None:
            m[mid] = value

    # --- retrieval ---
    labelled = [e for e in ex if "recall_hit" in e]
    put("retrieval.recall_at_k", ratio(sum(e["recall_hit"] for e in labelled), len(labelled)))
    put("retrieval.domain_hit_rate",
        round(statistics.fmean([e["recall_fraction"] for e in labelled]), 4) if labelled else None)
    put("retrieval.recall_at_k.n", len(labelled) or None)
    put("retrieval.nonempty_rate", ratio(sum(e["searches_nonempty"] for e in ex),
                                         sum(e["searches"] for e in ex)))
    put("retrieval.page_content_rate", ratio(sum(e["results_with_text"] for e in ex),
                                             sum(e["results_total"] for e in ex)))
    put("retrieval.truncated_rate", ratio(sum(e["truncated"] for e in ex),
                                          sum(e["passages"] for e in ex)))
    divs = [e["domain_diversity"] for e in ex if e.get("domain_diversity") is not None]
    put("retrieval.domain_diversity", round(statistics.fmean(divs), 4) if divs else None)
    corpus = [e for e in by("corpus") if e.get("total_citations")]
    put("retrieval.corpus_only_rate", ratio(sum(e["corpus_citations"] for e in corpus),
                                            sum(e["total_citations"] for e in corpus)))
    # retrieval.precision_at_k needs per-result relevance labels, which the golden set does
    # not carry yet. Emitting a made-up number would be worse than emitting none.

    # --- grounding ---
    live = [e for e in ex if e["claims"]]
    put("ground.hallucinated_citation_rate", ratio(sum(e["dangling"] for e in live),
                                                   sum(e["claims"] for e in live)))
    put("ground.unsupported_claim_rate", ratio(sum(e["unsupported"] for e in live),
                                               sum(e["claims"] for e in live)))
    put("ground.span_resolves_rate", ratio(sum(e["spans_resolve"] for e in live),
                                           sum(e["citations"] for e in live)))
    sups = [e["support_mean"] for e in live if e.get("support_mean") is not None]
    put("ground.claim_support_rate", round(statistics.fmean(sups), 4) if sups else None)
    forb = [e for e in ex if "forbidden_asserted" in e]
    put("ground.forbidden_claim_rate",
        ratio(sum(1 for e in forb if e["forbidden_asserted"]), len(forb)))
    put("ground.forbidden_claim_rate.n", len(forb) or None)

    # --- verify ---
    poison = [e["poison"] for e in by("poisoned") if e.get("poison")]
    if poison:
        tp, fn = sum(p["tp"] for p in poison), sum(p["fn"] for p in poison)
        tn, fp = sum(p["tn"] for p in poison), sum(p["fp"] for p in poison)
        put("verify.poison_recall", ratio(tp, tp + fn))
        put("verify.poison_precision", ratio(tn, tn + fp))
        put("verify.failure_mode_accuracy", ratio(sum(p["modes_right"] for p in poison),
                                                  sum(p["n_false"] for p in poison)))
        put("verify.poison_recall.n", tp + fn)
    abst = [e for e in ex if "abstain_ok" in e]
    put("verify.abstain_rate", ratio(sum(e["abstain_ok"] for e in abst), len(abst)))
    put("verify.abstain_rate.n", len(abst) or None)
    conf = [e for e in ex if "conflict_surfaced" in e]
    put("verify.conflict_surfaced_rate", ratio(sum(e["conflict_surfaced"] for e in conf), len(conf)))
    loops = [e["loopbacks"] for e in ex]
    put("verify.loopback_rate", round(statistics.fmean(loops), 4) if loops else None)

    # --- judged ---
    groups = {}
    for s in scored:
        for k, v in s.items():
            if isinstance(v, float):
                groups.setdefault(f"report.{k}", []).append(v)
    for name, vals in groups.items():
        m[f"{name}.avg_score"] = round(statistics.fmean(vals), 4)
        m[f"{name}.n"] = len(vals)

    # --- ops ---
    walls = [e["wall_ms"] for e in ex if e.get("wall_ms")]
    if len(walls) >= 2:
        q = statistics.quantiles(walls, n=20, method="inclusive")  # q[9]=p50, q[18]=p95
        m["ops.latency.p50_ms"], m["ops.latency.p95_ms"] = round(q[9]), round(q[18])
    elif walls:
        m["ops.latency.p50_ms"] = m["ops.latency.p95_ms"] = round(walls[0])
    searched = [e for e in ex if e.get("searches")]
    put("ops.websearch.calls_per_run",
        round(statistics.fmean([e["searches"] for e in searched]), 2) if searched else None)
    # priced from the documented rates, not reported by the API: $0.005/search on the pinned
    # engine, plus $0.001 per returned result carrying page text. meshapi.md section 4.
    put("ops.websearch.cost_usd_est", round(sum(e["searches"] for e in ex) * 0.005
                                            + sum(e["results_with_text"] for e in ex) * 0.001, 4))
    tok = [totals_of(r) for r in rows if r["run"]]
    put("ops.tokens.prompt_total", sum(t["prompt_tokens"] for t in tok) or None)
    put("ops.tokens.completion_total", sum(t["completion_tokens"] for t in tok) or None)
    put("ops.byok_fallback_steps", sum(t["byok_fallback_steps"] for t in tok))
    ran = [r for r in rows if r["run"]]
    put("ops.reliability.success_rate",
        ratio(sum(1 for r in ran if r["run"]["status"] != "failed"), len(ran)))
    engines = Counter(eng for e in ex for eng in e.get("engines", []))
    for name, n in engines.items():
        m[f"ops.websearch.engine_mix.{name}"] = n
    m["cases.run"] = len(ran)
    m["cases.skipped"] = sum(1 for r in rows if r["extra"] and r["extra"].get("skipped"))

    return {"metadata": {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(), "label": label, "judge_model": JUDGE_MODEL if scored else None,
        "websearch_mode": mode, "models": dict(config.MODELS),
        "evaluate_model": config.EVALUATE_MODEL,
        # the knobs that move cost and the loop; a snapshot is not interpretable without them
        "verify_threshold": config.VERIFY_ACCEPT_THRESHOLD,
        "max_loopbacks": config.MAX_LOOPBACKS,
        "max_subquestions": config.MAX_SUBQUESTIONS,
        "websearch_results": config.WEBSEARCH_RESULTS,
        "n_cases": len(rows),
    }, "metrics": m}


def totals_of(row):
    t = row.get("totals")
    return t if t else {"prompt_tokens": 0, "completion_tokens": 0, "byok_fallback_steps": 0}


def _git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def aggregate(snaps):
    """Mean of N snapshots, plus the spread each tolerance should be set above."""
    keys = {k for s in snaps for k in s["metrics"]}
    mean, spread = {}, {}
    for k in sorted(keys):
        vals = [s["metrics"][k] for s in snaps if isinstance(s["metrics"].get(k), (int, float))]
        if not vals:
            continue
        mean[k] = round(statistics.fmean(vals), 4)
        spread[k] = {"runs": vals, "min": min(vals), "max": max(vals),
                     "spread": round(max(vals) - min(vals), 4)}
    meta = dict(snaps[-1]["metadata"])
    meta["runs"] = [s["metadata"]["created_at"] for s in snaps]
    meta["label"] = f"{meta.get('label') or ''} (mean of {len(snaps)})".strip()
    return {"metadata": meta, "metrics": mean}, spread


# ---------------- main ----------------
def one_pass(cases, no_judge, mode):
    mesh = Mesh(agent="evals")
    rows = []
    for case in cases:
        print(f"  {case['id']:28} {case['kind']:9} {case.get('question', '(fixture)')[:52]}")
        try:
            run, extra = execute(case, mesh)
            # A failed run must never be scored. It has no claims and no passages, so every
            # rate computed from it is vacuously perfect -- a 402 once produced
            # poison_recall 1.00 because rejecting everything looks like catching everything.
            if run is not None and run.status == "failed":
                raise MeshError(run.report or "run failed")
            if run is not None:
                extra = score_case(case, run, extra, mesh, judge_cheap=not no_judge)
        except MeshError as e:
            # One case must never cost us the whole pass. A spend cap or a provider outage
            # part-way through used to discard every case already run; now the pass stops
            # and still writes a snapshot for what completed.
            print(f"    FAILED: {e}")
            rows.append({"case": case, "run": None, "totals": None,
                         "extra": {"skipped": f"{type(e).__name__}: {e}"}})
            if "spend_limit_exceeded" in str(e) or "402" in str(e):
                print("\n  ! spend cap or balance reached - stopping. Raise the cap on the Mesh "
                      "dashboard (Settings -> spend caps), then re-run.")
                for later in cases[cases.index(case) + 1:]:
                    rows.append({"case": later, "run": None, "totals": None,
                                 "extra": {"skipped": "not run: spend cap reached"}})
                break
            continue
        if run is None:
            print(f"    skipped: {extra['skipped']}")
            rows.append({"case": case, "run": None, "extra": extra, "totals": None})
            continue
        line = (f"    claims {extra['counts']['accepted']}/{extra['counts']['claims']} accepted, "
                f"{extra['passages']} passages, {extra['searches']} searches, "
                f"loopbacks {extra['loopbacks']}")
        if "recall_hit" in extra:
            line += (", recall hit" if extra["recall_hit"]
                     else f", recall MISS {extra['recall_missing']}")
        if "abstain_ok" in extra:
            line += f", abstain {'ok' if extra['abstain_ok'] else 'MISSED'}"
        if extra.get("poison"):
            p = extra["poison"]
            line += f", poison tp={p['tp']}/{p['n_false']} fp={p['fp']}"
        print(line)
        rows.append({"case": case, "run": run.dict(), "extra": extra, "totals": totals(run)})

    scored = []
    if not no_judge and any(r["run"] for r in rows):
        print(f"\n  judging reports with DeepEval ({JUDGE_MODEL} via MeshAPI)...")
        try:
            scored = judge(rows)
        except ImportError:
            print("    deepeval not installed (pip install -r evals/requirements.txt); "
                  "skipping the report.* metrics")
    return rows, scored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-judge", action="store_true", help="skip DeepEval; free and never flaky")
    ap.add_argument("--only", help="one case id (writes no snapshot)")
    ap.add_argument("--kind", help="only cases of this kind")
    ap.add_argument("--baseline", action="store_true", help="bless as baselines/baseline.json")
    ap.add_argument("--label", default="", help="what this snapshot measures")
    ap.add_argument("--repeat", type=int, default=1, help="N passes -> mean + spread.json")
    ap.add_argument("--mode", choices=["live", "replay"], help="override WEBSEARCH")
    args = ap.parse_args()

    if args.mode:
        config.WEBSEARCH_MODE = args.mode
    if config.missing_key():
        sys.exit("No API key. Set Mesh_AI_API_Key in .env (or MESH_API_KEY).")

    cases = json.loads((HERE / "golden.json").read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    if args.kind:
        cases = [c for c in cases if c["kind"] == args.kind]
    if not cases:
        sys.exit("no cases selected")

    snaps, last_rows, last_scored = [], None, None
    for i in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"\n=== pass {i}/{args.repeat}")
        rows, scored = one_pass(cases, args.no_judge, config.WEBSEARCH_MODE)
        last_rows, last_scored = rows, scored
        snaps.append(snapshot(rows, scored, args.label, config.WEBSEARCH_MODE))

    report = {"cases": [{"case": r["case"], "extra": r["extra"], "totals": r["totals"],
                         "report": (r["run"] or {}).get("report", ""),
                         "status": (r["run"] or {}).get("status")} for r in last_rows],
              "judged": last_scored}
    out = HERE / (f"report-{args.only}.json" if args.only else "report.json")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    if args.only:
        print(f"\n  one-case run, no snapshot written. Report: {out}")
        return

    BASELINES.mkdir(exist_ok=True)
    snap, spread = (snaps[0], None) if len(snaps) == 1 else aggregate(snaps)
    path = BASELINES / ("baseline.json" if args.baseline else "candidate.json")
    path.write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
    if spread:
        (BASELINES / "spread.json").write_text(json.dumps(spread, indent=2, sort_keys=True),
                                               encoding="utf-8")

    print("\n  snapshot")
    for k, v in sorted(snap["metrics"].items()):
        print(f"  {k:44} {v:.4f}" if isinstance(v, float) else f"  {k:44} {v}")
    if config.WEBSEARCH_MODE != "replay":
        print("\n  ! websearch=live, so compare.py downgrades every gate to a guardrail and this "
              "snapshot cannot FAIL. Record fixtures once, then run with --mode replay to gate.")
    print(f"\n  -> {path}" + ("" if args.baseline else "   (compare: python -m evals.compare)"))
    print(f"  -> {out}")
    if spread:
        print(f"  -> {BASELINES / 'spread.json'}   (set tolerances above these spreads)")


if __name__ == "__main__":
    main()
