"""Startup check: does the key work, and does every model id in config.py still exist?

    python -m backend.selfcheck            # key, catalogue, balance, one tiny chat call
    python -m backend.selfcheck --offline  # schemas and imports only, no network, no spend

The model registry is live and the platform can disable a model, so a hardcoded id is a
runtime failure waiting to happen. This is the cheapest way to turn it into a startup failure.
"""
import argparse
import json
import sys

from . import config
from .mesh import Mesh, MeshError
from .schemas import DRAFT_SCHEMA, GAPS_SCHEMA, PLAN_SCHEMA, Claim, Passage, Run


def offline():
    """No network: that the strict schemas are well formed and the dataclasses behave."""
    for name, schema in [("plan", PLAN_SCHEMA), ("draft", DRAFT_SCHEMA), ("gaps", GAPS_SCHEMA)]:
        s = schema["json_schema"]
        assert s["strict"] is True, name
        assert s["schema"]["additionalProperties"] is False, name
        # strict mode requires EVERY property to be listed in required
        assert set(s["schema"]["required"]) == set(s["schema"]["properties"]), (
            f"{name}: strict json_schema needs every property in `required`, missing "
            f"{set(s['schema']['properties']) - set(s['schema']['required'])}")
        json.dumps(schema)

    run = Run(question="q")
    run.passages = [Passage(id="p1", url="u", title="t", text="x")]
    run.claims = [Claim(id="c1", text="a", citations=["p1"], verdict="accepted"),
                  Claim(id="c2", text="b", citations=["p9"], verdict="rejected")]
    assert run.passage_ids() == {"p1"}
    assert [c.id for c in run.accepted()] == ["c1"]
    json.dumps(run.dict(), default=str)

    from .orchestrator import audit, totals, _needs_more
    a = audit(run)
    assert a["counts"] == {"claims": 2, "accepted": 1, "rejected": 1, "needs_evidence": 0,
                           "passages": 1, "web_passages": 1, "corpus_passages": 0}, a["counts"]
    assert totals(run)["steps"] == 0

    # The loop silently did nothing for a whole baseline because NEEDS_EVIDENCE_MODES did not
    # contain the modes that actually occur. Pin the split so narrowing it again fails here.
    from .agents import FAILURE_MODES, NEEDS_EVIDENCE_MODES
    terminal = {"contradicts_source", "unsupported", "dangling_citation"}
    every_mode = (set(FAILURE_MODES) - {"supported"}) | {"below_threshold", "unsupported",
                                                         "dangling_citation"}
    assert NEEDS_EVIDENCE_MODES | terminal == every_mode, (
        f"every failure mode must be classified; unclassified: "
        f"{every_mode - NEEDS_EVIDENCE_MODES - terminal}")
    assert not (NEEDS_EVIDENCE_MODES & terminal), "a mode cannot be both loopable and terminal"
    for mode in ("below_threshold", "overstated"):
        assert mode in NEEDS_EVIDENCE_MODES, f"{mode} occurs in real runs and must drive the loop"
    assert "contradicts_source" not in NEEDS_EVIDENCE_MODES, \
        "a claim that contradicts its own source is wrong, not under-researched"

    loop = Run(question="q")
    loop.claims = [Claim(id="c1", text="a", verdict="needs_evidence")]
    assert _needs_more(loop), "a needs_evidence claim must trigger a loop-back"
    loop.claims[0].verdict = "rejected"
    assert not _needs_more(loop), "a terminal rejection must not trigger one"

    print("selfcheck offline: ok - schemas strict-valid, audit counts correct, "
          "loop classification pinned")


def online():
    if config.missing_key():
        sys.exit("No API key. Set Mesh_AI_API_Key in .env (or MESH_API_KEY).")
    mesh = Mesh()

    models = mesh.list_models()
    ids = {m["id"] for m in models}
    wanted = dict(config.MODELS, evaluate=config.EVALUATE_MODEL, embedding=config.EMBEDDING_MODEL)
    missing = {role: mid for role, mid in wanted.items() if mid not in ids}
    print(f"  catalogue: {len(ids)} models enabled on this key")
    for role, mid in wanted.items():
        print(f"    {role:12} {mid:34} {'MISSING' if mid in missing.values() else 'ok'}")
    if missing:
        sys.exit(f"these ids are not in the live catalogue: {missing}. Pick replacements in config.py.")

    if config.MODELS["drafter"] == config.MODELS["verifier"]:
        print("  ! drafter and verifier are the same model. A model is a poor judge of its own "
              "output -- set VERIFIER_MODEL to something else.")

    caps = {m["id"]: m for m in models}
    for role in ("planner", "drafter"):
        mid = config.MODELS[role]
        if not caps[mid].get("supports_structured_output"):
            print(f"  ! {role} ({mid}) reports supports_structured_output=false; response_format "
                  f"will likely be rejected. Use tool calling, or pick another model.")

    print(f"  balance: {json.dumps(mesh.balance())}")

    reply = mesh.chat("utility", [{"role": "user", "content": "Reply with the single word: ok"}])
    print(f"  chat({config.MODELS['utility']}): {reply.strip()[:40]!r}")

    answers = mesh.evaluate(
        state={"claim": "The sky is green.", "evidence": [{"text": "The sky is blue."}]},
        questions={"support": {"type": "noul", "instructions":
                   "Probability the evidence supports the claim."}})
    noul = answers.get("support", {}).get("noul")
    print(f"  evaluate({config.EVALUATE_MODEL}): support={noul}")
    if noul is None:
        sys.exit("evaluate returned no answer; the key may lack the evaluate:write scope.")
    print("selfcheck online: ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="no network, no spend")
    args = ap.parse_args()
    offline()
    if not args.offline:
        try:
            online()
        except MeshError as e:
            sys.exit(f"MeshAPI error: {e}")


if __name__ == "__main__":
    main()
