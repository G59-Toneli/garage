"""Dense retrieval against a real Postgres, over the fixture Corpus and a deterministic fake.

Two things are being tested and they are worth keeping apart. This file tests **plumbing**: that the
schema holds vectors, that `WHERE model_key` really separates two embedders, that the write guard
covers the new tables, that the tier filter is applied inside the SQL, that `components` carries what
the Glass Box will render, that the endpoint serves a strategy it never learned the name of — and,
above all, that ingesting with one embedder and querying with another is a boot failure. Retrieval
**quality** is tested in exactly one place, the ADR-0004 gate, against committed questions and a
promoted baseline.

`FakeEmbedder` is what makes that separation possible: real dimension, real interface, no semantics
and no weights (see `fakes.py`). A test here that asserted a particular chunk comes back for a
particular question would be asserting a property of a hash function.

Skipped when no database is reachable, so `pytest` stays green on a bare checkout.
"""

import os

import psycopg
import pytest
from fakes import FakeEmbedder
from fastapi.testclient import TestClient

from garage.app import create_app
from garage.config import Settings
from garage.corpus import FIXTURE_CORPUS
from garage.embedding import EMBEDDING_DIMENSION, PASSAGE_PREFIX, QUERY_PREFIX
from garage.ingest import ArtifactMismatch, build, stored_embedders, verify_artifact
from garage.retrieval import (
    MAX_K,
    DenseRetriever,
    Filters,
    LexicalRetriever,
    available_retrievers,
)

DATABASE_URL = os.environ.get("GARAGE_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="GARAGE_DATABASE_URL is unset; start `docker compose up`"
)

# A chunk with a known identity, reused across this file the way `test_retrieval.py` reuses it.
KNOWN_CHUNK = "svc-kadett-1993#0006"


@pytest.fixture(scope="module")
def embedder():
    return FakeEmbedder()


@pytest.fixture(scope="module")
def artifact(embedder):
    """An artifact built with the fake, and *only* the fake.

    `build` takes the embedder rather than resolving one, so this fixture states which embedder the
    database holds instead of inheriting it from whatever the developer exported.
    """
    build(DATABASE_URL, FIXTURE_CORPUS, embedder=embedder)
    return DATABASE_URL


@pytest.fixture(scope="module")
def retriever(artifact, embedder):
    return DenseRetriever(artifact, embedder)


def rows(query, parameters=()):
    with psycopg.connect(DATABASE_URL) as connection:
        return connection.execute(query, parameters).fetchall()


# ------------------------------------------------------------------------------------------------
# The third acceptance criterion: ingestion and query cannot be different embedders.
# ------------------------------------------------------------------------------------------------


def test_ingesting_with_one_embedder_and_querying_with_another_is_a_boot_failure(artifact, embedder):
    """The test the whole issue turns on, and it names both fingerprints so the failure is fixable.

    Nothing else catches this. The other embedder here has the same dimension, produces perfectly
    valid vectors, and every cosine it computes against the stored index is a real number — the
    ranking would just be noise. Dimension checks, type signatures and `NOT NULL` all pass.
    """
    other = FakeEmbedder(model_id="a-different-embedder")
    assert other.dimension == embedder.dimension  # The check that would *not* have caught it.

    with pytest.raises(ArtifactMismatch) as failure:
        verify_artifact(artifact, FIXTURE_CORPUS, other)

    message = str(failure.value)
    assert embedder.fingerprint in message and other.fingerprint in message
    assert "ingest" in message


def test_swapping_only_the_e5_prefixes_is_also_a_boot_failure(artifact, embedder):
    # The realistic version of the test above. Same model, same weights, same dimension, same
    # `model_key` — one line of prefix, and retrieval quietly gets worse. The fingerprint is the
    # only thing in the system that can tell.
    swapped = FakeEmbedder(
        query_prefix=PASSAGE_PREFIX, passage_prefix=QUERY_PREFIX
    )

    with pytest.raises(ArtifactMismatch):
        verify_artifact(artifact, FIXTURE_CORPUS, swapped)


