"""ame-kb CLI: versioned ingest, async pipeline, query, search, reindex."""
from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from . import ingest as ingest_mod
from . import graphs as graphs_mod
from . import manifest as manifest_mod
from . import pipeline as pipeline_mod
from . import service as service_mod
from . import versioning as versioning_mod
from .config import get_settings
from .db import get_engine, ping
from .extract import extract
from .reindex import reindex_all
from .store import is_unchanged, node_no, store

app = typer.Typer(
    add_completion=False,
    help="Versioned knowledge-recall and agent-memory core (V6.3).",
)


@app.callback()
def _main(
    graph_no: str = typer.Option(
        None, "--graph-no", help="Target graph (system-generated graph_<id>)."
    ),
    graph_version: int = typer.Option(
        None, "--graph-version", help="Pin a specific version (default: latest)."
    ),
) -> None:
    """Inject the graph context (graph_no + resolved version) for every command.

    Fault tolerant: on a fresh DB (no kg_graph yet) the version lookup is
    skipped/swallowed so init-db can create the tables it depends on.
    """
    graphs_mod.apply_graph_context(graph_no, graph_version)


_SQL_DIR = Path(__file__).resolve().parents[2] / "sql"

# Idempotency: init-db reapplies DDL, so tolerate "already exists" style errors
# from ALTER/CREATE when a column/table is already present, and "can't drop"
# when an index a migration re-scopes was already dropped/renamed.
_IDEMPOTENT_ERRORS = (
    "duplicate column name",
    "already exists",
    "duplicate key name",
    "check that column/key exists",
    "can't drop",
)


def _is_idempotent_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _IDEMPOTENT_ERRORS)


def _run_sql_file(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    # Strip full-line `--` comments so a `;` inside a comment can't be mistaken
    # for a statement separator by the naive split below.
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--")
    )
    statements = [s.strip() for s in body.split(";") if s.strip()]
    # Run each statement in its own transaction so an idempotent failure (e.g.
    # a column that already exists) can be skipped without aborting the rest.
    for stmt in statements:
        try:
            with get_engine().begin() as conn:
                conn.execute(text(stmt))
        except (OperationalError, ProgrammingError) as exc:
            if _is_idempotent_error(exc):
                typer.echo(f"  skip (already applied): {exc.orig}")
                continue
            raise


@app.command("init-db")
def init_db() -> None:
    """Probe connectivity and create the tables. Schema is a fixed ontology."""
    typer.echo("Checking MySQL connectivity...")
    ping()
    typer.echo("  OK")
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        typer.echo(f"Applying DDL from {sql_file.name}...")
        _run_sql_file(sql_file)
    typer.echo("  tables ready")

    # When the Redis backend is active, create its FT index too.
    if get_settings().search_backend.lower() == "redis":
        typer.echo("Creating RediSearch index...")
        from .searchbackend import get_index

        get_index()._ensure_index()  # type: ignore[attr-defined]
        typer.echo("  RediSearch index ready")


@app.command("ingest")
def ingest_cmd(
    source_dir: str = typer.Option(None, help="Override SOURCE_DIR."),
    dry_run: bool = typer.Option(False, help="Extract and print, do not write DB."),
    force: bool = typer.Option(
        False, help="Re-extract even if content hash is unchanged."
    ),
    allow_empty: bool = typer.Option(
        False, help="Build a new version even when nothing changed (managed graphs)."
    ),
    graph_version: int = typer.Option(
        None, "--graph-version", help="(ignored for ingest; a new version is derived)."
    ),
) -> None:
    """Scan sources, LLM-extract entities+relations, and store them.

    Managed graphs (registered via create-graph) ingest from their file manifest
    and derive a new version vN+1: unchanged files inherit their knowledge, only
    changed/new files hit the LLM. Legacy graphs (e.g. default) keep the V3
    directory-scan behaviour unchanged.
    """
    settings = get_settings()
    if graph_version is not None:
        typer.echo(
            "  warning: ingest always derives a new version; --graph-version ignored."
        )

    if graphs_mod.is_managed(settings.graph_no):
        _ingest_managed(settings.graph_no, dry_run=dry_run, force=force, allow_empty=allow_empty)
        return

    root = source_dir or settings.source_dir
    docs = ingest_mod.scan(root)
    if not docs:
        typer.echo(f"No supported files found under {root}")
        raise typer.Exit(code=0)

    typer.echo(f"Found {len(docs)} document(s) under {root}")
    skipped = 0
    for doc in docs:
        if not dry_run and not force and is_unchanged(doc):
            skipped += 1
            typer.echo(f"\n== {doc.doc_id} == (unchanged, skipped)")
            continue
        typer.echo(f"\n== {doc.doc_id} ==")
        result = extract(doc)
        typer.echo(f"  extracted: {len(result.nodes)} node(s), {len(result.edges)} edge(s)")
        for d in result.dropped:
            typer.echo(f"  dropped: {d}")
        if dry_run:
            for n in result.nodes:
                spec = f"/{n.entity_spec}" if n.entity_spec else ""
                typer.echo(
                    f"    node  {n.entity_type}{spec}: {n.name} :: {n.description} "
                    f"{n.properties} src={n.source}"
                )
            for e in result.edges:
                typer.echo(
                    f"    edge  {e.source_name} -{e.label}[{e.confidence}]-> "
                    f"{e.target_name} :: {e.description} src={e.source}"
                )
            continue
        stats = store(result)
        ingest_mod.persist_doc(doc)
        typer.echo(
            "  stored: "
            f"nodes +{stats.nodes_new}/~{stats.nodes_updated}, "
            f"edges +{stats.edges_new}/~{stats.edges_updated}"
        )
    if skipped:
        typer.echo(f"\nSkipped {skipped} unchanged document(s).")


