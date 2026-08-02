"""Captured retrieval examples: the documentation's numbers, produced rather than written.

`docs/retrieval.md` opened with a response showing `svc-kadett-1993#0001` at
`components.trigram: 0.73`. Against the real fixture artifact that query returned **nothing at all**,
and the real `word_similarity` was 0.25. Nobody lied — the block was written by hand while the shape
of the response was being designed, and then the system moved and the block did not. That is the
default fate of every hand-written example in every repository, and this module exists so that this
project's documentation cannot have that fate quietly (issue #12, acceptance criterion two).

The mechanism is the one `eval/showcase/` already uses, because a second mechanism for "a committed
artifact the documentation cites" would be a second thing to keep honest:

1. `python -m garage docs capture` runs the **real** retriever against the real artifact and writes
   `docs/examples/retrieval-lexical.json` in the same canonical bytes a run record uses.
2. The same command rewrites the fenced block in `docs/retrieval.md` between two HTML comment
   markers, so the prose and the artifact cannot be updated separately.
3. `tests/test_capture.py` extracts that block from the Markdown and compares it byte for byte with
   the JSON file. No database, no network, no model — it runs on a bare checkout in CI.

Step three is what catches the failure that actually happens, which is not "the retriever changed
and nobody re-ran the command" — the run record and `INGEST_VERSION` already catch that. It is
somebody improving the wording of a documented score by editing the Markdown.

## Two questions, and the second one is the interesting one

`CAPTURED_QUESTIONS` holds a pair, deliberately. The obvious capture is the English question, which
finds the spec row at rank one and makes the fix look total. The Portuguese one does not find it —
under any lexical correction, including every variant measured for ADR-0010 — because `parafuso` and
`cabeçote` appear nowhere in an English service manual and no tsquery parser translates. It returns
Tier B forum material instead, which is the honest answer to that question from this corpus.

Capturing only the first would be the same failure as the block it replaces: choosing the sentence
that reads well. Capturing both, side by side, turns the limitation into the demonstration — which
is what this project is for, and it is the argument #13 is written from.

## What is *not* captured

No `text` and no `section`, exactly as `showcase.ShowcaseChunk` omits them and for exactly the same
reason (ADR-0003): a committed file must carry identifiers and measurements, never the source
document's own words. The class is reused rather than re-derived, so the redistribution rule has one
implementation. `GET /chunks` is where the words come from at render time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from garage.evaluation import _canonical_bytes
from garage.retrieval import TIERS, Filters, LexicalRetriever

# Bumped if the shape below changes, so a stale captured file is recognisable as stale rather than
# merely different. The same role `showcase_record_version` plays, at a much smaller scale.
CAPTURE_VERSION = 1

_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_PATH = _ROOT / "docs" / "examples" / "retrieval-lexical.json"
DOCUMENT_PATH = _ROOT / "docs" / "retrieval.md"

# Five rather than `DEFAULT_K`'s ten: the block is read in a browser by someone learning the response
# shape, and ten near-identical chunks teach nothing the first three do not. It is written into the
# file, so the capture says what it measured.
CAPTURE_K = 5

# The pair, and the order matters — the English one first because it establishes that the fix works,
# the Portuguese one second because it is the residual.
CAPTURED_QUESTIONS = (
    "What torque should the cylinder head bolts be tightened to in stage 1?",
    "torque do parafuso do cabeçote",
)

BEGIN_MARKER = "<!-- captured: docs/examples/retrieval-lexical.json -->"
END_MARKER = "<!-- end captured -->"

# `json` and not `jsonc`, because after this change the block is a *file*, and a comment in it would
# make the two copies differ and the test fail. The commentary moved into the prose around it, which
# is where commentary that nothing verifies belongs.
_FENCE = "```json"


class CaptureError(RuntimeError):
    """A capture could not be produced or the document could not be rewritten."""


def capture(database_url: str) -> dict[str, Any]:
    """Run the real retriever against the real artifact and return the record as a plain dict.

    A dict rather than a pydantic model, and the absence is not laziness. Every field here is either
    read straight off a `Candidate` or written from a constant in this module; there is no parsing
    to validate and no second producer to keep in agreement, so a model would only be a second place
    to declare a shape that `_canonical_bytes` already fixes. The showcase record is a model because
    a *browser* reads it by field name; nothing reads this but a diff.
    """
    from garage.showcase import ShowcaseChunk

    retriever = LexicalRetriever(database_url)
    queries = []
    for question in CAPTURED_QUESTIONS:
        candidates = retriever.retrieve(question, k=CAPTURE_K, filters=Filters(tiers=TIERS))
        queries.append(
            {
                "question": question,
                # Written down even when it is zero, because "this question retrieves nothing" is a
                # result this retriever is allowed to have and the documentation should be able to
                # show it. An absent key would read as a capture that failed.
                "chunk_count": len(candidates),
                "chunks": [
                    ShowcaseChunk.of(candidate).model_dump(mode="json")
                    for candidate in candidates
                ],
            }
        )
    return {
        "capture_version": CAPTURE_VERSION,
        "strategy": LexicalRetriever.name,
        "k": CAPTURE_K,
        "tiers": list(TIERS),
        "queries": queries,
    }


def write_capture(payload: dict[str, Any], path: Path | None = None) -> Path:
    """The same canonical bytes as a run record, for the same reasons (`evaluation._canonical_bytes`)."""
    path = Path(path) if path is not None else CAPTURE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(payload))
    return path


def embedded_block(document: str) -> str:
    """The captured JSON as the Markdown currently holds it, marker to marker.

    Raises rather than returning None on a malformed document: a missing marker means the mechanism
    has been dismantled, and the test that calls this must fail loudly rather than skip.
    """
    match = re.search(
        re.escape(BEGIN_MARKER) + r"\s*\n" + re.escape(_FENCE) + r"\n(.*?)```\s*\n" + re.escape(END_MARKER),
        document,
        flags=re.DOTALL,
    )
    if match is None:
        raise CaptureError(
            f"{DOCUMENT_PATH.name} has no captured block between {BEGIN_MARKER} and {END_MARKER}. "
            "Run `python -m garage docs capture` — and if the markers were deleted on purpose, "
            "issue #12 is being reopened."
        )
    return match.group(1)


def rewrite_document(payload_bytes: bytes, path: Path | None = None) -> Path:
    """Replace the block between the markers with the captured bytes. Touches nothing else."""
    path = Path(path) if path is not None else DOCUMENT_PATH
    document = path.read_text(encoding="utf-8")
    # Parsed before writing, so a document missing its markers fails without being modified.
    embedded_block(document)
    body = payload_bytes.decode("utf-8")
    replacement = f"{BEGIN_MARKER}\n{_FENCE}\n{body}```\n{END_MARKER}"
    updated = re.sub(
        re.escape(BEGIN_MARKER) + r"\s*\n" + re.escape(_FENCE) + r"\n.*?```\s*\n" + re.escape(END_MARKER),
        # A function replacement, because `re.sub` interprets backslashes and `\1` in a string one —
        # and the payload is JSON, which is full of both the day a chunk_id contains one.
        lambda _: replacement,
        document,
        flags=re.DOTALL,
    )
    # `newline=""` so the bytes go out exactly as assembled. The default would translate every `\n`
    # to the platform's line ending, which on Windows rewrites the whole document to CRLF as a side
    # effect of updating one block — and turns a four-line change into a diff of the entire file.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return path


def refresh(database_url: str) -> tuple[Path, Path]:
    """Capture, write the artifact, rewrite the document. One command, so the two cannot drift."""
    payload = capture(database_url)
    written = write_capture(payload)
    document = rewrite_document(written.read_bytes())
    return written, document


__all__ = [
    "CAPTURED_QUESTIONS",
    "CAPTURE_PATH",
    "CAPTURE_VERSION",
    "CaptureError",
    "DOCUMENT_PATH",
    "capture",
    "embedded_block",
    "refresh",
    "rewrite_document",
    "write_capture",
]
