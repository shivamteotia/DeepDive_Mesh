"""The typed objects every agent passes to the next one.

Two rules behind these shapes:

1. A claim carries its citations, and a citation points at a Passage we actually retrieved.
   That is what makes `ground.hallucinated_citation_rate` a free deterministic check rather
   than a judging call: a citation whose passage_id is not in the retrieval set is a
   fabrication, full stop, and no model has to be asked.

2. Nothing is prose. Agents return these schemas via response_format, so there is no parsing
   and no repair step. `json_schema()` below is what goes on the wire.
"""
from dataclasses import dataclass, field, asdict
from typing import Literal
import uuid

Verdict = Literal["accepted", "rejected", "needs_evidence"]


@dataclass
class Passage:
    """One retrieved piece of evidence. The only thing a claim may cite."""
    id: str
    url: str
    title: str
    text: str
    source: Literal["web", "corpus"] = "web"
    score: float | None = None
    truncated: bool = False

    @staticmethod
    def new_id(n):
        return f"p{n}"


@dataclass
class Claim:
    id: str
    text: str
    citations: list[str] = field(default_factory=list)   # Passage.id values
    verdict: Verdict | None = None
    support: float | None = None                          # calibrated probability from /v1/evaluate
    reason: str | None = None
    failure_mode: str | None = None                       # set when rejected; see evals fixture
    subquestion: str | None = None


@dataclass
class SubQuestion:
    id: str
    text: str
    why: str = ""
    queries: list[str] = field(default_factory=list)


@dataclass
class TraceStep:
    """One agent action, joinable to MeshAPI's own logs by request_id."""
    agent: str
    action: str
    model: str | None = None
    request_id: str | None = None          # X-Request-Id -> GET /v1/usage/requests/{id}
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    byok_used: bool | None = None          # X-BYOK-Used
    byok_fallback: bool | None = None      # X-BYOK-Fallback-Triggered -- see note in mesh.py
    detail: dict = field(default_factory=dict)


@dataclass
class Run:
    question: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Literal["running", "done", "failed", "abstained"] = "running"
    subquestions: list[SubQuestion] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    report: str = ""
    loopbacks: int = 0
    abstained: bool = False
    abstain_reason: str = ""
    trace: list[TraceStep] = field(default_factory=list)

    def passage_ids(self):
        return {p.id for p in self.passages}

    def accepted(self):
        return [c for c in self.claims if c.verdict == "accepted"]

    def dict(self):
        return asdict(self)


# ---- wire schemas for response_format ----

def json_schema(name, properties, required):
    """OpenAI strict json_schema envelope. additionalProperties must be false and every
    property must be required, or a strict-mode model rejects the schema outright."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


PLAN_SCHEMA = json_schema(
    "plan",
    {
        "answerable": {"type": "boolean"},
        "abstain_reason": {"type": "string"},
        "subquestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "why": {"type": "string"},
                    "queries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "why", "queries"],
                "additionalProperties": False,
            },
        },
    },
    ["answerable", "abstain_reason", "subquestions"],
)

DRAFT_SCHEMA = json_schema(
    "draft",
    {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "citations"],
                "additionalProperties": False,
            },
        }
    },
    ["claims"],
)

GAPS_SCHEMA = json_schema(
    "gaps",
    {"queries": {"type": "array", "items": {"type": "string"}}},
    ["queries"],
)