def _ingest_managed(
    graph_no: str, *, dry_run: bool, force: bool, allow_empty: bool
) -> None:
    files = manifest_mod.list_files(graph_no)
    if not files:
        typer.echo(f"Manifest for {graph_no} is empty; add files with add-file.")
        raise typer.Exit(code=0)
    res = versioning_mod.build_next_version(
        graph_no, files, force=force, allow_empty=allow_empty, dry_run=dry_run
    )
    for w in res.warnings:
        typer.echo(f"  warning: {w}")
    c = res.classification
    typer.echo(
        f"Base v{res.base_version if res.base_version else '-'} -> "
        f"target v{res.target_version}"
    )
    typer.echo(
        f"  classify: {len(c.unchanged)} unchanged, {len(c.changed)} changed, "
        f"{len(c.new)} new, {len(c.removed)} removed"
    )
    if dry_run:
        typer.echo("  (dry-run: no version built)")
        return
    if res.skipped:
        typer.echo("  no changes; kept current version (use --allow-empty to force).")
        return
    typer.echo(
        f"  projected: {res.projected_nodes} node(s), {res.projected_edges} edge(s); "
        f"extracted {res.extracted_docs} doc(s)."
    )
    typer.echo(f"Built v{res.target_version} (ACTIVE).")


@app.command("enqueue-ingest")
def enqueue_ingest_cmd(
    force: bool = typer.Option(False, help="Re-extract unchanged documents."),
    allow_empty: bool = typer.Option(False, help="Allow a no-change version."),
    max_attempts: int = typer.Option(3, min=1, help="Automatic task attempts."),
) -> None:
    """Snapshot the active graph manifest and enqueue a durable background ingest."""

    settings = get_settings()
    if not graphs_mod.is_managed(settings.graph_no):
        typer.echo("enqueue-ingest requires a managed graph (--graph-no).")
        raise typer.Exit(code=1)
    try:
        result = service_mod.enqueue_ingest(
            settings.graph_no,
            force=force,
            allow_empty=allow_empty,
            max_attempts=max_attempts,
        )
    except ValueError as exc:
        typer.echo(f"Cannot enqueue: {exc}")
        raise typer.Exit(code=1) from exc
    action = "Enqueued" if result.created else "Already active"
    typer.echo(f"{action}: {result.task_no}")
    if result.warning:
        typer.echo(f"  warning: {result.warning}")


@app.command("worker")
def worker_cmd(
    once: bool = typer.Option(False, help="Process at most one available task."),
    max_tasks: int = typer.Option(
        0, min=0, help="Exit after N tasks; 0 keeps running."
    ),
    worker_id: str = typer.Option(None, help="Worker identity (auto-generated)."),
) -> None:
    """Run the durable ingest worker (Ctrl-C to stop continuous mode)."""

    def _show(result: pipeline_mod.ProcessResult) -> None:
        suffix = f"  {result.error}" if result.error else ""
        retry = " (retry queued)" if result.will_retry else ""
        typer.echo(f"{result.task_no}: {result.status}{retry}{suffix}")

    try:
        results = service_mod.run_worker(
            once=once,
            max_tasks=max_tasks,
            worker_id=worker_id,
            on_result=_show,
        )
    except KeyboardInterrupt:
        typer.echo("Worker stopped.")
        raise typer.Exit(code=0)
    if once and not results:
        typer.echo("No eligible task.")


