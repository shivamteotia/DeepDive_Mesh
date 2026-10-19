"""The loop, and the audit trail it leaves behind.

    plan -> research -> draft -> verify -> [loop back to research] -> synthesize

The loop-back is the point of the whole project: when the verifier rejects claims, the gaps it
names go back to the researcher as fresh queries and the draft is rebuilt. It is bounded by
MAX_LOOPBACKS, because a loop that cannot converge still has to end.

`audit()` is what the UI renders and what a judge reads: every claim with its verdict, its
confidence and why, plus every agent step joined to MeshAPI's own request ids.
"""
import json
from datetime import datetime, timezone

from . import agents, config
from .mesh import Mesh
from .schemas import Run


def _abstain_report(question, reason):
    """The whole output when the system declines. One place, so every abstention reads the
    same whether the planner refused the question or retrieval failed to ground it."""
    return f"# {question}\n\n**Insufficient evidence.** {reason}\n"


def run_research(question, corpus_file_ids=None, on_event=None):
    run = Run(question=question)
    emit = on_event or (lambda *_, **__: None)
    # one "step" event per agent action, not just per stage, so the UI can show the whole
    # activity feed as it happens
    mesh = Mesh(run=run, on_step=lambda s: emit("step", {"n": len(run.trace), **step_dict(s)}))

    def stage(name, fn, *a, **kw):
        emit("stage", {"stage": name, "status": "running"})
        fn(*a, **kw)
        emit("stage", {"stage": name, "status": "done", "run": summary(run)})

    try:
        stage("planner", agents.plan, mesh, run)
        if run.abstained:
            run.status = "abstained"
            run.report = _abstain_report(question, run.abstain_reason)
            emit("done", {"run": summary(run)})
            return _persist(run)

        stage("researcher", agents.research, mesh, run, corpus_file_ids=corpus_file_ids)

        # Gate between retrieval and drafting. Everything downstream verifies claims against
        # the passages we hold; if those passages are about the wrong thing, every check
        # downstream passes and the report is still fabricated.
        stage("verifier", agents.ground_check, mesh, run)
        if run.abstained:
            run.status = "abstained"
            run.report = _abstain_report(question, run.abstain_reason)
            emit("done", {"run": summary(run)})
            return _persist(run)

        stage("drafter", agents.draft, mesh, run)
        stage("verifier", agents.verify, mesh, run)

        while run.loopbacks < config.MAX_LOOPBACKS and _needs_more(run):
            queries = agents.gap_queries(mesh, run)
            if not queries:
                break
            run.loopbacks += 1
            emit("loopback", {"n": run.loopbacks, "queries": queries})
            before = len(run.passages)
            stage("researcher", agents.research, mesh, run, queries=queries,
                  corpus_file_ids=corpus_file_ids)
            if len(run.passages) == before:
                break  # nothing new came back; another draft would reach the same verdict
            stage("drafter", agents.draft, mesh, run)
            stage("verifier", agents.verify, mesh, run)

        stage("synthesizer", agents.synthesize, mesh, run)
        run.status = "abstained" if run.abstained else "done"
    except Exception as e:  # noqa: BLE001 -- a failed run must still leave its trace behind
        run.status = "failed"
        run.report = f"# {question}\n\n**Run failed.** {type(e).__name__}: {e}\n"
        emit("error", {"error": f"{type(e).__name__}: {e}", "run": summary(run)})
        _persist(run)
        raise

    emit("done", {"run": summary(run)})
    return _persist(run)


def _needs_more(run):
    """Loop back only for claims that more evidence could fix. A claim that contradicts its
    own source is wrong, not under-researched -- searching again will not rescue it."""
    return any(c.verdict == "needs_evidence" for c in run.claims)


# ---- the audit trail ----
def step_dict(s):
    """One trace row. Shared by audit() and the live `step` event so the UI renders the feed
    and the final table from the same shape."""
    return {"agent": s.agent, "action": s.action, "model": s.model,
            "request_id": s.request_id, "latency_ms": s.latency_ms,
            "prompt_tokens": s.prompt_tokens, "completion_tokens": s.completion_tokens,
            "cost_usd": s.cost_usd, "byok_used": s.byok_used,
            "byok_fallback": s.byok_fallback, **s.detail}


def audit(run):
    accepted = run.accepted()
    rejected = [c for c in run.claims if c.verdict == "rejected"]
    pending = [c for c in run.claims if c.verdict == "needs_evidence"]
    return {
        "run_id": run.id,
        "question": run.question,
        "status": run.status,
        "decisions": [
            {"claim_id": c.id, "claim": c.text, "verdict": c.verdict,
             "confidence": c.support, "failure_mode": c.failure_mode, "why": c.reason,
             "cites": [{"passage_id": p.id, "url": p.url, "title": p.title, "source": p.source}
                       for p in run.passages if p.id in c.citations]}
            for c in run.claims
        ],
        "loopbacks": run.loopbacks,
        "abstained": run.abstained,
        "abstain_reason": run.abstain_reason,
        "grounding": run.grounding,
        "counts": {"claims": len(run.claims), "accepted": len(accepted),
                   "rejected": len(rejected), "needs_evidence": len(pending),
                   "passages": len(run.passages),
                   "web_passages": sum(1 for p in run.passages if p.source == "web"),
                   "corpus_passages": sum(1 for p in run.passages if p.source == "corpus")},
        "trace": [step_dict(s) for s in run.trace],
        "totals": totals(run),
    }


def totals(run):
    t = {
        "steps": len(run.trace),
        "prompt_tokens": sum(s.prompt_tokens for s in run.trace),
        "completion_tokens": sum(s.completion_tokens for s in run.trace),
        "latency_ms": sum(s.latency_ms for s in run.trace),
        "web_searches": sum(1 for s in run.trace if s.action == "researcher.web_search"),
        # a run where this is true spent Mesh balance instead of the free OpenAI tokens
        "byok_fallback_steps": sum(1 for s in run.trace if s.byok_fallback),
    }
    costs = [s.cost_usd for s in run.trace if s.cost_usd is not None]
    t["cost_usd_reported"] = round(sum(costs), 6) if costs else None
    return t


def summary(run):
    """Small enough to push on every event."""
    return {"run_id": run.id, "status": run.status, "loopbacks": run.loopbacks,
            "counts": audit(run)["counts"], "totals": totals(run)}


def _persist(run):
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config.RUN_DIR / f"{stamp}-{run.id}.json"
    path.write_text(json.dumps({"run": run.dict(), "audit": audit(run)}, indent=2, default=str),
                    encoding="utf-8")
    return run
