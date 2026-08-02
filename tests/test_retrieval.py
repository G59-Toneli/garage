"""Lexical retrieval against a real Postgres, over the fixture Corpus.

There is no useful unit test of this module. The ranking *is* the SQL — full text, trigram and the
fusion between them all live in the database — so a test with the database mocked out would assert
that Python passes a string to psycopg. What is worth asserting is that a question a person would
actually type finds the chunk that answers it. Skipped when no database is reachable, so `pytest`
stays green on a bare checkout.
"""

import os

import pytest
from fastapi.testclient import TestClient

from garage.app import create_app
from garage.config import Settings
from garage.corpus import FIXTURE_CORPUS, corpus_hash, load_manifest
from garage.ingest import build
from garage.retrieval import MAX_K, Filters, LexicalRetriever

DATABASE_URL = os.environ.get("GARAGE_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="GARAGE_DATABASE_URL is unset; start `docker compose up`"
)


@pytest.fixture(scope="module")
def retriever():
    build(DATABASE_URL, FIXTURE_CORPUS)
    return LexicalRetriever(DATABASE_URL)


def test_a_fixture_question_retrieves_the_known_correct_chunk(retriever):
    # The fixture service manual gives the flywheel bolt 63 N·m and nothing else in the Corpus
    # mentions a flywheel. If this ever stops being the first hit, retrieval has regressed.
    found = retriever.retrieve("flywheel bolt torque", k=5)

    assert found[0].chunk_id == "svc-kadett-1993#0006"
    assert "63" in found[0].text
    assert found[0].kind == "spec"
    assert found[0].section == "Section 3.2 — Cylinder head, tightening specifications"


def test_a_specification_arrives_with_the_heading_its_number_belongs_to(retriever):
    found = retriever.retrieve("valve clearance intake", k=1)

    # Structure-aware chunking is what makes this findable: the row carries its column headings, so
    # the number never travels without the thing it describes.
    assert found[0].text == (
        "Section 3.3 — Valve clearance, engine cold — Valve: Intake; Clearance (mm): 0.18"
    )


def test_an_unaccented_question_still_finds_the_accented_chunk(retriever):
    """Forum Portuguese drops accents constantly, and the thread has to stay reachable.

    This asserted that full text contributed **zero** and that trigram alone rescued the chunk. The
    second half was true for *this* query and is not a property of dropped accents, which is what the
    test was taken to prove. `word_similarity` peaks at 0.714 here and clears the 0.6 floor, but the
    bare word `cabecote` peaks at 0.500 and the sentence `torque do parafuso do cabeçote` peaks at
    0.357 — same misspelling, opposite outcomes, decided by how much of the rest of the query
    happens to match.

    ADR-0010 put `unaccent` in front of the stemmer, so the accented and unaccented spellings are one
    lexeme and full text finds it in all three cases. What is asserted now is that **both** signals
    fire: the full-text one because that is the new guarantee, and the trigram one because it did
    before and losing it would be a regression this test should catch rather than absorb.
    """
    found = retriever.retrieve("cabecote plainado", k=5)

    assert "forum-swap-250s#0010" in {candidate.chunk_id for candidate in found}
    top = found[0]
    # The new guarantee: this is a full-text hit, not a threshold that happened to be cleared.
    assert top.components["lexical"] > 0, "unaccent should make this a full-text hit"
    assert top.components["lexical_rank"] is not None
    # And the old behaviour is still there rather than displaced by the new one.
    assert top.components["trigram"] >= 0.6
    assert top.components["trigram_rank"] is not None


def test_every_candidate_carries_what_a_citation_needs(retriever):
    found = retriever.retrieve("coroa curta demais", k=3)

    top = found[0]
    assert top.doc_id == "forum-swap-250s"
    assert top.doc_title.startswith("Fórum Opala & Cia")
    assert top.tier == "B"
    # None, not missing: the fixture documents are Markdown and genuinely have no pages. A page is
    # read from a `<!-- page: n -->` marker or left null, never invented.
    assert top.page is None
    assert set(top.components) == {"lexical", "trigram", "lexical_rank", "trigram_rank"}


