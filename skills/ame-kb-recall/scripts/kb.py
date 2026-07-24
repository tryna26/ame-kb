#!/usr/bin/env python3
"""Thin REST client for an ame-kb server (Knowledge Recall / Agent Memory).

Zero third-party deps: uses only the standard library so any agent can run it
without installing anything. Talks to the FastAPI server started with
``ame-kb serve`` (or ``python -m ame_kb.cli serve``).

Base URL resolution order:
  1. --url flag
  2. AME_KB_URL environment variable
  3. http://127.0.0.1:8000 (local default)

Auth: if AME_KB_TOKEN is set, it is sent as ``Authorization: Bearer <token>``.
(The server does not enforce auth yet; this is forward-compatible plumbing.)

Commands:
  search <query> [--graph NO] [--version N] [--window N] [--state-id ID] [--no-trace] [--json]
  ingest <graph_no> [--force] [--allow-empty]
  tasks  [--graph NO] [--limit N]
  task   <task_no>
  graphs
  entities <graph_no> <name>

Every command exits non-zero on transport/HTTP error and prints a one-line
reason to stderr, so an agent can detect failure cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000"


def _base_url(cli_url: str | None) -> str:
    return (cli_url or os.getenv("AME_KB_URL") or DEFAULT_URL).rstrip("/")


def _request(method: str, url: str, body: dict | None = None) -> dict | list:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = os.getenv("AME_KB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (ValueError, AttributeError):
            pass
        _die(f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        _die(
            f"cannot reach ame-kb at {url} ({exc.reason}). "
            f"Is the server running? Start it with: ame-kb serve"
        )


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


# ---- human-readable rendering ---------------------------------------------


def _render_search(res: dict) -> str:
    out: list[str] = []
    for w in res.get("warnings", []):
        out.append(f"warning: {w}")
    seeds = res.get("seeds", [])
    out.append(f"# Seeds ({len(seeds)})")
    for s in seeds:
        spec = f"/{s['entity_spec']}" if s.get("entity_spec") else ""
        out.append(f"  [{s['type']}{spec}] {s['name']}  ({s['graph_node_no']})")
        if s.get("description"):
            out.append(f"      {s['description']}")
    neighbors = res.get("neighbors", [])
    out.append(f"\n# Neighbors ({len(neighbors)})")
    for n in neighbors:
        spec = f"/{n['entity_spec']}" if n.get("entity_spec") else ""
        out.append(f"  [{n['type']}{spec}] {n['name']}  ({n['graph_node_no']})")
        if n.get("description"):
            out.append(f"      {n['description']}")
    edges = res.get("edges", [])
    out.append(f"\n# Edges ({len(edges)})")
    for e in edges:
        tail = f"  ({e['description']})" if e.get("description") else ""
        out.append(f"  {e['source_node_no']} -{e['label']}-> {e['target_node_no']}{tail}")
    evidence = res.get("evidence", [])
    out.append(f"\n# Evidence lines ({len(evidence)})")
    for ev in evidence:
        out.append(f"  {ev['doc_no']}:{ev['line_no']}  {ev['content']}")
    chunks = res.get("doc_chunks", [])
    out.append(f"\n# Doc chunks ({len(chunks)})")
    for c in chunks:
        src = c.get("origin_url") or c.get("file_path") or c["doc_no"]
        snippet = (c.get("content") or "").replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"
        out.append(
            f"  {c['doc_no']}#{c['chunk_index']} "
            f"(L{c['line_start']}-{c['line_end']}) {src}\n      {snippet}"
        )
    sess = res.get("session")
    if sess:
        out.append("\n# Session trace")
        out.append(f"  embedding_available: {sess['embedding_available']}")
        out.append(f"  queries: {sess['queries']}")
        out.append(
            f"  pool_sizes: {sess['pool_sizes']}  final_pool: {sess['pool_size']}"
        )
        out.append(f"  tier_used: {sess['tier_used']}")
        for h in sess.get("hops", []):
            out.append(
                f"  hop {h['hop']}: {h['candidates']} candidate(s), {h['picked']} picked"
            )
        out.append(
            "  counts: "
            f"seeds={sess['seed_count']} neighbors={sess['neighbor_count']} "
            f"edges={sess['edge_count']} evidence={sess['evidence_count']} "
            f"doc_chunks={sess['doc_chunk_count']}"
        )
    if res.get("state_id"):
        out.append(
            f"\n# State {res['state_id']}: {res.get('explored_total', 0)} node(s) "
            "explored so far (rerun with the same --state-id to dig deeper)."
        )
    return "\n".join(out)


# ---- commands --------------------------------------------------------------


def cmd_search(args) -> None:
    base = _base_url(args.url)
    body = {
        "query": args.query,
        "graph_no": args.graph,
        "graph_version": args.version,
        "window": args.window,
        "trace": not args.no_trace,
        "state_id": args.state_id,
    }
    res = _request("POST", f"{base}/search", body)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(_render_search(res))


def cmd_ingest(args) -> None:
    base = _base_url(args.url)
    body = {"force": args.force, "allow_empty": args.allow_empty}
    res = _request("POST", f"{base}/graphs/{args.graph_no}/ingest", body)
    action = "Enqueued" if res.get("created") else "Already active"
    print(f"{action}: {res.get('task_no')}")
    if res.get("warning"):
        print(f"  warning: {res['warning']}")


def cmd_tasks(args) -> None:
    base = _base_url(args.url)
    q = {"limit": args.limit}
    if args.graph:
        q["graph_no"] = args.graph
    url = f"{base}/tasks?" + urllib.parse.urlencode(q)
    for row in _request("GET", url):
        print(
            f"{row['task_no']} [{row['status']}] graph={row['graph_no']} "
            f"progress={row['progress_current']}/{row['progress_total']} "
            f"attempts={row['attempts']}/{row['max_attempts']}"
        )


def cmd_task(args) -> None:
    base = _base_url(args.url)
    s = _request("GET", f"{base}/tasks/{args.task_no}")
    print(
        f"{s['task_no']} [{s['status']}] graph={s['graph_no']} "
        f"progress={s['progress_current']}/{s['progress_total']} "
        f"attempts={s['attempts']}/{s['max_attempts']}"
    )
    if s.get("error"):
        print(f"  error: {s['error']}")
    run = s.get("run")
    if run:
        print(
            f"  run {run['run_no']} [{run['status']}] "
            f"v{run['base_version'] or '-'} -> v{run['target_version']}"
        )
    for step in s.get("steps", []):
        print(f"  {step['doc_no']} [{step['status']}]")
        if step.get("error"):
            print(f"    error: {step['error']}")


def cmd_graphs(args) -> None:
    base = _base_url(args.url)
    rows = _request("GET", f"{base}/graphs")
    if not rows:
        print("No registered graphs.")
        return
    for g in rows:
        print(f"  {g['graph_no']}  v{g['graph_version']}  [{g['status']}]  {g['name']}")


def cmd_entities(args) -> None:
    base = _base_url(args.url)
    q = urllib.parse.urlencode({"name": args.name})
    rows = _request("GET", f"{base}/graphs/{args.graph_no}/entities?{q}")
    if not rows:
        print(f"No entity matching '{args.name}'.")
        return
    for h in rows:
        print(f"[{h['type']}] {h['name']}  ({h['graph_node_no']})")
        if h.get("properties"):
            print(f"    properties: {h['properties']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kb", description="ame-kb REST client")
    p.add_argument("--url", help=f"Base URL (default {DEFAULT_URL} / $AME_KB_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="Hybrid recall against the knowledge graph.")
    s.add_argument("query")
    s.add_argument("--graph", help="graph_no (default: server default graph).")
    s.add_argument("--version", type=int, help="Pin a graph version (default latest).")
    s.add_argument("--window", type=int, default=0, help="±lines around each cited line.")
    s.add_argument(
        "--state-id",
        dest="state_id",
        help="Progressive exploration: reuse across searches to skip "
        "already-returned nodes (dig deeper without repeats).",
    )
    s.add_argument("--no-trace", action="store_true", help="Suppress the session trace.")
    s.add_argument("--json", action="store_true", help="Print raw JSON instead of text.")
    s.set_defaults(func=cmd_search)

    i = sub.add_parser("ingest", help="Enqueue a background ingest for a graph.")
    i.add_argument("graph_no")
    i.add_argument("--force", action="store_true", help="Re-extract unchanged docs.")
    i.add_argument("--allow-empty", action="store_true", help="Allow a no-change version.")
    i.set_defaults(func=cmd_ingest)

    t = sub.add_parser("tasks", help="List recent ingest tasks.")
    t.add_argument("--graph", help="Filter by graph_no.")
    t.add_argument("--limit", type=int, default=20)
    t.set_defaults(func=cmd_tasks)

    tk = sub.add_parser("task", help="Show one task's progress.")
    tk.add_argument("task_no")
    tk.set_defaults(func=cmd_task)

    g = sub.add_parser("graphs", help="List registered graphs.")
    g.set_defaults(func=cmd_graphs)

    e = sub.add_parser("entities", help="Find entities by name in a graph.")
    e.add_argument("graph_no")
    e.add_argument("name")
    e.set_defaults(func=cmd_entities)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
