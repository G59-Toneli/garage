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

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import psycopg

from garage.chunking import INGEST_VERSION, Chunk, chunk_document
from garage.corpus import SOURCES_DIRNAME, Document, Manifest, load_manifest, validate_corpus
from garage.database import CREATE_SCHEMA, CREATE_WRITE_GUARD, DROP_SCHEMA, INGESTING_FLAG
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


def build(
    database_url: str,
    corpus_dir: Path,
    sources_dir: Path | None = None,
) -> IngestReport:
    """Rebuild the database from a Corpus. Verifies first, then writes, all in one transaction."""
    # Verification first, and everything after it reads the manifest this one call returned: a build
    # that re-parsed the catalogue could disagree with the gate it just passed (ADR-0002).
    report = validate_corpus(corpus_dir, sources_dir=sources_dir)
    manifest = load_manifest(corpus_dir)
    vocabulary = load_vocabulary()
    chunks = _chunk(manifest, _sources_dir(corpus_dir, sources_dir), vocabulary)

    with psycopg.connect(database_url) as connection:
        # psycopg commits on a clean exit and rolls back on an exception, which is the atomicity
        # this needs: a database is either the old build or the new one, never a mixture.
        with connection.cursor() as cursor:
            # Declares this session an ingestion, unlocking the write guard for this transaction
            # only. Every other connection — the server included — stays read-only.
            cursor.execute(f"SET LOCAL {INGESTING_FLAG} = 'on'")
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

    return IngestReport(
        corpus_id=report.corpus_id,
        corpus_hash=report.corpus_hash,
        ingest_version=INGEST_VERSION,
        document_count=report.document_count,
        chunk_count=len(chunks),
        chunks_by_kind=dict(Counter(chunk.kind for chunk in chunks)),
        jargon_term_count=len(vocabulary),
    )


def stored_corpus_hash(database_url: str) -> str | None:
    """The `corpus_hash` this database was built from, or None if it was never ingested.

    This is the boot check the service will hang off (ADR-0002): a database that does not match the
    commit is the wrong database, and serving from it silently is worse than not serving.
    """
    with psycopg.connect(database_url) as connection:
        # `to_regclass` rather than catching the error: a database that was never ingested is an
        # ordinary state at boot, not an exception to be recovered from.
        if connection.execute("SELECT to_regclass('corpus_meta')").fetchone()[0] is None:
            return None
        row = connection.execute("SELECT corpus_hash FROM corpus_meta").fetchone()
    return row[0] if row else None
