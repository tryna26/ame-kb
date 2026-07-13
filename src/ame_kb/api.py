"""V6.3 REST API (FastAPI) exposing the service facade.

Each endpoint delegates to :mod:`ame_kb.service`, which resolves the target
graph + version and runs the operation inside a request-local graph context, so
concurrent requests never race on process-wide state. The recall endpoint
returns the session trace by default as the "why these results" explanation for
an agent.

FastAPI/uvicorn are optional dependencies (``pip install ame-kb[api]``); this
module is only imported when the API is actually served.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import service as service_mod


class SearchRequest(BaseModel):
    query: str
    graph_no: Optional[str] = None
    graph_version: Optional[int] = None
    window: int = 0
    trace: bool = True


class IngestRequest(BaseModel):
    force: bool = False
    allow_empty: bool = False
    max_attempts: int = 3


class RetryRequest(BaseModel):
    max_attempts: Optional[int] = None


class CreateGraphRequest(BaseModel):
    name: str


def create_app() -> FastAPI:
    app = FastAPI(
        title="ame-kb",
        version="6.3",
        description="Knowledge-recall / agent-memory REST interface.",
    )

    # ----- recall / query -------------------------------------------------

    @app.post("/search")
    def search(req: SearchRequest) -> dict:
        res = service_mod.search(
            req.query,
            graph_no=req.graph_no,
            graph_version=req.graph_version,
            window=req.window,
            trace=req.trace,
        )
        return asdict(res)

    @app.get("/graphs/{graph_no}/entities")
    def entities(
        graph_no: str,
        name: str,
        graph_version: Optional[int] = None,
        limit: int = 20,
    ) -> List[dict]:
        hits = service_mod.find_entities(
            name, graph_no=graph_no, graph_version=graph_version, limit=limit
        )
        return [asdict(h) for h in hits]

    @app.get("/graphs/{graph_no}/entities/{node_no}/relations")
    def relations(
        graph_no: str, node_no: str, graph_version: Optional[int] = None
    ) -> List[dict]:
        rels = service_mod.relations_of(
            node_no, graph_no=graph_no, graph_version=graph_version
        )
        return [asdict(r) for r in rels]

    # ----- graph registry -------------------------------------------------

    @app.get("/graphs")
    def graphs() -> List[dict]:
        return [asdict(g) for g in service_mod.list_graphs()]

    @app.post("/graphs", status_code=201)
    def create_graph(req: CreateGraphRequest) -> dict:
        return {"graph_no": service_mod.create_graph(req.name)}

    @app.get("/graphs/{graph_no}/files")
    def files(graph_no: str) -> List[dict]:
        return [asdict(f) for f in service_mod.list_files(graph_no)]

    # ----- durable pipeline ----------------------------------------------

    @app.post("/graphs/{graph_no}/ingest", status_code=202)
    def ingest(graph_no: str, req: IngestRequest) -> dict:
        try:
            result = service_mod.enqueue_ingest(
                graph_no,
                force=req.force,
                allow_empty=req.allow_empty,
                max_attempts=req.max_attempts,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(result)

    @app.get("/tasks")
    def tasks(graph_no: Optional[str] = None, limit: int = 20) -> List[dict]:
        return service_mod.list_tasks(graph_no, limit)

    @app.get("/tasks/{task_no}")
    def task(task_no: str) -> dict:
        try:
            return service_mod.task_status(task_no)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tasks/{task_no}/retry")
    def retry(task_no: str, req: RetryRequest) -> dict:
        try:
            service_mod.retry_task(task_no, max_attempts=req.max_attempts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task_no": task_no, "status": "QUEUED"}

    return app
