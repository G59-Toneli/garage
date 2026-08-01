# The database is a derived artifact, not the source of truth

Reproducibility is the top driver (ADR-0001), and a mutable database is state that cannot be hashed
or versioned alongside a commit — which is why a plain file index was considered first. We use
**Postgres with pgvector anyway**, because two of the runtime configuration axes (tier filtering and
hybrid lexical+dense fusion) are query-engine features that a raw vector index would force us to
reimplement by hand. The tension is resolved by treating the database as **derived**: `corpus/`
is the source of truth, a deterministic ingestion pipeline builds the database from it, and nothing
writes to the database at runtime.

## Consequences

- The service verifies `corpus_hash` at boot and refuses to start if it disagrees with the commit.
  Serving measured numbers over a corpus that has silently drifted is the failure this prevents.
- Query logs and traces are written elsewhere, so they cannot contaminate the artifact.
- Rebuilding the database from scratch must remain a single command. If it ever stops being one,
  this ADR has been violated in practice regardless of what the code says.
