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

# Set for the duration of the ingestion transaction. `SET LOCAL` means it dies with the transaction,
# so the guard cannot be left switched off.
INGESTING_FLAG = "garage.ingesting"

# The tables ingestion owns: dropped, recreated and guarded as a set. A new ingested table — the
# `embeddings` table the dense retriever brings — belongs here, or the rebuild stops being total.
INGESTED_TABLES = ("documents", "chunks", "jargon", "corpus_meta")

# Dropped and recreated on every ingestion: the build is a rebuild, never a migration. `CASCADE`
# takes the dependent triggers and indexes with them.
DROP_SCHEMA = "\n".join(f"DROP TABLE IF EXISTS {table} CASCADE;" for table in INGESTED_TABLES)

# `pg_trgm` backs the trigram half of lexical retrieval. It is created here rather than in
# `docker/initdb/` — where `vector` lives — because initdb runs once, when the data directory is
# empty: a developer whose volume predates this line would get a database the server cannot query.
# Ingestion is the one step that already owns DDL and is always re-run, so requiring it here is what
# makes every built artifact complete.
CREATE_EXTENSIONS = "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

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
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('portuguese', text)) STORED,
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
