"""The build step: one command turns a validated Corpus into the database artifact.

The whole point is that there are no manual steps and no incremental state. Ingestion verifies the
Corpus, drops the ingested tables, recreates them, loads documents, chunks and Jargon, and records
the `corpus_hash` the service will check at boot (ADR-0002). It runs in a single transaction, so a
failure halfway through leaves the previous database intact rather than a half-built one.

Re-running it is the normal case, not the exception: a rebuild is how the artifact is produced, so
`ingest` is safe to run against a populated database and produces exactly the same rows from the
same Corpus.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from garage.chunking import INGEST_VERSION, Chunk, chunk_document
from garage.corpus import (
    SOURCES_DIRNAME,
    Document,
    Manifest,
    corpus_hash,
    load_manifest,
    validate_corpus,
)
from garage.database import (
    CREATE_EMBEDDING_INDEX,
    CREATE_EXTENSIONS,
    CREATE_SCHEMA,
    CREATE_WRITE_GUARD,
    DROP_SCHEMA,
    INGESTING_FLAG,
)
from garage.embedding import Embedder, EmbedderError, configured_embedder
from garage.jargon import JargonTerm, load_vocabulary


@dataclass(frozen=True)
class IngestReport:
    """What a completed build is allowed to claim."""

    corpus_id: str
    corpus_hash: str
    ingest_version: int
    document_count: int
    chunk_count: int
    chunks_by_kind: dict[str, int]
    jargon_term_count: int
    # Null when the build was `GARAGE_EMBEDDER=none`, which is a lexical-only artifact and a
    # supported outcome rather than a half-finished one. Reported either way, because "this database
    # has no vectors" is exactly the thing an operator debugging a missing `dense` arm needs told.
    embedder_model_key: str | None = None
    embedder_fingerprint: str | None = None
    embedding_count: int = 0


# `None` is a meaningful argument to `build` — it is the lexical-only build — so the default cannot
# be `None` and still mean "ask the environment". A sentinel keeps the two apart; the alternative,
# a second boolean flag, would let a caller pass a contradiction.
_UNSET: Any = object()


def _sources_dir(corpus_dir: Path, sources_dir: Path | None) -> Path:
    return Path(sources_dir) if sources_dir is not None else Path(corpus_dir) / SOURCES_DIRNAME


def chunk_corpus(corpus_dir: Path, sources_dir: Path | None = None) -> tuple[Chunk, ...]:
    """Every chunk of every catalogued document, in manifest order.

    Assumes the Corpus has already been verified — `build` is what enforces that ordering, because
    chunking material whose hash was never checked is precisely what ADR-0002 forbids.
    """
    return _chunk(
        load_manifest(corpus_dir),
        _sources_dir(corpus_dir, sources_dir),
        load_vocabulary(),
    )


def _chunk(manifest: Manifest, sources: Path, vocabulary: tuple[JargonTerm, ...]) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for document in manifest.documents:
        markdown = (sources / document.filename).read_text(encoding="utf-8")
        chunks.extend(
            chunk_document(
                markdown,
                doc_id=document.doc_id,
                tier=document.tier,
                vocabulary=vocabulary,
            )
        )
    return tuple(chunks)


def _write_documents(cursor: psycopg.Cursor, documents: tuple[Document, ...]) -> None:
    cursor.executemany(
        """
        INSERT INTO documents (doc_id, title, publisher, year, tier, provenance, filename, sha256, rights)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                document.doc_id,
                document.title,
                document.publisher,
                document.year,
                document.tier,
                document.provenance,
                document.filename,
                document.sha256,
                document.rights,
            )
            for document in documents
        ],
    )


