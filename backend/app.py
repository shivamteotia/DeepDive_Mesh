"""FastAPI surface.

    uvicorn backend.app:app --reload

    GET  /                               the UI (frontend/index.html)
    GET  /api/health                     key present, models reachable, balance
    POST /api/research                   {question, corpus_file_ids?} -> report + audit
    GET  /api/research/stream?question=  the same run as Server-Sent Events, stage by stage
                                         &corpus_file_ids=... repeated, one per document
    POST /api/corpus                     upload a document into MeshAPI RAG
    GET  /api/runs                       past runs, newest first
    GET  /api/runs/{run_id}              one run: report, audit trail, full trace

Web search is the primary source and always runs. Uploaded documents are searched in addition
to it, never instead of it -- see agents.research().
"""
import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import config
from .mesh import Mesh, MeshError
from .orchestrator import audit, run_research

app = FastAPI(title="DeepDive", description="Self-verifying research analyst — Agent Arena 2026")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Ask(BaseModel):
    question: str
    corpus_file_ids: list[str] | None = None


@app.get("/api/health")
def health():
    out = {"key_present": not config.missing_key(), "websearch_mode": config.WEBSEARCH_MODE,
           "models": config.MODELS, "evaluate_model": config.EVALUATE_MODEL}
    if config.missing_key():
        out["error"] = "No API key. Set Mesh_AI_API_Key in .env"
        return out
    try:
        mesh = Mesh()
        ids = {m["id"] for m in mesh.list_models()}
        wanted = set(config.MODELS.values()) | {config.EVALUATE_MODEL, config.EMBEDDING_MODEL}
        out["models_reachable"] = True
        out["models_missing"] = sorted(wanted - ids)  # the registry is live; ids can disappear
        out["balance"] = mesh.balance()
    except MeshError as e:
        out["models_reachable"] = False
        out["error"] = str(e)
    return out


@app.post("/api/research")
def research(ask: Ask):
    if not ask.question.strip():
        raise HTTPException(422, "question is empty")
    try:
        run = run_research(ask.question, corpus_file_ids=ask.corpus_file_ids)
    except MeshError as e:
        raise HTTPException(502, str(e)) from e
    return {"run_id": run.id, "status": run.status, "report": run.report, "audit": audit(run)}


@app.get("/api/research/stream")
def research_stream(question: str, corpus_file_ids: list[str] = Query(default=[])):
    """One SSE event per stage, so the UI can show the pipeline working rather than a spinner.
    The orchestrator runs on a thread and pushes events into a queue; None closes the stream.

    EventSource can only issue a GET, so the uploaded documents arrive as repeated
    ?corpus_file_ids= parameters rather than in a body. Web search always runs; these only
    add to it.
    """
    if not question.strip():
        raise HTTPException(422, "question is empty")
    events: queue.Queue = queue.Queue()

    def work():
        try:
            run = run_research(question, corpus_file_ids=corpus_file_ids or None,
                               on_event=lambda kind, data: events.put((kind, data)))
            events.put(("report", {"run_id": run.id, "status": run.status, "report": run.report,
                                   "audit": audit(run)}))
        except Exception as e:  # noqa: BLE001 -- already reported as an error event by the orchestrator
            events.put(("error", {"error": f"{type(e).__name__}: {e}"}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = events.get()
            if item is None:
                return
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/corpus")
async def upload_corpus(file: UploadFile = File(...)):
    data = await file.read()
    try:
        status = Mesh().upload_file(file.filename, data,
                                   file.content_type or "application/octet-stream")
    except MeshError as e:
        raise HTTPException(502, str(e)) from e
    return {"file_id": status["file_id"], "file_name": status.get("file_name"),
            "embedding_status": status.get("embedding_status"),
            "total_tokens": status.get("total_tokens")}


@app.get("/api/runs")
def list_runs(limit: int = 20):
    if not config.RUN_DIR.exists():
        return {"runs": []}
    out = []
    for path in sorted(config.RUN_DIR.glob("*.json"), reverse=True)[:limit]:
        a = json.loads(path.read_text(encoding="utf-8"))["audit"]
        out.append({"run_id": a["run_id"], "question": a["question"], "status": a["status"],
                    "counts": a["counts"], "totals": a["totals"], "file": path.name})
    return {"runs": out}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    for path in sorted(config.RUN_DIR.glob(f"*-{run_id}.json"), reverse=True):
        return json.loads(path.read_text(encoding="utf-8"))
    raise HTTPException(404, f"no run {run_id}")


# ---- the UI ----
# One static file, no build step. Mounted last so it cannot shadow an /api route.
FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


@app.get("/")
def index():
    if not FRONTEND.exists():
        raise HTTPException(404, f"{FRONTEND} is missing")
    return FileResponse(FRONTEND, media_type="text/html")
