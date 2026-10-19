"""Checks the poisoned fixture's invariants, no API and no judge:  cd evals && python test_fixtures.py

The fixture is the gate for verify.poison_recall, so a silent edit to it silently changes the
headline number. These are the invariants that are easy to break by hand and impossible to spot
by reading: the dangling citation must actually dangle, the non-dangling ones must actually
resolve, and the answer key must still agree with golden.json.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
golden = json.loads((HERE / "golden.json").read_text(encoding="utf-8"))
case = next(c for c in golden["cases"] if c["kind"] == "poisoned")
fx = json.loads((HERE / case["draft"]).read_text(encoding="utf-8"))

claims = {c["id"]: c for c in fx["input"]["claims"]}
passages = {p["id"]: p for p in fx["input"]["retrieval_set"]}
urls = {p["url"] for p in passages.values()}
expected = fx["expected"]

assert len(claims) == len(fx["input"]["claims"]), "duplicate claim id"
assert len(passages) == len(fx["input"]["retrieval_set"]), "duplicate passage id"
assert set(expected) == set(claims), "expected keys and claim ids disagree"

rejected = {cid for cid, e in expected.items() if e["verdict"] == "reject"}
accepted = set(claims) - rejected
assert rejected == set(case["false_claim_ids"]), (
    f"golden.json false_claim_ids {sorted(case['false_claim_ids'])} != fixture rejects {sorted(rejected)}")
assert len(accepted) >= len(rejected), "need at least as many true claims as false, or poison_precision is meaningless"

modes = [expected[cid]["failure_mode"] for cid in rejected]
assert len(set(modes)) == len(modes), f"two false claims share a failure mode: {modes}"

# every citation is either a passage id in the retrieval set, or a URL — and a URL that is NOT
# in the set is exactly the dangling_citation failure, so the two must line up both ways.
dangling = {cid for cid, e in expected.items() if e.get("failure_mode") == "dangling_citation"}
for cid, claim in claims.items():
    unresolved = [c for c in claim["citations"] if c not in passages and c not in urls]
    assert bool(unresolved) == (cid in dangling), (
        f"{cid}: unresolved citations {unresolved} but failure_mode is {expected[cid].get('failure_mode')}")
    assert claim["citations"], f"{cid} cites nothing — that is an unsupported claim, not a poisoned one"

# a reject must not be rejectable for the wrong reason: only the dangling one may miss the set
for cid in rejected - dangling:
    assert all(c in passages for c in claims[cid]["citations"]), f"{cid} should cite retrieved passages"

print(f"fixtures: ok - {len(accepted)} true, {len(rejected)} false, modes {sorted(set(modes))}")
