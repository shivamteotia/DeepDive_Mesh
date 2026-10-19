"""Model choice and runtime switches, all overridable from .env.

Model ids live here and nowhere else. `python -m backend.selfcheck` validates every id in
this module against GET /v1/models, so a model the platform disabled fails loudly at startup
instead of mid-run.

BYOK: an OpenAI key is registered on the Mesh dashboard, so OpenAI models bill to that key
(5% of Mesh list price as a platform fee, first 1M BYOK tokens/month fee-free) instead of the
Mesh balance. Hence OpenAI defaults everywhere we have a choice. Rule 1 still holds: the call
goes to Mesh, Mesh uses our upstream credential.

Not covered by the OpenAI key, and billed to the Mesh balance normally:
  - typesafe/jev-1.13   (the only evaluation model; $0.042/1M in, output free)
  - /v1/web/search      ($0.005-0.008 per call, priced per call not per token)
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass


def _env(name, default):
    return os.environ.get(name, default)


# ---- auth ----
# .env uses Mesh_AI_API_Key; CI uses the MESH_API_KEY repo secret. Accept either.
MESH_API_KEY = _env("Mesh_AI_API_Key", "") or _env("MESH_API_KEY", "")
MESH_BASE_URL = _env("MESH_BASE_URL", "https://api.meshapi.ai/v1")

# ---- models, one per agent role ----
# Cheap by default. The first baseline run spent 540k prompt tokens on frontier models for a
# single pass over 8 cases; this set does the same work for roughly a thirtieth of the cost.
#
# The planner is NOT on the cheapest model, on purpose. It makes one ~400-token call per run,
# so its model costs fractions of a cent whatever you pick — the $3.90 baseline went on the
# drafter and synthesizer, not here. gpt-4o-mini could not decide whether a period was past or
# future even when the prompt stated today's date ("since 2023" came back as "after today"),
# and abstained on every research question. Optimise where the tokens actually are.
#
# Context, not price, is the binding constraint. The drafter and verifier are handed the whole
# evidence block — 80 passages at up to 6k chars each — so gpt-4o-mini's 128K window is not
# enough for them however cheap it is. gpt-4.1-nano is *cheaper* than 4o-mini (0.10/0.40
# against 0.15/0.60) and carries 1M, so it takes the two big-prompt roles.
#
# Drafter and verifier MUST differ: a model is a poor judge of its own output. Per-claim
# support comes from /v1/evaluate, not from the verifier's chat model, so that model only
# writes rejection reasons and gap queries — cheap is fine there.
#
# For a quality run, raise them: DRAFTER_MODEL=openai/gpt-5.5 with
# VERIFIER_MODEL=google/gemini-3.1-pro-preview buys cross-family checking back. evals/ is how
# you find out whether the difference is real rather than assumed.
MODELS = {
    "planner": _env("PLANNER_MODEL", "openai/gpt-4.1-mini"),       # 0.40/1.60, 1M -- see note below
    "researcher": _env("RESEARCHER_MODEL", "openai/gpt-4o-mini"),  # unused: research() makes no chat call
    "drafter": _env("DRAFTER_MODEL", "openai/gpt-4.1-mini"),       # 0.40/1.60, 1M ctx
    "verifier": _env("VERIFIER_MODEL", "openai/gpt-4o-mini"),      # must differ from the drafter
    "synthesizer": _env("SYNTHESIZER_MODEL", "openai/gpt-4.1-mini"),
    "utility": _env("UTILITY_MODEL", "openai/gpt-5-nano"),         # 0.05/0.40, cheapest with tools+schema
}
EVALUATE_MODEL = _env("EVALUATE_MODEL", "typesafe/jev-1.13")
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "openai/text-embedding-3-small")

# ---- web search ----
# "live"   hits /v1/web/search and caches the response
# "replay" serves from the cache only; a miss raises. evals gate in replay mode -- see
#          evals/README.md for why the live web can never gate.
WEBSEARCH_MODE = _env("WEBSEARCH", "live")
WEBSEARCH_CACHE = Path(_env("WEBSEARCH_CACHE", Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "websearch"))
# page_content is TinyFish-only, and pinning the provider disables engine failover. We pin
# anyway: a researcher reading snippets instead of pages is the failure this whole project
# exists to avoid. See meshapi.md section 4.
WEBSEARCH_PROVIDER = _env("WEBSEARCH_PROVIDER", "tinyfish")
WEBSEARCH_RESULTS = int(_env("WEBSEARCH_RESULTS", "6"))

# ---- the verification loop ----
VERIFY_ACCEPT_THRESHOLD = float(_env("VERIFY_ACCEPT_THRESHOLD", "0.70"))  # noul is a probability, not a bool
MAX_LOOPBACKS = int(_env("MAX_LOOPBACKS", "2"))      # hard stop: a loop that never converges must still end
MAX_SUBQUESTIONS = int(_env("MAX_SUBQUESTIONS", "5"))

RUN_DIR = Path(_env("RUN_DIR", Path(__file__).resolve().parents[1] / "runs"))


def missing_key():
    return not MESH_API_KEY
