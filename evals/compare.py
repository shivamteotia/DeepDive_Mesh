"""Regression gate: diff the candidate snapshot against the blessed baseline, give a verdict.

    python -m evals.run --baseline --label "main"     # once, on the version we trust
    python -m evals.run --label "k=8 retrieval"       # after a change -> baselines/candidate.json
    python -m evals.compare [--all]                   # PASS=0  FAIL=1  REVIEW=2

Every metric id resolves to a rule: which direction is better, whether it is
  gate      - a drop beyond tolerance BLOCKS (fabrication, abstention, toxicity),
  guardrail - a drop beyond tolerance is flagged for a human (quality, cost, latency),
  info      - shown, never affects the verdict (counts, p50, engine mix).

Two things differ from the HR harness this descends from:

  1. Direction is per metric, not per namespace. `ground.hallucinated_citation_rate` and every
     cost and latency metric are lower-is-better, and inferring that from the prefix would
     silently invert them.

  2. **A live-mode candidate cannot fail.** Gates are downgraded to guardrails when the
     candidate did not run in web-search replay mode, because the live web moving is not our
     regression. See evals/README.md.
"""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

BASELINES = Path(__file__).resolve().parent / "baselines"
EXIT_CODE = {"PASS": 0, "FAIL": 1, "REVIEW": 2}

HIGHER, LOWER = "higher", "lower"

# (match, direction, kind, (absolute_tolerance, relative_tolerance)), first match wins.
# A match containing "*" is head-prefix plus optional tail-substring; one starting with "."
# is a suffix; anything else is exact. See rule_for().
RULES = [
    # counts and diagnostics
    (".n", HIGHER, "info", (0, 0)),
    ("retrieval.truncated_rate", LOWER, "info", (0, 0)),
    ("retrieval.domain_hit_rate", HIGHER, "info", (0, 0)),   # softer signal behind recall_at_k
    ("verify.loopback_rate", HIGHER, "info", (0, 0)),
    ("ops.latency.p50_ms", LOWER, "info", (0, 0)),
    ("ops.websearch.engine_mix*", HIGHER, "info", (0, 0)),
    ("cases.*", HIGHER, "info", (0, 0)),

    # fabrication: the gates. lower-is-better ones first so the prefix rules can't catch them.
    ("ground.hallucinated_citation_rate", LOWER, "gate", (0, 0)),
    ("ground.unsupported_claim_rate", LOWER, "gate", (0, 0)),
    ("ground.forbidden_claim_rate", LOWER, "gate", (0, 0)),
    ("ground.span_resolves_rate", HIGHER, "gate", (0, 0)),
    ("ground.claim_support_rate", HIGHER, "guardrail", (0.05, 0)),

    ("verify.poison_recall", HIGHER, "gate", (0, 0)),
    ("verify.abstain_rate", HIGHER, "gate", (0, 0)),
    ("verify.poison_precision", HIGHER, "guardrail", (0.05, 0)),
    ("verify.failure_mode_accuracy", HIGHER, "guardrail", (0.05, 0)),
    ("verify.conflict_surfaced_rate", HIGHER, "guardrail", (0.05, 0)),

    ("retrieval.recall_at_k", HIGHER, "gate", (0, 0)),
    ("retrieval.nonempty_rate", HIGHER, "gate", (0, 0)),
    ("retrieval.page_content_rate", HIGHER, "gate", (0, 0)),
    ("retrieval.corpus_only_rate", HIGHER, "gate", (0, 0)),
    ("retrieval.precision_at_k", HIGHER, "guardrail", (0.05, 0)),
    ("retrieval.domain_diversity", HIGHER, "guardrail", (0.05, 0)),

    # judged report quality
    ("report.toxicity.avg_score", HIGHER, "gate", (0.02, 0)),
    ("report.*", HIGHER, "guardrail", (0.05, 0)),

    # operational
    ("ops.latency.p95_ms", LOWER, "guardrail", (0, 0.25)),
    ("ops.reliability.success_rate", HIGHER, "guardrail", (0, 0)),
    ("ops.websearch.calls_per_run", LOWER, "guardrail", (0, 0.25)),
    # a run that starts falling back is spending Mesh balance instead of the free BYOK tokens
    ("ops.byok_fallback_steps", LOWER, "guardrail", (0, 0)),
    ("ops.*cost*", LOWER, "guardrail", (0, 0.25)),
    ("ops.tokens*", LOWER, "guardrail", (0, 0.25)),
]


def rule_for(mid):
    """-> (direction, kind, (absolute, relative))

    A pattern with "*" matches a head prefix plus, if there is one, a tail substring after it,
    so "ops.*cost*" catches ops.websearch.cost_usd_est. A pattern starting with "." is a
    suffix, which is how every ".n" count lands on info. Otherwise it is an exact id.
    """
    for match, direction, kind, tol in RULES:
        if "*" in match:
            head, _, tail = match.partition("*")
            tail = tail.strip("*")
            if mid.startswith(head) and (not tail or tail in mid[len(head):]):
                return direction, kind, tol
        elif match.startswith("."):
            if mid.endswith(match):
                return direction, kind, tol
        elif mid == match:
            return direction, kind, tol
    return HIGHER, "info", (0, 0)


