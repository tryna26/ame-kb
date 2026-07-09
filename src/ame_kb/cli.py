"""ame-kb V3 CLI: init-db, ingest, query, search, reindex."""
from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from . import ingest as ingest_mod
from .config import get_settings
from .db import get_engine, ping
from .extract import extract
from .query import find_entities, relations_of
from .recall import recall
from .reindex import reindex_all
from .resolve import alias_of, resolve_all, rollback
from .schema import seed_schema
from .store import is_unchanged, node_no, store

app = typer.Typer(add_completion=False, help="Minimal knowledge-graph builder (V3).")

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
    """Probe connectivity, create the tables, and seed the fixed schema."""
    typer.echo("Checking MySQL connectivity...")
    ping()
    typer.echo("  OK")
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        typer.echo(f"Applying DDL from {sql_file.name}...")
        _run_sql_file(sql_file)
    typer.echo("  tables ready")
    inserted = seed_schema()
    typer.echo(f"Seeded schema: {inserted} new type row(s).")

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
) -> None:
    """Scan sources, LLM-extract entities+relations, and store them.

    Incremental: docs whose SHA-256 matches the last run (kg_doc.sha256) are
    skipped (unless --force or --dry-run). --dry-run never touches the DB.
    On store, doc + numbered lines + search index (with embedding) are written.
    """
    settings = get_settings()
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
                typer.echo(
                    f"    node  {n.type}: {n.name} :: {n.description} "
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
    hits = find_entities(name)
    if not hits:
        typer.echo(f"No entity matching '{name}'.")
        raise typer.Exit(code=0)
    for h in hits:
        typer.echo(f"[{h.type}] {h.name}  ({h.graph_node_no})")
        if h.properties:
            typer.echo(f"    properties: {h.properties}")
        if relations:
            for r in relations_of(h.graph_node_no):
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
) -> None:
    """Run hybrid recall (FULLTEXT + vector, RRF) and print seeds, neighbors,
    connecting edges, and the original evidence lines."""
    res = recall(query, window=window)
    for w in res.warnings:
        typer.echo(f"  warning: {w}")

    typer.echo(f"\n# Seeds ({len(res.seeds)})")
    for s in res.seeds:
        typer.echo(f"  [{s.type}] {s.name}  ({s.graph_node_no})")
        if s.description:
            typer.echo(f"      {s.description}")

    typer.echo(f"\n# Neighbors ({len(res.neighbors)})")
    for n in res.neighbors:
        typer.echo(f"  [{n.type}] {n.name}  ({n.graph_node_no})")
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
def node_no_cmd(type_: str = typer.Argument(...), name: str = typer.Argument(...)) -> None:
    """Print the business key (graph_node_no) for a type/name pair."""
    typer.echo(node_no(type_, name))


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
    stats = resolve_all(type_filter=type_, dry_run=dry_run, limit=limit)
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
        rollback(merge_id)
    except ValueError as exc:
        typer.echo(f"Cannot rollback: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Rolled back merge {merge_id}.")


@app.command("alias-of")
def alias_of_cmd(
    entity: str = typer.Argument(..., help="Canonical node_no or exact name.")
) -> None:
    """List the aliases that resolve to a canonical entity."""
    aliases = alias_of(entity)
    if not aliases:
        typer.echo(f"No aliases for '{entity}'.")
        raise typer.Exit(code=0)
    for a in aliases:
        typer.echo(f"  {a}")


if __name__ == "__main__":
    app()
