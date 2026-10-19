"""The five agents. One function each, no base class, no registry -- five functions is the
whole abstraction the orchestrator needs.

Each takes (mesh, run) and mutates `run`. Nothing returns prose: planner and drafter answer
under a strict JSON schema, and the verifier's confidence comes from /v1/evaluate as a
calibrated probability rather than a model's opinion of its own certainty.
"""
from datetime import date

from . import config
from .schemas import Claim, Passage, SubQuestion, DRAFT_SCHEMA, GAPS_SCHEMA, PLAN_SCHEMA

# The failure modes the verifier may assign. Deliberately the same vocabulary as
# evals/fixtures/poisoned/draft-v1.json, so the fixture scores what the system actually emits.
FAILURE_MODES = {
    "supported": "The evidence states or directly implies the claim.",
    "contradicts_source": "The evidence addresses the claim but says something different, e.g. a different number.",
    "irrelevant_source": "The evidence is on a related topic but never addresses the claim.",
    "wrong_attribution": "The claim is plausible but this evidence is not what carries it.",
    "overstated": "The evidence partly supports the claim, but the claim is stronger than the evidence allows.",
}

# Which failures a second pass could actually fix. The split is between a claim that is
# *under-evidenced* (go and find better sources) and one that is simply *wrong* or fabricated
# (no amount of searching rescues it, and looping on it just burns searches).
#
# This set used to hold only irrelevant_source and wrong_attribution, and across 99 real
# claims neither ever occurred -- so the loop never once fired. The modes that did occur were
# below_threshold, overstated and contradicts_source. The first two are evidence problems;
# the third is not.
NEEDS_EVIDENCE_MODES = {
    "below_threshold",    # the cited passage roughly supports it but scored under the bar
    "overstated",         # true in direction, too strong for what the evidence shows
    "irrelevant_source",  # cited the wrong passage; the right one may exist
    "wrong_attribution",  # right fact, wrong source
}
# contradicts_source, unsupported and dangling_citation stay terminal: the first disagrees
# with its own evidence, and the other two are drafter faults that a re-draft, not a re-search,
# would have to fix.


# ---- 1. planner ----
def plan(mesh, run):
    """Decompose the question, or refuse it. Abstention is a first-class outcome: a question
    with no answerable evidence base must stop here rather than produce a confident report."""
    m = mesh.as_("planner")
    # The date has to be given, not assumed. A model decides "is this period in the future?"
    # against its own training cutoff unless told otherwise, so a cheaper or older model
    # refuses every question about the current year. gpt-4o-mini abstained on all three
    # research cases for exactly this reason.
    today = date.today()
    out = m.chat_json("planner", [
        {"role": "system", "content":
            f"Today's date is {today:%d %B %Y}. Any period up to and including today is "
            "historical and researchable, however recent it feels to you; only a period after "
            "today is in the future. Do not rely on your own sense of the current date.\n\n"
            "You plan a research investigation. Break the question into independent sub-questions, "
            f"at most {config.MAX_SUBQUESTIONS}, each with 1-3 web search queries that would find "
            "evidence for it.\n\n"
            "Set answerable=false, with a reason, when no amount of searching could settle the "
            "question: it asks for figures from a period after today, names an entity that does "
            "not appear to exist, or is a counterfactual no source can test. Refusing a question "
            "you cannot evidence is a correct answer, not a failure."},
        {"role": "user", "content": run.question},
    ], PLAN_SCHEMA)

    if not out.get("answerable", True):
        run.abstained = True
        run.abstain_reason = out.get("abstain_reason") or "The planner found no answerable evidence base."
        return run

    for i, sq in enumerate(out.get("subquestions", [])[:config.MAX_SUBQUESTIONS], 1):
        run.subquestions.append(SubQuestion(id=f"sq{i}", text=sq["text"], why=sq.get("why", ""),
                                            queries=sq.get("queries", [])[:3]))
    return run