@app.command("task-status")
def task_status_cmd(task_no: str = typer.Argument(...)) -> None:
    """Show task, run, and per-document checkpoint progress."""

    try:
        snapshot = service_mod.task_status(task_no)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"{snapshot['task_no']} [{snapshot['status']}] graph={snapshot['graph_no']} "
        f"progress={snapshot['progress_current']}/{snapshot['progress_total']} "
        f"attempts={snapshot['attempts']}/{snapshot['max_attempts']}"
    )
    if snapshot["error"]:
        typer.echo(f"  error: {snapshot['error']}")
    run = snapshot["run"]
    if run:
        typer.echo(
            f"  run {run['run_no']} [{run['status']}] "
            f"v{run['base_version'] or '-'} -> v{run['target_version']}"
        )
    for step in snapshot["steps"]:
        typer.echo(
            f"  {step['doc_no']} [{step['status']}] "
            f"attempts={step['attempts']}/{step['max_attempts']}"
        )
        if step["error"]:
            typer.echo(f"    error: {step['error']}")


@app.command("list-tasks")
def list_tasks_cmd(limit: int = typer.Option(20, min=1)) -> None:
    """List recent tasks, scoped to --graph-no when provided/configured."""

    settings = get_settings()
    graph_no = None if settings.graph_no == "default" else settings.graph_no
    for row in service_mod.list_tasks(graph_no, limit):
        typer.echo(
            f"{row['task_no']} [{row['status']}] graph={row['graph_no']} "
            f"progress={row['progress_current']}/{row['progress_total']} "
            f"attempts={row['attempts']}/{row['max_attempts']}"
        )


@app.command("retry-task")
def retry_task_cmd(
    task_no: str = typer.Argument(...),
    max_attempts: int = typer.Option(None, min=1, help="Override retry budget."),
) -> None:
    """Manually requeue a terminal FAILED task, preserving completed checkpoints."""

    try:
        service_mod.retry_task(task_no, max_attempts=max_attempts)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"Requeued {task_no}.")


@app.command("create-graph")
def create_graph_cmd(
    name: str = typer.Option(..., "--name", help="Display name for the graph.")
) -> None:
    """Register a new versioned graph. Prints its system-generated graph_no."""
    graph_no = graphs_mod.create_graph(name)
    typer.echo(graph_no)


@app.command("list-graphs")
def list_graphs_cmd() -> None:
    """List registered graphs and their latest version."""
    graphs = graphs_mod.list_graphs()
    if not graphs:
        typer.echo("No registered graphs.")
        raise typer.Exit(code=0)
    for g in graphs:
        typer.echo(f"  {g.graph_no}  v{g.graph_version}  [{g.status}]  {g.name}")


@app.command("add-file")
def add_file_cmd(
    path: str = typer.Argument(..., help="File or directory to add to the manifest."),
) -> None:
    """Add a file or directory to the active graph's manifest."""
    settings = get_settings()
    if not graphs_mod.is_managed(settings.graph_no):
        typer.echo(
            "add-file requires a managed graph. Create one with create-graph and "
            "pass --graph-no."
        )
        raise typer.Exit(code=1)
    added = manifest_mod.add_file(settings.graph_no, path)
    typer.echo(f"Added {len(added)} file(s) to {settings.graph_no}.")
    for doc_no in added:
        typer.echo(f"  + {doc_no}")


@app.command("remove-file")
def remove_file_cmd(
    doc_no: str = typer.Argument(..., help="doc_no to remove (see list-files).")
) -> None:
    """Soft-remove a file from the active graph's manifest."""
    settings = get_settings()
    if manifest_mod.remove_file(settings.graph_no, doc_no):
        typer.echo(f"Removed {doc_no} from {settings.graph_no}.")
    else:
        typer.echo(f"No manifest entry '{doc_no}' in {settings.graph_no}.")
        raise typer.Exit(code=1)


@app.command("list-files")
def list_files_cmd() -> None:
    """List the active graph's manifest files."""
    settings = get_settings()
    files = manifest_mod.list_files(settings.graph_no)
    if not files:
        typer.echo(f"No files in manifest for {settings.graph_no}.")
        raise typer.Exit(code=0)
    for f in files:
        typer.echo(f"  {f.doc_no}  ({f.source_type})  {f.path}")


