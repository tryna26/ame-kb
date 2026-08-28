# Node identity is decoupled from the entity name

**Context.** V1/V2 used `graph_node_no = type:slug(name)` — the name *was* the primary
key. V3 introduces cross-document entity resolution, where the same real-world entity
can carry several surface names (e.g. "Ada" / "Ada Lovelace" / "阿达"). A name-derived
key cannot survive a merge: the key itself would change and edges would dangle.

**Decision.** A Node's identity is a synthetic, name-independent key (`node:{uuid}`).
`name` becomes an ordinary attribute; every surface name lives in `aliases`. Merging
picks a **Survivor**, **re-points** the merged-away Node's edges to it (dropping
self-loops and de-duping collapsed `(source,target,label)` edges), then soft-deletes
the loser with a `merged_into` pointer back to the Survivor.

## Considered options

- **Alias→canonical redirect table over a name-derived key** — avoids migrating the
  key, but leaves identity name-derived, so two *already-stored* Nodes judged the same
  later still hit primary-key migration / dangling-pointer problems. Rejected.
- **`type:slug(canonical_name)`** — same problem: choosing a new canonical name on
  merge mutates the key. Rejected.
- **Rebuild the whole output version with fresh UUIDs each run** (what `oa_jarvis`
  `FuseKGGraph` does) — correct for a "recompute the graph every fusion" engine, but
  ame-kb is single-machine and incremental; long-lived stable Node UUIDs with post-hoc
  merge-by-re-point fit better and avoid rebuilding the graph each ingest.

## Consequences

- One-time migration: existing `type:slug(name)` rows must be rewritten to synthetic
  keys, with edge endpoints re-pointed.
- Re-point cleanup (self-loop drop, duplicate-edge dedupe) is mandatory, not optional —
  mirrors `oa_jarvis` `remapHumanEdges`.
