"""The database as a derived artifact (ADR-0002).

Three claims are worth a database to test: the rebuild is safe to re-run, the ingested tables reject
writes from anything that is not an ingestion, and the `corpus_hash` the service checks at boot is
actually stored. Skipped when no database is reachable, so `pytest` stays green on a bare checkout.
"""

import os
from pathlib import Path

import psycopg
import pytest

from garage.chunking import INGEST_VERSION
from garage.corpus import FIXTURE_CORPUS, CorpusError
from garage.ingest import ArtifactMismatch, build, stored_corpus_hash, verify_artifact

DATABASE_URL = os.environ.get("GARAGE_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="GARAGE_DATABASE_URL is unset; start `docker compose up`"
)


@pytest.fixture(scope="module")
def ingested():
    return build(DATABASE_URL, FIXTURE_CORPUS)


def _query(sql: str, parameters: tuple = ()):
    with psycopg.connect(DATABASE_URL) as connection:
        return connection.execute(sql, parameters).fetchall()


def test_one_command_rebuilds_the_database_from_scratch(ingested):
    assert ingested.document_count == 5
    assert ingested.chunk_count > 0
    assert set(ingested.chunks_by_kind) == {"spec", "procedure", "prose"}

    counted = _query("SELECT count(*) FROM chunks")[0][0]
    assert counted == ingested.chunk_count


def test_rebuilding_is_safe_to_re_run_and_produces_the_same_rows(ingested):
    before = _query("SELECT chunk_id, text FROM chunks ORDER BY chunk_id")

    again = build(DATABASE_URL, FIXTURE_CORPUS)

    assert again.chunk_count == ingested.chunk_count
    assert _query("SELECT chunk_id, text FROM chunks ORDER BY chunk_id") == before
    assert _query("SELECT count(*) FROM corpus_meta")[0][0] == 1


def test_the_corpus_hash_is_stored_and_readable(ingested):
    assert stored_corpus_hash(DATABASE_URL) == ingested.corpus_hash

    row = _query("SELECT corpus_id, ingest_version FROM corpus_meta")[0]
    assert row == ("fixture", INGEST_VERSION)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO chunks (chunk_id, doc_id, ordinal, tier, kind, text) "
        "VALUES ('x', 'svc-kadett-1993', 9999, 'A', 'prose', 'smuggled in')",
        "UPDATE chunks SET text = 'rewritten'",
        "DELETE FROM chunks",
        "UPDATE corpus_meta SET corpus_hash = 'lie'",
    ],
)
def test_nothing_writes_to_the_ingested_tables_outside_ingestion(ingested, statement):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(statement)


def test_specification_rows_survive_ingestion_intact(ingested):
    rows = _query("SELECT text, kind, section, tier FROM chunks WHERE text LIKE %s", ("%77%",))

    assert len(rows) == 1
    text, kind, section, tier = rows[0]
    # The fastener, its thread and its torque reached the database in one row.
    assert "Cylinder head bolt, stage 2" in text
    assert "Thread: M11" in text
    assert "Torque (N·m): 77" in text
    assert (kind, tier) == ("spec", "A")
    assert "3.2" in section


def test_chunks_keep_the_metadata_tier_filtering_and_citation_will_need(ingested):
    assert _query("SELECT count(*) FROM chunks WHERE tier NOT IN ('A', 'B')")[0][0] == 0
    assert _query("SELECT count(*) FROM chunks WHERE section IS NULL")[0][0] == 0
    assert _query("SELECT count(*) FROM chunks WHERE tsv IS NULL")[0][0] == 0
    # Tier B material is where Jargon lives, and it has to reach the database with it.
    assert _query(
        "SELECT count(*) FROM chunks WHERE 'swap 250-S' = ANY (jargon_terms) AND tier = 'B'"
    )[0][0] > 0


def test_every_chunk_points_at_a_catalogued_document(ingested):
    orphans = _query(
        "SELECT count(*) FROM chunks LEFT JOIN documents USING (doc_id) WHERE documents.doc_id IS NULL"
    )
    assert orphans[0][0] == 0


def test_a_corpus_that_fails_verification_never_reaches_the_database(ingested, corpus_copy: Path):
    before = _query("SELECT chunk_id FROM chunks ORDER BY chunk_id")
    (corpus_copy / "sources" / "svc-kadett-1993.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(CorpusError):
        build(DATABASE_URL, corpus_copy)

    # Not merely refused: the database an operator was already serving from is untouched.
    assert _query("SELECT chunk_id FROM chunks ORDER BY chunk_id") == before
    assert stored_corpus_hash(DATABASE_URL) == ingested.corpus_hash


def test_the_boot_gate_accepts_the_database_it_just_built(ingested):
    artifact = verify_artifact(DATABASE_URL, FIXTURE_CORPUS)

    assert artifact.corpus_hash == ingested.corpus_hash
    assert artifact.ingest_version == INGEST_VERSION


def test_the_boot_gate_refuses_a_database_holding_a_different_corpus(ingested, corpus_copy: Path):
    manifest = corpus_copy / "manifest.yaml"
    # Only the catalogue changes, and only in a way that changes the Corpus identity. The material
    # on disk still verifies — this is the wrong-database failure, not the tampered-source one.
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("corpus_id: fixture", "corpus_id: other"),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactMismatch) as refusal:
        verify_artifact(DATABASE_URL, corpus_copy)

    assert ingested.corpus_hash in str(refusal.value)


def test_the_boot_gate_refuses_a_database_that_was_never_ingested(ingested):
    # Nothing here is a rebuild, so the tables stay dropped only for as long as this test.
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute("DROP TABLE corpus_meta CASCADE")

    try:
        with pytest.raises(ArtifactMismatch):
            verify_artifact(DATABASE_URL, FIXTURE_CORPUS)
    finally:
        build(DATABASE_URL, FIXTURE_CORPUS)
