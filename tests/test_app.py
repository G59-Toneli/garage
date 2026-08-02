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
from garage.generation import Answer, Citation, Claim, Contract
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


class FakeGenerator:
    """A `Generator` with no provider under it, in the three shapes that matter.

    Same argument as `FakeRetriever`: if the endpoint behaves against this, it genuinely does not
    know which implementation it holds. It also keeps the suite free of `google.genai`, which is an
    optional extra and is not installed here.
    """

    name = "fake"
    model = "fake-1"

    def __init__(self, answer: Answer | None = None, fails: Exception | None = None):
        self.answer = answer
        self.fails = fails
        self.calls = []

    def generate(self, query, *, context, contract=Contract()):
        self.calls.append((query, tuple(context), contract))
        if self.fails is not None:
            raise self.fails
        return self.answer or answered()


def answered(chunk_id="svc-kadett-1993#0001") -> Answer:
    return Answer(
        text="O torque é 41 N·m.",
        claims=(Claim(text="O torque é 41 N·m.", citations=(Citation(1, chunk_id),)),),
        support="supported",
        provider="fake",
        model="fake-1",
        tokens_in=812,
        tokens_out=96,
        cost_usd=0.0005,
        cost_estimated=True,
        pricing_as_of="2026-08-01",
    )


def abstention() -> Answer:
    return Answer(
        abstained=True,
        abstention_reason="os trechos não cobrem a pergunta",
        provider="fake",
        model="fake-1",
        tokens_in=700,
        tokens_out=20,
    )


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

    def client(retriever, generator=None):
        monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
        return TestClient(create_app(settings, retriever=retriever, generator=generator))

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


def test_a_query_without_a_generator_answers_with_the_chunks_alone(booted):
    # Generation is the optional layer. No generator, no `answer`, no `generate` span — and the
    # response a retrieval-only deployment produces is still the one the ADR-0004 gate scores.
    with booted(FakeRetriever([candidate()])) as client:
        body = client.post("/query", json={"question": "torque"}).json()

    assert body["answer"] is None
    assert body["chunks"] and body["contract"] == "cited"
    assert [child["name"] for child in body["trace"]["children"]] == ["retrieve"]


def test_every_citation_in_the_answer_resolves_to_a_chunk_in_the_same_response(booted):
    retriever = FakeRetriever([candidate(), candidate("forum-swap-250s#0003", "B", 0.4)])

    with booted(retriever, FakeGenerator()) as client:
        body = client.post("/query", json={"question": "torque do cabeçote"}).json()

    assert body["answer"]["support"] == "supported"
    cited = {c["chunk_id"] for claim in body["answer"]["claims"] for c in claim["citations"]}
    served = {chunk["chunk_id"] for chunk in body["chunks"]}
    # A set relation, not a positional one: what matters is that no citation names a chunk the
    # reader was not given, not which position it happens to occupy.
    assert cited and cited <= served
    assert all(claim["supported"] for claim in body["answer"]["claims"])


def test_a_question_the_corpus_does_not_cover_abstains_without_calling_the_model(booted):
    generator = FakeGenerator()

    with booted(FakeRetriever([]), generator) as client:
        response = client.post("/query", json={"question": "receita de brigadeiro"})

    body = response.json()
    # 200, because a correct refusal is the behaviour we want and design §6 says it is routinely
    # misread as a failure.
    assert response.status_code == 200
    assert body["answer"]["abstained"] is True and body["answer"]["claims"] == []
    assert body["answer"]["degraded"] is False
    # Zero cost: the model was never asked, and the trace says so by omitting the stage entirely.
    assert generator.calls == []
    assert [child["name"] for child in body["trace"]["children"]] == ["retrieve"]


