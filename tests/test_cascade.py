"""The four origins of `POST /query`, end to end, with no database and no provider.

This is the file that holds issue #11's acceptance criteria three, four and five to their word. Each
one is a claim about what a *visitor* experiences when the free tier is under pressure, so each one
is asserted against the HTTP surface rather than against the limiter — `test_limits.py` already
proves the arithmetic, and arithmetic that is right and wired up wrong is the failure mode this file
exists for.

Everything runs through `TestClient` against the same fakes `test_app.py` uses, plus a committed
showcase record built by `test_showcase.py`'s own helpers. No network, no key, no Postgres. The one
database call the precomputed path makes — hydrating chunk text out of the artifact — is faked here
for the same reason it is faked there: what matters is that it is *local and free*, not that it is
Postgres.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from garage import app as app_module
from garage.app import create_app
from garage.retrieval import StoredChunk

from test_app import (
    ARTIFACT,
    FakeGenerator,
    FakeRetriever,
    candidate,
    settings,  # noqa: F401 — a fixture, used by name
)
from test_showcase import (
    IDENTITY,
    QUESTION,
    CountingGenerator,
    ExplodingGenerator,
    build,
    offline,  # noqa: F401 — a fixture, used by name
)
from garage.showcase import write_showcase_record


@pytest.fixture
def hydration(monkeypatch):
    """`fetch_chunks`, answered from memory. The precomputed path's only database read."""

    def fetch(url, ids):
        return tuple(
            StoredChunk(
                chunk_id=chunk_id,
                doc_id="svc-kadett-1993",
                doc_title="Manual de Serviço",
                tier="A",
                page=12,
                section="Section 3.2",
                kind="spec",
                text="Torque (N·m): 41",
            )
            for chunk_id in ids
        )

    monkeypatch.setattr(app_module, "fetch_chunks", fetch)
    return fetch


@pytest.fixture
def curated(offline, settings, tmp_path):  # noqa: F811
    """Settings pointing at a directory holding one committed showcase record."""
    record = build(CountingGenerator())
    directory = tmp_path / "showcase"
    write_showcase_record(record, directory)
    return settings.model_copy(update={"showcase_dir": directory}), record


def client(settings, *, retriever=None, generator=None, monkeypatch):  # noqa: F811
    monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
    # The showcase boot gate reads the live text search configuration when a record is committed
    # (ADR-0010), and the `curated` fixture commits one. Stubbed with the values `PROVENANCE` gives
    # those records, so the cascade tests exercise a record that *does* describe this build — which
    # is the precondition for every assertion in this file about the precomputed origin.
    monkeypatch.setattr(app_module, "artifact_identity", lambda *_: IDENTITY)
    return TestClient(
        create_app(
            settings,
            retriever=retriever if retriever is not None else FakeRetriever([candidate()]),
            generator=generator,
        )
    )


def ask(http, **body):
    body.setdefault("question", "torque do cabeçote")
    response = http.post("/query", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- criterion three: free-form questions are cached ----------------------------------------------


def test_the_second_identical_question_is_served_from_cache_without_a_second_model_call(
    monkeypatch, settings  # noqa: F811
):
    generator = FakeGenerator()
    with client(settings, generator=generator, monkeypatch=monkeypatch) as http:
        first = ask(http)
        second = ask(http)

    assert first["origin"] == "live"
    assert second["origin"] == "cache"
    # The assertion that matters. Everything else here is bookkeeping; this is the money.
    assert len(generator.calls) == 1
    # And the response is the *same* response, not a reconstruction of one.
    assert second["answer"] == first["answer"]
    assert second["trace"] == first["trace"]
    assert second["chunks"] == first["chunks"]


def test_the_cache_says_when_the_answer_it_is_serving_was_generated(monkeypatch, settings):  # noqa: F811
    with client(settings, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        ask(http)
        second = ask(http)

    detail = second["origin_detail"]
    # Displayed by `render.originSentence` as "resposta em cache, gerada às HH:MM". A cache the
    # visitor cannot see is a cache making the site look faster than it is.
    assert detail["stored_at"] and detail["age_seconds"] >= 0
    assert detail["hits"] == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"contract": "free"},
        {"k": 4},
        {"tiers": ["A"]},
        {"question": "torque do virabrequim"},
    ],
)
def test_changing_a_runtime_axis_is_a_different_question_and_costs_another_call(
    monkeypatch, settings, changed  # noqa: F811
):
    """The cache must never be the thing that makes two configurations agree.

    `contract` is the one that carries the argument: ADR-0005 says `free` exists *only* to show what
    the citation contract buys, so serving a `cited` answer to a `free` request would delete the
    comparison the site was built for. The others are here because a cache is only safe if every axis
    that changes the output is in the key, and a test per axis is how that stays true.
    """
    generator = FakeGenerator()
    with client(settings, generator=generator, monkeypatch=monkeypatch) as http:
        ask(http)
        second = ask(http, **changed)

    assert second["origin"] == "live"
    assert len(generator.calls) == 2


