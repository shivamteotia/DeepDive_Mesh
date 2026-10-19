"""The one place that talks to MeshAPI. Every call in the project goes through here.

Why a wrapper rather than the OpenAI SDK directly:

  - Every call is tagged {agent, run_id} so MeshAPI's usage events slice by agent, and every
    call appends a TraceStep carrying X-Request-Id. That is the auditable decision trail, and
    it is only trustworthy if nothing can bypass it.

  - BYOK fallback is silent by design. If our OpenAI key hits a 401 or a rate limit, Mesh
    transparently retries on its own credentials and bills our Mesh balance instead -- the
    only signal is the X-BYOK-Fallback-Triggered header. Unwatched, the free OpenAI tokens
    stop being used and nothing in the app notices. We record both BYOK headers on every step
    and warn once per run.

  - Web search needs the replay/live switch from day one (see evals/README.md). Retrofitting
    determinism onto a live-only client is worse than building it in.

stdlib only, so the eval harness can import this without the app's dependency tree.
"""
import hashlib
import json
import time
import urllib.error
import urllib.request

from . import config
from .schemas import TraceStep


class MeshError(RuntimeError):
    pass


class WebSearchCacheMiss(MeshError):
    """Replay mode found no fixture. Never falls through to the live web: a silent fallthrough
    would lose determinism exactly when we are relying on it."""


