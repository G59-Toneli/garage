"""The showcase record's wire contract, asserted key by key against the models the interface reads.

The sibling of `tests/test_ui_contract.py`, and it exists for the same reason that one does. The
showcase screen reads `sample.answer.contract_violation`, `arm.retrieval.chunks[].doc_title` and
`span.durationMs` by name from a JSON file, and nothing in Python knows that. Renaming a field on
`app.GeneratedAnswer` would leave the whole suite green and break the demo at runtime, in a browser,
in front of whoever the demo was for.

The central assertion is that the key set of `samples[].answer` is **exactly**
`app.GeneratedAnswer`'s. `showcase.Sample` reuses that class rather than redeclaring it, so this
looks tautological — and is not, for two reasons. It is asserted on the **serialized bytes**, so a
`model_dump(exclude=...)`, a `Field(exclude=True)` or a hand-edited record are all caught. And it is
asserted against the key set `test_ui_contract.py` names, so if a future edit ever does redeclare
the shape here, the two files disagree loudly instead of drifting quietly.

Exact rather than "contains", on the same argument as the live contract: a field added to the record
and not shown is also a regression, because this interface's whole claim is that it displays what
the system produced.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from garage.app import STATIC_DIR, GeneratedAnswer, HydratedChunk
from garage.showcase import (
    SPREAD_METRICS,
    Sample,
    Sampling,
    ShowcaseArm,
    ShowcaseChunk,
    ShowcaseItem,
    ShowcaseRecord,
    Spread,
    write_showcase_record,
)

from test_showcase import CountingGenerator, build, offline  # noqa: F401 — `offline` is a fixture
from test_ui_contract import CITATION_KEYS, CLAIM_KEYS, GENERATED_ANSWER_KEYS, SPAN_KEYS

SHOWCASE_RECORD_KEYS = {
    "showcase_record_version",
    "showcase_id",
    "started_at",
    "duration_ms",
    "layer",
    "scope",
    "provenance",
    "sampling",
    "redistribution",
    "displayed_sample_rule",
    "items",
}

ITEM_KEYS = {"question_id", "question", "why", "arms"}

ARM_KEYS = {
    "strategy",
    "embedder",
    "k",
    "tiers",
    "contract",
    "retrieval",
    "samples",
    "spread",
    "displayed_sample",
}

SAMPLE_KEYS = {"index", "answer", "trace"}

# `RetrievedChunk` minus every field that carries the source document's own words, which is the
# whole point of the class (ADR-0003). `section` is on that list and was not on it at first: it is
# `chunking`'s `heading.group(2)`, so it is the document's heading text, and it leaked twelve-token
# runs of the fixture into a committed record.
SHOWCASE_CHUNK_KEYS = {
    "chunk_id",
    "doc_id",
    "doc_title",
    "tier",
    "page",
    "kind",
    "score",
    "components",
}

SPREAD_KEYS = {"values", "n", "minimum", "maximum", "distinct"}

SAMPLING_KEYS = {"n", "generator", "model", "temperature", "measured_on"}

REDISTRIBUTION_KEYS = {
    "chunk_text_stored",
    "verbatim_token_limit",
    "verbatim_subsequence_limit",
    "worst_verbatim",
    "worst_verbatim_subsequence",
}


@pytest.fixture
def written(offline, tmp_path) -> dict:  # noqa: F811
    """A real record, built and serialized, read back as the interface reads it.

    Through the file rather than through `model_dump`, because the file is what the endpoint hands
    over and what git holds. Everything asserted below is asserted about bytes.
    """
    record = build(CountingGenerator(), n=3)
    return json.loads(write_showcase_record(record, tmp_path).read_text(encoding="utf-8"))


def test_the_answer_in_every_sample_carries_exactly_the_keys_the_interface_reads(written):
    """The acceptance criterion of this file, and the one that would otherwise fail silently.

    `GENERATED_ANSWER_KEYS` is imported from `test_ui_contract.py` rather than restated here. One
    list, checked against both the live wire and the stored record — a second copy would let the two
    surfaces drift apart while both files stayed green.
    """
    for item in written["items"]:
        for arm in item["arms"]:
            for sample in arm["samples"]:
                assert set(sample["answer"]) == GENERATED_ANSWER_KEYS
                assert set(sample["answer"]["claims"][0]) == CLAIM_KEYS
                assert set(sample["answer"]["claims"][0]["citations"][0]) == CITATION_KEYS


def test_the_answer_model_is_the_endpoint_s_own_and_not_a_copy_of_it():
    """Imported, never redeclared — the same rule `provenance` follows.

    The assertion above is about the serialized bytes; this one is about the source, and both are
    needed. Together they say: the shape on disk is the endpoint's shape, and it is that shape
    because it is literally the same class rather than because two declarations happen to agree
    today.
    """
    assert Sample.model_fields["answer"].annotation is GeneratedAnswer

    from garage.evaluation import Provenance

    assert ShowcaseRecord.model_fields["provenance"].annotation is Provenance


def test_every_span_in_every_stored_trace_carries_exactly_the_keys_the_waterfall_reads(written):
    seen = []

    def visit(span):
        assert set(span) == SPAN_KEYS
        # Strings, because a nanosecond timestamp does not survive a JavaScript number. Stored as
        # strings in the record for the same reason they are sent as strings on the wire.
        assert isinstance(span["startTimeUnixNano"], str)
        assert isinstance(span["endTimeUnixNano"], str)
        assert isinstance(span["durationMs"], float)
        seen.append(span["name"])
        for child in span["children"]:
            visit(child)

    sample = written["items"][0]["arms"][0]["samples"][0]
    visit(sample["trace"])
    # The same span names, in the same order, that `app.query` produces. The waterfall reads them by
    # name and draws `rerank` as absent off `KNOWN_STAGES`; a record with differently named stages
    # would need a second renderer.
    assert seen == ["query", "retrieve", "generate"]
    assert "rerank" not in seen


def test_the_record_carries_exactly_the_keys_the_screen_reads(written):
    assert set(written) == SHOWCASE_RECORD_KEYS
    assert set(written["sampling"]) == SAMPLING_KEYS
    assert set(written["redistribution"]) == REDISTRIBUTION_KEYS
    for finding in ("worst_verbatim", "worst_verbatim_subsequence"):
        assert set(written["redistribution"][finding]) == {"tokens", "question_id", "chunk_id"}

    item = written["items"][0]
    assert set(item) == ITEM_KEYS
    arm = item["arms"][0]
    assert set(arm) == ARM_KEYS
    assert set(arm["retrieval"]) == {"chunks"}
    assert set(arm["retrieval"]["chunks"][0]) == SHOWCASE_CHUNK_KEYS
    assert set(arm["samples"][0]) == SAMPLE_KEYS


def test_the_stored_chunk_is_the_retrieved_chunk_minus_the_document_s_own_words(written):
    """Stated as a relation rather than as two independent lists, so it cannot quietly become a
    different set of fields with the same size.

    The subtracted set is `showcase._SOURCE_TEXT_FIELDS`, read from the module rather than restated,
    because that tuple is the answer to "which `Candidate` fields carry the operator's prose" and a
    second copy of it here is exactly how `section` was missed the first time.
    """
    from garage.showcase import _SOURCE_TEXT_FIELDS
    from test_ui_contract import RETRIEVED_CHUNK_KEYS

    assert SHOWCASE_CHUNK_KEYS == RETRIEVED_CHUNK_KEYS - set(_SOURCE_TEXT_FIELDS)
    assert set(ShowcaseChunk.model_fields) == SHOWCASE_CHUNK_KEYS
    stored = set(written["items"][0]["arms"][0]["retrieval"]["chunks"][0])
    assert not stored & set(_SOURCE_TEXT_FIELDS)


def test_the_hydration_endpoint_returns_the_field_the_record_is_missing_and_no_ranking():
    """`GET /chunks` is the other half of the ADR-0003 split: the record has the identity, this has
    the words. It carries no `score` and no `components`, because nothing ranked anything."""
    from test_ui_contract import RETRIEVED_CHUNK_KEYS

    assert set(HydratedChunk.model_fields) == RETRIEVED_CHUNK_KEYS - {"score", "components"}
    # Between them, and with nothing invented, they cover exactly what a live chunk carries.
    assert SHOWCASE_CHUNK_KEYS | set(HydratedChunk.model_fields) == RETRIEVED_CHUNK_KEYS


def test_every_spread_carries_exactly_the_metrics_and_exactly_the_keys_the_strip_plot_reads(written):
    for item in written["items"]:
        for arm in item["arms"]:
            assert set(arm["spread"]) == set(SPREAD_METRICS)
            for spread in arm["spread"].values():
                assert set(spread) == SPREAD_KEYS
                # One value per sample, always. The strip plot draws `values` and nothing else, so a
                # spread of the wrong length is a plot with the wrong number of marks.
                assert len(spread["values"]) == len(arm["samples"]) == spread["n"]


def test_no_scalar_field_for_a_stochastic_metric_exists_anywhere_in_the_record(written):
    """ADR-0004, enforced against the whole document rather than against one model.

    The interface must be *unable* to render a point value for a wobbling number, and the way that
    is guaranteed is that the file does not contain one. This walks every object in the record and
    fails on any key that names an estimate.
    """
    forbidden = {"mean", "average", "avg", "stddev", "std", "sigma", "variance", "median", "p50", "p95"}
    offenders: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in forbidden:
                    offenders.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(written, "$")
    assert offenders == []


def test_the_screen_reads_only_free_endpoints_and_never_posts_a_query():
    """The acceptance criterion, checked against the source of the page that has to satisfy it.

    A test asserting behaviour would pass on a page that calls `/query` only when a button is
    pressed. This asserts the page has no such call to make. Comments are stripped first, exactly as
    `test_ui_contract.py` strips them, so the reason for the rule can be written down beside it.
    """
    source = _without_comments((STATIC_DIR / "showcasescreen.js").read_text(encoding="utf-8"))
    assert "/query" not in source
    assert '"POST"' not in source and "'POST'" not in source
    # Three reads, and each is free: the listing, the record, the hydration.
    assert sorted(set(re.findall(r'fetch\(([^)]*)\)', source))) == ["path"]
    for endpoint in ("/showcase", "/chunks"):
        assert endpoint in source


def test_the_showcase_screen_is_served_and_holds_no_innerhtml():
    """The XSS ban covers the new module too. `test_ui_contract.py` globs `static/*.js` and would
    catch it, and this states it as a property of this screen rather than as a lucky side effect."""
    sinks = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    for name in ("showcasescreen.js", "adapt.js", "render.js"):
        source = _without_comments((STATIC_DIR / name).read_text(encoding="utf-8"))
        assert not any(sink in source for sink in sinks), name
    assert (STATIC_DIR / "showcase.html").is_file()


def _without_comments(source: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_blocks, flags=re.MULTILINE)
