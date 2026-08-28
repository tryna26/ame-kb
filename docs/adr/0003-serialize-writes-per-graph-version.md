# Serialize writes per graph version

ame-kb serializes ingest, entity merge, rollback, and identity migration with
one database row lock per `(graph_no, graph_version)`. Contribution replacement
must read all current facts before rebuilding materialized Nodes and Edges; two
writers otherwise can commit complete contribution rows but overwrite the graph
with different partial snapshots, and ingest/merge naturally acquire lower-level
rows in opposite orders. Per-graph serialization trades write throughput for a
simple atomic contract that fits ame-kb's single-machine, small-graph scope while
still allowing independent graph versions to write concurrently.