# ---- 2. researcher ----
def research(mesh, run, queries=None, corpus_file_ids=None):
    """Fan out over web search and the uploaded corpus, appending Passages. Deduplicates by
    URL so a loop-back does not re-add what we already have."""
    m = mesh.as_("researcher")
    queries = queries or [q for sq in run.subquestions for q in sq.queries]
    seen = {p.url for p in run.passages}
    n = len(run.passages)

    for query in queries:
        for r in m.web_search(query).get("results", []):
            if not r.get("url") or r["url"] in seen:
                continue
            seen.add(r["url"])
            n += 1
            run.passages.append(Passage(
                id=Passage.new_id(n), url=r["url"], title=r.get("title") or r["url"],
                # page_content is the real evidence; content is a snippet. Falling back to the
                # snippet is honest but weak -- retrieval.page_content_rate tracks how often.
                text=(r.get("page_content") or r.get("content") or "")[:12000],
                source="web", score=r.get("score"),
                truncated=bool(r.get("page_content_truncated")),
            ))

    if corpus_file_ids:
        for query in queries:
            for r in m.corpus_search(query, file_ids=corpus_file_ids):
                n += 1
                run.passages.append(Passage(
                    id=Passage.new_id(n), url=f"corpus://{r.get('file_name', 'file')}#{r.get('chunk_index')}",
                    title=r.get("file_name") or "uploaded document",
                    text=r.get("parent_text") or r.get("text") or "",
                    source="corpus", score=r.get("score"),
                ))
    return run


# ---- 3. drafter ----
def draft(mesh, run):
    """Turn the evidence into discrete claims, each citing passage ids. One claim = one
    checkable statement; the verifier works per claim, so a paragraph-sized claim cannot be
    judged."""
    m = mesh.as_("drafter")
    out = m.chat_json("drafter", [
        {"role": "system", "content":
            "You write research findings as discrete, individually checkable claims. Every claim "
            "cites the passage ids it rests on, using only the ids given. One fact per claim. "
            "Include the figure's unit and its year whenever you state a number. Do not write a "
            "claim the evidence does not carry -- fewer, well-evidenced claims beat broad coverage."},
        {"role": "user", "content": f"QUESTION\n{run.question}\n\nEVIDENCE\n{_evidence_block(run)}"},
    ], DRAFT_SCHEMA)

    run.claims = [
        Claim(id=f"c{i}", text=c["text"], citations=list(dict.fromkeys(c.get("citations", []))))
        for i, c in enumerate(out.get("claims", []), 1)
    ]
    return run


# ---- 4. verifier ----
def verify(mesh, run):
    """Check every claim against the passages it cites.

    Three stages, cheapest first, and the cheap ones are the ones that catch the worst
    failures:
      a) deterministic -- a citation that is not in the retrieval set is a fabrication. No
         model is consulted, and no model's opinion could overturn it.
      b) /v1/evaluate  -- a calibrated support probability plus a failure mode, per claim.
      c) one chat pass -- a sentence of reasoning, only for the claims that failed.
    """
    m = mesh.as_("verifier")
    known = run.passage_ids()

    for claim in run.claims:
        dangling = [c for c in claim.citations if c not in known]
        if not claim.citations:
            claim.verdict, claim.failure_mode = "rejected", "unsupported"
            claim.reason, claim.support = "The claim cites no evidence.", 0.0
            continue
        if dangling:
            claim.verdict, claim.failure_mode = "rejected", "dangling_citation"
            claim.reason = f"Cites {dangling}, which is not in the retrieval set."
            claim.support = 0.0
            continue

        cited = [p for p in run.passages if p.id in claim.citations]
        answers = m.evaluate(
            state={"claim": claim.text,
                   "evidence": [{"id": p.id, "title": p.title, "text": p.text[:6000]} for p in cited]},
            questions={
                "support": {"type": "noul", "instructions":
                    "Probability that the EVIDENCE passages, taken together, state or directly "
                    "imply the CLAIM. Judge only what the evidence says, not whether the claim "
                    "sounds true."},
                "failure": {"type": "choice", "instructions":
                    "Which best describes the relationship between the claim and the evidence?",
                    "criteria": FAILURE_MODES},
            })
        # noul is a probability in [0,1], never a boolean -- threshold it. meshapi.md section 4.
        claim.support = float(answers.get("support", {}).get("noul", 0.0))
        mode = answers.get("failure", {}).get("choice", "supported")
        if claim.support >= config.VERIFY_ACCEPT_THRESHOLD and mode == "supported":
            claim.verdict, claim.failure_mode = "accepted", None
        else:
            claim.failure_mode = mode if mode != "supported" else "below_threshold"
            claim.verdict = ("needs_evidence" if claim.failure_mode in NEEDS_EVIDENCE_MODES
                             else "rejected")

    failed = [c for c in run.claims if c.verdict != "accepted" and not c.reason]
    if failed:
        _explain(m, run, failed)
    return run