def test_the_same_text_embeds_identically_at_ingestion_and_at_query(artifact, embedder):
    """Read the stored vector back and recompute it. Bit for bit, through the database round trip.

    This is the positive half of the criterion: the check above proves a *different* embedder is
    rejected, and this proves the *same* one actually agrees with what it wrote. `vector` is float4
    in pgvector, so the stored value is the float32 of what was computed — compared as float32 on
    both sides rather than with a tolerance, because a tolerance here would hide the drift it exists
    to catch.
    """
    import struct

    stored, text = rows(
        "SELECT embeddings.embedding::text, chunks.text FROM embeddings "
        "JOIN chunks ON chunks.chunk_id = embeddings.chunk_id "
        "WHERE embeddings.chunk_id = %s AND embeddings.model_key = %s",
        (KNOWN_CHUNK, embedder.model_key),
    )[0]

    def as_float32(values):
        # Both sides, and not just the recomputed one. pgvector prints a float4 as the shortest
        # decimal that round-trips *to a float4*, so parsing that text as a Python double lands a
        # few bits away from the widened float4. Narrowing both is what compares the two numbers
        # rather than two renderings of them.
        return tuple(struct.unpack("f", struct.pack("f", value))[0] for value in values)

    from_database = tuple(float(value) for value in stored.strip("[]").split(","))
    recomputed = embedder.embed_passages([text])[0]

    assert as_float32(from_database) == as_float32(recomputed)
    # And the query side reaches the same function for the same text, which is the property the
    # single `embedder_for` factory exists to guarantee: one object, two call sites, no drift.
    assert embedder.embed_passages([text])[0] == recomputed


def test_a_boot_against_a_database_with_no_embeddings_at_all_refuses(embedder, tmp_path):
    # The other half of the mismatch: a lexical-only artifact served by a build that expects dense.
    # Silently returning nothing would look exactly like a corpus that covers nothing.
    build(DATABASE_URL, FIXTURE_CORPUS, embedder=None)
    try:
        with pytest.raises(ArtifactMismatch) as failure:
            verify_artifact(DATABASE_URL, FIXTURE_CORPUS, embedder)
        assert "no embedders at all" in str(failure.value)
    finally:
        build(DATABASE_URL, FIXTURE_CORPUS, embedder=embedder)


def test_a_lexical_only_build_is_a_declaration_and_boots_fine(embedder):
    build(DATABASE_URL, FIXTURE_CORPUS, embedder=None)
    try:
        assert stored_embedders(DATABASE_URL) == ()
        # `None` is the explicit assertion that this build wants no dense arm, and it passes.
        verify_artifact(DATABASE_URL, FIXTURE_CORPUS, None)
        assert [r.name for r in available_retrievers(DATABASE_URL, None)] == ["lexical"]
    finally:
        build(DATABASE_URL, FIXTURE_CORPUS, embedder=embedder)


# ------------------------------------------------------------------------------------------------
# What the artifact holds.
# ------------------------------------------------------------------------------------------------


def test_the_dimension_is_recorded_and_agrees_with_the_column_and_the_embedder(artifact, embedder):
    """Three places have to say 384, and ADR-0008 makes it a build-time commitment.

    `vector_dims` is read off a stored row rather than off the column definition on purpose: it
    reports the width of the vector that is actually there, which is the number the Phase 4
    fine-tuned embedder has to preserve.
    """
    (stored,) = stored_embedders(artifact)
    (width,) = rows(
        "SELECT DISTINCT vector_dims(embedding) FROM embeddings WHERE model_key = %s",
        (embedder.model_key,),
    )[0]

    assert width == stored.dimension == embedder.dimension == EMBEDDING_DIMENSION
    assert stored.fingerprint == embedder.fingerprint
    assert stored.normalized is True


def test_every_chunk_got_exactly_one_vector(artifact, embedder):
    (chunks,) = rows("SELECT count(*) FROM chunks")[0]
    (vectors,) = rows("SELECT count(*) FROM embeddings WHERE model_key = %s", (embedder.model_key,))[0]

    assert vectors == chunks == 53


