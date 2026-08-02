"""The trace is the product (design §12), so it is tested like one.

What matters is the shape a consumer depends on: a tree that nests, every stage timed, and a stage
that failed still present in it.
"""

import re

import pytest

from garage.tracing import Tracer


def test_the_tree_nests_stages_under_the_query():
    tracer = Tracer()

    with tracer.span("query", question="torque do cabeçote"):
        with tracer.span("retrieve", **{"retrieval.strategy": "lexical"}) as retrieve:
            retrieve.set(**{"retrieval.candidates": 3})

    tree = tracer.tree()

    assert tree["name"] == "query"
    assert tree["attributes"]["question"] == "torque do cabeçote"
    assert [child["name"] for child in tree["children"]] == ["retrieve"]
    assert tree["children"][0]["attributes"] == {
        "retrieval.strategy": "lexical",
        "retrieval.candidates": 3,
    }


def test_identifiers_and_times_are_opentelemetry_shaped():
    tracer = Tracer()

    with tracer.span("query"):
        with tracer.span("retrieve"):
            pass

    tree = tracer.tree()
    child = tree["children"][0]

    assert re.fullmatch(r"[0-9a-f]{32}", tree["traceId"])
    assert re.fullmatch(r"[0-9a-f]{16}", tree["spanId"])
    assert tree["parentSpanId"] is None
    # The child names its parent, which is what flattening this tree into OTLP relies on.
    assert child["parentSpanId"] == tree["spanId"]
    assert child["traceId"] == tree["traceId"]
    assert int(tree["endTimeUnixNano"]) >= int(tree["startTimeUnixNano"])


def test_every_span_is_timed():
    tracer = Tracer()

    with tracer.span("query"):
        with tracer.span("retrieve"):
            pass

    tree = tracer.tree()

    assert tree["durationMs"] >= 0
    assert tree["children"][0]["durationMs"] >= 0
    # The parent contains the child, so it cannot have taken less time.
    assert tree["durationMs"] >= tree["children"][0]["durationMs"]


def test_a_stage_that_raised_is_still_in_the_trace():
    tracer = Tracer()

    with pytest.raises(RuntimeError):
        with tracer.span("query"):
            with tracer.span("retrieve"):
                raise RuntimeError("database went away")

    failed = tracer.tree()["children"][0]

    assert failed["attributes"]["error"] is True
    assert failed["attributes"]["exception.type"] == "RuntimeError"
    assert failed["durationMs"] is not None


def test_a_tracer_that_traced_nothing_has_no_tree():
    assert Tracer().tree() is None
