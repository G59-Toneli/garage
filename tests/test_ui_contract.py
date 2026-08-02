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


def _without_comments(source: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_blocks, flags=re.MULTILINE)
