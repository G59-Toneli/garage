"""The documented example is a capture, and this is the test that keeps it one.

No database, no network, no model. It reads two files out of the repository and compares them, which
means it runs on a bare checkout, in CI, on every push — and that is the whole design. A test that
needed Postgres would be skipped exactly where the drift happens, which is on somebody's laptop while
they improve the wording of a documented score.

Issue #12's second acceptance criterion is "captured from a real response, not written, and a test
fails if it drifts". `capture.refresh` supplies the first half. This file is the second.
"""

from __future__ import annotations

import json

import pytest

from garage.capture import (
    CAPTURE_PATH,
    CAPTURE_VERSION,
    CAPTURED_QUESTIONS,
    CaptureError,
    DOCUMENT_PATH,
    embedded_block,
    rewrite_document,
)


@pytest.fixture
def document() -> str:
    return DOCUMENT_PATH.read_text(encoding="utf-8")


@pytest.fixture
def captured() -> dict:
    return json.loads(CAPTURE_PATH.read_text(encoding="utf-8"))


def test_the_block_in_the_document_is_byte_for_byte_the_captured_artifact(document):
    """The assertion this file exists for, and it is deliberately about *bytes*.

    Not "the same JSON" — comparing parsed objects would pass on a hand-edited block with a score
    retyped to four decimals, a key reordered, or the indentation changed, and every one of those is
    somebody editing the documentation instead of re-running the capture. The canonical form is
    fixed by `evaluation._canonical_bytes`, so equality of bytes is a reachable bar rather than a
    pedantic one.
    """
    assert embedded_block(document) == CAPTURE_PATH.read_text(encoding="utf-8")


def test_a_hand_edited_score_in_the_markdown_fails(document, tmp_path):
    """The failure this is actually defending against, exercised rather than asserted about.

    The original defect was a documented `trigram: 0.73` that no query ever produced. This is that
    edit, made to a copy: change one number in the prose and the comparison must notice.
    """
    tampered = tmp_path / "retrieval.md"
    body = embedded_block(document)
    first_score = json.loads(body)["queries"][0]["chunks"][0]["score"]
    tampered.write_text(document.replace(str(first_score), "0.73", 1), encoding="utf-8")

    assert embedded_block(tampered.read_text(encoding="utf-8")) != CAPTURE_PATH.read_text(
        encoding="utf-8"
    )


def test_deleting_the_markers_is_a_failure_and_not_a_skip(tmp_path):
    """A document with no captured block must fail loudly.

    The cheapest way to make this test green forever is to remove the markers, so removing them has
    to be the loudest thing a person can do to this file.
    """
    stripped = tmp_path / "retrieval.md"
    stripped.write_text("# Retrieval\n\nnothing captured here\n", encoding="utf-8")

    with pytest.raises(CaptureError):
        embedded_block(stripped.read_text(encoding="utf-8"))
    with pytest.raises(CaptureError):
        # And the rewrite refuses too, rather than appending a block somewhere arbitrary.
        rewrite_document(b"{}\n", stripped)


def test_the_capture_holds_both_questions_and_the_second_one_finds_no_manual(captured):
    """The editorial decision, asserted so that it cannot be quietly undone.

    The tempting capture is the English question alone: it puts the spec row at rank one and makes
    the fix look total. The Portuguese one is the residual — `parafuso` and `cabeçote` are in no
    English service manual, and no lexical correction reaches them (ADR-0010 measured a six-cell grid). It
    returns Tier B forum material, which is the honest answer, and dropping it from the capture
    would be the same failure as the block this replaced: choosing the sentence that reads well.

    So: the pair is asserted, *and* the shape of the second result is asserted. A future edit that
    keeps two questions but swaps the second for one that also succeeds has removed the point.
    """
    assert tuple(query["question"] for query in captured["queries"]) == CAPTURED_QUESTIONS
    assert captured["capture_version"] == CAPTURE_VERSION

    english, portuguese = captured["queries"]
    # The criterion from the issue: a question phrased as a sentence retrieves what the keywords do,
    # and the target is a `kind=spec` table row, which is the class the issue reports as unreachable.
    assert english["chunks"][0]["chunk_id"] == "svc-kadett-1993#0001"
    assert english["chunks"][0]["kind"] == "spec"

    # And the limitation, stated as a property rather than as prose: nothing from the English
    # service manual is reachable from this question, at any rank.
    assert portuguese["chunks"], "the Portuguese question should still find the forum material"
    assert {chunk["tier"] for chunk in portuguese["chunks"]} == {"B"}
    assert not any(
        chunk["doc_id"] == "svc-kadett-1993" for chunk in portuguese["chunks"]
    ), "if this passes, #13 has landed and this document's second half needs rewriting"


def test_the_capture_carries_no_word_of_any_source_document(captured):
    """ADR-0003 over the new committed artifact, by the same rule the showcase record follows.

    Field-level rather than n-gram level, because `capture.py` builds its chunks through
    `showcase.ShowcaseChunk` — the class whose whole job is this omission — so the property to check
    here is that it still does, not that the redistribution rule works.
    """
    from garage.showcase import _SOURCE_TEXT_FIELDS

    for query in captured["queries"]:
        for chunk in query["chunks"]:
            assert not set(chunk) & set(_SOURCE_TEXT_FIELDS)
