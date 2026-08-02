"""The wire contract the interface is written against, asserted field by field.

This file buys the type-checking a hand-written JavaScript client does not have. `render.js` reads
`chunk.doc_title`, `answer.contract_violation` and `span.durationMs` by name; nothing in Python knows
that, so renaming a field on `QueryResponse` would leave the whole suite green and break the demo at
runtime, in the browser, silently. The assertions below are deliberately about the **exact** set of
keys rather than the presence of the ones in use: a field *added* to the response and not shown is
also a regression — this project's claim is that the interface displays what the system produced,
and a payload growing a component the panel drops on the floor quietly falsifies it.

Everything runs against `TestClient`, which is `httpx` over the ASGI app, with the same fakes
`test_app.py` uses. No database, no network, no model.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from garage import app as app_module
from garage.app import STATIC_DIR, create_app
from garage.evaluation import BASELINE_PATH, RUNS_DIR

from test_app import (
    ARTIFACT,
    FakeGenerator,
    FakeRetriever,
    abstention,
    answered,
    candidate,
    settings,  # noqa: F401 — a fixture, used by name
)

QUERY_RESPONSE_KEYS = {
    # Issue #11. These two are what let the screen say whether a number was measured for this
    # visitor, copied out of a cache, or read off a file somebody committed in April — and this test
    # failing when they were added is the test working. The exact-key assertion exists precisely so
    # that growing this payload is a decision somebody takes in a diff rather than a thing that
    # happens; the band that renders them is `render.originBand`.
    "origin",
    "origin_detail",
    "question",
    "corpus_hash",
    "strategy",
    "embedder",
    "k",
    "tiers",
    "chunks",
    "contract",
    "answer",
    "trace",
}

RETRIEVED_CHUNK_KEYS = {
    "chunk_id",
    "doc_id",
    "doc_title",
    "tier",
    "page",
    "section",
    "kind",
    "text",
    "score",
    "components",
}

GENERATED_ANSWER_KEYS = {
    "text",
    "claims",
    "abstained",
    "abstention_reason",
    "degraded",
    "degradation_reason",
    "contract_violation",
    "support",
    "provider",
    "model",
    "contract",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "cost_estimated",
    "pricing_as_of",
    "invalid_citations",
    "unsupported_claims",
    "contradictory",
}

# `origin_detail` is a bare dict on the model, so its shape is asserted here rather than by Pydantic
# — which is the whole reason these two constants exist. `adapt.originView` reads every one of these
# by name, and a renamed key would leave the suite green and blank the origin band in a browser.
#
# Two sets and not one union: the fields genuinely do not overlap, and a flat model holding both
# would have to publish a null `showcase_id` on every live answer and a null `generations_remaining`
# on every recorded one. Nulls that mean "not applicable" are how a screen ends up rendering
# "showcase: —" beside a live number.
LIVE_ORIGIN_DETAIL_KEYS = {
    "key",
    "refusal",
    "rerun",
    "rerun_refused",
    "utc_day",
    "generation_budget",
    "generations_used",
    "generations_remaining",
}

PRECOMPUTED_ORIGIN_DETAIL_KEYS = {
    "showcase_id",
    "scope",
    "question_id",
    "why",
    "measured_on",
    "generator",
    "model",
    "temperature",
    "n",
    "displayed_sample",
    "display_rule",
    "git_sha",
    "git_dirty",
    "spread",
    "rerun_refused",
    "chunks_absent",
}

CACHE_ORIGIN_DETAIL_KEYS = {
    "key",
    "stored_at",
    "age_seconds",
    "entries",
    "max_entries",
    "hits",
    "misses",
}

PROVENANCE_KEYS = {"corpus_id", "corpus_hash", "ingest_version", "git_sha", "version", "budget"}

CLAIM_KEYS = {"text", "citations", "supported"}
CITATION_KEYS = {"index", "chunk_id"}

SPAN_KEYS = {
    "traceId",
    "spanId",
    "parentSpanId",
    "name",
    "startTimeUnixNano",
    "endTimeUnixNano",
    "durationMs",
    "attributes",
    "children",
}


@pytest.fixture
def booted(monkeypatch, settings):  # noqa: F811
    def client(retriever=None, generator=None):
        monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
        return TestClient(
            create_app(settings, retriever=retriever or FakeRetriever([candidate()]), generator=generator)
        )

    return client


def test_the_query_response_carries_exactly_the_keys_the_interface_reads(booted):
    with booted(generator=FakeGenerator()) as client:
        body = client.post("/query", json={"question": "torque do cabeçote"}).json()

    assert set(body) == QUERY_RESPONSE_KEYS
    assert set(body["chunks"][0]) == RETRIEVED_CHUNK_KEYS
    assert set(body["answer"]) == GENERATED_ANSWER_KEYS
    assert set(body["answer"]["claims"][0]) == CLAIM_KEYS
    assert set(body["answer"]["claims"][0]["citations"][0]) == CITATION_KEYS
    assert body["origin"] == "live"
    assert set(body["origin_detail"]) == LIVE_ORIGIN_DETAIL_KEYS


def test_the_provenance_endpoint_carries_exactly_what_the_fixture_banner_reads(booted):
    """`banner.js` decides whether to draw the fixture warning off `corpus_id`, by name.

    The whole banner is one string comparison against one field, and the field is on an endpoint the
    interface calls at load on all three screens. A rename here is a site that stops saying its
    corpus is invented, which is the one statement issue #11 required to be impossible to forget.
    """
    with booted() as client:
        body = client.get("/provenance").json()

    assert set(body) == PROVENANCE_KEYS
    assert body["corpus_id"] == "fixture"
    # The budget is published to everyone, not only after it runs out. `banner.js` does not read it
    # today; an operator curling the VM does, and `render.originSentence` reads the same numbers off
    # `origin_detail`.
    assert set(body["budget"]) == {
        "utc_day",
        "generation_budget",
        "generations_used",
        "generations_remaining",
    }


def test_every_span_in_the_tree_carries_exactly_the_keys_the_waterfall_reads(booted):
    with booted(generator=FakeGenerator()) as client:
        trace = client.post("/query", json={"question": "torque"}).json()["trace"]

    seen = []

    def visit(span):
        assert set(span) == SPAN_KEYS
        # Strings, because a nanosecond timestamp does not survive a JavaScript number. The
        # interface positions the waterfall by summing `durationMs` instead, which is the only field
        # that is a number and the only clock that measures duration.
        assert isinstance(span["startTimeUnixNano"], str)
        assert isinstance(span["endTimeUnixNano"], str)
        assert isinstance(span["durationMs"], float)
        seen.append(span["name"])
        for child in span["children"]:
            visit(child)

    visit(trace)
    assert seen == ["query", "retrieve", "generate"]
    # `rerank` is absent rather than zero, which is what lets the panel draw it as "não executado".
    assert "rerank" not in seen


def test_a_page_may_be_null_and_the_wire_says_so_rather_than_omitting_it(booted):
    pageless = candidate()
    # `Candidate` is frozen, and a page of `None` is a legitimate state rather than an edge case:
    # plenty of documents genuinely have none. The interface prints "sem página" off this null and
    # must never print "p. null".
    object.__setattr__(pageless, "page", None)

    with booted(FakeRetriever([pageless])) as client:
        chunk = client.post("/query", json={"question": "q"}).json()["chunks"][0]

    assert "page" in chunk and chunk["page"] is None


def test_the_zero_cost_abstention_names_no_provider(booted):
    with booted(FakeRetriever([]), generator=FakeGenerator()) as client:
        body = client.post("/query", json={"question": "como faço pão"}).json()

    answer = body["answer"]
    # The interface prints "nenhuma chamada" off exactly these two nulls. A provider name here would
    # make it print "fake" for a call that never happened.
    assert (answer["abstained"], answer["provider"], answer["model"]) == (True, None, None)
    assert answer["cost_usd"] is None
    assert body["trace"]["children"] == [] or all(
        child["name"] != "generate" for child in body["trace"]["children"]
    )


def test_an_abstention_still_carries_the_full_key_set(booted):
    with booted(generator=FakeGenerator(abstention())) as client:
        answer = client.post("/query", json={"question": "q"}).json()["answer"]

    # The panel reads `invalid_citations` and `unsupported_claims` in every state, including this
    # one, and prints them even at zero.
    assert set(answer) == GENERATED_ANSWER_KEYS
    assert answer["text"] == "" and answer["claims"] == []


def test_no_generator_means_a_null_answer_rather_than_an_empty_one(booted):
    with booted() as client:
        body = client.post("/query", json={"question": "q"}).json()

    # State one of five. The interface tests `answer === null` first, before `abstained`, and this is
    # the payload that makes that ordering necessary.
    assert body["answer"] is None


def test_component_keys_differ_by_strategy_and_the_panel_must_iterate_them(booted):
    answer_shape = answered()
    with booted(generator=FakeGenerator(answer_shape)) as client:
        body = client.post("/query", json={"question": "q"}).json()

    # Asserted as a dict of key to nullable number, which is the only thing `render.componentTable`
    # assumes. It never names a key, so `hybrid` needs no change there.
    components = body["chunks"][0]["components"]
    assert isinstance(components, dict) and components
    assert all(value is None or isinstance(value, float) for value in components.values())


def test_the_served_strategies_are_published_in_pipeline_order(monkeypatch, settings):  # noqa: F811
    """The endpoint that removed a regular expression from the interface.

    Before it existed, the only place this list appeared was the `msg` of a validation error, so the
    interface provoked a deliberate 422 at load and parsed a human sentence out of it — prose
    parsing plus a red console error on a page that was working correctly.

    The order is the retrievers' own order and never sorted. It decides which arm a comparison opens
    with, so alphabetising it here would be a presentation decision taken in the wrong layer.
    """
    monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
    first, second = FakeRetriever(), FakeRetriever()
    first.name, second.name = "zulu", "alpha"
    second.embedder_id = "baseline@abc123"

    with TestClient(create_app(settings, retrievers=[first, second])) as client:
        body = client.get("/strategies").json()

    assert set(body) == {"strategies", "default"}
    assert body["strategies"] == [
        {"name": "zulu", "embedder": None},
        {"name": "alpha", "embedder": "baseline@abc123"},
    ]
    # An omitted `strategy` resolves to the first one, and the endpoint says so rather than leaving
    # a reader to infer it from the ordering.
    assert body["default"] == "zulu"


def test_an_unknown_strategy_still_carries_the_list_structurally(booted):
    with booted() as client:
        response = client.post("/query", json={"question": "q", "strategy": "hybrid"})

    assert response.status_code == 422
    error = response.json()["detail"][0]
    # The sentence stays, unchanged, because someone is already reading it in a terminal. The
    # structured field is added beside it, in pipeline order.
    assert "this build serves" in error["msg"]
    assert error["ctx"]["strategies"] == ["fake"]


# --- the interface itself ---------------------------------------------------------------------


def test_the_interface_is_served_by_the_same_container(booted):
    with booted() as client:
        index = client.get("/")

    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert client.get("/styles.css").status_code == 200
    assert client.get("/adapt.js").status_code == 200


def test_the_catch_all_mount_does_not_swallow_the_api(booted):
    with booted(generator=FakeGenerator()) as client:
        # The whole reason `app.mount("/")` is the last statement in `create_app`.
        assert client.get("/health").status_code == 200
        assert client.post("/query", json={"question": "q"}).status_code == 200
        assert client.get("/eval/runs").status_code == 200


def test_the_committed_metrics_reach_the_interface_from_the_files(booted):
    with booted() as client:
        baseline = client.get("/eval/baseline").json()
        listing = client.get("/eval/runs").json()

    # Byte-identical to the file, because the screen's whole claim is that no number on it was typed
    # into this repository's JavaScript.
    assert baseline == json.loads(BASELINE_PATH.read_bytes())
    assert listing["run_ids"] == sorted((path.stem for path in RUNS_DIR.glob("*.json")), reverse=True)

    with booted() as client:
        record = client.get(f"/eval/runs/{listing['run_ids'][0]}").json()
    assert record["run_id"] == listing["run_ids"][0]


def test_an_unknown_run_record_is_a_404_and_never_a_path_traversal(booted):
    with booted() as client:
        assert client.get("/eval/runs/nao-existe").status_code == 404
        assert client.get("/eval/runs/..%2F..%2Fbaseline").status_code == 404


def test_no_module_in_the_interface_interpolates_into_innerhtml():
    """The XSS rule, enforced rather than reviewed.

    `chunk.text` comes from the corpus, `answer.text` and `claim.text` from a language model, and
    `exception.message` from whatever the provider sent. All of it is rendered on a page. A single
    `innerHTML =` anywhere in this directory would be enough to execute it, so the ban is a test
    rather than a convention someone has to remember during review.
    """
    # Comments are stripped first, and crudely, because they are where the *reason* for the ban is
    # written down. A test that forbade the word outright would forbid explaining it, and the
    # explanation is the part that survives a refactor.
    sinks = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    offenders = [
        path.name
        for path in sorted(STATIC_DIR.glob("*.js"))
        if any(sink in _without_comments(path.read_text(encoding="utf-8")) for sink in sinks)
    ]
    assert offenders == []


def test_no_module_writes_an_inline_style_attribute():
    """The second mechanical rule in this directory, and it is a deployment fact rather than taste.

    `deploy/Caddyfile` serves `style-src 'self'`, which blocks the `style` **attribute** outright.
    Every bar in the waterfall, every tick on a score axis and every mark on a strip plot is
    positioned by percentage, and when this was first written as `attrs: {style: …}` the entire
    geometry silently failed in production — the page rendered as a column of empty tracks, with
    eighty-five CSP violations in a console the project promises will be quiet, and nothing a visitor
    could see saying anything was wrong.

    `dom.el` now has a `style` channel that goes through `element.style.setProperty`, which is the
    CSSOM path and is not covered by `style-src`, and it takes numbers only. This test is what stops
    the attribute coming back — the failure it prevents is invisible in development, where there is
    no Caddy and therefore no policy.
    """
    offenders = [
        path.name
        for path in sorted(STATIC_DIR.glob("*.js"))
        if 'style: `' in _without_comments(path.read_text(encoding="utf-8"))
        or 'setAttribute("style"' in _without_comments(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def _without_comments(source: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_blocks, flags=re.MULTILINE)