def test_a_generator_may_abstain_even_when_chunks_were_retrieved(booted):
    with booted(FakeRetriever([candidate()]), FakeGenerator(abstention())) as client:
        response = client.post("/query", json={"question": "pressão do turbo"})

    body = response.json()
    assert response.status_code == 200
    assert body["answer"]["abstained"] is True
    assert body["answer"]["claims"] == []
    assert body["answer"]["abstention_reason"]
    # The chunks still come back: abstaining is refusing to assert, not refusing to show the work.
    assert len(body["chunks"]) == 1
    assert body["trace"]["children"][1]["attributes"]["generation.abstained"] is True


def test_the_citation_contract_is_the_default_and_free_is_only_ever_asked_for(booted):
    generator = FakeGenerator()

    with booted(FakeRetriever([candidate()]), generator) as client:
        client.post("/query", json={"question": "torque"})
        default_contract = generator.calls[-1][2]
        client.post("/query", json={"question": "torque", "contract": "free"})
        asked_for = generator.calls[-1][2]
        rejected = client.post("/query", json={"question": "torque", "contract": "livre-demais"})

    # ADR-0005: `free` exists so the demo can show the contrast, and never as what you get by
    # leaving a field out.
    assert default_contract == Contract(mode="cited")
    assert asked_for == Contract(mode="free")
    assert rejected.status_code == 422


def test_the_generation_span_carries_the_model_its_tokens_and_an_estimated_cost(booted):
    with booted(FakeRetriever([candidate()]), FakeGenerator()) as client:
        trace = client.post("/query", json={"question": "torque"}).json()["trace"]

    generate = trace["children"][1]
    assert generate["name"] == "generate"
    assert generate["parentSpanId"] == trace["spanId"]
    attributes = generate["attributes"]
    assert attributes["generation.provider"] == "fake"
    assert attributes["generation.model"] == "fake-1"
    assert attributes["generation.contract"] == "cited"
    assert (attributes["generation.tokens.input"], attributes["generation.tokens.output"]) == (
        812,
        96,
    )
    assert attributes["generation.tokens.total"] == 908
    assert attributes["generation.cost.usd_estimated"] == 0.0005
    assert attributes["generation.pricing.as_of"] == "2026-08-01"
    assert attributes["generation.citations"] == 1
    assert attributes["generation.citations.invalid"] == 0
    assert attributes["generation.claims.unsupported"] == 0
    assert attributes["generation.degraded"] is False
    assert generate["durationMs"] >= 0


def test_a_provider_failure_degrades_to_the_retrieved_chunks_rather_than_a_blank_page(booted):
    generator = FakeGenerator(fails=RuntimeError("quota"))

    with booted(FakeRetriever([candidate()]), generator) as client:
        response = client.post("/query", json={"question": "torque"})

    body = response.json()
    # Explicitly not a 500. A visitor who asked a fair question and hit the free tier's quota gets
    # the chunks, which are most of the value, and a legible reason.
    assert response.status_code == 200
    assert len(body["chunks"]) == 1
    assert body["answer"]["degraded"] is True
    # Not an abstention: the corpus may well cover this, we simply never got to ask.
    assert body["answer"]["abstained"] is False
    assert "quota" in body["answer"]["degradation_reason"]
    generate = body["trace"]["children"][1]
    assert generate["name"] == "generate"
    assert generate["attributes"]["error"] is True
    assert generate["attributes"]["exception.type"] == "RuntimeError"
    assert generate["attributes"]["generation.degraded"] is True


def test_creating_the_app_without_an_api_key_does_not_raise(monkeypatch, settings):
    # No key is a supported configuration, not a misconfiguration: the boot gate is the corpus hash
    # alone, and `google-genai` is an optional extra this environment does not have installed.
    # Both spellings, because `Settings` accepts both (see `config.Settings.gemini_api_key`) and a
    # test that only cleared one would pass or fail depending on the developer's shell.
    monkeypatch.delenv("GARAGE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    app = create_app(Settings(database_url=settings.database_url), retriever=FakeRetriever())

    assert app.state.generator is None