def test_a_second_embedder_coexists_under_a_second_model_key(artifact, embedder):
    """ADR-0005, demonstrated rather than asserted: two embedders, one table, no schema change.

    This is what makes Phase 4 cost nothing. The row is written through the ingestion flag because
    the write guard is real, and it is removed afterwards so the artifact this module built stays
    the artifact the other tests measure.
    """
    finetuned = FakeEmbedder(model_id="fine-tuned", model_key="finetuned")
    vector = "[" + ",".join(repr(v) for v in finetuned.embed_passages(["x"])[0]) + "]"
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL garage.ingesting = 'on'")
            cursor.execute(
                "INSERT INTO embeddings (chunk_id, model_key, embedding) VALUES (%s, %s, %s)",
                (KNOWN_CHUNK, finetuned.model_key, vector),
            )
            # Both, side by side, distinguished by a value in a column and not by a table name.
            assert cursor.execute(
                "SELECT count(DISTINCT model_key) FROM embeddings"
            ).fetchone()[0] == 2
            cursor.execute("DELETE FROM embeddings WHERE model_key = %s", (finetuned.model_key,))


def test_the_write_guard_covers_the_new_tables(artifact):
    # Membership of `INGESTED_TABLES` is what gives a table its four triggers (ADR-0002). A new
    # ingested table that was added to the schema and forgotten there would be writable at runtime.
    for table in ("embeddings", "embeddings_meta"):
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(f"DELETE FROM {table}")


def test_the_partial_index_exists_and_names_the_model_key(artifact, embedder):
    # Built after the rows, and partial by `model_key` because pgvector applies the predicate after
    # walking the index — a shared index would hand back half the candidates it was asked for.
    (definition,) = rows(
        "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
        (f"embeddings_{embedder.model_key}_hnsw",),
    )[0]

    assert "hnsw" in definition and "vector_cosine_ops" in definition
    assert f"model_key = '{embedder.model_key}'" in definition


# ------------------------------------------------------------------------------------------------
# The retriever itself.
# ------------------------------------------------------------------------------------------------


def test_the_planner_still_scans_which_is_what_makes_the_search_exact(artifact, embedder):
    """The premise every other claim about `dense` rests on, asserted instead of assumed.

    `DenseRetriever` contracts *exact* search and treats the HNSW index as an optimisation, and the
    deterministic gate (ADR-0004) is built on that. It is true only while the planner sequentially
    scans fifty-three rows. The day the corpus is large enough for it to reach for the index,
    `ef_search` starts deciding which neighbours come back, approximation becomes visible in the
    ranking, and the gate would quietly compare two things that are no longer comparable — with
    nothing anywhere noticing the assumption had expired.

    So the expiry is a red build. When this fails, the fix is not to loosen it: it is to add
    `ef_search` to the run record's `Configuration` and re-promote the baseline deliberately.
    """
    from garage.retrieval import _DENSE_SEARCH, _as_vector, _ef_search

    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(f"SET LOCAL hnsw.ef_search = {_ef_search(10)}")
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                "EXPLAIN " + _DENSE_SEARCH,
                {
                    "vector": _as_vector(embedder.embed_query("torque")),
                    "model_key": embedder.model_key,
                    "tiers": ["A", "B"],
                    "k": 10,
                },
            ).fetchall()
        )

    assert "Seq Scan on embeddings" in plan, plan
    assert "hnsw" not in plan.lower(), plan


def test_the_dense_retriever_honours_the_contract_every_strategy_shares(retriever):
    found = retriever.retrieve("torque do parafuso do cabeçote", k=5)

    assert len(found) == 5
    top = found[0]
    assert top.doc_title and top.tier in ("A", "B") and top.kind
    # Two components and no nulls: dense has one signal and reporting a null `lexical` beside it
    # would be this strategy claiming a signal it never computed.
    assert set(top.components) == {"cosine", "dense_rank"}
    assert top.components["dense_rank"] == 1.0
    assert -1.0 <= top.components["cosine"] <= 1.0
    assert top.score == top.components["cosine"]


