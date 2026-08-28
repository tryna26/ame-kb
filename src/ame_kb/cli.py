"""ame-kb V3 CLI: initialize, ingest, resolve, and query a graph."""
from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy import text

from . import ingest as ingest_mod
from .config import get_settings
from .db import get_engine, ping
from .extract import extract
from .migrations import ensure_v3_schema, migrate_identity
from .query import find_entities, find_entities_exact, relations_of
from .resolve import alias_of, resolve_all, rollback
from .schema import seed_schema
from .store import is_unchanged, store_document

app = typer.Typer(add_completion=False, help="Minimal knowledge-graph builder (V3).")

_SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def _split_sql_statements(body: str) -> list[str]:
    """Split SQL on semicolons that are outside quoted literals.

    The bootstrap files intentionally stay simple (no DELIMITER/procedures),
    but table comments can contain semicolons.  A plain ``str.split(';')``
    corrupts those CREATE TABLE statements on real MySQL.
    """

    statements: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(body):
        char = body[index]
        current.append(char)
        if quote:
            if char == "\\" and quote in {"'", '"'} and index + 1 < len(body):
                index += 1
                current.append(body[index])
            elif char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 1
                    current.append(body[index])
                else:
                    quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == ";":
            statement = "".join(current[:-1]).strip()
            if statement:
                statements.append(statement)
            current = []
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    if quote:
        raise ValueError(f"unterminated SQL quote {quote!r}")
    return statements


def _run_sql_file(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    # Strip full-line `--` comments so a `;` inside a comment can't be mistaken
    # for a statement separator by the naive split below.
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--")
    )
    statements = _split_sql_statements(body)
    with get_engine().begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


@app.command("init-db")
def init_db() -> None:
    """Probe connectivity, create/upgrade tables, and seed the ontology."""
    typer.echo("Checking MySQL connectivity...")
    ping()
    typer.echo("  OK")
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        typer.echo(f"Applying DDL from {sql_file.name}...")
        _run_sql_file(sql_file)
    typer.echo("Ensuring V3 schema...")
    ensure_v3_schema()
    typer.echo("  tables ready")
    inserted = seed_schema()
    typer.echo(f"Seeded schema: {inserted} new type row(s).")


@app.command("ingest")
def ingest_cmd(
    source_dir: str = typer.Option(None, help="Override SOURCE_DIR."),
    dry_run: bool = typer.Option(False, help="Extract and print, do not write DB."),
    force: bool = typer.Option(
        False, help="Re-extract even if content hash is unchanged."
    ),
) -> None:
    """Scan sources, LLM-extract entities+relations, and store them.

    Incremental: docs whose SHA-256 matches the last run are skipped (unless
    --force or --dry-run). --dry-run never touches the DB, including hashes.
    """
    settings = get_settings()
    root = source_dir or settings.source_dir
    source_id = settings.source_id
    docs = ingest_mod.scan(root)
    if not docs:
        typer.echo(f"No supported files found under {root}")
        raise typer.Exit(code=0)

    typer.echo(f"Found {len(docs)} document(s) under {root}")
    skipped = 0
    for doc in docs:
        if not dry_run and not force and is_unchanged(doc, source_id=source_id):
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
                typer.echo(f"    node  {n.type}: {n.name} {n.properties} src={n.source}")
            for e in result.edges:
                typer.echo(
                    f"    edge  {e.source_name} -{e.label}[{e.confidence}]-> "
                    f"{e.target_name} src={e.source}"
                )
            continue
        stats = store_document(result, doc, source_id=source_id)
        typer.echo(
            "  stored: "
            f"nodes +{stats.nodes_new}/~{stats.nodes_updated}, "
            f"edges +{stats.edges_new}/~{stats.edges_updated}"
        )
    if skipped:
        typer.echo(f"\nSkipped {skipped} unchanged document(s).")


@app.command("query")
def query_cmd(
    name: str = typer.Argument(..., help="Entity name (substring match)."),
    relations: bool = typer.Option(
        True, help="Also show each matched node's direct relations."
    ),
    type_: str = typer.Option(None, "--type", help="Only return this node type."),
    limit: int = typer.Option(20, min=1, help="Maximum canonical nodes to return."),
) -> None:
    """Find canonical nodes by name/alias and optionally show relations."""
    hits = find_entities(name, limit=limit, type_filter=type_)
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


@app.command("node-no")
def node_no_cmd(type_: str = typer.Argument(...), name: str = typer.Argument(...)) -> None:
    """Look up an existing UUID by exact type and canonical name/alias."""
    hits = find_entities_exact(type_, name)
    if not hits:
        typer.echo(f"No stored {type_} node matching '{name}'.")
        raise typer.Exit(code=1)
    if len(hits) > 1:
        typer.echo(f"Ambiguous {type_} name '{name}'; matching node IDs:")
        for hit in hits:
            typer.echo(f"  {hit.graph_node_no}  {hit.name}")
        raise typer.Exit(code=1)
    typer.echo(hits[0].graph_node_no)


@app.command("migrate-identity")
def migrate_identity_cmd() -> None:
    """Migrate legacy name-derived node IDs to stable V3 UUIDs."""
    stats = migrate_identity()
    typer.echo(
        "Identity migration complete: "
        f"nodes {stats.nodes_migrated}, edges {stats.edges_updated}, "
        f"aliases {stats.aliases_added}, "
        f"node contributions {stats.node_contributions_added}, "
        f"edge contributions {stats.edge_contributions_added}."
    )


@app.command("resolve")
def resolve_cmd(
    type_: str = typer.Option(None, "--type", help="Only resolve this node type."),
    dry_run: bool = typer.Option(False, help="Judge and report without merging."),
    limit: int = typer.Option(
        None, min=1, help="Maximum seed nodes to scan in this run."
    ),
) -> None:
    """Resolve duplicate mentions into canonical nodes."""
    stats = resolve_all(type_filter=type_, dry_run=dry_run, limit=limit)
    for judgment in stats.judgments:
        typer.echo(f"  {judgment}")
    verb = "would merge" if dry_run else "merged"
    typer.echo(
        f"Scanned {stats.scanned} node(s), compared {stats.pairs} pair(s); "
        f"{verb} {stats.merged} "
        f"(exact {stats.exact_merged}, high {stats.high_merged}, "
        f"LLM {stats.llm_merged}); skipped {stats.skipped}, failed {stats.failed}."
    )


@app.command("rollback-merge")
def rollback_merge_cmd(
    merge_id: str = typer.Argument(..., help="Merge audit ID to undo.")
) -> None:
    """Restore the two nodes and affected edges from a merge snapshot."""
    try:
        rollback(merge_id)
    except ValueError as exc:
        typer.echo(f"Cannot rollback: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Rolled back merge {merge_id}.")


@app.command("alias-of")
def alias_of_cmd(
    entity: str = typer.Argument(..., help="Canonical/legacy node ID or exact name.")
) -> None:
    """List alternate surface names for a canonical node."""
    aliases = alias_of(entity)
    if not aliases:
        typer.echo(f"No aliases for '{entity}'.")
        raise typer.Exit(code=0)
    for alias in aliases:
        typer.echo(alias)


if __name__ == "__main__":
    app()