def _explain(m, run, failed):
    """One batched chat call for the reasons, not one per claim."""
    listing = "\n".join(
        f"{c.id} [{c.failure_mode}, support={c.support:.2f}] {c.text}\n"
        f"    cites: {', '.join(f'{p.id} ({p.title})' for p in run.passages if p.id in c.citations)}"
        for c in failed)
    out = m.chat("verifier", [
        {"role": "system", "content":
            "For each rejected claim, write one sentence saying what its cited evidence actually "
            "says and how the claim departs from it. Format: '<id>: <sentence>', one per line, "
            "nothing else."},
        {"role": "user", "content": f"EVIDENCE\n{_evidence_block(run)}\n\nREJECTED\n{listing}"},
    ], action="verifier.explain")
    reasons = {}
    for line in out.splitlines():
        cid, _, text = line.partition(":")
        if text.strip():
            reasons[cid.strip()] = text.strip()
    for c in failed:
        c.reason = reasons.get(c.id, f"Rejected: {c.failure_mode}.")


def gap_queries(mesh, run):
    """What the verifier's rejections say we still need. Drives the loop-back."""
    weak = [c for c in run.claims if c.verdict != "accepted"]
    if not weak:
        return []
    m = mesh.as_("verifier")
    out = m.chat_json("verifier", [
        {"role": "system", "content":
            "These claims failed verification. Write up to 4 web search queries that would find "
            "the evidence needed to settle them. Queries only, no commentary."},
        {"role": "user", "content": "\n".join(f"- {c.text} ({c.failure_mode})" for c in weak)},
    ], GAPS_SCHEMA, action="verifier.gaps")
    return out.get("queries", [])[:4]


# ---- 5. synthesizer ----
def synthesize(mesh, run):
    """The final report, written from accepted claims only. Rejected claims are not quietly
    softened and kept -- they are gone, and the audit trail says so."""
    m = mesh.as_("synthesizer")
    accepted = run.accepted()
    if not accepted:
        run.abstained = True
        run.abstain_reason = "No claim survived verification."
        run.report = (f"# {run.question}\n\n**Insufficient evidence.** No claim drawn from the "
                      f"retrieved sources survived verification.\n")
        return run

    numbered = {p.id: i for i, p in enumerate(
        [p for p in run.passages if any(p.id in c.citations for c in accepted)], 1)}
    body = "\n".join(
        f"- {c.text} " + " ".join(f"[{numbered[cid]}]" for cid in c.citations if cid in numbered)
        for c in accepted)
    sources = "\n".join(
        f"[{n}] {p.title} — {p.url}"
        for p in run.passages for n in [numbered.get(p.id)] if n)

    report = m.chat("synthesizer", [
        {"role": "system", "content":
            "Write a research report in Markdown from the verified claims below. Keep every "
            "bracketed citation marker exactly where it sits on its claim. Add no fact that is "
            "not in the claims -- you are organising and connecting them, not researching. "
            "Structure: a two-sentence answer up front, then themed sections, then a short "
            "'What we could not establish' section if any gaps were noted. Do not write the "
            "Sources list; it is appended for you."},
        {"role": "user", "content":
            f"QUESTION\n{run.question}\n\nVERIFIED CLAIMS\n{body}\n\n"
            f"GAPS\n{_gaps_note(run)}"},
    ])
    run.report = f"{report.strip()}\n\n## Sources\n{sources}\n"
    return run


# ---- helpers ----
def _evidence_block(run):
    return "\n\n".join(f"[{p.id}] {p.title} ({p.url})\n{p.text[:6000]}" for p in run.passages)


def _gaps_note(run):
    weak = [c for c in run.claims if c.verdict != "accepted"]
    if not weak:
        return "None."
    return "\n".join(f"- {c.text} (not established: {c.failure_mode})" for c in weak)
