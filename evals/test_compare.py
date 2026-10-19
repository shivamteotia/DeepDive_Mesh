"""Checks the regression verdict logic. No API, no judge, no spend:

    cd evals && python test_compare.py

compare.py decides whether a change ships. Its two easiest mistakes to make and hardest to
notice are an inverted direction on a lower-is-better metric, and a live-mode run that fails
CI because the web moved. Both are covered below.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals.compare import compare, rule_for  # noqa: E402


def snap(mode="replay", **metrics):
    return {"metadata": {"websearch_mode": mode, "judge_model": "m", "label": "t",
                         "git_sha": "x", "created_at": "now", "n_cases": 1},
            "metrics": {k.replace("__", "."): v for k, v in metrics.items()}}


def verdict(base, **changes):
    cand = {"metadata": dict(base["metadata"]), "metrics": dict(base["metrics"])}
    cand["metrics"].update({k.replace("__", "."): v for k, v in changes.items()})
    return compare(base, cand)[0]


base = snap(
    verify__poison_recall=1.0,
    verify__poison_precision=0.83,
    verify__abstain_rate=1.0,
    ground__hallucinated_citation_rate=0.0,
    ground__unsupported_claim_rate=0.0,
    ground__span_resolves_rate=1.0,
    ground__claim_support_rate=0.92,
    ground__forbidden_claim_rate=0.0,
    retrieval__recall_at_k=1.0,
    retrieval__page_content_rate=0.95,
    report__toxicity__avg_score=1.0,
    report__faithfulness__avg_score=0.90,
    ops__latency__p95_ms=40000,
    ops__websearch__cost_usd_est=0.08,
    ops__byok_fallback_steps=0,
    retrieval__recall_at_k__n=18,
)

assert verdict(base) == "PASS", "a snapshot against itself is flat"

# --- gates block ---
assert verdict(base, verify__poison_recall=0.75) == "FAIL", "the verifier stopped catching poison"
assert verdict(base, verify__abstain_rate=0.6) == "FAIL", "it stopped abstaining on traps"
assert verdict(base, retrieval__recall_at_k=0.9) == "FAIL"
assert verdict(base, report__toxicity__avg_score=0.9) == "FAIL"

# --- lower-is-better gates: a RISE is the regression ---
assert verdict(base, ground__hallucinated_citation_rate=0.05) == "FAIL", \
    "fabricated citations appeared and the direction was read the wrong way round"
assert verdict(base, ground__unsupported_claim_rate=0.02) == "FAIL"
assert verdict(base, ground__forbidden_claim_rate=0.25) == "FAIL"
assert verdict(base, ground__hallucinated_citation_rate=0.0) == "PASS"

# --- guardrails ask a human, they do not block ---
assert verdict(base, ground__claim_support_rate=0.88) == "PASS", "inside the 0.05 tolerance"
assert verdict(base, ground__claim_support_rate=0.80) == "REVIEW"
assert verdict(base, report__faithfulness__avg_score=0.70) == "REVIEW"
assert verdict(base, verify__poison_precision=0.60) == "REVIEW"
assert verdict(base, ops__byok_fallback_steps=3) == "REVIEW", "BYOK started falling back"

# --- latency and cost: relative tolerance, and faster/cheaper is an improvement ---
assert verdict(base, ops__latency__p95_ms=48000) == "PASS", "within 25%"
assert verdict(base, ops__latency__p95_ms=60000) == "REVIEW"
assert verdict(base, ops__latency__p95_ms=20000) == "PASS", "faster is better"
assert verdict(base, ops__websearch__cost_usd_est=0.09) == "PASS", "within 25%"
assert verdict(base, ops__websearch__cost_usd_est=0.20) == "REVIEW"
assert verdict(base, ops__websearch__cost_usd_est=0.01) == "PASS", "cheaper is better"

# --- info never decides ---
assert verdict(base, retrieval__recall_at_k__n=3) == "PASS", "a count is info only"

# --- a live-mode candidate cannot FAIL: the web moving is not our regression ---
live = snap(mode="live", **{k.replace(".", "__"): v for k, v in base["metrics"].items()})
assert compare(base, live)[0] == "PASS"
live["metrics"]["verify.poison_recall"] = 0.5
assert compare(base, live)[0] == "REVIEW", "a live gate is downgraded to a guardrail, not ignored"
assert verdict(base, verify__poison_recall=0.5) == "FAIL", "...but in replay mode it still blocks"

# --- the rule table itself ---
assert rule_for("ground.hallucinated_citation_rate")[:2] == ("lower", "gate")
assert rule_for("verify.poison_recall")[:2] == ("higher", "gate")
assert rule_for("verify.loopback_rate")[1] == "info"
assert rule_for("report.coverage.avg_score")[:2] == ("higher", "guardrail")
assert rule_for("report.toxicity.avg_score")[1] == "gate", "toxicity must not fall to the report.* rule"
assert rule_for("ops.websearch.engine_mix.tinyfish")[1] == "info"
assert rule_for("retrieval.recall_at_k.n")[1] == "info", "a .n count must never gate"
assert rule_for("something.unknown")[1] == "info", "an unknown metric is shown, never decisive"

# --- a drop of exactly the tolerance reads as flat, not a regression ---
edge = snap(ground__claim_support_rate=0.92)
assert verdict(edge, ground__claim_support_rate=0.87) == "PASS"
assert verdict(edge, ground__claim_support_rate=0.869) == "REVIEW"

print("compare: ok - 30 assertions")
