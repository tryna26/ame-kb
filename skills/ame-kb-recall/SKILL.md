---
name: ame-kb-recall
description: Query and update a shared ame-kb Knowledge Recall / Agent Memory server over its REST API. Use when the agent needs project/team memory — prior decisions, architecture, code structure, or document knowledge — to answer a question (recall), or to feed new/changed source documents into the shared graph (ingest). Also covers checking ingest task progress and listing graphs. Prefer this over guessing when the answer likely lives in the team knowledge base.
metadata:
  short-description: Recall from / ingest into a shared ame-kb server
---

# ame-kb Recall (Agent Memory)

ame-kb is a **Knowledge Recall / Agent Memory** layer: source documents are
turned into a graph (entities, relations, evidence lines, doc chunks), and a
hybrid recall pipeline (full-text + vector, RRF) answers natural-language
questions with the original evidence. This skill talks to a **shared, running
ame-kb server** over REST via `scripts/kb.py` (standard-library only, no install).

## When to use

- **Recall** (`search`): the user's question likely depends on team/project
  knowledge you don't have in context — past decisions, why something was built
  a certain way, where a concept lives, how modules relate. Recall first, then
  answer grounded in the returned seeds + evidence lines.
- **Ingest** (`ingest`): new or changed documents should enter the shared graph.
  Ingest is asynchronous — it enqueues a background task; poll with `task`.
- **Do NOT use** for general knowledge, or when the answer is already in context.

## Setup

Point the client at the server (once per session):

```
export AME_KB_URL=http://<host>:8000      # default http://127.0.0.1:8000
export AME_KB_TOKEN=<token>               # optional; sent as Bearer if set
```

Run commands with the repo's Python (or any Python 3.8+):

```
python scripts/kb.py <command> ...
```

## Commands

```
# Recall: returns seeds, neighbors, edges, evidence lines, doc chunks, + trace
python scripts/kb.py search "why did we pick RediSearch for vectors?" --graph graph_abc

# Narrow/pin: choose a graph, pin a version, widen evidence context
python scripts/kb.py search "<q>" --graph graph_abc --version 3 --window 2

# Dig deeper: reuse the same --state-id across follow-ups to get NEW nodes each
# time (already-returned nodes are excluded). Use a fresh id to start over.
python scripts/kb.py search "how does login work?" --graph graph_abc --state-id s1
python scripts/kb.py search "what else?"           --graph graph_abc --state-id s1

# Raw JSON (when you need to parse fields programmatically)
python scripts/kb.py search "<q>" --graph graph_abc --json

# Ingest (async): enqueue a background build for a managed graph
python scripts/kb.py ingest graph_abc            # add --force to re-extract unchanged
python scripts/kb.py tasks --graph graph_abc     # list recent tasks
python scripts/kb.py task task_<id>              # poll one task to completion

# Discovery
python scripts/kb.py graphs                       # list graphs + latest version
python scripts/kb.py entities graph_abc "Aurora"  # find entities by name
```

## Reading a recall result

- **Seeds** — the strongest matches; the primary anchors for the answer.
- **Neighbors** — one/multi-hop related nodes that add context.
- **Edges** — how the returned nodes connect (labelled relations).
- **Evidence lines** — `doc_no:line_no  content`: the *original text* backing the
  answer. **Ground your answer in these**, and cite them.
- **Doc chunks** — fallback passages retrieved even when the graph is sparse.
- **State line** — when `--state-id` is used: how many distinct nodes have been
  explored under that id so far. Rerun the same id to page through more of the
  graph without repeats; switch ids (or omit) to reset.
- **Session trace** — *why* these results: `embedding_available` (was vector
  search active), `queries` (rewrites used), `pool_sizes`/`final_pool`,
  `tier_used` (retry ladder), per-hop candidate/picked counts, and final counts.
  Use it to judge confidence: empty pool or `embedding_available: false` +
  zero seeds means the graph likely lacks this knowledge — say so instead of
  inventing an answer.

## Failure handling

The script exits non-zero and prints one line to stderr on error:
- "cannot reach ame-kb …" → the server isn't running or the URL is wrong. Ask
  the user to start it (`ame-kb serve`) or fix `AME_KB_URL`. Do not fabricate.
- "HTTP 400 … managed graph" → `ingest` targeted a graph with no manifest, or an
  unmanaged/default graph. Check `graphs` and the graph's files first.
- "HTTP 404" (task) → the task_no doesn't exist.

## Notes

- Recall is **read-only and safe**; ingest **mutates the shared graph** — confirm
  the target `graph_no` before ingesting, since teammates share it.
- If unsure which graph holds the knowledge, run `graphs` first.
- `--graph` omitted uses the server's default graph (often `default`), which may
  not be the team graph — prefer passing an explicit `graph_no`.
