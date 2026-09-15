# ADR-0002: pgvector over a dedicated vector database

## Status

Proposed

## Context

Relay needs vector search for document chunks, tool-description retrieval, and memory
retrieval. At the reference deployment's scale (~300k chunks, well under a million vectors —
see docs/system-design.md section 2.3), a dedicated vector database is not required for
performance, but it is one more stateful service to run, secure, and back up.

## Decision

Use PostgreSQL 16 with the `pgvector` extension (HNSW index) for all vector storage,
alongside the relational data it already holds. A `VectorStore` interface (`upsert`, `search`,
`delete`) isolates every caller from the concrete backend.

## Consequences

- One fewer stateful service in local dev and in the cloud deployment; one connection pool,
  one backup story, one place to enforce tenant filtering.
- Filtered vector search (`workspace_id`, `collection_id`) is a normal SQL `WHERE` clause
  instead of a second filtering language.
- HNSW index build and query performance need to be watched as chunk volume grows.

## Alternatives considered

- Qdrant (or a similar dedicated vector DB): better suited past roughly 5M vectors, heavy
  filtered-search latency requirements, or multi-vector/sparse hybrid indexing needs. Revisit
  this decision if the design doc's section 11.6 thresholds are hit — the `VectorStore`
  interface keeps that swap contained to one module.