@app.command("ingest-url")
def ingest_url_cmd(
    url: str = typer.Option(None, help="A single URL to ingest."),
    url_file: str = typer.Option(
        None, help="Path to a newline-delimited URL manifest (# comments ok)."
    ),
    dry_run: bool = typer.Option(False, help="Fetch + extract and print, no DB."),
    force: bool = typer.Option(
        False, help="Re-extract even if content hash is unchanged."
    ),
) -> None:
    """Fetch web page(s), extract entities+relations, and store them.

    origin_url is recorded on kg_doc / kg_doc_chunk for provenance. Same
    incremental hashing and store path as file ingestion.
    """
    urls = []
    if url:
        urls.append(url)
    if url_file:
        urls.extend(ingest_mod.read_url_list(url_file))
    if not urls:
        typer.echo("Provide --url or --url-file.")
        raise typer.Exit(code=1)

    typer.echo(f"Ingesting {len(urls)} URL(s)")
    skipped = 0
    for u in urls:
        typer.echo(f"\n== {u} ==")
        try:
            doc = ingest_mod.load_url(u)
        except Exception as exc:  # noqa: BLE001 - one bad URL shouldn't abort all
            typer.echo(f"  fetch failed: {exc}")
            continue
        if not dry_run and not force and is_unchanged(doc):
            skipped += 1
            typer.echo("  (unchanged, skipped)")
            continue
        result = extract(doc)
        typer.echo(
            f"  extracted: {len(result.nodes)} node(s), {len(result.edges)} edge(s)"
        )
        for d in result.dropped:
            typer.echo(f"  dropped: {d}")
        if dry_run:
            continue
        stats = store(result)
        ingest_mod.persist_doc(doc)
        typer.echo(
            "  stored: "
            f"nodes +{stats.nodes_new}/~{stats.nodes_updated}, "
            f"edges +{stats.edges_new}/~{stats.edges_updated}"
        )
    if skipped:
        typer.echo(f"\nSkipped {skipped} unchanged URL(s).")


@app.command("query")
def query_cmd(
    name: str = typer.Argument(..., help="Entity name (substring match)."),
    relations: bool = typer.Option(
        True, help="Also show each matched node's direct relations."
    ),
) -> None:
    """Find entities by name and (optionally) list their direct relations."""
    hits = service_mod.find_entities(name)
    if not hits:
        typer.echo(f"No entity matching '{name}'.")
        raise typer.Exit(code=0)
    for h in hits:
        typer.echo(f"[{h.type}] {h.name}  ({h.graph_node_no})")
        if h.properties:
            typer.echo(f"    properties: {h.properties}")
        if relations:
            for r in service_mod.relations_of(h.graph_node_no):
                arrow = "-->" if r.direction == "out" else "<--"
                typer.echo(
                    f"    {arrow} {r.label} [{r.other_type}] {r.other_name}"
                )


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Natural-language query."),
    window: int = typer.Option(
        0, help="Expand evidence lines by ±window around each cited line."
    ),
    trace: bool = typer.Option(
        False, help="Print a recall session trace (pool sizes, tiers, hops)."
    ),
) -> None:
    """Run hybrid recall (FULLTEXT + vector, RRF) and print seeds, neighbors,
    connecting edges, and the original evidence lines."""
    res = service_mod.search(query, window=window, trace=trace)
    for w in res.warnings:
        typer.echo(f"  warning: {w}")

    typer.echo(f"\n# Seeds ({len(res.seeds)})")
    for s in res.seeds:
        spec = f"/{s.entity_spec}" if s.entity_spec else ""
        typer.echo(f"  [{s.type}{spec}] {s.name}  ({s.graph_node_no})")
        if s.description:
            typer.echo(f"      {s.description}")

    typer.echo(f"\n# Neighbors ({len(res.neighbors)})")
    for n in res.neighbors:
        spec = f"/{n.entity_spec}" if n.entity_spec else ""
        typer.echo(f"  [{n.type}{spec}] {n.name}  ({n.graph_node_no})")
        if n.description:
            typer.echo(f"      {n.description}")

    typer.echo(f"\n# Edges ({len(res.edges)})")
    for e in res.edges:
        typer.echo(
            f"  {e.source_node_no} -{e.label}-> {e.target_node_no}"
            + (f"  ({e.description})" if e.description else "")
        )

    typer.echo(f"\n# Evidence lines ({len(res.evidence)})")
    for ev in res.evidence:
        typer.echo(f"  {ev.doc_no}:{ev.line_no}  {ev.content}")

    typer.echo(f"\n# Doc chunks ({len(res.doc_chunks)})")
    for c in res.doc_chunks:
        src = c.origin_url or c.file_path or c.doc_no
        snippet = (c.content or "").replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"
        typer.echo(
            f"  {c.doc_no}#{c.chunk_index} (L{c.line_start}-{c.line_end}) {src}\n"
            f"      {snippet}"
        )

    if res.session is not None:
        s = res.session
        typer.echo("\n# Session trace")
        typer.echo(f"  embedding_available: {s.embedding_available}")
        typer.echo(f"  queries: {s.queries}")
        typer.echo(f"  pool_sizes: {s.pool_sizes}  final_pool: {s.pool_size}")
        typer.echo(f"  tier_used: {s.tier_used}")
        for h in s.hops:
            typer.echo(
                f"  hop {h.hop}: {h.candidates} candidate(s), {h.picked} picked"
            )
        typer.echo(
            f"  counts: seeds={s.seed_count} neighbors={s.neighbor_count} "
            f"edges={s.edge_count} evidence={s.evidence_count} "
            f"doc_chunks={s.doc_chunk_count}"
        )