def classify(mid, base, cand, downgrade_gates=False):
    direction, kind, (tol, rel) = rule_for(mid)
    if downgrade_gates and kind == "gate":
        kind = "guardrail"
    row = {"id": mid, "baseline": base, "candidate": cand, "kind": kind, "delta": None,
           "direction": direction}
    if base is None or cand is None:
        return {**row, "status": "new" if base is None else "dropped"}
    row["delta"] = delta = cand - base
    if kind == "info":
        return {**row, "status": "info"}
    if delta != 0 and (delta > 0) == (direction == HIGHER):
        return {**row, "status": "improved"}
    # round: 0.92 - 0.90 is 0.0200000000000000018, and a drop of exactly the tolerance is flat
    if round(abs(delta), 9) <= max(tol, rel * abs(base)):
        return {**row, "status": "flat"}
    return {**row, "status": "blocked" if kind == "gate" else "regressed"}


def compare(baseline, candidate):
    b, c = baseline["metrics"], candidate["metrics"]
    # only a replay-mode candidate may fail: the live web moving is not our regression
    mode = (candidate.get("metadata") or {}).get("websearch_mode", "replay")
    downgrade = mode != "replay"
    rows = [classify(mid, b.get(mid), c.get(mid), downgrade) for mid in sorted(set(b) | set(c))]
    statuses = {r["status"] for r in rows}
    verdict = "FAIL" if "blocked" in statuses else "REVIEW" if "regressed" in statuses else "PASS"
    return verdict, rows


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float) and not math.isnan(v):
        return f"{v:.4g}"
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=BASELINES / "baseline.json")
    ap.add_argument("--candidate", default=BASELINES / "candidate.json")
    ap.add_argument("--all", action="store_true", help="also show info metrics")
    args = ap.parse_args()

    try:
        baseline, candidate = (json.loads(Path(p).read_text(encoding="utf-8"))
                               for p in (args.baseline, args.candidate))
    except FileNotFoundError as e:
        sys.exit(f"{e.filename} not found. Bless a baseline first: python -m evals.run --baseline")

    for name, snap in (("baseline", candidate and baseline), ("candidate", candidate)):
        m = snap["metadata"]
        print(f"{name:10} {m.get('label') or '(no label)'}  git {m.get('git_sha')}  "
              f"judge {m.get('judge_model')}  websearch {m.get('websearch_mode')}  "
              f"n={m.get('n_cases')}  {m.get('created_at')}")

    bm, cm = baseline["metadata"], candidate["metadata"]
    if bm.get("judge_model") != cm.get("judge_model"):
        print("  ! judge models differ - the report.* rows are not comparable")
    if cm.get("websearch_mode") != "replay":
        print(f"  ! candidate ran websearch={cm.get('websearch_mode')}, so gates are downgraded "
              f"to guardrails and this run cannot FAIL. Re-run with WEBSEARCH=replay to gate.")
    if bm.get("models") != cm.get("models"):
        changed = {k: (bm.get("models", {}).get(k), v) for k, v in cm.get("models", {}).items()
                   if bm.get("models", {}).get(k) != v}
        print(f"  ! models changed: {changed}")

    verdict, rows = compare(baseline, candidate)
    order = ["blocked", "regressed", "dropped", "new", "improved", "flat", "info"]
    print("=" * 100)
    print(f"{'metric':<46} {'baseline':>10} {'candidate':>10} {'delta':>10}  status")
    print("-" * 100)
    for r in sorted(rows, key=lambda r: (order.index(r["status"]), r["id"])):
        if r["kind"] == "info" and not args.all:
            continue
        mark = "  <<<" if r["status"] in ("blocked", "regressed") else ""
        arrow = "" if r["direction"] == HIGHER else "  (lower is better)"
        delta = f"{r['delta']:+.4g}" if r["delta"] is not None else ""
        print(f"{r['id']:<46} {_fmt(r['baseline']):>10} {_fmt(r['candidate']):>10} {delta:>10}  "
              f"{r['status']}{mark}{arrow if mark else ''}")
    counts = Counter(r["status"] for r in rows)
    print("-" * 100)
    print("  ".join(f"{k}={counts[k]}" for k in order if counts[k]))
    print({"PASS": "VERDICT: PASS   - no gate blocked, no guardrail regressed.",
           "REVIEW": "VERDICT: REVIEW - a guardrail regressed beyond tolerance; a human decides.",
           "FAIL": "VERDICT: FAIL   - a fabrication gate regressed. Blocked."}[verdict])
    sys.exit(EXIT_CODE[verdict])


if __name__ == "__main__":
    main()
