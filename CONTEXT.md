# ame-kb

A minimal knowledge-graph builder: scan local documents → LLM-extract entities and
relations → store in MySQL → query by name and one-hop relations. This glossary pins
the language used while designing cross-document **entity resolution** (V3).

## Language

### Graph structure

**Node**:
A resolved entity instance stored in `kg_graph_node`. After V3, one Node stands for
one real-world entity, no matter how many documents mention it.
_Avoid_: "entity" (ambiguous here — see Entity Type).

**Entity Type**:
A type definition (schema-layer row) in `kg_domain_entity`, e.g. `Person`, `works_for`.
In ame-kb `kg_domain_entity` holds *type definitions*, not instances.
_Avoid_: calling a type an "entity"; calling an instance an "entity".

**Edge**:
A relation instance in `kg_graph_edge` connecting two Nodes by their Node Identity.

**Node Identity** (`graph_node_no`):
The durable business key of a Node. After V3 it is a synthetic name-independent key
(`node:{uuid}`); `name` becomes an ordinary attribute and all surface names live in
Aliases. This lets two already-stored Nodes be judged the same later without any
primary-key migration or dangling edges. Legacy `type:slug(name)` identities are
kept only in the compatibility mapping after the one-time V3 migration.
_Avoid_: deriving identity from the name.

**Survivor**:
When two Nodes are merged, the one that is kept. The other (the merged-away Node) is
soft-deleted and keeps a `merged_into` pointer to the Survivor.

**Re-point**:
On merge, rewriting an Edge's endpoint from the merged-away Node to the Survivor.
Re-pointing can collapse an edge into a self-loop (drop it) or create a duplicate
`(source, target, label)` edge (dedupe it). Both cleanups are mandatory.

### Entity resolution

**Mention**:
A single node as extracted from one document, before resolution. Multiple Mentions
across documents may denote the same real-world entity and resolve to one Node.
_Avoid_: "raw node", "candidate node".

**Entity Resolution**:
Deciding whether two Mentions (or a Mention and an existing Node) denote the same
real-world entity, and merging them into one Node. Runs as a separate offline
`resolve` step, not inline during ingest: `ingest`/`store` persist Mentions as Nodes
without cross-document merging, and `resolve` performs global resolution afterwards
(mirrors the big system's split of `BatchExtractDocs` from `FuseKGGraph`).
_Avoid_: "dedup" (too narrow — implies exact-name only), "linking".

**Alias**:
An alternate surface name that resolves to the same Node (e.g. "Ada" for
"Ada Lovelace"). Aliases are attributes of a Node, not separate Nodes.

**Blocking** (candidate generation):
Narrowing the whole graph down to a small candidate set for a Mention before any
pairwise comparison, so resolution does not scan every Node. Candidates are drawn
**within the same Entity Type only**.
_Avoid_: "bucketing".

**Vector Recall**:
Fetching merge candidates for a Mention by embedding similarity (name + description),
used when exact name/Alias Blocking finds nothing. Two thresholds gate the outcome:
at/above `HIGH` the pair is merged without the LLM; between `LOW` and `HIGH` it goes
to the LLM Judge; below `LOW` the Mention becomes a new Node.

**LLM Judge**:
Using the LLM to decide whether a candidate pair denotes the same entity, and if so
to fuse their schema. Invoked **only for the vector-similarity grey zone** — never on
exact/alias hits (those merge for free) and never on high-similarity hits.
_Avoid_: "matcher".

**Schema Fusion**:
Merging the `properties` / schema of two matched entities into one, with a defined
canonical-wins policy. The Survivor's values are canonical; the merged-away Node only
fills gaps. `aliases` take the union; `ref`/`source` merge by docId with line-range
union; a merged Edge keeps the higher `confidence`. Deterministic by default — the
LLM Judge is invoked to fuse schema only when there is a real key conflict, never on
every pair.