def _write_chunks(cursor: psycopg.Cursor, chunks: tuple[Chunk, ...]) -> None:
    cursor.executemany(
        """
        INSERT INTO chunks (chunk_id, doc_id, ordinal, tier, page, section, kind, text, jargon_terms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                chunk.chunk_id,
                chunk.doc_id,
                chunk.ordinal,
                chunk.tier,
                chunk.page,
                chunk.section,
                chunk.kind,
                chunk.text,
                list(chunk.jargon_terms),
            )
            for chunk in chunks
        ],
    )


_MODEL_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _write_embeddings(
    cursor: psycopg.Cursor, chunks: tuple[Chunk, ...], embedder: Embedder
) -> int:
    """Embed every chunk as a *passage* and store the vectors under this embedder's `model_key`.

    `embed_passages`, never a generic `embed`: the e5 family is trained with asymmetric prefixes and
    the passage side is the one ingestion is on. The two-method interface is what makes that a fact
    about the type rather than a fact about this line being written carefully
    (`garage.embedding` explains why the design's `embed(texts)` was diverged from).
    """
    vectors = embedder.embed_passages([chunk.text for chunk in chunks])
    for chunk, vector in zip(chunks, vectors):
        if len(vector) != embedder.dimension:
            # Cheap, and it fires before the `vector(384)` column would: a mismatch caught here
            # names the chunk and the embedder, while the column's own error names neither.
            raise EmbedderError(
                f"{embedder.model_key} produced {len(vector)} dimensions for {chunk.chunk_id}, "
                f"but declares {embedder.dimension}"
            )
    cursor.executemany(
        "INSERT INTO embeddings (chunk_id, model_key, embedding) VALUES (%s, %s, %s)",
        [
            # The pgvector text input form. Sent as a string rather than through a registered
            # adapter so that ingestion needs no `pgvector` Python package on top of the extension
            # — one dependency fewer on an ARM VM (ADR-0001), and the format is three characters of
            # syntax. `repr` of a Python float round-trips exactly, so this is lossless.
            (chunk.chunk_id, embedder.model_key, "[" + ",".join(repr(value) for value in vector) + "]")
            for chunk, vector in zip(chunks, vectors)
        ],
    )
    return len(vectors)


def build(
    database_url: str,
    corpus_dir: Path,
    sources_dir: Path | None = None,
    embedder: Embedder | None = _UNSET,  # type: ignore[assignment]
) -> IngestReport:
    """Rebuild the database from a Corpus. Verifies first, then writes, all in one transaction.

    `embedder` defaults to whatever `GARAGE_EMBEDDER` names, resolved through the *same*
    `embedder_for` factory `retrieval.available_retrievers` calls. That shared factory is acceptance
    criterion three: the two sides of the index cannot be configured differently because there is
    only one place either of them is configured. It is injectable so tests can build an artifact
    with a deterministic fake, and `None` is the explicit lexical-only build.

    The embedder is constructed **before** the transaction opens. Loading 470 MB of weights and
    verifying their digest can fail, and it should fail against a database still holding the
    previous build rather than halfway through dropping it.
    """
    # Verification first, and everything after it reads the manifest this one call returned: a build
    # that re-parsed the catalogue could disagree with the gate it just passed (ADR-0002).
    report = validate_corpus(corpus_dir, sources_dir=sources_dir)
    manifest = load_manifest(corpus_dir)
    vocabulary = load_vocabulary()
    chunks = _chunk(manifest, _sources_dir(corpus_dir, sources_dir), vocabulary)
    if embedder is _UNSET:
        embedder = configured_embedder()
    if embedder is not None and not _MODEL_KEY.fullmatch(embedder.model_key):
        # `model_key` reaches SQL as an index name and as a literal in a partial index predicate,
        # neither of which can be parameterised. The values come from a closed set in
        # `embedder_for`, so this is not a live injection path — it is the assertion that keeps it
        # from becoming one the day someone adds a name from a config file.
        raise EmbedderError(f"model_key {embedder.model_key!r} is not a bare lowercase identifier")

    with psycopg.connect(database_url) as connection:
        # psycopg commits on a clean exit and rolls back on an exception, which is the atomicity
        # this needs: a database is either the old build or the new one, never a mixture.
        with connection.cursor() as cursor:
            # Declares this session an ingestion, unlocking the write guard for this transaction
            # only. Every other connection — the server included — stays read-only.
            cursor.execute(f"SET LOCAL {INGESTING_FLAG} = 'on'")
            cursor.execute(CREATE_EXTENSIONS)
            cursor.execute(DROP_SCHEMA)
            cursor.execute(CREATE_SCHEMA)
            cursor.execute(CREATE_WRITE_GUARD)

            _write_documents(cursor, manifest.documents)
            _write_chunks(cursor, chunks)
            cursor.executemany(
                "INSERT INTO jargon (term, canonical, notes) VALUES (%s, %s, %s)",
                [(term.term, term.canonical, term.notes) for term in vocabulary],
            )
            cursor.execute(
                """
                INSERT INTO corpus_meta (corpus_id, corpus_hash, ingest_version)
                VALUES (%s, %s, %s)
                """,
                (report.corpus_id, report.corpus_hash, INGEST_VERSION),
            )

            embedding_count = 0
            if embedder is not None:
                embedding_count = _write_embeddings(cursor, chunks, embedder)
                cursor.execute(
                    """
                    INSERT INTO embeddings_meta (model_key, dimension, fingerprint, normalized)
                    VALUES (%s, %s, %s, %s)
                    """,
                    # Every value read off the embedder instance that just ran, never off the name
                    # that was asked for or a constant restated here. That is the difference between
                    # recording what happened and recording what was intended, and the boot gate is
                    # only worth anything if this row describes the former.
                    (
                        embedder.model_key,
                        embedder.dimension,
                        embedder.fingerprint,
                        embedder.normalized,
                    ),
                )
                # Last, after the rows exist. See `CREATE_EMBEDDING_INDEX` for why the ordering is a
                # constant of its own rather than a comment on a line inside `CREATE_SCHEMA`.
                cursor.execute(CREATE_EMBEDDING_INDEX.format(model_key=embedder.model_key))

    return IngestReport(
        corpus_id=report.corpus_id,
        corpus_hash=report.corpus_hash,
        ingest_version=INGEST_VERSION,
        document_count=report.document_count,
        chunk_count=len(chunks),
        chunks_by_kind=dict(Counter(chunk.kind for chunk in chunks)),
        jargon_term_count=len(vocabulary),
        embedder_model_key=embedder.model_key if embedder else None,
        embedder_fingerprint=embedder.fingerprint if embedder else None,
        embedding_count=embedding_count,
    )


@dataclass(frozen=True)
class Artifact:
    """What a database says about itself: which Corpus it holds and which rules built it."""

    corpus_id: str
    corpus_hash: str
    ingest_version: int


class ArtifactMismatch(Exception):
    """The database is not the artifact this commit describes. A boot failure, never a warning."""


def stored_artifact(database_url: str) -> Artifact | None:
    """What this database was built from, or None if it was never ingested."""
    with psycopg.connect(database_url) as connection:
        # `to_regclass` rather than catching the error: a database that was never ingested is an
        # ordinary state at boot, not an exception to be recovered from.
        if connection.execute("SELECT to_regclass('corpus_meta')").fetchone()[0] is None:
            return None
        row = connection.execute(
            "SELECT corpus_id, corpus_hash, ingest_version FROM corpus_meta"
        ).fetchone()
    return Artifact(*row) if row else None


@dataclass(frozen=True)
class StoredEmbedder:
    """What a database says about one set of vectors it holds."""

    model_key: str
    dimension: int
    fingerprint: str
    normalized: bool


def stored_embedders(database_url: str) -> tuple[StoredEmbedder, ...]:
    """Every embedder this database holds vectors for, by `model_key`.

    Plural, and ordered, because the table is deliberately not a singleton: ADR-0005 has the
    baseline and the Phase 4 fine-tuned embedder coexisting in one `embeddings` table, so anything
    that reads this has to be written for two from the start.
    """
    with psycopg.connect(database_url) as connection:
        # `to_regclass` rather than catching the error, exactly as `stored_artifact` does: a
        # database built before the dense retriever existed is an ordinary state at boot.
        if connection.execute("SELECT to_regclass('embeddings_meta')").fetchone()[0] is None:
            return ()
        rows = connection.execute(
            "SELECT model_key, dimension, fingerprint, normalized FROM embeddings_meta "
            "ORDER BY model_key"
        ).fetchall()
    return tuple(StoredEmbedder(*row) for row in rows)


def stored_corpus_hash(database_url: str) -> str | None:
    """The `corpus_hash` this database was built from, or None if it was never ingested."""
    artifact = stored_artifact(database_url)
    return artifact.corpus_hash if artifact else None


def verify_artifact(
    database_url: str, corpus_dir: Path, embedder: Embedder | None = _UNSET  # type: ignore[assignment]
) -> Artifact:
    """The boot gate (ADR-0002): refuse to serve a database that is not this commit's artifact.

    Three numbers have to agree, and they fail differently (ADR-0007). A wrong `corpus_hash` means the
    database holds *different material* than the manifest in this checkout describes — every
    citation it produces would name a document the reader cannot check. A wrong `ingest_version`
    means the same material processed by *rules this code no longer implements* — the chunks are
    real but they are not the chunks this code would build, so a `chunk_id` in an evaluation set
    quietly points somewhere else.

    A wrong embedder `fingerprint` is the third, and it is the only one of the three that is
    *invisible without this check*. The other two produce citations a reader can see are wrong; a
    database whose vectors were written by a different embedder than the one answering queries
    boots, serves, ranks, and is merely worse. Nothing about a 384-float vector says which model
    made it, and the most likely divergence of all — the two e5 prefixes swapped — has the same
    model, the same weights and the same dimension. This is the check, and there is no other.

    `embedder` defaults to whatever `GARAGE_EMBEDDER` names, so callers that never heard of
    embedders — `run_evaluation`, the server's lifespan — get the check without asking for it.
    Passing `None` explicitly asserts a lexical-only build and skips it.

    Only the manifest is read, never the source documents: a real Corpus keeps its material on the
    operator's disk and the serving container has none of it (ADR-0003). That is sound because the
    hash is taken over the catalogue, and ingestion verified the material against that catalogue
    before it wrote a single row.
    """
    expected = corpus_hash(load_manifest(corpus_dir))
    found = stored_artifact(database_url)

    if found is None:
        raise ArtifactMismatch(
            "the database was never ingested: no corpus_meta row.\n"
            "Run `python -m garage ingest` before serving."
        )
    if found.corpus_hash != expected:
        raise ArtifactMismatch(
            "the database holds a different Corpus than this checkout describes.\n"
            f"  database: corpus_hash {found.corpus_hash} (corpus_id {found.corpus_id})\n"
            f"  manifest: corpus_hash {expected} (from {corpus_dir})\n"
            "Run `python -m garage ingest` to rebuild it."
        )
    if found.ingest_version != INGEST_VERSION:
        raise ArtifactMismatch(
            "the database was built by chunking rules this code no longer implements.\n"
            f"  database: ingest_version {found.ingest_version}\n"
            f"  code:     ingest_version {INGEST_VERSION}\n"
            "Run `python -m garage ingest` to rebuild it."
        )

    if embedder is _UNSET:
        embedder = configured_embedder()
    if embedder is not None:
        _verify_embedder(database_url, embedder)
    return found


def _verify_embedder(database_url: str, embedder: Embedder) -> None:
    """Refuse to serve dense retrieval over vectors this embedder did not write."""
    stored = {held.model_key: held for held in stored_embedders(database_url)}
    held = stored.get(embedder.model_key)

    if held is None:
        raise ArtifactMismatch(
            f"the database holds no {embedder.model_key!r} embeddings, so dense retrieval has "
            "nothing to search.\n"
            f"  database: {', '.join(sorted(stored)) or 'no embedders at all'}\n"
            f"  code:     {embedder.model_key}\n"
            "Run `python -m garage ingest` to rebuild it, or set GARAGE_EMBEDDER=none to serve "
            "lexical retrieval alone."
        )
    if held.fingerprint != embedder.fingerprint:
        raise ArtifactMismatch(
            "the database was embedded by a different embedder than this code would query with.\n"
            f"  database: fingerprint {held.fingerprint} (model_key {held.model_key}, "
            f"{held.dimension} dimensions)\n"
            f"  code:     fingerprint {embedder.fingerprint} (model_key {embedder.model_key}, "
            f"{embedder.dimension} dimensions)\n"
            "Vectors from two embedders are comparable arithmetic and incomparable meaning: the "
            "cosine would be a number, the ranking would be quietly worse, and nothing else would "
            "say so.\n"
            "Run `python -m garage ingest` to rebuild it."
        )
    if held.dimension != embedder.dimension:
        # Unreachable while `dimension` is a fingerprint field, and kept anyway: this is the
        # invariant ADR-0008 makes a build-time promise, and an assertion that can never fire is
        # the cheapest possible way to notice the day the fingerprint stops covering it.
        raise ArtifactMismatch(
            f"the {held.model_key} vectors are {held.dimension}-dimensional and this embedder "
            f"produces {embedder.dimension}. ADR-0008 makes the dimension a build-time commitment; "
            "a second embedder must preserve it."
        )
