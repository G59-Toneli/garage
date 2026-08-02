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
# `unaccent` joined them with #12, and it is the one extension here whose reason is a *correctness*
# claim rather than a capability. `to_tsvector('portuguese', 'cabeçote')` and the same call on
# `cabecote` produce different lexemes, so a Brazilian who types the word the way Brazilians type it
# in a hurry matched nothing at all through full text and depended entirely on trigram to be found.
#
# Trigram does sometimes rescue that, and the measurement is the reason to stop relying on it: whether
# it does depends on how much of the *rest* of the query happens to match, not on the misspelt word.
# `word_similarity('cabecote plainado', ...)` peaks at 0.714 and clears the 0.6 floor; the bare word
# `cabecote` peaks at 0.500 and is dropped; `torque do parafuso do cabeçote` peaks at 0.357 across all
# 53 chunks. Same word, three outcomes, decided by sentence length. `unaccent` folds both spellings to
# one lexeme at index time and at query time, so the hit is a full-text hit with a rank behind it
# rather than a coin flip against a threshold nobody measured.
CREATE_EXTENSIONS = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;
"""

# The one text search configuration this project uses, named once. Both halves of full text have to
# agree on it — the stored `tsvector` below and the `plainto_tsquery` in `retrieval` — and if they
# ever disagreed, queries would silently stop matching stems the index holds. It is also what a run
# record cites, because the stemmer behind this name is part of what produced the ranking, so a
# constant is the only way the record cannot drift from the SQL it claims to describe.
#
# It stopped being `portuguese` with #12 (ADR-0010). The corpus is bilingual — Brazilian forum prose
# on one side, English service-manual headings and spec tables on the other — and one language's
# stock configuration treats the other language's function words as content. Under `portuguese`,
# `what`, `is`, `the`, `of` and `how` are ordinary lexemes, which `plainto_tsquery` then ANDs into
# every query, and every natural-language English question aimed at a spec table became
# unsatisfiable by construction.
TEXT_SEARCH_CONFIG = "garage_bi"

# Created by ingestion, between `DROP_SCHEMA` and `CREATE_SCHEMA`, and the ordering is load bearing:
# `chunks.tsv` is a generated column that references this configuration **by OID**, so the config
# cannot be dropped while the table stands (Postgres refuses, or takes the column with it under
# CASCADE). Dropped and recreated rather than `IF NOT EXISTS`-ed, on the same principle as the rest
# of this module: the build is a rebuild, so an operator who edits the mapping below gets the edit
# by re-running ingestion and never by remembering to drop something by hand.
#
# `ACCEPT = false` is the whole trick, and it is easy to miss in the documentation. A dictionary
# chain stops at the first dictionary that *recognises* a token. The `simple` template normally
# recognises everything — it lowercases and returns — so `simple` anywhere but last would swallow
# the corpus and leave nothing for the stemmer. With `ACCEPT = false` it returns NULL for anything
# that is not in its stop word list, which passes the token *down the chain* instead of ending it.
# So `en_stop` and `pt_stop` here are pure filters: they delete stop words in either language and
# are otherwise invisible, and `portuguese_stem` still sees every content word.
#
# What this deliberately does **not** do is stack `portuguese_stem, english_stem`. That was measured
# and it cannot work: a Snowball stemmer recognises every token it is given, so the second stemmer
# in a chain is unreachable code. `to_tsvector` under such a chain returns `'running' 'tightened'
# 'hous'` — the English words untouched, the Portuguese one stemmed, `english_stem` never consulted.
# Bilingual *stemming* is not available from a dictionary chain at all; bilingual *stop words* are,
# and measurement says the stop words were carrying the failure.
#
# `unaccent` runs first so that both the accented and the unaccented spelling reach the stop word
# lists and the stemmer as the same string.
CREATE_TEXT_SEARCH_CONFIG = f"""
DROP TEXT SEARCH CONFIGURATION IF EXISTS {TEXT_SEARCH_CONFIG};
DROP TEXT SEARCH DICTIONARY IF EXISTS garage_en_stop;
DROP TEXT SEARCH DICTIONARY IF EXISTS garage_pt_stop;

CREATE TEXT SEARCH DICTIONARY garage_en_stop (
    TEMPLATE = pg_catalog.simple, STOPWORDS = english, ACCEPT = false
);
CREATE TEXT SEARCH DICTIONARY garage_pt_stop (
    TEMPLATE = pg_catalog.simple, STOPWORDS = portuguese, ACCEPT = false
);

CREATE TEXT SEARCH CONFIGURATION {TEXT_SEARCH_CONFIG} (COPY = pg_catalog.portuguese);
ALTER TEXT SEARCH CONFIGURATION {TEXT_SEARCH_CONFIG}
    ALTER MAPPING FOR asciiword, asciihword, hword_asciipart, word, hword, hword_part
    WITH unaccent, garage_en_stop, garage_pt_stop, portuguese_stem;
"""

# One reproducibility caveat, written here because it is new with this configuration and nothing in
# the pipeline can catch it.
#
# `unaccent` is not self-contained: it loads its fold table from `unaccent.rules` in the server's
# `$SHAREDIR/tsearch_data`. That file is part of the *server installation*, not of this repository,
# so the stored `tsvector` is now a function of one file that neither `corpus_hash` nor
# `INGEST_VERSION` covers (ADR-0007 covers the material and the rules; this is neither). The same is
# true of `english.stop` and `portuguese.stop` behind the two dictionaries above.
#
# `measurement()` records `postgres_version` and `text_search_dictionaries`, so a server swap that
# moves the ranking makes the baseline stop comparing — it is *detected*, by way of the version
# string, and it is not *prevented*. Preventing it would mean shipping our own rules file and
# pointing the dictionaries at it, which is a real option the day two machines disagree. Nothing
# measured today says they do: the fixture corpus produces identical metrics on the CI image and on
# the deployment image, both `pgvector/pgvector:pg16`.

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
    -- The configuration is `garage_bi` and is created by ingestion immediately before this
    -- statement; see `CREATE_TEXT_SEARCH_CONFIG` for why a stock `portuguese` was measurably wrong
    -- for a corpus half of which is written in English, and `INGEST_VERSION` for why changing it
    -- was a version bump.
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
