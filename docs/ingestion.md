# Ingestion

One command turns a verified Corpus into the database:

```bash
python -m garage ingest                          # the fixture Corpus
python -m garage ingest corpus/v1 --sources /mnt/manuais
```

It validates first, then rebuilds. There are no manual steps, no migrations to apply in order, and
no incremental state — the database is a **derived artifact** (ADR-0002), so the build is always a
full rebuild and always safe to re-run:

```
corpus_id:      fixture
corpus_hash:    21c4e571b96fefae82062b11d1cdd0f237b0b311d781d8a11f975d8b650b75d6
ingest_version: 1
documents:      5
chunks:         53 (procedure 6, prose 26, spec 21)
jargon terms:   12
embeddings:     53 × baseline (fingerprint 37211055ebcf…)
```

The whole thing runs in one transaction. A Corpus that fails verification, a source file that
disappeared, a database that went away mid-load — all of them leave the previous database exactly as
it was. The URL comes from `GARAGE_DATABASE_URL` unless `--database-url` overrides it.

## Structure-aware chunking

A chunker that splits on a character budget eventually cuts a torque figure away from the fastener it
applies to, and the answer is then wrong in the worst possible way: confidently, with a citation.
So the shape of the text decides the split.

| Kind        | Split                      | Why                                                           |
| ----------- | -------------------------- | ------------------------------------------------------------- |
| `spec`      | one table row per chunk    | A specification is a row. Cutting one apart separates a number from its subject. |
| `procedure` | one numbered step per chunk | A step is the smallest unit that is still an instruction.     |
| `prose`     | one paragraph per chunk, with the previous paragraph's last sentence carried in | A paragraph's subject is often named only in the one before it. |

A `spec` chunk repeats its column headings, because retrieval scores a chunk on its own text: a chunk
reading `41` is unfindable, `Torque (N·m): 41` is not. Prose overlap stops at a section or page
boundary — text on the far side of a heading is a different subject, not context.

Chunking is deterministic. The same bytes produce the same `chunk_id`s (`<doc_id>#<ordinal>`) in the
same order, which is what lets an evaluation set point at a `chunk_id` and still mean something after
a rebuild. Change the rules and `INGEST_VERSION` in `src/garage/chunking.py` goes up, because chunks
already in a database were not built by the new rules.

Input is Markdown. Real Tier A material arrives as PDF; the extraction step that produces this
intermediate form lands with the real corpus, and chunking is written against the intermediate form
rather than any one file format. Pages come from `<!-- page: 12 -->` markers: a scanned manual has
pages and a citation is worth much less without them, while a Markdown document genuinely has none
and its `page` stays `NULL` rather than being invented.

## Jargon

`corpus/jargon.yaml` is the curated workshop vocabulary — `term`, `canonical`, `notes`. It describes
the car, not any one Corpus, which is why it sits beside `corpus/` and is **not** part of the corpus
hash — editing a `notes` line must not invalidate a run record. Adding or renaming a *term* is a
different matter: the detected terms are stored on the chunk, so that is a chunking-rules change and
takes `INGEST_VERSION` with it ([ADR-0007](adr/0007-corpus-hash-and-ingest-version-are-separate.md)).

Detection is per chunk and conservative. It casefolds and strips accents, because forum Portuguese
drops them constantly (`cabecote` is `cabeçote`), and matches whole terms only — `mesa` is not found
inside `mesada`. It matches terms, never concepts: nothing here guesses that `cabeça` meant
`cabeçote`.

## What ingestion writes

```
documents(doc_id pk, title, publisher, year, tier, provenance, filename, sha256, rights)
chunks(chunk_id pk, doc_id fk, ordinal, tier, page, section, kind, text, jargon_terms[], tsv)
jargon(term pk, canonical, notes)
corpus_meta(corpus_id, corpus_hash, ingest_version, built_at)   -- exactly one row
embeddings(chunk_id fk, model_key, embedding vector(384))       -- pk (chunk_id, model_key)
embeddings_meta(model_key pk, dimension, fingerprint, normalized, built_at)
```

`tsv` is a generated column (`to_tsvector('portuguese', text)`) with a GIN index, so lexical
retrieval searches exactly what ingestion stored. `tier` is denormalised onto `chunks` on purpose:
the tier filter is a runtime axis applied to every query and must not cost a join.

## Embeddings

The embedder is a **build-time** axis (ADR-0005): its output is stored, so changing it is a rebuild
rather than a flag. `python -m garage ingest` embeds every chunk as a *passage* and writes one row
per chunk per `model_key`, then creates a partial HNSW index over that `model_key` — after the rows
exist, never before. Two embedders coexist in the one table under two keys, which is what makes the
Phase 4 fine-tuned embedder cost zero lines of schema and zero lines of SQL.

`embeddings_meta` is `corpus_meta` for this axis, and is deliberately **not** a singleton:
`corpus_meta.singleton` exists because a database describes exactly one Corpus, while `model_key`
is a primary key here because a database is meant to describe more than one embedder at once.

```bash
python -m garage embedder fetch    # once: 470 MB, pinned by sha256, never committed to git
python -m garage embedder show     # what this code would query with, beside what the database holds
```

`GARAGE_EMBEDDER=none` builds a lexical-only artifact — no vectors, no `embeddings_meta` row, no
dense arm — and `ingest` prints that it did. That is a declaration, not an absence: a *missing or
altered* weights file makes `ingest` refuse before the transaction opens, leaving the previous
database intact, because silently degrading to lexical would report a configuration mistake as a
retrieval quality result.

`fingerprint` is a sha256 over everything that changes a vector — model id, weights digest,
tokenizer digest, dimension, sequence length, pooling, normalisation, both e5 prefixes, and
`EMBED_VERSION`. It is written from the embedder object that actually ran, and the boot gate refuses
to serve when it disagrees with the live one. It is a **fourth** identity number and is deliberately
not folded into `corpus_hash` or `INGEST_VERSION`
([ADR-0007](adr/0007-corpus-hash-and-ingest-version-are-separate.md)): numbers that fail for
different reasons are separate numbers, and a pooling change must not re-issue the identity of a
Corpus nobody touched. Changing the embedder does **not** bump `INGEST_VERSION`, because the chunks
are the same chunks.

## Nothing writes to these tables at runtime

Every ingested table carries a trigger rejecting `INSERT`, `UPDATE`, `DELETE` and `TRUNCATE` unless
the session has set `garage.ingesting` — which only ingestion does, with `SET LOCAL`, so it cannot be
left switched off. A stray write from the serving path fails loudly instead of quietly making the
database unreproducible.

This is enforced in the schema rather than with a read-only role because Compose, CI and the ARM VM
all connect as the same owner; a role grant would protect none of them. It is not a security
boundary either: `garage.ingesting` is an ordinary session setting, and DDL is unguarded because a
rebuild *is* DDL. It catches the failure that actually happens — serving code that quietly starts
writing — not one that already has the connection string and means harm.

`corpus_meta.corpus_hash` is what the service checks at boot: a database that does not match the
commit is the wrong database, and serving from it silently is worse than not serving at all.
