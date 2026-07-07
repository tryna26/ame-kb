"""ame-kb V1 CLI: init-db, ingest, query."""
from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy import text

from . import ingest as ingest_mod
from .config import get_settings
from .db import get_engine, ping
from .extract import extract
from .query import find_entities, relations_of
from .schema import seed_schema
from .store import is_unchanged, mark_processed, node_no, store

app = typer.Typer(add_completion=False, help="Minimal knowledge-graph builder (V2).")

_SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def _run_sql_file(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    # Strip full-line `--` comments so a `;` inside a comment can't be mistaken
    # for a statement separator by the naive split below.
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--")
    )
    statements = [s.strip() for s in body.split(";") if s.strip()]
    with get_engine().begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


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
                typer.echo(f"    node  {n.type}: {n.name} {n.properties} src={n.source}")
            for e in result.edges:
                typer.echo(
                    f"    edge  {e.source_name} -{e.label}[{e.confidence}]-> "
                    f"{e.target_name} src={e.source}"
                )
            continue
        stats = store(result)
        mark_processed(doc)
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


@app.command("node-no")
def node_no_cmd(type_: str = typer.Argument(...), name: str = typer.Argument(...)) -> None:
    """Print the business key (graph_node_no) for a type/name pair."""
    typer.echo(node_no(type_, name))


if __name__ == "__main__":
    app()
