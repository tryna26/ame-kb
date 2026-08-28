# Entity resolution uses embeddings + LLM, but deliberately adds no vector store

**Context.** V3 resolves same-entity Mentions across documents (ADR-0001). Doing this
well needs semantic candidate recall (embeddings) and an LLM to judge the grey zone.
ame-kb's whole value is that it runs on one MySQL + one LLM key and can run outside any
internal infra. Aligning with the big system's V3 must not silently break that.

**Decision.** Resolution runs as a separate offline `resolve` step (ingest keeps
persisting Mentions without cross-doc merging). Its pipeline: exact name/alias Blocking
(within the same Entity Type) → merge for free; else Vector Recall → at/above `HIGH`
merge without the LLM, in the `LOW..HIGH` grey zone call the LLM Judge, below `LOW`
create a new Node.

Embeddings come from an **OpenAI-compatible `/embeddings` endpoint** (new `EMBED_*`
env vars), reusing the existing `openai` SDK. `EMBED_API_KEY` and `EMBED_BASE_URL` may
fall back to their `LLM_*` counterparts, while `EMBED_MODEL` and `EMBED_DIM` must be
set explicitly because chat and embedding models have different contracts. Vectors
are stored as a column in MySQL and cosine similarity is computed **in memory** over
the small, type-blocked candidate set. No vector database, no local embedding model.

## Considered options

- **Vector DB (Milvus / pgvector / FAISS)** — what the big system uses, justified by
  million-node online recall. ame-kb is a single-machine small graph; type-blocking
  leaves a tiny candidate set that brute-force cosine handles. Rejected to preserve the
  one-MySQL footprint.
- **Local embedding model (sentence-transformers)** — removes the endpoint dependency
  but adds a heavy library and model download, breaking "clone and run". Rejected.

## Consequences

- Net new dependency is exactly one embedding endpoint (can be the same gateway as the
  LLM). No new storage or indexing component.
- Embeddings are computed lazily in `resolve` for Nodes lacking one and cached back to
  the column (same content-hash / `saveIfChanged` spirit as V2 incremental ingest).
- Brute-force cosine is acceptable only because of type-blocking; if a graph ever grows
  past single-machine scale this decision must be revisited.
