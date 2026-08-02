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
from garage.generation import Answer, Citation, Claim, Contract, MalformedPayload
from garage.retrieval import Candidate, Filters

ARTIFACT = Artifact(corpus_id="fixture", corpus_hash="0" * 64, ingest_version=1)


class FakeRetriever:
    """A `Retriever` with no database under it. Records what it was asked."""

    name = "fake"
    # Both are part of the `Retriever` contract, so the fake implements them rather than letting the
    # endpoint fall back to a default: the endpoint reads them directly, and a fake that omitted one
    # would hide a real implementation forgetting it too.
    embedder_id = None
    embedder = None

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


def invented() -> Answer:
    """An answer citing a chunk that was never retrieved — and billed for, like any other."""
    return Answer(
        text="inventado",
        claims=(Claim(text="inventado", citations=(Citation(1, "NAO-RECUPERADO#7"),)),),
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
    # `gemini_api_key=None` explicitly, and it is not decoration. `create_app` builds a real
    # `GeminiGenerator` when it finds a key, and `Settings` reads the process environment — so on a
    # developer's machine with `GEMINI_API_KEY` exported, every test here that passes no generator
    # would quietly make a paid network call. It did, before this line: one test took sixteen
    # seconds and hit the real API. A test's dependencies must come from the test.
    return Settings(database_url="postgresql://u:p@db:5432/garage", gemini_api_key=None)


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
    # The provider's raw message is *not* echoed to the visitor — free surface area — but it is in
    # the trace, where an operator reads it.
    assert "quota" not in body["answer"]["degradation_reason"]
    assert "RuntimeError" in body["answer"]["degradation_reason"]
    generate = body["trace"]["children"][1]
    assert generate["name"] == "generate"
    assert generate["attributes"]["error"] is True
    assert generate["attributes"]["exception.type"] == "RuntimeError"
    assert generate["attributes"]["exception.message"] == "quota"
    assert generate["attributes"]["generation.degraded"] is True


def test_a_provider_that_answers_in_the_wrong_shape_degrades_rather_than_abstaining(booted):
    # A payload that parses but does not match the schema is a provider that changed behaviour, and
    # reading it as an abstention would put provider breakage into the ADR-0004 abstention rate.
    generator = FakeGenerator(fails=MalformedPayload("'claims' must be a list, got dict"))

    with booted(FakeRetriever([candidate()]), generator) as client:
        body = client.post("/query", json={"question": "torque"}).json()

    assert body["answer"]["degraded"] is True
    assert body["answer"]["abstained"] is False


def test_an_internal_bug_is_not_reported_as_the_provider_failing(booted):
    # The `try` wraps the provider call and nothing else. If it wrapped the code around it, every
    # mistake of ours would reach the visitor as an accusation against a dependency that behaved.
    class BrokenGenerator(FakeGenerator):
        """Answers fine, then hands back something this endpoint mishandles."""

        def generate(self, query, *, context, contract=Contract()):
            super().generate(query, context=context, contract=contract)
            return "not an Answer at all"

    with booted(FakeRetriever([candidate()]), BrokenGenerator()) as client:
        with pytest.raises(AttributeError):
            client.post("/query", json={"question": "torque"})


def test_a_generator_that_invents_a_citation_is_caught_by_the_endpoint(booted):
    # `Generator` is a runtime axis, so the implementation that forgets to validate is the next one.
    # "Every citation resolves" has to hold for the system, not for one adapter's diligence.
    with booted(FakeRetriever([candidate()]), FakeGenerator(invented())) as client:
        response = client.post("/query", json={"question": "torque"})

    body = response.json()
    assert response.status_code == 200
    answer = body["answer"]
    # Not a degradation and not an abstention: our bug, reported as ours.
    assert answer["degraded"] is False and answer["abstained"] is False
    assert answer["support"] == "rejected" and answer["contract_violation"]
    assert answer["claims"] == [] and answer["text"] == ""
    # The retriever's work is untouched and still served.
    assert len(body["chunks"]) == 1
    generate = body["trace"]["children"][1]
    assert generate["attributes"]["generation.contract.violated"] is True
    assert generate["attributes"]["error"] is True


def test_a_rejected_answer_still_records_what_the_call_cost(booted):
    # The provider answered and charged for it. We refused the answer; nobody refunded the tokens.
    # A span that dropped the cost here would make a configuration that reliably breaks the citation
    # contract look like the cheap one in the comparison the demo puts on screen.
    with booted(FakeRetriever([candidate()]), FakeGenerator(invented())) as client:
        body = client.post("/query", json={"question": "torque"}).json()

    assert body["answer"]["support"] == "rejected"
    attributes = body["trace"]["children"][1]["attributes"]
    assert attributes["generation.tokens.input"] == 812
    assert attributes["generation.tokens.output"] == 96
    assert attributes["generation.tokens.total"] == 908
    assert attributes["generation.cost.usd_estimated"] == 0.0005
    assert attributes["generation.cost.estimated"] is True
    assert attributes["generation.pricing.as_of"] == "2026-08-01"
    assert attributes["generation.support"] == "rejected"


def test_a_degraded_call_records_no_cost_because_none_was_incurred(booted):
    # The mirror image, and the asymmetry with the test above is the correct behaviour rather than
    # an inconsistency: no answer came back, so there is nothing to bill and nothing to record.
    # Writing a zero here would invent a charge, exactly as writing none above would hide one.
    with booted(FakeRetriever([candidate()]), FakeGenerator(fails=RuntimeError("quota"))) as client:
        body = client.post("/query", json={"question": "torque"}).json()

    attributes = body["trace"]["children"][1]["attributes"]
    assert attributes["generation.degraded"] is True
    assert "generation.cost.usd_estimated" not in attributes
    assert "generation.tokens.total" not in attributes


def test_the_served_rejection_carries_the_cost_the_span_carries(booted):
    """The sibling of the span test above, one layer out, and it exists because that test passed
    while the response was wrong.

    The span recorded the cost of a rejection from the day the state was introduced. The `Answer`
    did not — `reject_unverifiable` filled five fields and let the billing fall to its defaults — so
    the trace said 812/96 tokens at $0.0005 and the wire said 0/0 at null for the same call. Nothing
    read the `Answer` in that state until an interface did, and it then printed a cost panel
    identical to a degradation's under a sentence explaining that this one was paid for.

    A pair of assertions on the span alone cannot catch that, because the two are separate readings
    of the same event. So the pair is doubled: cost present on the span *and* on the response here,
    absent on both in the degradation below.
    """
    with booted(FakeRetriever([candidate()]), FakeGenerator(invented())) as client:
        body = client.post("/query", json={"question": "torque"}).json()

    answer = body["answer"]
    assert answer["support"] == "rejected"
    assert (answer["tokens_in"], answer["tokens_out"]) == (812, 96)
    assert answer["cost_usd"] == 0.0005
    assert answer["cost_estimated"] is True
    assert answer["pricing_as_of"] == "2026-08-01"
    # One invalid citation, counted. Zero here was the worst of the three wrong numbers: it was
    # printed in the one state whose entire cause is an invalid citation.
    assert answer["invalid_citations"] == 1

    # And the two readings agree, which is now structural rather than coincidental — `app._answer`
    # builds the rejected answer first and writes the span from it.
    attributes = body["trace"]["children"][1]["attributes"]
    assert attributes["generation.cost.usd_estimated"] == answer["cost_usd"]
    assert attributes["generation.tokens.input"] == answer["tokens_in"]
    assert attributes["generation.citations.invalid"] == answer["invalid_citations"]


def test_the_served_degradation_carries_no_cost_because_none_was_incurred(booted):
    # The mirror image at the response level. Do not "tidy" these two into agreement: the provider
    # never answered here, so a zero cost would be an invented charge exactly as a missing one above
    # would be a hidden charge. The interface renders these two panels from these two payloads, and
    # if they ever match, one of them is lying.
    with booted(FakeRetriever([candidate()]), FakeGenerator(fails=RuntimeError("quota"))) as client:
        answer = client.post("/query", json={"question": "torque"}).json()["answer"]

    assert answer["degraded"] is True
    assert answer["cost_usd"] is None
    assert (answer["tokens_in"], answer["tokens_out"]) == (0, 0)
    assert answer["pricing_as_of"] is None
    # Nothing was validated because nothing came back, so this is genuinely zero rather than unknown.
    assert answer["invalid_citations"] == 0


def test_every_unresolvable_citation_is_counted_not_just_the_first(booted):
    # `verify_citations` used to raise on the first violation, which made the served count 1 for any
    # number of them. The count is printed, so it has to be the real one.
    two_bad = Answer(
        text="inventado",
        claims=(
            Claim(text="um", citations=(Citation(1, "NAO-RECUPERADO#7"),)),
            Claim(text="dois", citations=(Citation(2, "TAMBEM-NAO#9"),)),
        ),
        support="supported",
        provider="fake",
        model="fake-1",
        tokens_in=10,
        tokens_out=2,
    )

    with booted(FakeRetriever([candidate()]), FakeGenerator(two_bad)) as client:
        answer = client.post("/query", json={"question": "torque"}).json()["answer"]

    assert answer["support"] == "rejected"
    assert answer["invalid_citations"] == 2


def test_the_rejection_reason_matches_the_violation_it_describes(booted):
    """Two sentences, chosen by the count, because one of them used to be a contradiction.

    Every rejection said "o gerador produziu citações que não resolvem" — including the one caused by
    a claim marked supported with *no citations at all*, where the prefix asserted there were
    citations and the clause behind it said there were none. `ContractViolation` already treated that
    as a different violation; the sentence on screen did not.
    """
    unsourced = Answer(
        text="sem fonte",
        claims=(Claim(text="afirmação sem fonte", citations=(), supported=True),),
        support="supported",
        provider="fake",
        model="fake-1",
    )

    with booted(FakeRetriever([candidate()]), FakeGenerator(unsourced)) as client:
        answer = client.post("/query", json={"question": "torque"}).json()["answer"]

    assert answer["support"] == "rejected"
    assert answer["contract_violation"] == "o gerador marcou uma afirmação como sustentada sem nenhuma citação"
    # No citation was offered, so none could fail to resolve. Zero is the honest count here and the
    # sentence above is what says which violation it was.
    assert answer["invalid_citations"] == 0

    with booted(FakeRetriever([candidate()]), FakeGenerator(invented())) as client:
        answer = client.post("/query", json={"question": "torque"}).json()["answer"]

    assert answer["contract_violation"] == "o gerador citou trechos que não foram recuperados"


def test_the_clause_by_clause_violation_stays_on_the_span(booted):
    # Same split degradation already uses: a short sentence in the page's language on the wire, the
    # raw detail on the span. At k=10 with every citation bad the clause list runs to ten
    # near-identical English clauses, which belongs behind a `<details>` and not in the one paragraph
    # a visitor reads to understand what a rejection is.
    with booted(FakeRetriever([candidate()]), FakeGenerator(invented())) as client:
        body = client.post("/query", json={"question": "torque"}).json()

    reason = body["answer"]["contract_violation"]
    detail = body["trace"]["children"][1]["attributes"]["exception.message"]
    assert "NAO-RECUPERADO#7" in detail
    assert "NAO-RECUPERADO#7" not in reason
    assert len(reason) < len(detail)


def test_the_contract_that_ran_is_the_contract_the_answer_reports(booted):
    # Both no-call paths defaulted this field to `cited`, so a `free` run was filed under the wrong
    # configuration axis — invisible in the response, corrupting in a stored run record.
    with booted(FakeRetriever([]), FakeGenerator()) as client:
        abstained = client.post("/query", json={"question": "x", "contract": "free"}).json()

    with booted(FakeRetriever([candidate()]), FakeGenerator(fails=RuntimeError("quota"))) as client:
        degraded = client.post("/query", json={"question": "x", "contract": "free"}).json()

    assert (abstained["contract"], abstained["answer"]["contract"]) == ("free", "free")
    assert (degraded["contract"], degraded["answer"]["contract"]) == ("free", "free")
    assert degraded["answer"]["degraded"] is True and abstained["answer"]["abstained"] is True


def test_creating_the_app_without_an_api_key_does_not_raise(monkeypatch, settings):
    # No key is a supported configuration, not a misconfiguration: the boot gate is the corpus hash
    # alone, and `google-genai` is an optional extra this environment does not have installed.
    # Both spellings, because `Settings` accepts both (see `config.Settings.gemini_api_key`) and a
    # test that only cleared one would pass or fail depending on the developer's shell.
    monkeypatch.delenv("GARAGE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    # Built from the environment here, unlike everywhere else in this file, because the environment
    # is precisely what is under test.
    app = create_app(Settings(database_url=settings.database_url), retriever=FakeRetriever())

    assert app.state.generator is None