def test_the_tier_filter_is_applied_inside_the_sql(retriever):
    tier_a_only = retriever.retrieve("swap 250-S no Kadett", k=10, filters=Filters(tiers=("A",)))

    assert tier_a_only != ()
    assert {candidate.tier for candidate in tier_a_only} == {"A"}
    # Not merely filtered afterwards: a retriever that scored ten candidates and then dropped the
    # Tier B ones would return fewer than k and quietly change what recall@10 means.
    assert len(tier_a_only) == 10


def test_ranking_is_deterministic_and_bounded(retriever):
    first = retriever.retrieve("cylinder head bolt torque", k=3)
    again = retriever.retrieve("cylinder head bolt torque", k=3)

    assert [c.chunk_id for c in first] == [c.chunk_id for c in again]
    assert [c.score for c in first] == sorted((c.score for c in first), reverse=True)
    assert [c.components["dense_rank"] for c in first] == [1.0, 2.0, 3.0]
    # k is capped for the same reason it is under `lexical`: the endpoint is public.
    assert len(retriever.retrieve("torque", k=10_000)) <= MAX_K


def test_dense_reads_only_its_own_model_key(artifact, embedder):
    """A retriever bound to a `model_key` no vectors exist under retrieves nothing at all.

    The negative half of the ADR-0005 claim. If the `WHERE` were missing, this would return the
    baseline's chunks scored against the wrong index and look perfectly healthy.
    """
    absent = FakeEmbedder(model_id="fine-tuned", model_key="finetuned")

    assert DenseRetriever(artifact, absent).retrieve("torque", k=10) == ()


def test_dense_does_not_abstain_and_lexical_does(retriever, artifact):
    """The behavioural difference between the two arms, asserted rather than left in a docstring.

    `lexical` has a similarity floor and can honestly return nothing; nearest-neighbour search has
    no such notion and always hands back its k least-distant vectors. The zero-cost abstention in
    `app._answer` is therefore reachable under one strategy and unreachable under the other, which
    is a real consequence of shipping `dense` and is documented in `docs/retrieval.md`. No floor is
    invented here: a threshold picked to make this test read nicer is a number the gate would then
    be defending.
    """
    uncovered = "receita de brigadeiro de colher"

    assert LexicalRetriever(artifact).retrieve(uncovered, k=10) == ()
    assert len(retriever.retrieve(uncovered, k=10)) == 10


# ------------------------------------------------------------------------------------------------
# End to end.
# ------------------------------------------------------------------------------------------------


def test_the_endpoint_serves_dense_without_learning_anything_about_it(artifact, embedder):
    """ADR-0005's real test: a second strategy reaches the wire with no change to the response shape.

    `RetrievedChunk.components` is `dict[str, float | None]` and was written before `dense` existed;
    if it had been four named fields, this response would have needed a new model and the endpoint
    would have learned which retriever it holds.
    """
    settings = Settings(database_url=artifact, corpus_dir=FIXTURE_CORPUS, gemini_api_key=None)
    retrievers = (LexicalRetriever(artifact), DenseRetriever(artifact, embedder))

    with TestClient(create_app(settings, retriever=None, retrievers=retrievers)) as client:
        lexical = client.post("/query", json={"question": "flywheel bolt torque", "k": 5}).json()
        dense = client.post(
            "/query", json={"question": "flywheel bolt torque", "k": 5, "strategy": "dense"}
        ).json()
        unknown = client.post("/query", json={"question": "x", "strategy": "hybrid"})

    # Strategy is a runtime axis: same process, same artifact, same request shape, different arm.
    assert lexical["strategy"] == "lexical"
    assert dense["strategy"] == "dense"
    assert set(dense["chunks"][0]["components"]) == {"cosine", "dense_rank"}
    assert dense["trace"]["children"][0]["attributes"]["retrieval.strategy"] == "dense"
    # A typo is a 422 naming what this build serves, never a silent fall back to the default: a
    # visitor comparing two strategies who is quietly served the other reads a difference that is
    # not there.
    assert unknown.status_code == 422
    assert "lexical, dense" in unknown.text or "dense, lexical" in unknown.text
