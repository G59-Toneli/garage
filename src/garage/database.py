"""The database schema, and the guard that keeps it a derived artifact.

The database is built from the Corpus by a deterministic pipeline and **nothing writes to it at
runtime** (ADR-0002). That is a claim the schema itself enforces here rather than a rule people are
asked to remember: every ingested table carries a trigger that rejects writes unless the session has
declared itself an ingestion by setting `garage.ingesting`. A stray `UPDATE` from the serving path
fails loudly instead of quietly making the database unreproducible.

Enforcing it in the schema rather than with a read-only role is what keeps it true in every
deployment: Compose, CI and the ARM VM all connect as the same owner today, and a role grant would
protect none of them.

What it is not: a security boundary. `garage.ingesting` is an ordinary session setting, so anything
holding the connection string could set it and write — and DDL is not guarded at all, because a
rebuild is DDL. This catches the failure that actually happens (serving code that quietly starts
writing) rather than one that does not (an attacker who already owns the database).
"""

from __future__ import annotations

# The width of the `vector` column, from the module that owns the commitment rather than as a
# literal in the DDL. ADR-0008 makes 384 a build-time promise the Phase 4 fine-tuned embedder has to
# keep, and a promise spelled in two places is a promise that gets broken in one of them.
from garage.embedding import EMBEDDING_DIMENSION

# Set for the duration of the ingestion transaction. `SET LOCAL` means it dies with the transaction,
# so the guard cannot be left switched off.
INGESTING_FLAG = "garage.ingesting"

# The tables ingestion owns: dropped, recreated and guarded as a set. Membership is what puts a
# table into `DROP_SCHEMA` and gives it the four triggers of `CREATE_WRITE_GUARD`, so a new ingested
# table listed anywhere but here is a table the rebuild is not total over.
INGESTED_TABLES = ("documents", "chunks", "jargon", "corpus_meta", "embeddings", "embeddings_meta")

# Dropped and recreated on every ingestion: the build is a rebuild, never a migration. `CASCADE`
# takes the dependent triggers and indexes with them.
DROP_SCHEMA = "\n".join(f"DROP TABLE IF EXISTS {table} CASCADE;" for table in INGESTED_TABLES)

# `pg_trgm` backs the trigram half of lexical retrieval and `vector` backs the dense half. Both are
# created here rather than in `docker/initdb/`, because initdb runs once, when the data directory is
# empty: a developer whose volume predates a line added there would get a database the server cannot
# query. Ingestion is the one step that already owns DDL and is always re-run, so requiring it here
# is what makes every built artifact complete.
#
# `vector` moved up from `docker/initdb/001-extensions.sql` with the dense retriever, and the file
# and the hand-copied `psql` line in `ci.yml` went with it. Up to now the extension was optional —
# the schema did not reference it — and creating it in two places that could drift was survivable.
# It is not optional any more: `embeddings.embedding` is a `vector(384)` column, so a database
# without the extension is not a database this code can build at all, and the one statement that
# creates it belongs in the one step that always runs.
CREATE_EXTENSIONS = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
"""

# The one text search configuration this project uses, named once. Both halves of full text have to
# agree on it — the stored `tsvector` below and the `plainto_tsquery` in `retrieval` — and if they
# ever disagreed, queries would silently stop matching stems the index holds. It is also what a run
# record cites, because the stemmer behind this name is part of what produced the ranking, so a
# constant is the only way the record cannot drift from the SQL it claims to describe.
TEXT_SEARCH_CONFIG = "portuguese"

CREATE_SCHEMA = f"""
CREATE TABLE documents (
    doc_id      text PRIMARY KEY,
    title       text NOT NULL,
    publisher   text NOT NULL,
    year        integer NOT NULL,
    tier        text NOT NULL CHECK (tier IN ('A', 'B')),
    provenance  text NOT NULL,
    filename    text NOT NULL,
    sha256      text NOT NULL,
    rights      text NOT NULL
);

CREATE TABLE chunks (
    chunk_id     text PRIMARY KEY,
    doc_id       text NOT NULL REFERENCES documents (doc_id) ON DELETE CASCADE,
    ordinal      integer NOT NULL,
    -- Denormalised from `documents` on purpose: the tier filter is a runtime axis applied on every
    -- query (design §9), and it must not cost a join on the hot path.
    tier         text NOT NULL CHECK (tier IN ('A', 'B')),
    -- Null where the source has no pages. A Markdown document genuinely has none, and inventing a
    -- number would put a false page into a citation.
    page         integer,
    section      text,
    kind         text NOT NULL CHECK (kind IN ('spec', 'procedure', 'prose')),
    text         text NOT NULL,
    jargon_terms text[] NOT NULL DEFAULT '{{}}',
    -- Generated, not populated: lexical retrieval (#5) must search exactly what ingestion stored.
    -- 'portuguese' because the material is overwhelmingly Brazilian Portuguese; the stemmer being
    -- wrong for the English headings costs far less than no stemming for the workshop text.
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('{TEXT_SEARCH_CONFIG}', text)) STORED,
    UNIQUE (doc_id, ordinal)
);

CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
-- Unused by today's query, which computes `word_similarity` per row and scans: at fixture scale
-- that is free. It is here because the corpus this is built for is a shelf of scanned manuals, and
-- the rewrite to an indexable word-similarity operator must not also be a rebuild of every database.
CREATE INDEX chunks_text_trgm_idx ON chunks USING gin (text gin_trgm_ops);
CREATE INDEX chunks_tier_idx ON chunks (tier);
CREATE INDEX chunks_jargon_idx ON chunks USING gin (jargon_terms);

