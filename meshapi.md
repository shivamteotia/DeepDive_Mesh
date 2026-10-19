# MeshAPI — features & models we use

> Sponsor of Agent Arena 2026. **Every LLM/AI call in this project must route through MeshAPI.**
> Docs: https://developers.meshapi.ai · full doc index: https://developers.meshapi.ai/llms.txt
> Catalogue + live prices: https://meshapi.ai/models

## 1. The basics

| Thing | Value |
|---|---|
| Base URL | `https://api.meshapi.ai/v1` |
| Auth | `Authorization: Bearer rsk_...` (Router Service Key) |
| Key in this repo | `.env` → `Mesh_AI_API_Key` (never hardcode, never commit) |
| Wire format | OpenAI-compatible. Also Anthropic-compatible at `/v1/messages` |
| Model ID format | `<brand>/<model-name>`, e.g. `openai/gpt-5.5`, `anthropic/claude-opus-5.5` |
| Pin API version | `X-Mesh-Version` header (contract is versioned by date) |
| Request id | `X-Request-Id` response header — **our audit trail joins on this** |

Drop-in with the OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(api_key=os.environ["Mesh_AI_API_Key"], base_url="https://api.meshapi.ai/v1")
```

## 2. Full feature surface

Everything MeshAPI offers. The **Use** column says what DeepDive does with it.

### Inference
| Feature | Endpoint | Use |
|---|---|---|
| Chat Completions | `POST /v1/chat/completions` | yes — every agent turn |
| Responses API (reasoning effort, hosted tools, background jobs) | `POST /v1/responses` | yes — planner / verifier where reasoning effort helps |
| Messages API (Anthropic-compatible) | `POST /v1/messages` | no |
| Streaming | `stream: true` | yes — live trace in the UI |
| Tool / function calling | `tools` on chat completions | yes — researcher agent's tool loop |
| Structured Output (JSON schema) | `response_format` | yes — claims, citations, verdicts all typed |
| **Evaluate** (typed decision + calibrated probability) | `POST /v1/evaluate` | yes — **verifier agent confidence scores** |
| Moderations | `POST /v1/moderations` | optional input guard |
| Model Compare | `POST /v1/chat/compare` | yes — offline eval, same prompt across N models |
| Batch API | `POST /v1/batches` | yes — running our test set cheaply |
| Auto Routing | `"model": "auto"` + `POST /v1/router/select` | yes — cost/quality routing per agent role |

### Retrieval & grounding
| Feature | Endpoint | Use |
|---|---|---|
| **Web Search** | `POST /v1/web/search` | yes — **researcher agent's source fetcher** |
| **Files & RAG** (upload → embed → hybrid search) | `POST /v1/files`, `/v1/files/embed`, `/v1/files/search`, `GET`/`DELETE /v1/files/{id}` | yes — **user-uploaded corpus** |
| Embeddings | `POST /v1/embeddings` | yes — claim↔evidence similarity, dedupe |
| Memory (per-user facts/guardrails via one header) | `/v1/memory/*` | yes — research preferences, trusted-source rules |
| Prompt Templates (versioned, publish/rollback) | `/v1/templates/*` | yes — agent prompts live here, versioned |

### Multimodal (not core to DeepDive, available)
Image gen/edit `POST /v1/images/generations|edits` · vision (image input on chat) · video gen `POST /v1/video/generations` (async, poll) · TTS `POST /v1/audio/speech` · STT `POST /v1/audio/transcriptions|translations` · voices `GET /v1/audio/voices` · realtime speech-to-speech over WebSocket.

### Platform / governance — where the "auditable decision trail" points come from
| Feature | Endpoint | Use |
|---|---|---|
| Usage summary | `GET /v1/usage/summary` | yes — cost per run |
| Per-request usage events | `GET /v1/usage/events` | yes — tokens + $ per agent step |
| Single request lookup | `GET /v1/usage/requests/{id}` | yes — drill into one trace row |
| **Per-attempt timeline** (every model tried on one request) | `GET /v1/usage/events/{id}/attempts` | yes — shows fallbacks in the audit log |
| Rejections (refused before a model ran) | `GET /v1/usage/rejections` | yes — surface rate-limit / cap hits |
| Live rate limits (RPM/RPD/TPM) | `GET /v1/usage/rate-limits` | yes — dashboard health strip |
| Spend trend | `GET /v1/usage/spend-trend` | yes — run-over-run cost chart |
| Balance | `GET /v1/balance` | yes — guard before a long run |
| Org audit logs (+ CSV export) | `GET /v1/audit-logs`, `/export` | optional |
| `usage` object on every response | response body | yes — tokens incl. cached, per call |
| `tags` + `safety_identifier` on requests | request fields | yes — tag calls by agent role + run id |
| Retry & cross-provider fallback | automatic | yes — free resilience, logged per attempt |
| Response cache (24h, free) + provider prompt caching | automatic / per-provider | yes — cuts re-run cost |
| Service tier `flex` (cheaper, slower, OpenAI only) | `service_tier: "flex"` | optional for batch evals |
| Spend caps, rate limits, model allow-list per key | dashboard / `/v1/keys` | yes — safety net during the hackathon |
| **BYOK** (our own OpenAI key) | dashboard → BYOK | **yes — configured.** See §5 |
| Orgs/teams/roles, notifications/webhooks, Apigee | — | not needed |

### Model catalogue
`GET /v1/models` (keyed, your prices) · `GET /v1/models/search` · `GET /v1/models/{id}/providers` · public and keyless: `GET /v1/public/models`.
The registry is live — **read it at runtime, do not hardcode a model list.**

### Tooling (outside the API)
MCP Server (https://developers.meshapi.ai/agent/mcp) — exposes models, tools, RAG and `evaluate` to any MCP client · Mesh Code desktop app · Mesh CLI · Claude Code routed through Mesh.

## 3. Models

438 text models live right now, 1,300+ total across modalities. Verified from `GET /v1/public/models` on 2026-10-16. Prices are USD per 1M tokens (in / out).

### What DeepDive assigns to each agent

Defaults live in `backend/config.py`, overridable per role from `.env`. **OpenAI everywhere we
have a choice**, because our own OpenAI key is registered as BYOK (§5) — those calls bill to it
rather than to the Mesh balance. All eight ids below verified present via
`python -m backend.selfcheck` on 2026-10-16.

| Agent | Model | in / out | ctx | Why |
|---|---|---|---|---|
| Planner | `openai/gpt-4o-mini` | 0.15 / 0.60 | 128K | small prompt, so the short window is fine |
| Researcher | `openai/gpt-4o-mini` | 0.15 / 0.60 | 128K | **unused** — `research()` makes no chat call, only `/v1/web/search` and `/v1/files/search` |
| Drafter | `openai/gpt-4.1-nano` | 0.10 / 0.40 | 1M | gets the whole evidence block. *Cheaper* than 4o-mini and 8× the window |
| Verifier (per claim) | `typesafe/jev-1.13` via `/v1/evaluate` | 0.042 / free | 32K | a **calibrated probability**, not prose. Not covered by BYOK |
| Verifier (rejection reasons, gap queries) | `openai/gpt-4o-mini` | 0.15 / 0.60 | 128K | differs from the drafter |
| Synthesizer | `openai/gpt-4.1-nano` | 0.10 / 0.40 | 1M | final cited report |
| Utility | `openai/gpt-5-nano` | 0.05 / 0.40 | 400K | cheapest with tools + structured output |
| Embeddings | `openai/text-embedding-3-small` | 0.02 | 8K | BYOK-covered |

**Why not gpt-4o-mini everywhere:** context, not price. The drafter and verifier receive up to
80 passages at 6k chars each; 128K is not enough however cheap it is. `gpt-4.1-nano` costs less
*and* carries 1M.

For a quality run: `DRAFTER_MODEL=openai/gpt-5.5` with
`VERIFIER_MODEL=google/gemini-3.1-pro-preview`. That buys cross-family checking back and leaves
the BYOK key. One frontier-model baseline pass over 8 cases cost about **$3.90**; the cheap set
above does the same work for roughly a thirtieth of it.

Rule of thumb: **never verify with the same model that drafted** — `selfcheck` warns if the two
ids match. Note the compromise BYOK forces: drafter and verifier are both OpenAI, which is
weaker than cross-family checking. Setting `VERIFIER_MODEL=google/gemini-3.1-pro-preview` or
`anthropic/claude-opus-5.5` buys back an independent family at the cost of leaving the free
tokens. `evals/` is what decides whether that trade is worth it — don't guess.

### Frontier text models available (top by context)

**OpenAI** (66 models) — all 1.05M ctx, all tools + structured output
`gpt-6-sol` 2.00/10.00 · `gpt-6-luna` 0.10/0.50 · `gpt-6.1-sol` 2.00/10.00 · `gpt-5.6-sol` 4.00/20.00 · `gpt-5.6-terra` 2.00/12.00 · `gpt-5.6-luna` 0.20/1.20 · `gpt-5.5` 5.00/30.00 · `gpt-5.5-pro` 30.00/180.00 · `gpt-5.4` 2.50/15.00 · `gpt-4.1` 2.00/8.00 · `gpt-4.1-mini` 0.40/1.60 · `gpt-5-nano` 0.05/0.40 · `gpt-oss-20b` 0.07/0.30

**Anthropic** (25) — all 1M ctx
`claude-opus-5.5` 4.00/20.00 · `claude-opus-5` 5.00/25.00 · `claude-opus-5-fast` 10.00/50.00 (thinking) · `claude-opus-4.8` / `4.7` / `4.6` 5.00/25.00 (plus `-coding` variants) · `claude-fable-5.1` / `claude-fable-5` 10.00/50.00 · `claude-haiku-5.5` 0.10/0.50

**Google** (33) — all ~1.05M ctx
`gemini-3.6-flash` 1.50/7.50 · `gemini-3.5-flash` 1.50/9.00 · `gemini-3.5-flash-lite` 0.30/2.50 · `gemini-3.1-pro-preview` 2.00/12.00 · `gemini-3.1-flash-lite` 0.25/1.50 · `gemini-2.5-pro` 1.25/10.00 · `gemini-2.5-flash` 0.30/2.50 · `gemini-2.5-flash-lite` 0.10/0.40

**DeepSeek** (20) — `deepseek-v4-pro` 0.66/1.98 (1.05M) · `deepseek-v4-flash` 0.09/0.18 (1M) · `deepseek-v4.1-flash` 0.15/0.60 · `deepseek-v3.2` 0.62/1.85 · `deepseek-v3.2-exp` 0.27/0.41 · `deepseek-v3.1-terminus` 0.27/1.00

**Qwen** (78) — `qwen3.8-max` 2.00/6.00 · `qwen3.7-max` 2.50/7.50 · `qwen3.7-plus` 0.32/1.28 · `qwen3.7-flash` 0.03/0.13 · `qwen3.5-plus` 0.40/2.40 · `qwen3.5-flash` 0.10/0.40 · `qwen3-coder-plus` 0.65/3.25 · `qwen-flash` 0.05/0.40 — all 1M ctx

**Z.ai GLM** (13) — `glm-5.2` 0.94/2.95 · `glm-5.1` 1.33/4.18 · `glm-5` 1.00/3.20 · `glm-4.7` 0.60/2.20 · `glm-4.7-flash` 0.07/0.40 · `glm-4.6` 0.50/2.00

Also on the gateway: Meta Llama, Mistral, Cohere Command, NVIDIA Nemotron, Amazon Nova (`nova-2-lite-v1` 0.30/2.50, 1M ctx), AI21 Jamba, xAI, Moonshot, Sarvam (Indian languages), plus image/video/TTS brands (Wan, Qwen-Image, Z-Image, Kandinsky, ElevenLabs voices, BytePlus Seedance video).

### Cheapest tool + structured-output models (for high-fan-out agents)
`meta-llama/meta-llama-3.1-8b-instruct` 0.02/0.05 · `mistralai/mistral-nemo` 0.029/0.03 · `qwen/qwen-2.5-7b-instruct` 0.04/0.10 · `openai/gpt-5-nano` 0.05/0.40 · `deepseek-ai/deepseek-v4-flash-0731` 0.06/0.18 · `nvidia/nemotron-nano-9b-v2` 0.06/0.23

### Embeddings
`qwen/qwen3-embedding-8b` 0.01 (32K) · `qwen/qwen3-embedding-4b` 0.02 · `baai/bge-m3` 0.01 (8K, multilingual) · `google/embeddinggemma-300m` 0.002 (2K) · `nvidia/nemotron-3-embed-8b` 0.035 (32K) · `cohere/embed-v4` 0.12 · `google/gemini-embedding-2-preview` 0.25 · `openai/text-embedding-3-small`

### Evaluation
`typesafe/jev-1.13` — 32K ctx, **$0.042 in, output free**. The only evaluation model. Beta endpoint; the key needs the `evaluate:write` scope (an unrestricted key already has it).

## 4. Gotchas we must respect

- **Web search:** `page_content` is **TinyFish only**, and pinning `provider` **turns failover off**. Native is tried first by default, so an unpinned call usually comes back with no page text. Page content costs $0.001 per returned result on top of the $0.005 search and takes 3–14s instead of ~2s. Check `page_content_truncated` before treating text as a whole page.
- **RAG upload is two steps:** `POST /v1/files` returns a `signed_url` that **expires in 15 minutes**; PUT the bytes with every header in `required_headers` **verbatim** (they are part of the signature). 50 MB cap. Then poll `GET /v1/files/{id}` until `embedding_status == "ready"` — with a deadline, or it hangs forever. A failed embedding cannot be re-queued for 30 minutes. RAG needs a dashboard-created key, not one minted by an admin key.
- **Evaluate `noul` is a probability, not a boolean.** 0.8 means "probably". Threshold it.
- **Evaluate `score` legend is keyed by array position** — reordering `criteria` silently changes the meaning of every past score. Treat the order as schema.
- **Evaluate rejects unknown fields** (a typo is a 422, not ignored). At least 2 criteria on `choice`/`score`; none allowed on `noul`.
- `supports_structured_output` is **False on several Claude models** (`claude-opus-5.5`, `4.7`, `4.8`, `claude-haiku-5.5`) — check the flag before sending `response_format`, or use tool-calling to get structure instead.
- Billing on an unpinned web search follows whichever engine answered — a failover to Tavily costs $0.008, not $0.005.
- Free models are effectively nonexistent (only `openai/omni-moderation-latest`). Budget real money.
- **Measured costs, 2026-10-17.** TinyFish with `include_page_content` bills ~**$0.0100 per
  search** in practice — the documented $0.005 base plus $0.001 per returned result with page
  text, which at ~5 page-results per search doubles it. 205 searches cost $2.053; the
  documented formula predicted $2.063, so it is accurate to 0.5%.
- **A key carries a spend cap as well as a balance, and the cap bites first.** Ours was $5.00
  and returned `402 spend_limit_exceeded` at $5.0075 while the balance still showed $1.18.
  Raising it is a dashboard action.
- **Once models are cheap, web search dominates.** On the cheap set a full 10-case eval pass
  costs ~$0.76 of search against ~$0.06 of tokens. The lever is replay mode and
  `MAX_SUBQUESTIONS`, not the model picker.
- The model registry is live and models can be disabled. Read `GET /v1/models` at startup.

## 5. BYOK — our OpenAI key

An OpenAI key is registered on the Mesh dashboard (BYOK → OpenAI), so requests for OpenAI
models use it upstream. Rule 1 still holds: the call goes to Mesh, Mesh uses our credential.

- **Platform fee:** 5% of the request's cost at Mesh list price. **First 1,000,000 BYOK tokens
  each month are fee-free.** The fee counts against spend caps.
- **Not covered by it:** `typesafe/jev-1.13` and `/v1/web/search` bill to the Mesh balance as
  normal. Those are our real spend during development — web search at $0.005–0.008 per call
  dominates, not tokens.
- **Errors carry the provider's status.** A `401` on an OpenAI model means *the OpenAI key* was
  rejected, not our `rsk_` key.
- **Fallback is silent and ON by default.** If the OpenAI key hits a 401 or a rate limit, Mesh
  transparently retries on its own credentials and bills the Mesh balance. The only signal is
  the `X-BYOK-Fallback-Triggered` response header, so `backend/mesh.py` records it on every
  trace step, warns once per run, and `orchestrator.totals()` reports
  `byok_fallback_steps`. **Watch that number** — unwatched, the free tokens quietly stop being
  used. Set `allow_fallback: false` on the key if you'd rather the request fail loudly.
  (`X-Mesh-Routing-Fallback` is a different thing: models and providers, not credentials.)
- **Allowed models** on the key, if set, decides which models it serves; a model outside the
  list is served by Mesh's credentials instead (with fallback on) and shows `X-BYOK-Used: false`.
- Registering, rotating and deleting provider keys is a **dashboard action only** — an `rsk_`
  or `mak_` key gets 401 on `/v1/provider-keys`.

## 6. Learning resources
- MeshAPI crash course — RAG, LangChain multi-agent, built-in RAG/image/audio/video, MCP server, CLI agent: https://www.youtube.com/watch?v=yAczx0_NtyU
- Hackathon starter repo: https://github.com/d-hackmt/meshapi