def test_a_degradation_is_never_cached(monkeypatch, settings):  # noqa: F811
    """A cached refusal would go on refusing for a day after the budget came back at midnight."""
    tight = settings.model_copy(update={"generation_budget_per_day": 0})
    with client(tight, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        first = ask(http)
        second = ask(http)

    assert first["origin"] == second["origin"] == "live_degraded"
    assert first["origin_detail"]["generations_remaining"] == 0


def test_a_zero_cost_abstention_is_neither_cached_nor_billed(monkeypatch, settings):  # noqa: F811
    """No candidates means nobody was asked, so the budget must not have moved."""
    with client(
        settings, retriever=FakeRetriever([]), generator=FakeGenerator(), monkeypatch=monkeypatch
    ) as http:
        body = ask(http, question="como faço pão")
        again = ask(http, question="como faço pão")

    assert body["answer"]["abstained"] is True and body["answer"]["provider"] is None
    assert body["origin"] == again["origin"] == "live"
    # Refunded. A budget that counted abstentions would spend the day's quota on exactly the
    # questions the corpus does not cover — the ones that are supposed to be free.
    assert body["origin_detail"]["generations_used"] == 0


# --- criterion three: free-form questions are rate limited ------------------------------------------


def test_the_anti_abuse_bucket_is_the_only_429_and_it_carries_retry_after(monkeypatch, settings):  # noqa: F811
    hammered = settings.model_copy(update={"requests_per_minute": 2})
    with client(hammered, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        codes = [
            http.post("/query", json={"question": f"pergunta {index}"}).status_code
            for index in range(4)
        ]
        refused = http.post("/query", json={"question": "mais uma"})

    assert codes == [200, 200, 429, 429]
    assert int(refused.headers["Retry-After"]) >= 1


def test_a_spent_generation_budget_is_a_200_and_never_a_429(monkeypatch, settings):  # noqa: F811
    """The central decision of the cascade, asserted as a status code.

    A 429 here would throw away the retrieval — which is local, free, eleven milliseconds and the
    actual product — in order to report that the paid half is unavailable.
    """
    tight = settings.model_copy(update={"generation_budget_per_day": 1})
    with client(tight, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        ask(http, question="primeira")
        response = http.post("/query", json={"question": "segunda"})

    assert response.status_code == 200
    body = response.json()
    assert body["origin"] == "live_degraded"
    # Five states, not six. `degraded` is reused, with a reason of its own.
    assert body["answer"]["degraded"] is True and body["answer"]["abstained"] is False
    assert "orçamento diário" in body["answer"]["degradation_reason"]
    assert body["origin_detail"]["refusal"] == "global_daily"
    # And the chunks are still there, which is the entire argument.
    assert len(body["chunks"]) == 1


def test_a_degraded_response_opens_no_generate_span(monkeypatch, settings):  # noqa: F811
    """The trace must say truthfully that nothing was asked."""
    tight = settings.model_copy(update={"generation_budget_per_day": 0})
    with client(tight, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        body = ask(http)

    assert [child["name"] for child in body["trace"]["children"]] == ["retrieve"]
    assert body["trace"]["attributes"]["generation.admitted"] is False


def test_the_per_client_limit_names_itself_separately_from_the_global_one(monkeypatch, settings):  # noqa: F811
    tight = settings.model_copy(
        update={"generations_per_day_per_client": 1, "generation_budget_per_day": 100}
    )
    with client(tight, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        ask(http, question="primeira")
        body = ask(http, question="segunda")

    assert body["origin_detail"]["refusal"] == "client_daily"
    # A different sentence, because "you used your share" and "the site used its share" are
    # different facts and only one of them is the visitor's fault.
    assert "sua cota" in body["answer"]["degradation_reason"]


def test_a_build_with_no_generator_reports_live_and_not_degraded(monkeypatch, settings):  # noqa: F811
    """The retrieval-only configuration is not a failure and must not start reading as one."""
    with client(settings, monkeypatch=monkeypatch) as http:
        body = ask(http)

    assert body["origin"] == "live"
    assert body["answer"] is None


# --- criteria two and four: the precomputed path ------------------------------------------------------


def test_a_curated_question_is_answered_from_the_record_with_a_generator_that_would_explode(
    monkeypatch, curated, hydration
):
    """Criterion two, on `POST /query` and not only on the showcase screen.

    `ExplodingGenerator` raises on any call, so this is the criterion mechanised rather than
    observed: a test that merely happened not to make a model call would still pass on the day
    something started making them.
    """
    settings, record = curated  # noqa: F811
    with client(settings, generator=ExplodingGenerator(), monkeypatch=monkeypatch) as http:
        body = ask(http, question=QUESTION.question)

    assert body["origin"] == "precomputed"
    assert body["origin_detail"]["showcase_id"] == record.showcase_id
    assert body["origin_detail"]["question_id"] == QUESTION.question_id
    # The single displayed draw never travels without the distribution it came from (ADR-0004).
    assert body["origin_detail"]["n"] == body["origin_detail"]["spread"]["tokens_out"]["n"]
    assert body["answer"]["text"]
    # Hydrated locally, out of the artifact. The record stores identifiers and never words.
    assert body["chunks"][0]["text"] == "Torque (N·m): 41"
    assert body["origin_detail"]["chunks_absent"] == []


def test_the_curated_question_echoes_what_the_visitor_typed_not_the_records_phrasing(
    monkeypatch, curated, hydration
):
    settings, _ = curated  # noqa: F811
    typed = f"  {QUESTION.question.upper()}  "
    with client(settings, generator=ExplodingGenerator(), monkeypatch=monkeypatch) as http:
        body = ask(http, question=typed)

    assert body["origin"] == "precomputed"
    assert body["question"] == typed


def test_a_curated_question_is_still_precomputed_after_the_budget_is_gone(
    monkeypatch, curated, hydration
):
    """Criterion four, in its only honest form.

    The showcase lookup is *before* the budget, so exhausting the budget changes nothing about a
    curated question. That is what "degrades to the pre-computed path" can mean — and the test below
    states the limit of it.
    """
    settings, _ = curated  # noqa: F811
    spent = settings.model_copy(update={"generation_budget_per_day": 0})
    with client(spent, generator=ExplodingGenerator(), monkeypatch=monkeypatch) as http:
        first = ask(http, question=QUESTION.question)
        second = ask(http, question=QUESTION.question)

    assert first["origin"] == second["origin"] == "precomputed"
    assert first["answer"] == second["answer"]


def test_a_free_form_question_has_no_precomputed_path_to_fall_back_to(monkeypatch, curated, hydration):
    """The honest limitation, asserted so the documentation cannot drift away from the behaviour.

    "Quota exhaustion degrades to the pre-computed path" is only true for questions that are actually
    in the curated set. For anything else the real cascade ends at retrieval-only, and promising more
    would be a lie the site tells about itself.
    """
    settings, _ = curated  # noqa: F811
    spent = settings.model_copy(update={"generation_budget_per_day": 0})
    with client(spent, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        body = ask(http, question="uma pergunta que ninguém curou")

    assert body["origin"] == "live_degraded"
    assert body["answer"]["degraded"] is True
    assert len(body["chunks"]) == 1


def test_a_curated_question_under_a_different_contract_is_not_the_recorded_one(
    monkeypatch, curated, hydration
):
    """Same question, different runtime axis, different answer — or the axis does not exist."""
    settings, _ = curated  # noqa: F811
    with client(settings, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        body = ask(http, question=QUESTION.question, contract="free")

    assert body["origin"] == "live"


# --- criterion five: the re-run button -----------------------------------------------------------------


def test_the_rerun_flag_bypasses_both_the_record_and_the_cache(monkeypatch, curated, hydration):
    settings, _ = curated  # noqa: F811
    generator = FakeGenerator()
    with client(settings, generator=generator, monkeypatch=monkeypatch) as http:
        recorded = ask(http, question=QUESTION.question)
        live = ask(http, question=QUESTION.question, rerun=True)
        again = ask(http, question=QUESTION.question, rerun=True)

    assert recorded["origin"] == "precomputed"
    assert live["origin"] == "live"
    # The second re-run is a second call. A re-run served out of the cache is a re-run of nothing.
    assert again["origin"] == "live"
    assert len(generator.calls) == 2


def test_a_rerun_the_budget_refuses_falls_back_to_the_record_and_says_so(
    monkeypatch, curated, hydration
):
    """They asked to falsify a published number and were not allowed to. Saying so is the feature."""
    settings, record = curated  # noqa: F811
    spent = settings.model_copy(update={"generation_budget_per_day": 0})
    with client(spent, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        body = ask(http, question=QUESTION.question, rerun=True)

    assert body["origin"] == "precomputed"
    assert body["origin_detail"]["rerun_refused"] is True
    assert body["origin_detail"]["showcase_id"] == record.showcase_id


def test_a_refused_rerun_of_an_uncurated_question_degrades_and_flags_itself(monkeypatch, settings):  # noqa: F811
    spent = settings.model_copy(update={"generation_budget_per_day": 0})
    with client(spent, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        body = ask(http, question="nada curado aqui", rerun=True)

    assert body["origin"] == "live_degraded"
    assert body["origin_detail"]["rerun_refused"] is True


def test_a_rerun_spends_the_budget_like_any_other_generation(monkeypatch, curated, hydration):
    """The button is not a way around the quota, and this is the test that keeps it that way."""
    settings, _ = curated  # noqa: F811
    tight = settings.model_copy(update={"generation_budget_per_day": 1})
    with client(tight, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        first = ask(http, question=QUESTION.question, rerun=True)
        second = ask(http, question=QUESTION.question, rerun=True)

    assert first["origin"] == "live"
    assert second["origin"] == "precomputed"
    assert second["origin_detail"]["rerun_refused"] is True


# --- the forwarded-for question ------------------------------------------------------------------------


def test_the_forwarded_header_is_ignored_unless_the_deployment_trusts_it(monkeypatch, settings):  # noqa: F811
    """Spoofing the header must not buy a fresh per-client allowance on a direct deployment."""
    direct = settings.model_copy(
        update={"generations_per_day_per_client": 1, "generation_budget_per_day": 100}
    )
    with client(direct, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        ask(http, question="um")
        spoofed = http.post(
            "/query",
            json={"question": "dois"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        ).json()

    assert spoofed["origin_detail"]["refusal"] == "client_daily"


def test_behind_a_trusted_proxy_the_forwarded_address_is_the_client(monkeypatch, settings):  # noqa: F811
    """And with it, per-IP limiting stops silently collapsing into a second global limit."""
    proxied = settings.model_copy(
        update={
            "trust_forwarded_for": True,
            "generations_per_day_per_client": 1,
            "generation_budget_per_day": 100,
        }
    )
    with client(proxied, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        one = http.post(
            "/query", json={"question": "um"}, headers={"X-Forwarded-For": "203.0.113.1"}
        ).json()
        two = http.post(
            "/query", json={"question": "dois"}, headers={"X-Forwarded-For": "203.0.113.2"}
        ).json()
        again = http.post(
            "/query", json={"question": "tres"}, headers={"X-Forwarded-For": "203.0.113.1"}
        ).json()

    assert one["origin"] == two["origin"] == "live"
    assert again["origin_detail"]["refusal"] == "client_daily"


def test_a_cache_hit_echoes_the_question_this_visitor_typed(monkeypatch, settings):  # noqa: F811
    """The cached payload carries whoever asked first. Echoing that back is a small lie.

    `cache.py` argues at length that accents are deliberately not folded because the question travels
    back out on the response and is printed on screen — and then the cache served the *first*
    asker's spelling to everyone who followed. Case and spacing normalise to the same key on purpose;
    they must not normalise on the way back out.
    """
    generator = FakeGenerator()
    typed_first = "torque do cabeçote"
    typed_second = "  TORQUE   DO   CABEÇOTE  "
    with client(settings, generator=generator, monkeypatch=monkeypatch) as http:
        first = ask(http, question=typed_first)
        second = ask(http, question=typed_second)

    # Same key — one model call, not two.
    assert (first["origin"], second["origin"]) == ("live", "cache")
    assert len(generator.calls) == 1
    # Different echo. Each visitor reads back what they wrote.
    assert first["question"] == typed_first
    assert second["question"] == typed_second


def test_a_build_with_no_generator_does_not_advertise_a_generation_budget(monkeypatch, settings):  # noqa: F811
    """A retrieval-only deployment is a supported configuration, not a quota of two hundred.

    Without this flag the origin band announced "200 de 200 gerações restantes hoje" on a service
    that cannot generate anything, which is a budget for a thing it does not do. The numbers stay on
    the wire — an operator may still want them — but the interface is told not to make the claim.
    """
    with client(settings, monkeypatch=monkeypatch) as http:
        body = ask(http)
        provenance = http.get("/provenance").json()

    assert body["origin"] == "live" and body["answer"] is None
    assert body["origin_detail"]["generation_configured"] is False
    assert provenance["generation_configured"] is False

    with client(settings, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        configured = ask(http)
        provenance = http.get("/provenance").json()

    assert configured["origin_detail"]["generation_configured"] is True
    assert provenance["generation_configured"] is True


def test_the_suite_wide_default_leaves_the_bucket_off_but_a_test_can_turn_it_on(settings):  # noqa: F811
    """The order-independence policy, asserted rather than assumed.

    `conftest.the_anti_abuse_bucket_is_off_unless_a_test_asks_for_it` sets the environment variable
    for every test, so an app built from ambient settings anywhere in this suite cannot accumulate a
    hidden budget and fail whichever test happens to be eleventh. This asserts both halves of that:
    the default really is off, and an explicit value really does win — the second half is what keeps
    `test_the_anti_abuse_bucket_is_only_429_and_it_carries_retry_after` above from being silently
    neutered by the very fixture that makes the rest of the suite safe.
    """
    from garage.config import Settings

    assert Settings(database_url="postgresql://u:p@db/g").requests_per_minute == 0
    # A constructor argument beats the environment (pydantic-settings priority), and so does
    # `model_copy`, which bypasses validation entirely. Both spellings are used by tests above.
    assert Settings(database_url="postgresql://u:p@db/g", requests_per_minute=7).requests_per_minute == 7
    assert settings.model_copy(update={"requests_per_minute": 2}).requests_per_minute == 2


def test_an_app_built_from_ambient_settings_cannot_run_out_of_bucket(monkeypatch, settings):  # noqa: F811
    """The regression, as a test: more than ten `POST /query` calls against one app object.

    Before the conftest fixture this was a 429 on the eleventh call, and the failure landed on
    whichever unlucky test in the suite crossed the line first rather than on anything to do with
    rate limiting.
    """
    with client(settings, generator=FakeGenerator(), monkeypatch=monkeypatch) as http:
        codes = [
            http.post("/query", json={"question": f"pergunta {index}"}).status_code
            for index in range(25)
        ]

    assert set(codes) == {200}