def test_the_tier_filter_excludes_community_sources(retriever):
    """The filter, and a consequence of ADR-0010 that is worth stating rather than hiding.

    The unfiltered query is Tier B material and nothing else, which is the assertion this test is
    for. What it used to assert as well — that narrowing to Tier A returns an *empty hand*, because
    no manual discusses an engine swap — is no longer true and should not be. `Kadett` is in the
    parts catalogue, and under the disjunctive fallback one matching term is enough, so the filtered
    query comes back with a Tier A chunk about the right car and the wrong subject.

    That is the trade the ADR names: the fallback fires exactly when the strict reading found
    nothing, and here it does. The filter is still doing its job and the honest way to say so is
    that every result respects it — not that the answer is empty, which was a property of the bug.
    Genuine abstention is asserted below, on a question with no term in the corpus at all.
    """
    both = retriever.retrieve("swap 250-S no Kadett", k=10)
    tier_a_only = retriever.retrieve("swap 250-S no Kadett", k=10, filters=Filters(tiers=("A",)))

    assert {candidate.tier for candidate in both} == {"B"}
    assert {candidate.tier for candidate in tier_a_only} == {"A"}
    # No chunk crosses the filter in either direction, which is the property that makes a
    # tier-narrowed comparison mean anything.
    assert not {candidate.chunk_id for candidate in both} & {
        candidate.chunk_id for candidate in tier_a_only
    }


def test_a_question_the_corpus_does_not_cover_retrieves_nothing(retriever):
    # The floor under trigram matching is what makes this possible, and abstention depends on it:
    # a retriever that always returns its ten least-bad chunks gives a generator nothing to abstain
    # on (CONTEXT.md).
    assert retriever.retrieve("receita de brigadeiro de colher", k=10) == ()


def test_ranking_is_deterministic_and_bounded(retriever):
    first = retriever.retrieve("cylinder head bolt torque", k=3)
    again = retriever.retrieve("cylinder head bolt torque", k=3)

    assert [candidate.chunk_id for candidate in first] == [c.chunk_id for c in again]
    assert len(first) <= 3
    assert [c.score for c in first] == sorted((c.score for c in first), reverse=True)
    # k is capped rather than trusted: the endpoint is public and a huge k is a way to make the
    # service read the whole corpus.
    assert len(retriever.retrieve("torque", k=10_000)) <= MAX_K


def test_the_endpoint_serves_the_same_retrieval_end_to_end(retriever):
    # No key, whatever the developer's environment holds: these tests are about retrieval, and one
    # of them would otherwise construct a real generator and call a paid API.
    settings = Settings(database_url=DATABASE_URL, corpus_dir=FIXTURE_CORPUS, gemini_api_key=None)

    # No monkeypatching: the boot check runs against the database that was just ingested, which is
    # what makes this the whole path — verify, retrieve, rank, trace, serialise.
    with TestClient(create_app(settings)) as client:
        body = client.post("/query", json={"question": "flywheel bolt torque", "k": 5}).json()

    assert body["strategy"] == "lexical"
    assert body["corpus_hash"] == corpus_hash(load_manifest(FIXTURE_CORPUS))
    assert body["chunks"][0]["chunk_id"] == "svc-kadett-1993#0006"
    assert body["chunks"][0]["tier"] == "A"
    assert body["trace"]["children"][0]["attributes"]["retrieval.strategy"] == "lexical"


class RefusingGenerator:
    """A generator that fails the test if it is ever called. No provider, no network, no key."""

    name = "must-not-be-called"
    model = "must-not-be-called"

    def generate(self, query, *, context, contract=None):
        raise AssertionError(f"the model was asked {query!r} with {len(context)} chunks")


def test_a_question_the_corpus_does_not_cover_abstains_against_the_real_database(retriever):
    # The unit tests prove the endpoint abstains when handed zero candidates. This proves the real
    # retriever hands it zero for a real question: the trigram floor and the abstention are one
    # mechanism, and testing the halves separately would let them drift apart.
    # No key, whatever the developer's environment holds: these tests are about retrieval, and one
    # of them would otherwise construct a real generator and call a paid API.
    settings = Settings(database_url=DATABASE_URL, corpus_dir=FIXTURE_CORPUS, gemini_api_key=None)

    with TestClient(create_app(settings, generator=RefusingGenerator())) as client:
        body = client.post("/query", json={"question": "receita de brigadeiro de colher"}).json()

    assert body["chunks"] == []
    assert body["answer"]["abstained"] is True
    assert body["answer"]["degraded"] is False
    # Zero cost and no stage: the model was never asked, so there is no `generate` span to show.
    assert [child["name"] for child in body["trace"]["children"]] == ["retrieve"]