CREATE TABLE jargon (
    term      text PRIMARY KEY,
    canonical text NOT NULL,
    notes     text NOT NULL DEFAULT ''
);

CREATE TABLE corpus_meta (
    -- One row, forever: the database describes exactly one Corpus build.
    singleton      boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    corpus_id      text NOT NULL,
    corpus_hash    text NOT NULL,
    ingest_version integer NOT NULL,
    built_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE embeddings (
    chunk_id  text NOT NULL REFERENCES chunks (chunk_id) ON DELETE CASCADE,
    -- The whole of ADR-0005 in one column. Embedder is a build-time axis, so a second embedder
    -- means a second set of vectors — but *in this table*, under a second `model_key`, never in a
    -- second table and never in a second column. That is what makes "switching embedder is a WHERE
    -- clause, not a redeploy" true, and it is why the Phase 4 fine-tuned embedder costs zero lines
    -- of schema and zero lines of SQL.
    model_key text NOT NULL,
    -- `vector`, not `halfvec`. Half precision would save half the storage and lose numeric fidelity
    -- on the ranking path of a benchmark whose entire claim is that its numbers are trustworthy.
    -- That is not a trade to make without a measurement saying the storage mattered, and at
    -- fifty-three chunks it demonstrably does not.
    embedding vector({EMBEDDING_DIMENSION}) NOT NULL,
    -- Composite, so one chunk carries one vector per embedder and a rebuild cannot half-write a
    -- model_key's set without the primary key noticing.
    PRIMARY KEY (chunk_id, model_key)
);

CREATE TABLE embeddings_meta (
    -- `corpus_meta` for the embedder axis, and deliberately **not** a singleton. The difference is
    -- the argument: `corpus_meta.singleton` exists because the database describes exactly one
    -- Corpus, while here `model_key` is the primary key because the database is meant to describe
    -- more than one embedder at once (ADR-0005). A singleton table here would have made the second
    -- embedder a schema change, which is the thing ADR-0005 exists to prevent.
    model_key   text PRIMARY KEY,
    -- Recorded even though the column already declares it, because the two are different claims:
    -- the column says what this build can store, this says what the embedder that wrote these rows
    -- produced. The Phase 4 embedder must preserve 384 (ADR-0008) and this is the row that would
    -- prove it did rather than assert it.
    dimension   integer NOT NULL,
    -- The digest of everything that changes a vector, read off the embedder object that actually
    -- ran rather than off the configuration that was supposed to produce it. `verify_artifact`
    -- refuses to boot a `dense` build when this disagrees with the live embedder — the one check
    -- that catches ingesting with one embedder and querying with another, which no dimension check
    -- and no shape check can see.
    fingerprint text NOT NULL,
    normalized  boolean NOT NULL,
    built_at    timestamptz NOT NULL DEFAULT now()
);
"""

# Created *after* the rows are inserted, never inside `CREATE_SCHEMA`, and the ordering is load
# bearing enough to keep the statement in its own constant where it cannot be pasted back in.
#
# HNSW rather than IVFFlat for exactly that reason: IVFFlat trains its centroids during
# `CREATE INDEX`, so an IVFFlat index built over an empty table is structurally useless until
# somebody remembers to `REINDEX` — a trap waiting for whoever reorders the statements in `build()`.
# HNSW has no training step, so the worst an ordering mistake costs here is a slower build.
#
# **The contracted semantics are exact search; this index is an optimisation.** HNSW is approximate,
# and the ADR-0004 gate is built on total determinism, so that sentence has to be true rather than
# hoped for. At fifty-three chunks the planner sequentially scans and the search *is* exact; the
# index earns its place the day the corpus is a shelf of scanned manuals. Same reasoning, and same
# honesty, as `chunks_text_trgm_idx` above.
#
# Partial on `model_key`, and not merely for size. pgvector applies the `WHERE` clause *after*
# walking the index, so a shared index over two embedders with `ef_search = 40` returns about twenty
# candidates for a predicate matching half the rows, not forty. One index per `model_key` is what
# keeps a second embedder from silently halving the recall of the first.
#
# `m` and `ef_construction` are pgvector's defaults, written out rather than omitted so that a
# future change to them is visible in a diff. Nobody should move them without a number.
CREATE_EMBEDDING_INDEX = """
CREATE INDEX embeddings_{model_key}_hnsw ON embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    WHERE model_key = '{model_key}';
"""

CREATE_WRITE_GUARD = f"""
CREATE OR REPLACE FUNCTION garage_reject_runtime_write() RETURNS trigger AS $$
BEGIN
    IF current_setting('{INGESTING_FLAG}', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION
            'ingested table %.% is read-only outside ingestion (ADR-0002)',
            TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = 'read_only_sql_transaction';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
""" + "\n".join(
    f"""
CREATE TRIGGER {table}_read_only
    BEFORE INSERT OR UPDATE OR DELETE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION garage_reject_runtime_write();
CREATE TRIGGER {table}_read_only_truncate
    BEFORE TRUNCATE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION garage_reject_runtime_write();
"""
    for table in INGESTED_TABLES
)