@app.command("reindex")
def reindex_cmd() -> None:
    """Rebuild the search index (searchable_text + embedding) from current
    nodes/edges/chunks into the active backend. Useful after enabling/rotating
    the embedding endpoint or switching SEARCH_BACKEND."""
    n_nodes, n_edges, n_chunks = reindex_all()
    typer.echo(
        f"Reindexed {n_nodes} node(s), {n_edges} edge(s), {n_chunks} chunk(s)."
    )


@app.command("node-no")
def node_no_cmd(
    type_: str = typer.Argument(...),
    name: str = typer.Argument(...),
    spec: str = typer.Option(
        None, "--spec", help="Archetype (entity_spec); only for Asset nodes."
    ),
) -> None:
    """Print the business key (graph_node_no) for a type/spec/name triple."""
    typer.echo(node_no(type_, spec, name))


@app.command("resolve")
def resolve_cmd(
    type_: str = typer.Option(
        None, "--type", help="Only resolve nodes of this type."
    ),
    limit: int = typer.Option(
        None, help="Candidate top-K per node (default RESOLVE_CANDIDATE_TOPK)."
    ),
    dry_run: bool = typer.Option(
        False, help="Judge and print, do not merge or write."
    ),
) -> None:
    """Cross-document entity fusion: find duplicate nodes (vector/full-text KNN),
    LLM-judge same/related/different, and merge duplicates into a canonical
    survivor. Merges record an alias + a rollback-able audit row."""
    stats = service_mod.resolve_all(type_filter=type_, dry_run=dry_run, limit=limit)
    for j in stats.judgments:
        typer.echo(f"  {j}")
    verb = "would merge" if dry_run else "merged"
    typer.echo(
        f"\nScanned {stats.scanned} node(s); {verb} {stats.merged}, "
        f"skipped {stats.skipped} non-duplicate pair(s)."
    )


@app.command("rollback-merge")
def rollback_merge_cmd(merge_id: str = typer.Argument(..., help="merge_id to undo.")) -> None:
    """Undo a merge from its snapshot (restore both nodes, un-remap edges, drop
    the added aliases)."""
    try:
        service_mod.rollback_merge(merge_id)
    except ValueError as exc:
        typer.echo(f"Cannot rollback: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Rolled back merge {merge_id}.")


@app.command("alias-of")
def alias_of_cmd(
    entity: str = typer.Argument(..., help="Canonical node_no or exact name.")
) -> None:
    """List the aliases that resolve to a canonical entity."""
    aliases = service_mod.alias_of(entity)
    if not aliases:
        typer.echo(f"No aliases for '{entity}'.")
        raise typer.Exit(code=0)
    for a in aliases:
        typer.echo(f"  {a}")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
) -> None:
    """Serve the REST API (requires the `api` extra: pip install ame-kb[api])."""
    try:
        import uvicorn

        from .api import create_app
    except ImportError as exc:
        typer.echo(
            "REST API dependencies missing. Install with: pip install 'ame-kb[api]'"
        )
        raise typer.Exit(code=1) from exc
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    app()
