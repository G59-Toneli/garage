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
from garage.database import TEXT_SEARCH_CONFIG
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


def test_two_rebuilds_leave_tsv_a_generated_column_over_the_project_s_own_configuration():
    """The DDL ordering, held as a property, because exactly one ordering works and it looks arbitrary.

    `chunks.tsv` is `GENERATED ALWAYS AS (to_tsvector('garage_bi', text)) STORED`, so it depends on
    the text search configuration **by OID**. That pins the sequence in `ingest.build` to
    extensions, drop the tables, drop and recreate the configuration, create the tables — and every
    other arrangement fails in a way nobody would predict from reading it. Recreate the
    configuration before dropping `chunks` and Postgres either refuses outright or, with CASCADE,
    quietly takes the generated column with it and leaves a table that ingests fine and retrieves
    nothing. The failure is silent at build time and total at query time, which is the worst
    combination available.

    Twice, and not once, because the first build runs against an empty database where the ordering
    cannot be wrong. The second is the one that has to drop a live dependency, and it is the one a
    developer runs every day.

    Its own build rather than the module fixture: this test is about what repeated building does,
    so sharing a fixture with the tests that assume a built database would make it depend on
    collection order — the defect `conftest.py` was written to stop repeating.
    """
    build(DATABASE_URL, FIXTURE_CORPUS)
    build(DATABASE_URL, FIXTURE_CORPUS)

    generated, expression = _query(
        """
        SELECT is_generated, generation_expression
        FROM information_schema.columns
        WHERE table_name = 'chunks' AND column_name = 'tsv'
        """
    )[0]
    assert generated == "ALWAYS"
    # The configuration name is read out of the stored expression rather than asserted as a literal,
    # so this follows `database.TEXT_SEARCH_CONFIG` instead of becoming a second declaration of it.
    assert TEXT_SEARCH_CONFIG in expression

    # And it still holds lexemes, which `is_generated` alone would not tell us: a column that exists
    # and is empty passes every structural check and returns nothing for every query.
    populated = _query("SELECT count(*) FROM chunks WHERE tsv IS NOT NULL AND tsv != ''::tsvector")
    assert populated[0][0] == _query("SELECT count(*) FROM chunks")[0][0]


def test_the_search_configuration_folds_accents_and_drops_stop_words_in_both_languages():
    """`garage_bi` itself, asserted against the server rather than against the DDL that asked for it.

    Three claims, and each was a measured failure of the stock `portuguese` configuration (#12):
    `unaccent` reaches the same lexeme from `cabecote` and `cabeçote`; English function words are
    dropped instead of becoming mandatory query terms; Portuguese ones still are too. The third is
    there because it is the one a careless mapping would break — reordering the dictionaries so that
    `garage_en_stop` accepted everything would leave Portuguese stop words in the index and nothing
    in this suite would otherwise notice.
    """
    build(DATABASE_URL, FIXTURE_CORPUS)

    folded = _query(
        f"SELECT to_tsvector('{TEXT_SEARCH_CONFIG}', 'cabecote') = "
        f"to_tsvector('{TEXT_SEARCH_CONFIG}', 'cabeçote')"
    )[0][0]
    assert folded

    for stop_words in ("what is the of and how", "o a de para com que"):
        empty = _query(f"SELECT to_tsvector('{TEXT_SEARCH_CONFIG}', %s) = ''::tsvector", (stop_words,))
        assert empty[0][0], stop_words

    # Stemming survives the two filter dictionaries, which is the whole reason they are declared
    # `ACCEPT = false`. Without it `simple` would consume every token and this would be `torques`.
    assert _query(f"SELECT to_tsvector('{TEXT_SEARCH_CONFIG}', 'torques')::text")[0][0] == "'torqu':1"


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
