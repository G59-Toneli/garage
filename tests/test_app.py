"""The HTTP surface, tested without a database behind it.

`Retriever` is an interface (design §7.1), so the endpoint can be exercised against one that answers
from memory. That is not a shortcut — it is the property under test: if these pass with a fake, the
endpoint genuinely does not know which implementation it holds. What the fake cannot prove is that
lexical retrieval finds the right chunk; that lives in `test_retrieval.py`, against Postgres.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from garage import app as app_module
from garage.app import create_app
from garage.config import Settings
from garage.ingest import Artifact, ArtifactMismatch
from garage.retrieval import Candidate, Filters

ARTIFACT = Artifact(corpus_id="fixture", corpus_hash="0" * 64, ingest_version=1)


class FakeRetriever:
    """A `Retriever` with no database under it. Records what it was asked."""

    name = "fake"

    def __init__(self, candidates=()):
        self.candidates = tuple(candidates)
        self.calls = []

    def retrieve(self, query, *, k=10, filters=None):
        self.calls.append((query, k, filters))
        return self.candidates[:k]


def candidate(chunk_id="svc-kadett-1993#0001", tier="A", score=0.9) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_id="svc-kadett-1993",
        doc_title="Manual de Serviço",
        tier=tier,
        page=12,
        section="Section 3.2",
        kind="spec",
        text="Torque (N·m): 41",
        score=score,
        components={"lexical": 0.8, "trigram": 0.5},
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="postgresql://u:p@db:5432/garage")


@pytest.fixture
def booted(monkeypatch, settings):
    """A client whose boot check passed, holding a retriever the test controls."""

    def client(retriever):
        monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
        return TestClient(create_app(settings, retriever=retriever))

    return client


def test_health_reports_the_running_version(settings):
    response = TestClient(create_app(settings)).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_booting_without_configuration_fails_loudly(monkeypatch):
    monkeypatch.delenv("GARAGE_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        create_app()


def test_the_service_refuses_to_start_when_the_corpus_hash_does_not_match(monkeypatch, settings):
    def mismatch(*_):
        raise ArtifactMismatch("the database holds a different Corpus")

    monkeypatch.setattr(app_module, "verify_artifact", mismatch)

    # Entering the client is what runs the boot check, exactly as uvicorn does.
    with pytest.raises(ArtifactMismatch):
        with TestClient(create_app(settings, retriever=FakeRetriever())):
            pass


def test_a_query_returns_ranked_chunks_with_score_tier_document_and_page(booted):
    retriever = FakeRetriever([candidate(score=0.9), candidate("forum-swap-250s#0003", "B", 0.4)])

    with booted(retriever) as client:
        response = client.post("/query", json={"question": "torque do cabeçote"})

    assert response.status_code == 200
    body = response.json()
    assert [chunk["score"] for chunk in body["chunks"]] == [0.9, 0.4]
    first = body["chunks"][0]
    assert (first["tier"], first["doc_id"], first["page"]) == ("A", "svc-kadett-1993", 12)
    assert first["doc_title"] and first["chunk_id"] and first["text"]
    # The per-signal scores behind the total: the demo shows them, so the wire carries them.
    assert set(first["components"]) == {"lexical", "trigram"}
    assert body["corpus_hash"] == ARTIFACT.corpus_hash
    assert body["strategy"] == "fake"


def test_the_response_carries_a_span_tree_with_per_stage_timings(booted):
    with booted(FakeRetriever([candidate()])) as client:
        trace = client.post("/query", json={"question": "folga de válvula"}).json()["trace"]

    assert trace["name"] == "query"
    assert trace["attributes"]["corpus.hash"] == ARTIFACT.corpus_hash
    retrieve = trace["children"][0]
    assert retrieve["name"] == "retrieve"
    assert retrieve["attributes"]["retrieval.strategy"] == "fake"
    assert retrieve["attributes"]["retrieval.candidates"] == 1
    assert retrieve["durationMs"] >= 0 and trace["durationMs"] >= 0
    assert retrieve["parentSpanId"] == trace["spanId"]


def test_the_tier_filter_and_k_reach_the_retriever(booted):
    retriever = FakeRetriever([candidate()])

    with booted(retriever) as client:
        response = client.post("/query", json={"question": "swap 250-S", "k": 3, "tiers": ["A"]})

    assert response.status_code == 200
    assert retriever.calls == [("swap 250-S", 3, Filters(tiers=("A",)))]


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "torque", "k": 0},
        {"question": "torque", "k": 10_000},
        {"question": "torque", "tiers": ["C"]},
        {"question": "torque", "tiers": []},
        # An unknown field is a rejected request, not an ignored one: `strategy` will be a real axis,
        # and a client that misspells it must hear about it rather than silently get the default.
        {"question": "torque", "strategy": "dense"},
    ],
)
def test_a_malformed_query_is_rejected_rather_than_guessed_at(booted, payload):
    with booted(FakeRetriever()) as client:
        assert client.post("/query", json=payload).status_code == 422