class Mesh:
    def __init__(self, run=None, agent="system", on_step=None):
        """on_step(TraceStep) fires for every recorded action. _record is the single choke
        point every call passes through, so hooking it here is what lets the UI show each
        individual agent action live rather than only stage boundaries."""
        if config.missing_key():
            raise MeshError("No API key. Set Mesh_AI_API_Key in .env (or MESH_API_KEY).")
        self.run, self.agent, self.on_step = run, agent, on_step
        self._byok_warned = False

    def as_(self, agent):
        """A view of this client that tags its calls with a different agent role."""
        clone = object.__new__(Mesh)
        clone.run, clone.agent, clone.on_step = self.run, agent, self.on_step
        clone._byok_warned = self._byok_warned
        return clone

    # ---- plumbing ----
    def _request(self, path, payload=None, method="POST", timeout=180):
        url = config.MESH_BASE_URL + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {config.MESH_API_KEY}",
            "Content-Type": "application/json",
        })
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read() or b"{}")
                headers = r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                detail = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                detail = {"raw": raw[:400].decode(errors="replace")}
            # BYOK errors surface with the PROVIDER's status: a 401 here means our OpenAI key
            # was rejected, not our rsk_ key.
            raise MeshError(f"{method} {path} -> {e.code}: {json.dumps(detail)[:400]}") from e
        return body, headers, round((time.perf_counter() - t0) * 1000)

    def _tags(self):
        t = {"agent": self.agent}
        if self.run is not None:
            t["run_id"] = self.run.id
        return t

    def _record(self, action, headers, latency_ms, model=None, usage=None, **detail):
        if self.run is None:
            return
        usage = usage or {}
        byok_used = _header_bool(headers, "X-BYOK-Used")
        byok_fallback = _header_bool(headers, "X-BYOK-Fallback-Triggered")
        if byok_fallback and not self._byok_warned:
            self._byok_warned = True
            print("  ! BYOK fallback triggered: the OpenAI key was rejected or rate limited, so "
                  "Mesh served this on its own credentials and billed the Mesh balance. "
                  "Check the key on the dashboard.")
        step = TraceStep(
            agent=self.agent, action=action, model=model,
            request_id=headers.get("X-Request-Id") if headers else None,
            prompt_tokens=usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or usage.get("output_tokens") or 0,
            cost_usd=usage.get("cost") or usage.get("total_cost_usd"),
            latency_ms=latency_ms, byok_used=byok_used, byok_fallback=byok_fallback,
            detail=detail,
        )
        self.run.trace.append(step)
        if self.on_step:
            try:
                self.on_step(step)
            except Exception as e:  # noqa: BLE001 -- a UI listener must never kill the run
                print(f"  ! on_step listener raised {type(e).__name__}: {e}")

    # ---- chat ----
    def chat(self, role, messages, response_format=None, temperature=0, action=None, **kw):
        """One chat turn as `role`. Returns the message content as a string."""
        model = config.MODELS[role]
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "tags": self._tags(), **kw}
        if response_format:
            payload["response_format"] = response_format
        body, headers, ms = self._request("/chat/completions", payload)
        self._record(action or f"{role}.chat", headers, ms, model=model, usage=body.get("usage", {}))
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise MeshError(f"unexpected chat response: {json.dumps(body)[:300]}") from e

    def chat_json(self, role, messages, response_format, **kw):
        raw = self.chat(role, messages, response_format=response_format, **kw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            # strict json_schema makes this close to impossible; if it happens the schema was
            # rejected and the model fell back to prose, which is worth seeing in full.
            raise MeshError(f"{role} returned non-JSON under a strict schema: {raw[:300]}") from e

    # ---- evaluate: typed decisions with a calibrated probability ----
    def evaluate(self, state, questions, action="verifier.evaluate"):
        payload = {"model": config.EVALUATE_MODEL, "state": state, "questions": questions,
                   "tags": self._tags()}
        body, headers, ms = self._request("/evaluate", payload)
        self._record(action, headers, ms, model=config.EVALUATE_MODEL, usage=body.get("usage", {}))
        return body.get("answers", {})

    # ---- web search, with the replay/live switch ----
    def web_search(self, query, max_results=None, include_page_content=True, **kw):
        max_results = max_results or config.WEBSEARCH_RESULTS
        payload = {"query": query, "max_results": max_results,
                   "include_page_content": include_page_content, **kw}
        if config.WEBSEARCH_PROVIDER:
            payload["provider"] = config.WEBSEARCH_PROVIDER
        key = _cache_key(payload)
        path = config.WEBSEARCH_CACHE / f"{key}.json"

        if config.WEBSEARCH_MODE == "replay":
            if not path.exists():
                raise WebSearchCacheMiss(
                    f"no fixture for {query!r} ({key}). Record it with WEBSEARCH=live, or the "
                    f"query drifted -- freeze the planner too (see evals/README.md).")
            body = json.loads(path.read_text(encoding="utf-8"))
            self._record("researcher.web_search", None, 0, detail_query=query, replayed=True)
            return body

        body, headers, ms = self._request("/web/search", {**payload, "tags": self._tags()})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        results = body.get("results", [])
        with_text = sum(1 for r in results if r.get("page_content"))
        if results and not with_text:
            # the TinyFish trap: snippets only, nothing errored. meshapi.md section 4.
            print(f"  ! web_search returned {len(results)} results and no page text "
                  f"(engine={body.get('provider')}). Reasoning on snippets only.")
        self._record("researcher.web_search", headers, ms,
                     detail_query=query, engine=body.get("provider"),
                     n_results=len(results), n_with_page_content=with_text)
        return body

    # ---- corpus (Files & RAG) ----
    def upload_file(self, filename, data, mime_type, metadata=None, wait=300):
        """Register, PUT the bytes, then poll until embedded. Returns the file status object.

        Three details that bite (meshapi.md section 4): the signed URL expires in 15 minutes,
        `required_headers` are part of the signature so they go up verbatim, and the poll
        needs a deadline -- if the PUT never lands, upload_status stays `pending` forever.
        """
        init, headers, ms = self._request("/files", {
            "file_name": filename, "mime_type": mime_type, "embed": True,
            **({"metadata": metadata} if metadata else {}),
        })
        self._record("corpus.upload_init", headers, ms, detail_file=filename)

        if len(data) > init.get("max_size_bytes", 52428800):
            raise MeshError(f"{filename} is {len(data)} bytes, over the {init['max_size_bytes']} limit")
        put = urllib.request.Request(init["signed_url"], data=data, method="PUT",
                                     headers=init.get("required_headers") or {})
        try:
            urllib.request.urlopen(put, timeout=300).read()
        except urllib.error.HTTPError as e:
            raise MeshError(f"upload PUT rejected ({e.code}). required_headers must go up "
                            f"unchanged, and the signed URL expires after 15 minutes.") from e

        file_id, deadline = init["file_id"], time.monotonic() + wait
        while True:
            status, _, _ = self._request(f"/files/{file_id}", method="GET")
            if status.get("upload_status") == "failed" or status.get("embedding_status") in ("ready", "failed"):
                break
            if time.monotonic() > deadline:
                raise MeshError(f"{filename} not embedded after {wait}s "
                                f"(upload={status.get('upload_status')}, embed={status.get('embedding_status')})")
            time.sleep(3)
        if status.get("embedding_status") != "ready":
            raise MeshError(f"{filename} failed: upload={status.get('upload_status')}, "
                            f"embed={status.get('embedding_status')}, {status.get('last_error_code')}. "
                            f"A failed embedding cannot be re-queued for 30 minutes.")
        self._record("corpus.upload_ready", None, 0, detail_file=filename, file_id=file_id)
        return status

    def corpus_search(self, query, top_k=5, file_ids=None):
        payload = {"query": query, "top_k": top_k, "tags": self._tags()}
        if file_ids:
            payload["file_ids"] = file_ids
        body, headers, ms = self._request("/files/search", payload)
        self._record("researcher.corpus_search", headers, ms,
                     detail_query=query, n_results=len(body.get("results", [])))
        return body.get("results", [])

    # ---- startup checks ----
    def list_models(self):
        body, _, _ = self._request("/models", method="GET")
        return body if isinstance(body, list) else body.get("data", [])

    def balance(self):
        body, _, _ = self._request("/balance", method="GET")
        return body


def _header_bool(headers, name):
    if not headers:
        return None
    v = headers.get(name)
    return None if v is None else str(v).strip().lower() in ("true", "1", "yes")


def _cache_key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
