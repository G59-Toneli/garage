"""The precomputed showcase: curated questions rendered with no model call, and no leaked text.

This is the *second* record format in the repository and it is deliberately not the first one grown
a few fields. `evaluation.RunRecord` is deterministic, offline, needs no API key, and is what
`eval gate` re-measures in CI on every push. A showcase is stochastic, costs money on every line of
it, and cannot be produced without a provider. ADR-0007 already says that numbers which fail for
different reasons are different numbers; fusing the two would put the CI gate one dependency away
from needing `GEMINI_API_KEY`, against `pyproject.toml`, where `gemini` is an optional extra and
`addopts = "-m 'not live'"` keeps the default suite off the network. So: two files, two commands,
two lifecycles, and `eval gate` never learns this module exists.

What it is *for* is the one thing the Run Record structurally cannot serve — a curated question
that renders its answer, its citations, its chunks, its trace and its cost with **zero** model
calls. `ItemResult` holds no answer, no claims, no trace and no cost, and by ADR-0003 no chunk text
either. This record adds the first four. It adds none of the fifth.

## ADR-0003, resolved rather than deferred

**No chunk text is ever written into this record.** The acceptance criterion says "no *model*
call"; it does not say "no database". Chunk text already lives in `chunks.text`, in the derived
artifact, which ADR-0002 makes the one place third-party material legitimately sits and which
ADR-0003 keeps out of git. So `chunk_id` is what travels, `GET /chunks?ids=...` is what hydrates —
local, free, deterministic, no model — and a clone without the operator's material renders the
metrics, the answer and the trace with the chunks shown as **absent and identified**. That is
already this interface's vocabulary: an absence travels as an absence (`docs/ui.md`).

"No text" is narrower than the rule, and the difference was a real leak rather than a hypothetical.
The rule is **no source prose of any kind**. `section` is set by `chunking` from the source
document's own heading, and the first version of this module wrote it to disk: sixteen fields of the
committed record carried twelve-token runs of the fixture through it. `_SOURCE_TEXT_FIELDS` now
names both fields in one place. `doc_title` is the one string that stays, because it is the
manifest's own catalogue entry — already in git, by hand, as ADR-0002 says a Corpus *is*.

Today the whole fixture Corpus is `rights: original-work`, so storing any of it here would be legal
now and illegal the day a real manual is catalogued. A format that is correct only until the project
gets serious breaks exactly when it matters.

The check that holds this line is deliberately **not** the model's field list, because the field
list is what got `section` wrong. It is
`test_no_ngram_of_any_source_document_reaches_a_committed_record`: it reads the bytes on disk,
tokenises every string in them, and rejects any seven-token run of any source document that is not
already in the manifest. It knows nothing about which fields exist, which is why it caught what the
enumeration missed. The test it replaced matched whole markdown *lines*, so it never fired — the
record stores headings without their `## ` prefix, and `line in written` was false every time. Seven
is a swept threshold rather than a guessed one, and the sweep is in the test beside it.

## The second leak, which is real

The generated prose is the other way source material could reach git. A model answering over a
scanned manual can emit a sentence verbatim, and that sentence would be committed. So the build
measures every claim against every cited Tier A chunk, **twice**, because either measure alone is
escapable:

- **Longest contiguous run** (`VERBATIM_TOKEN_LIMIT`) — the sharper evidence. Twenty-six identical
  tokens in a row with nothing between them is a quotation in any language.
- **Longest common subsequence** (`VERBATIM_SUBSEQUENCE_LIMIT`), in order, gaps allowed. This exists
  because the first is evadable with one edit. A whole Tier A paragraph copied in order with a
  linking word dropped in every twenty tokens — "ou seja", "segundo o manual" — scores a contiguous
  run of 20, passes a limit of 25, and redistributes the paragraph word for word. That is not an
  attack, it is ordinary LLM behaviour over a manual. Against a 44-token paragraph the run says 20
  and the subsequence says 44.

Over either limit the build *fails and names the question*, and says which measure fired: a long run
is a quotation, a long subsequence with a short run is a paraphrase-shaped copy, and the two have
different fixes.

It does not truncate and it does not redact. Silent redaction would be the interface lying about
what the model produced, which is the one thing this whole project is built not to do — and it
would hide the exact signal an operator needs, which is "this configuration copies". A human either
rewrites the question, drops it, or raises a threshold deliberately. Both thresholds and both worst
observed values go into the record under `redistribution`, so the decision is auditable by whoever
reads the file rather than by whoever ran the command.

The issue asked for the subsequence measure; the first version of this module substituted the
contiguous one and argued that a scattered subsequence is only two Portuguese sentences sharing
articles. Measured, that argument does not survive. Over unrelated Portuguese paragraph pairs in
this corpus the longest common subsequence is **9** tokens, and correct original cited answers score
**14–21** — so a subsequence limit of 25 sits sixteen tokens above the worst false positive. Both
measures are kept, because the contiguous one is still the better evidence of literal copying and
costs nothing to keep.

## Why the shape is what it is

`provenance` is `evaluation.Provenance`, imported and never redeclared, so it cannot drift from the
Run Record's idea of which build produced a number. `samples[].answer` is `app.GeneratedAnswer`, for
the same reason and one more: the interface reads those keys by name, so a redeclaration here would
let a renamed field leave the suite green and break the demo in a browser. That import points at the
HTTP layer from a record module, which is the wrong direction for a dependency arrow and is chosen
anyway — the alternative is a second description of the same object, and a second description is a
place to drift. `tests/test_showcase_contract.py` asserts it on the serialized bytes regardless,
because "we imported the right class" is a claim about today's source, not about the file on disk.

`sampling` sits at the top, once, exactly where `RunRecord` puts `sample_count`: every sample in the
file was drawn the same way, or the file is comparing different things and no reader can tell.

Per question, per arm, two different kinds of measurement are kept apart:

- `retrieval` is **one** measurement with no spread, and that is honest rather than lazy: the same
  query against the same artifact returns the same order, every time, and the build asserts it
  across all n samples instead of assuming it.
- `samples[]` is n draws, and every stochastic quantity is stored as its raw values. **There is no
  scalar field for a stochastic metric anywhere in this format**, which is ADR-0004 made structural:
  the interface cannot render a point estimate for a wobbling number because the file does not
  contain one. `Spread` carries `values`, `minimum`, `maximum` and `distinct` — order statistics and
  a count, all four of them things that were observed — and deliberately no mean and no standard
  deviation. At n=10 a sigma is a number invented from too little evidence, drawn with the same
  authority as one that was measured.

`displayed_sample` is chosen by a rule that is written into the record: the median of `tokens_out`,
ties broken to the lowest index. Not "the best one" — picking the nicest of ten answers to put on a
demo is cherry-picking dressed as curation, and the whole point of storing all ten is that a reader
can check the choice.

`showcase_id` is `<timestamp>-<git_sha[:12]>`, the same format as `run_id` and produced by the same
helpers, so the two directories sort chronologically under the same rules. Serving refuses a record
whose `corpus_hash` does not match the artifact, exactly as it refuses a database that is not this
commit's (ADR-0002): a precomputed answer over material the server does not hold is a citation
nobody can check.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Imported, never restated. See the module docstring: a second description of either of these is a
# place for the format to drift, and the drift would be invisible until a browser rendered it.
from garage.app import GeneratedAnswer
from garage.evaluation import (
    EVAL_DIR,
    Provenance,
    _compact,
    _canonical_bytes,
    _iso,
    _now,
    _one_line,
    local_provenance,
)
from garage.generation import Contract, Generator
from garage.jargon import _fold as fold
from garage.retrieval import DEFAULT_K, TIERS, Candidate, Filters, Retriever
from garage.tracing import Tracer

SHOWCASE_DIR = EVAL_DIR / "showcase"
QUESTIONS_PATH = SHOWCASE_DIR / "questions.jsonl"

SHOWCASE_RECORD_VERSION = 1

# How many draws per question per arm. Ten is the number the issue argues for and the number the
# strip plot is drawn against; it is a default rather than a constant because every call of it costs
# money, and the sample record committed to this repository was built with three.
DEFAULT_SAMPLE_COUNT = 10

# The free tier is roughly 10 requests per minute and 250 per day, and a 429 is its ordinary state
# rather than an exception to it (`generation.GeminiGenerator`). Six seconds between calls keeps a
# long build under the per-minute limit; without it the build eats its own rate limit and writes
# degradations into the record as though they were results, which is the single most misleading
# thing this file could contain.
THROTTLE_SECONDS = 6.0

# The verbatim gate's two thresholds, in tokens. Both travel into the record so a reader can
# disagree with them, which is the only thing that makes a policy number auditable.
#
# **Contiguous.** Twenty-five tokens in a row, no gaps. Roughly a long sentence.
VERBATIM_TOKEN_LIMIT = 25

# **Subsequence.** In order, gaps allowed, and this is the one with evidence behind it rather than
# an eyeball. Measured over this corpus: the longest common subsequence between any two *unrelated*
# Portuguese paragraphs is **9** tokens, and a correct original cited answer scores **14–21**. So a
# limit of 25 sits sixteen tokens above the worst false positive and four above the most overlapping
# legitimate answer observed.
#
# Four tokens is a thin margin and is written down rather than smoothed over. It is also the margin
# on *this* corpus, whose paragraphs are short invented tables; the number must be re-measured
# before the first real manual is catalogued, and a longer generated answer mechanically scores
# higher against the same chunk because the measure is absolute rather than normalised by length.
# The failure mode of getting it wrong is a build that stops and names a question, which is
# recoverable, so it is deliberately set on the strict side of comfortable.
VERBATIM_SUBSEQUENCE_LIMIT = 25

# Written into the record rather than only into this source, because the choice of which of n
# answers a visitor sees is exactly the kind of decision a demo can be accused of making
# self-servingly. The rule is stated in the file the demo reads.
DISPLAY_RULE = (
    "median of tokens_out across the samples, ties broken to the lowest sample index; "
    "for an even n, the lower median"
)

# Which per-sample quantities get a `Spread`. Every one of them wobbles between draws of the same
# question against the same chunks, which is precisely why none of them may be stored as a scalar.
SPREAD_METRICS = (
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "claims",
    "citations",
    "invalid_citations",
    "unsupported_claims",
    "generate_ms",
)


class ShowcaseError(Exception):
    """The build refuses. Never a warning, and never a partially written record."""


class VerbatimLeak(ShowcaseError):
    """A generated claim copied enough of a Tier A chunk that committing it would redistribute it.

    Its own class rather than a message, because the caller's reaction is different in kind: a
    provider failure is retried, a bad question is rewritten or dropped, and this one is a decision
    about what may enter version control (ADR-0003).
    """


# ------------------------------------------------------------------------------------------------
# The curated questions.
# ------------------------------------------------------------------------------------------------


class ShowcaseQuestion(BaseModel):
    """One curated question, and — required — why it is worth a visitor's attention.

    `why` is not documentation. A showcase is a set of questions someone chose, so the reason for
    each choice is the difference between a benchmark and a highlight reel, and a format that makes
    the reason optional is a format where it will be absent. It is displayed beside the question.

    Deliberately *not* an `eval/facts.jsonl` fact. A fact names an expected chunk and an expected
    value so a deterministic gate can score it; a showcase question names neither, because there is
    nothing here to score — the artifact is the answer itself, its citations and its cost. Reusing
    `Fact` would have meant either inventing expected values for questions that have none, or making
    two required fields optional on the model the gate depends on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=1000)
    why: str = Field(min_length=1)


def load_questions(path: Path | None = None) -> tuple[ShowcaseQuestion, ...]:
    """Read the curated set, reporting every bad line rather than the first.

    Same discipline as `evaluation.load_facts`: someone repairing a file wants the whole list.
    """
    path = Path(path) if path is not None else QUESTIONS_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as missing:
        raise ShowcaseError(f"no curated question set at {path}") from missing

    questions: list[ShowcaseQuestion] = []
    problems: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            questions.append(ShowcaseQuestion.model_validate_json(line))
        except ValidationError as invalid:
            problems.append(f"  line {number}: {_one_line(invalid)}")
        except json.JSONDecodeError as invalid:
            problems.append(f"  line {number}: not JSON ({invalid.msg})")

    identifiers = [question.question_id for question in questions]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        problems.append(f"  duplicate question_id: {', '.join(duplicates)}")
    if problems:
        raise ShowcaseError(f"{path} is not a valid question set:\n" + "\n".join(problems))
    if not questions:
        raise ShowcaseError(f"{path} holds no questions")
    return tuple(questions)


# ------------------------------------------------------------------------------------------------
# The verbatim gate.
# ------------------------------------------------------------------------------------------------


def tokens(text: str) -> tuple[str, ...]:
    """Words, accent-folded and lowercased, punctuation dropped.

    Folded through `jargon._fold`, the same function ingestion detects terms with and the same one
    the deterministic gate matches values with. Copying is copying whether or not the model kept the
    cedilla, and three definitions of "the same word" in one repository would be two too many.
    """
    return tuple(part for part in fold(text).split() if part)


def longest_common_run(left: Sequence[str], right: Sequence[str]) -> int:
    """The longest run of tokens appearing contiguously in both sequences.

    Ordinary dynamic programming, O(len(left) x len(right)), over a claim of tens of tokens against
    a chunk of hundreds — microseconds, and worth exactly nothing more. One row of the table is kept
    rather than the whole matrix, which is not a performance decision but a size one: the full table
    for the largest pair here is a few hundred thousand cells that nobody reads.

    Contiguous. It is the sharper signal of the two — a run of twenty-six identical tokens in order
    with nothing between them is not a coincidence in any language — and it is *not* sufficient on
    its own. `longest_common_subsequence` below is the other half, and the module docstring explains
    why the pair is needed.
    """
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for token in left:
        current = [0] * (len(right) + 1)
        for column, other in enumerate(right, start=1):
            if token == other:
                current[column] = previous[column - 1] + 1
                if current[column] > best:
                    best = current[column]
        previous = current
    return best


def longest_common_subsequence(left: Sequence[str], right: Sequence[str]) -> int:
    """The longest sequence of tokens appearing in both, in order, gaps allowed.

    The second half of the verbatim gate, and it exists because the first half is evadable with one
    edit. A whole Tier A paragraph copied in order with a linking word dropped in every twenty
    tokens — "ou seja", "segundo o manual", which is *ordinary* LLM behaviour over a manual, not an
    attack — scores a contiguous run of twenty and sails through a limit of twenty-five while
    redistributing the paragraph word for word. This measure scores that at the full length of the
    paragraph.

    Same one-row dynamic programming as above, same negligible cost at these sizes.
    """
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for token in left:
        current = [0] * (len(right) + 1)
        for column, other in enumerate(right, start=1):
            current[column] = (
                previous[column - 1] + 1 if token == other else max(previous[column], current[column - 1])
            )
        previous = current
    return previous[-1]


class VerbatimFinding(BaseModel):
    """The worst overlap the gate saw, whether or not it fired.

    Recorded even when nothing was wrong, because "the gate ran and found 6" and "the gate never
    ran" are different facts and a record that reported only failures could not tell them apart.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens: int = Field(ge=0)
    # Null when nothing was compared at all — every answer abstained, or no claim cited a Tier A
    # chunk. Not zero-with-an-id, which would name a question the gate never looked at.
    question_id: str | None = None
    chunk_id: str | None = None


@dataclass(frozen=True)
class VerbatimReading:
    """Both measures over one answer, taken together because neither is sufficient alone.

    A plain dataclass and not a Pydantic model: it never reaches disk. What reaches disk is the two
    `VerbatimFinding`s inside `Redistribution`.
    """

    run: VerbatimFinding
    subsequence: VerbatimFinding

    def merge(self, other: VerbatimReading) -> VerbatimReading:
        """The worse of two readings, measure by measure.

        Per measure, not per answer. The answer with the longest contiguous run is frequently not
        the one with the longest subsequence, and reporting one answer's pair would throw away the
        other's worse half — which is the half a reader calibrating a threshold needs.
        """
        return VerbatimReading(
            run=max(self.run, other.run, key=lambda finding: finding.tokens),
            subsequence=max(
                self.subsequence, other.subsequence, key=lambda finding: finding.tokens
            ),
        )


def worst_verbatim(
    answer: GeneratedAnswer, chunks: Sequence[Candidate], *, question_id: str
) -> VerbatimReading:
    """Both measures over every claim against every Tier A chunk it cited.

    Tier A only, and cited only. Tier A is the operator's licensed material — a service manual, a
    scanned repair guide — and it is the material ADR-0003 forbids redistributing; Tier B is a forum
    post whose copying is a different problem with a different answer, and folding the two together
    would make this gate fire on the wrong thing. "Cited" narrows it further because a chunk the
    claim did not cite is not a chunk the claim copied: the model saw all k in its prompt, so
    comparing against all of them would flag a coincidence as a leak.
    """
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    worst = VerbatimReading(run=VerbatimFinding(tokens=0), subsequence=VerbatimFinding(tokens=0))
    for claim in answer.claims:
        claim_tokens = tokens(claim.text)
        for citation in claim.citations:
            chunk = by_id.get(citation.chunk_id)
            if chunk is None or chunk.tier != "A":
                continue
            chunk_tokens = tokens(chunk.text)
            worst = worst.merge(
                VerbatimReading(
                    run=_finding(
                        longest_common_run(claim_tokens, chunk_tokens), question_id, chunk.chunk_id
                    ),
                    subsequence=_finding(
                        longest_common_subsequence(claim_tokens, chunk_tokens),
                        question_id,
                        chunk.chunk_id,
                    ),
                )
            )
    return worst


def _finding(count: int, question_id: str, chunk_id: str) -> VerbatimFinding:
    # A zero-token overlap names nothing. Two claims can be compared against two chunks and share
    # not one word, and pointing at whichever pair happened to be last would read as though that
    # pair were the worst offender rather than a non-event.
    if count == 0:
        return VerbatimFinding(tokens=0)
    return VerbatimFinding(tokens=count, question_id=question_id, chunk_id=chunk_id)


# ------------------------------------------------------------------------------------------------
# Spread.
# ------------------------------------------------------------------------------------------------


class Spread(BaseModel):
    """What n draws of one stochastic quantity actually were.

    The raw `values` are kept, in sample order, and they are the field the screen draws: a strip
    plot with n marks, no error bar, no mean, no standard deviation. That absence is the design.
    Ten draws do not support a sigma, and a whisker drawn from them would carry exactly the
    authority of a measured number while being an invented one (ADR-0004).

    `minimum`, `maximum` and `distinct` are here because all three were *observed* rather than
    estimated — two order statistics and a count. `distinct` is the one worth having on a first
    read: at temperature 0 a well-behaved configuration frequently returns 1, and "this number did
    not move at all across ten calls" is the most useful single thing a reader can learn about it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Nulls are carried through rather than dropped. `cost_usd` is null for a model with no
    # published price and for a call that never happened, and a spread that silently shortened
    # itself would report n=7 for ten samples with no way to see it.
    values: tuple[float | None, ...] = Field(min_length=1)
    n: int = Field(ge=1)
    minimum: float | None
    maximum: float | None
    distinct: int = Field(ge=0)

    @model_validator(mode="after")
    def _n_is_the_number_of_values(self) -> Spread:
        if self.n != len(self.values):
            raise ValueError(f"n is {self.n} but {len(self.values)} values were kept")
        return self


def spread_of(values: Sequence[float | None]) -> Spread:
    """Derive a `Spread`, and nothing that was not in the numbers."""
    present = [value for value in values if value is not None]
    return Spread(
        values=tuple(values),
        n=len(values),
        minimum=min(present) if present else None,
        maximum=max(present) if present else None,
        distinct=len(set(present)),
    )


class Sample(BaseModel):
    """One draw: what the model said, and the trace of the call that said it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    # The wire model itself. See the module docstring: imported so it cannot drift, and asserted on
    # the serialized bytes anyway so that "we imported it" is not the only evidence.
    answer: GeneratedAnswer
    # `tracer.tree()`, verbatim. Null is a legitimate value everywhere else in this codebase and is
    # not one here: every sample is a call that was attempted, so every sample has a root span.
    trace: dict[str, Any]


def sample_metrics(sample: Sample) -> dict[str, float | None]:
    """The stochastic quantities of one draw, named the same way for every draw.

    `generate_ms` is read off the trace rather than measured a second time here, so the number in
    the spread and the bar in the waterfall are the same measurement. Null when the model was never
    called — the zero-cost abstention has no `generate` span at all, and a zero there would claim an
    instantaneous call rather than an absent one.
    """
    answer = sample.answer
    return {
        "tokens_in": float(answer.tokens_in),
        "tokens_out": float(answer.tokens_out),
        "cost_usd": answer.cost_usd,
        "claims": float(len(answer.claims)),
        "citations": float(sum(len(claim.citations) for claim in answer.claims)),
        "invalid_citations": float(answer.invalid_citations),
        "unsupported_claims": float(answer.unsupported_claims),
        "generate_ms": _span_duration_ms(sample.trace, "generate"),
    }


def spreads_from(samples: Sequence[Sample]) -> dict[str, Spread]:
    """Every `Spread` in an arm, derived from its samples and from nothing else.

    Public and pure so `tests/test_showcase.py` can recompute it from a committed record and compare
    — the stored spread is a convenience for the screen, and a convenience that is allowed to
    disagree with the data it summarises is a defect waiting for a reader to trust it.
    """
    measured = [sample_metrics(sample) for sample in samples]
    return {name: spread_of([row[name] for row in measured]) for name in SPREAD_METRICS}


def choose_displayed_sample(samples: Sequence[Sample]) -> int:
    """Which of the n a visitor sees, by `DISPLAY_RULE` and by nothing else.

    Never the longest, never the one with the most citations, never the one that reads best. Those
    would all be defensible-sounding ways of putting the flattering answer on the screen, and the
    reason all ten are stored is so that nobody has to take this choice on trust.
    """
    if not samples:
        raise ShowcaseError("an arm with no samples has nothing to display")
    ordered = sorted(range(len(samples)), key=lambda index: (samples[index].answer.tokens_out, index))
    return ordered[(len(ordered) - 1) // 2]


def _span_duration_ms(trace: dict[str, Any] | None, name: str) -> float | None:
    if not trace:
        return None
    if trace.get("name") == name:
        return trace.get("durationMs")
    for child in trace.get("children") or ():
        found = _span_duration_ms(child, name)
        if found is not None:
            return found
    return None


# ------------------------------------------------------------------------------------------------
# The record.
# ------------------------------------------------------------------------------------------------


# Every `Candidate` field whose value is a run of words lifted out of a source document. Both of
# them are dropped on the way into a record, and the list is named rather than inlined into
# `ShowcaseChunk.of` so that "which fields carry the operator's prose" is a question with one answer
# in this codebase.
#
# `section` is on this list and its absence from the first version of this module was a real leak,
# caught by an n-gram grep over the committed record: `chunking` sets it from `heading.group(2)`, so
# it *is* the source document's own heading text. Sixteen fields of the committed record carried
# twelve-token runs of the fixture through it. Harmless there — the fixture is `rights:
# original-work` and the leaked headings are short Tier B thread titles — and not harmless at all on
# the day a scanned manual is catalogued, because `Section 3.2 — Cylinder head, tightening
# specifications` is fifty-four characters of a publisher's table of contents going into git without
# passing any gate. The verbatim gate below cannot catch it: that one reads `claim.text` against
# cited Tier A chunks, and a `section` is neither.
#
# `doc_title` deliberately stays. It is the manifest's own `title` field, which is in git already,
# by hand, as the catalogue entry ADR-0002 says a Corpus *is*. Committing it a second time
# redistributes nothing.
#
# Both dropped fields are recovered at render time from `GET /chunks`, exactly as `text` is, so
# nothing is lost on screen and nothing is stored.
_SOURCE_TEXT_FIELDS = ("text", "section")


class ShowcaseChunk(BaseModel):
    """One retrieved chunk as the record stores it: everything except the operator's words.

    `retrieval.Candidate` minus `_SOURCE_TEXT_FIELDS`, and the omissions are the point of this class
    (ADR-0003). Scores and components stay, because they are measurements this project produced
    *about* the material rather than the material itself; identifiers, tier, page and kind stay for
    the same reason.

    `extra="forbid"` is what stops a *new* text-bearing field on `Candidate` from reaching disk —
    and it is worth being precise about what that buys, because an earlier version of this comment
    was not. It catches fields nobody has enumerated yet. It does **not** catch a field that already
    existed and was enumerated wrongly, which is exactly how `section` got through. The check that
    catches that class is `test_no_ngram_of_any_source_document_reaches_a_committed_record`, which
    reads the bytes on disk and knows nothing about this model's field list.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    doc_id: str
    doc_title: str
    tier: str
    page: int | None
    kind: str
    score: float
    components: dict[str, float | None]

    @classmethod
    def of(cls, candidate: Candidate) -> ShowcaseChunk:
        fields = dict(vars(candidate))
        for name in _SOURCE_TEXT_FIELDS:
            fields.pop(name, None)
        return cls(**fields)


class ShowcaseRetrieval(BaseModel):
    """The deterministic half of an arm: one ranking, measured once.

    No spread, and the absence is honest rather than an omission. The same question against the same
    artifact under the same strategy returns the same chunks in the same order — the SQL makes the
    order total (`ORDER BY score DESC, chunk_id`) — and the build asserts that across all n samples
    instead of taking it on faith. A spread here would be a distribution over a constant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunks: tuple[ShowcaseChunk, ...]


class ShowcaseArm(BaseModel):
    """One question answered n times under one Configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: str = Field(min_length=1)
    # Null under `lexical`, written down anyway. Same argument as `evaluation.Configuration`: a
    # field that appears only when set would make two arms standing on different vectors look equal.
    embedder: str | None
    k: int = Field(ge=1)
    tiers: tuple[str, ...] = Field(min_length=1)
    contract: str
    retrieval: ShowcaseRetrieval
    samples: tuple[Sample, ...] = Field(min_length=1)
    spread: dict[str, Spread]
    displayed_sample: int = Field(ge=0)

    @model_validator(mode="after")
    def _the_displayed_sample_exists_and_the_spread_matches(self) -> ShowcaseArm:
        if self.displayed_sample >= len(self.samples):
            raise ValueError(
                f"displayed_sample is {self.displayed_sample} but the arm holds "
                f"{len(self.samples)} samples"
            )
        if tuple(index for index, _ in enumerate(self.samples)) != tuple(
            sample.index for sample in self.samples
        ):
            raise ValueError("samples must be stored in draw order, indexed from zero")
        wrong = sorted(name for name, spread in self.spread.items() if spread.n != len(self.samples))
        if wrong:
            raise ValueError(
                f"the spread of {', '.join(wrong)} does not hold one value per sample"
            )
        return self


class ShowcaseItem(BaseModel):
    """One curated question, answered by every arm."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    question: str
    why: str
    arms: tuple[ShowcaseArm, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_arm_per_strategy(self) -> ShowcaseItem:
        names = [arm.strategy for arm in self.arms]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"more than one arm for: {', '.join(duplicates)}")
        return self


class Sampling(BaseModel):
    """How every sample in this file was drawn — once, at the top.

    The same structural argument that puts `sample_count` above `arms` in a Run Record. If `n`, the
    generator or the temperature could differ between two items, the file would be comparing things
    drawn differently while looking like one measurement, and no reader could see it.

    `model` sits beside `generator` rather than being folded into it, which is one field more than
    the issue specifies and is the same split `Answer` already makes: cost, token accounting and
    retirement dates belong to the model, the adapter belongs to the vendor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    n: int = Field(ge=1)
    generator: str
    model: str | None
    temperature: float
    # A date, in the vocabulary `pricing_as_of` already established, and distinct from `started_at`:
    # that one is a stopwatch reading for this build, this one is "when these samples were drawn
    # from that provider", which is what a reader comparing against a later model release needs.
    measured_on: str


class Redistribution(BaseModel):
    """What the ADR-0003 gates decided, written down so the decision is auditable.

    Three gates, all reported. `chunk_text_stored` is a `Literal[False]`, so a build that ever
    wanted to store text would have to change this line, in a commit, where someone can object to
    it. The two verbatim findings are recorded whether or not they fired, because a threshold with
    no observed value beside it is a policy nobody can calibrate — and because "the gate ran and saw
    six" and "the gate never ran" are different facts.

    Two measures rather than one, and the pair is the fix for a real hole. A contiguous-run gate
    alone is evadable by dropping a linking word every twenty tokens, which redistributes a whole
    paragraph in order at a run of twenty. See `longest_common_subsequence`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_text_stored: Literal[False] = False
    verbatim_token_limit: int = Field(ge=1)
    verbatim_subsequence_limit: int = Field(ge=1)
    worst_verbatim: VerbatimFinding
    worst_verbatim_subsequence: VerbatimFinding

    @model_validator(mode="after")
    def _neither_gate_would_have_fired(self) -> Redistribution:
        # A record cannot exist describing a build a gate should have stopped. Belt and braces:
        # `build_showcase` raises before writing, and this makes a hand-edited file unloadable too.
        over: list[str] = []
        if self.worst_verbatim.tokens > self.verbatim_token_limit:
            over.append(
                f"the worst contiguous run is {self.worst_verbatim.tokens} tokens, over the "
                f"declared limit of {self.verbatim_token_limit}"
            )
        if self.worst_verbatim_subsequence.tokens > self.verbatim_subsequence_limit:
            over.append(
                f"the worst subsequence is {self.worst_verbatim_subsequence.tokens} tokens, over "
                f"the declared limit of {self.verbatim_subsequence_limit}"
            )
        if over:
            raise ValueError("; ".join(over) + "; this record should never have been written")
        return self


class ShowcaseRecord(BaseModel):
    """One precomputed showcase: curated questions, sampled, with everything a screen needs.

    `showcase_record_version` is a `Literal` for the same reason `RunRecord.run_record_version` is:
    a file written by a later version must fail to load rather than load partially, because a screen
    that silently ignored a field it did not understand would render a claim it cannot support.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    showcase_record_version: Literal[1] = SHOWCASE_RECORD_VERSION
    showcase_id: str
    started_at: str
    duration_ms: int = Field(ge=0)
    # Names the ADR-0004 layer this file belongs to, exactly as `RunRecord.layer` does, and it is
    # neither of that model's two values. Nothing should ever compare a showcase against a gate.
    layer: Literal["showcase"] = "showcase"
    # What this file is *for*, in the file. A small proving run and a curated release set have the
    # same schema and completely different standing, and a reader who cannot tell them apart will
    # cite the wrong one. Required, so it cannot be forgotten on the one that matters.
    scope: str = Field(min_length=1)
    provenance: Provenance
    sampling: Sampling
    redistribution: Redistribution
    # The rule, in the record. See `DISPLAY_RULE`.
    displayed_sample_rule: str = Field(min_length=1)
    items: tuple[ShowcaseItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _questions_are_distinct_and_sampled_alike(self) -> ShowcaseRecord:
        identifiers = [item.question_id for item in self.items]
        duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
        if duplicates:
            raise ValueError(f"more than one item for question: {', '.join(duplicates)}")
        # The promise `sampling` makes at the top of the file, checked against every arm underneath.
        # Without this the block would be a claim rather than a fact about the contents.
        wrong = [
            f"{item.question_id}/{arm.strategy} has {len(arm.samples)}"
            for item in self.items
            for arm in item.arms
            if len(arm.samples) != self.sampling.n
        ]
        if wrong:
            raise ValueError(
                f"sampling.n is {self.sampling.n} but: {'; '.join(wrong)}"
            )
        return self


# ------------------------------------------------------------------------------------------------
# Reading and writing.
# ------------------------------------------------------------------------------------------------


def write_showcase_record(record: ShowcaseRecord, directory: Path | None = None) -> Path:
    """One record, one new file. Same canonical bytes as a run record, for the same reasons."""
    directory = Path(directory) if directory is not None else SHOWCASE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.showcase_id}.json"
    path.write_bytes(_canonical_bytes(record.model_dump(mode="json")))
    return path


def load_showcase_record(path: Path) -> ShowcaseRecord:
    try:
        return ShowcaseRecord.model_validate_json(Path(path).read_bytes())
    except FileNotFoundError as missing:
        raise ShowcaseError(f"no showcase record at {path}") from missing
    except ValidationError as invalid:
        raise ShowcaseError(
            f"{path} is not a valid showcase record:\n{_one_line(invalid)}"
        ) from invalid


def showcase_ids(directory: Path | None = None) -> tuple[str, ...]:
    """Every record on disk, newest first — the id leads with a sortable timestamp."""
    directory = Path(directory) if directory is not None else SHOWCASE_DIR
    if not directory.is_dir():
        return ()
    return tuple(sorted((path.stem for path in directory.glob("*.json")), reverse=True))


def verify_showcase_records(corpus_hash: str, directory: Path | None = None) -> tuple[str, ...]:
    """Refuse to serve a precomputed answer over material this artifact does not hold.

    The boot gate for this format, and the same argument as `ingest.verify_artifact`: a showcase
    citing `svc-kadett-1993#0002` is worthless if that identifier now points at a different
    paragraph, and it is worse than worthless because it looks exactly as authoritative as a correct
    one. `GET /chunks` would happily hydrate it with the wrong text and nothing on screen would say
    so.

    Loud, at boot, and never per request — a service that checks this on the way out is a service
    willing to hold a stale record as long as nobody opens that page. Every mismatched record is
    named at once rather than the first, because the operator's next action is to rebuild all of
    them.
    """
    identifiers = showcase_ids(directory)
    stale: list[str] = []
    for showcase_id in identifiers:
        base = Path(directory) if directory is not None else SHOWCASE_DIR
        record = load_showcase_record(base / f"{showcase_id}.json")
        if record.provenance.corpus_hash != corpus_hash:
            stale.append(f"  {showcase_id}: corpus_hash {record.provenance.corpus_hash}")
    if stale:
        raise ShowcaseError(
            "the database holds a different Corpus than these showcase records were built "
            f"against.\n  database: corpus_hash {corpus_hash}\n" + "\n".join(stale) + "\n"
            "Rebuild them with `python -m garage showcase build`, or delete them. A precomputed "
            "answer over material this artifact does not hold cites chunks nobody can check."
        )
    return identifiers


# ------------------------------------------------------------------------------------------------
# The build.
# ------------------------------------------------------------------------------------------------


def build_showcase(
    database_url: str,
    corpus_dir: Path,
    *,
    generator: Generator,
    questions: Sequence[ShowcaseQuestion] | None = None,
    retrievers: Sequence[Retriever] | None = None,
    scope: str,
    n: int = DEFAULT_SAMPLE_COUNT,
    k: int = DEFAULT_K,
    tiers: tuple[str, ...] = TIERS,
    contract: Contract = Contract(),
    verbatim_token_limit: int = VERBATIM_TOKEN_LIMIT,
    verbatim_subsequence_limit: int = VERBATIM_SUBSEQUENCE_LIMIT,
    allow_dirty: bool = False,
    throttle_seconds: float = THROTTLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = lambda line: None,
) -> ShowcaseRecord:
    """Sample every curated question with every strategy, and refuse to write a record that leaks.

    The order of operations is the safe one and is the same order `run_evaluation` uses: verify the
    artifact, then the questions, then measure. A record naming a `corpus_hash` the database does
    not hold would be a reproducible-looking claim about material nobody measured.

    Every provider call goes through `app._answer`, which is a private name imported across a module
    boundary on purpose. That function *is* the five-exit degradation policy — no generator, no
    candidates, provider failure, contract violation, good answer — and a showcase that reimplemented
    it would be recording what a second copy of the endpoint would have done. The record has to hold
    what the endpoint holds, or the page it feeds is showing something the service never produced.

    `sleep` and `report` are injected so the tests can run the entire build with no wall clock and no
    silence: a throttle verified by waiting six seconds is a throttle nobody will keep testing.
    """
    from garage.ingest import verify_artifact
    from garage.retrieval import available_retrievers

    started = _now()
    clock = time.perf_counter()

    artifact = verify_artifact(database_url, corpus_dir)
    curated = tuple(questions) if questions is not None else load_questions()
    strategies = tuple(retrievers) if retrievers is not None else available_retrievers(database_url)
    if not strategies:
        raise ShowcaseError("this build serves no retrieval strategy; there is nothing to show")

    provenance = local_provenance(
        database_url, artifact.corpus_id, artifact.corpus_hash, artifact.ingest_version
    )
    if provenance.git_dirty and not allow_dirty:
        # `showcase_id` is `<timestamp>-<git_sha[:12]>`, deliberately the same format as `run_id`,
        # and that format is a *promise*: the sha identifies the code that produced the numbers. A
        # dirty tree breaks it — the sha names the last commit, and the record was produced by
        # something else that no longer exists anywhere.
        #
        # `eval run` only warns, and the asymmetry is the point rather than an inconsistency. A run
        # record is regenerated by one free local command, so a dirty one costs a minute to replace.
        # This one costs 160 provider calls, gets committed, and is then the thing a demo cites for
        # months — an unidentifiable build is not recoverable at that price. So the default is a
        # refusal, and `--allow-dirty` makes the exception a deliberate, visible act. Both facts
        # survive into the record through `provenance.git_dirty`, and the screen says so.
        raise ShowcaseError(
            "the working tree is dirty, so `git_sha` would not identify the code that produced "
            "this record — and `showcase_id` promises that it does.\n"
            f"  git_sha: {provenance.git_sha}\n"
            "Commit first, or pass --allow-dirty to build an unidentifiable record on purpose. "
            "Nothing was called."
        )
    worst = VerbatimReading(run=VerbatimFinding(tokens=0), subsequence=VerbatimFinding(tokens=0))
    # Counts the draws that actually reached the provider, and nothing else. Two consequences, both
    # wanted. The throttle applies *between* calls rather than before the first, so the smallest
    # possible run does not cost six seconds for nothing. And the zero-cost abstention — an empty
    # retrieval, where `app._answer` refuses without asking anybody — consumes no quota, so it must
    # not consume a pause either: sleeping six seconds to protect a rate limit nothing touched would
    # make a `lexical` arm that correctly abstains the slowest part of the build.
    calls = 0
    attempts = 0
    items: list[ShowcaseItem] = []

    for question in curated:
        arms: list[ShowcaseArm] = []
        for retriever in strategies:
            samples: list[Sample] = []
            retrieval: ShowcaseRetrieval | None = None
            for index in range(n):
                if calls:
                    sleep(throttle_seconds)
                attempts += 1
                report(
                    f"[{attempts}] {question.question_id} · {retriever.name} · sample {index + 1}/{n}"
                )
                candidates, answer, trace = _one_query(
                    retriever,
                    generator=generator,
                    question=question.question,
                    corpus_hash=artifact.corpus_hash,
                    k=k,
                    tiers=tiers,
                    contract=contract,
                )
                wire = GeneratedAnswer.of(answer)
                # `provider` is null exactly when nobody was asked: `abstain_without_asking` leaves
                # it unset, while a degradation — the provider was reached and failed — names it.
                # That is the same null the interface prints "nenhuma chamada" off, read here for
                # the same fact.
                if wire.provider is not None:
                    calls += 1

                # Checked here, before the next call, so a leak costs one question rather than the
                # whole run. The build must not spend another twenty calls on a record it is about
                # to refuse to write.
                found = worst_verbatim(wire, candidates, question_id=question.question_id)
                worst = worst.merge(found)
                _refuse_a_leak(
                    found,
                    question=question,
                    strategy=retriever.name,
                    token_limit=verbatim_token_limit,
                    subsequence_limit=verbatim_subsequence_limit,
                )

                ranking = ShowcaseRetrieval(
                    chunks=tuple(ShowcaseChunk.of(candidate) for candidate in candidates)
                )
                if retrieval is None:
                    retrieval = ranking
                elif ranking != retrieval:
                    # The claim this format makes about retrieval, checked rather than assumed. If
                    # it ever fires, storing one ranking for n samples is wrong and the format needs
                    # a spread here — so the build stops instead of quietly recording the first one.
                    raise ShowcaseError(
                        f"retrieval for {question.question_id!r} under {retriever.name} was not "
                        f"deterministic across samples: draw {index} returned a different ranking "
                        "than draw 0. The record stores one ranking per arm because retrieval is "
                        "deterministic; that premise no longer holds."
                    )
                samples.append(Sample(index=index, answer=wire, trace=trace))

            assert retrieval is not None  # n >= 1 by the signature's contract
            arms.append(
                ShowcaseArm(
                    strategy=retriever.name,
                    embedder=retriever.embedder_id,
                    k=k,
                    tiers=tiers,
                    contract=contract.mode,
                    retrieval=retrieval,
                    samples=tuple(samples),
                    spread=spreads_from(samples),
                    displayed_sample=choose_displayed_sample(samples),
                )
            )
        items.append(
            ShowcaseItem(
                question_id=question.question_id,
                question=question.question,
                why=question.why,
                arms=tuple(arms),
            )
        )

    return ShowcaseRecord(
        showcase_id=f"{_compact(started)}-{provenance.git_sha[:12]}",
        started_at=_iso(started),
        duration_ms=int((time.perf_counter() - clock) * 1000),
        scope=scope,
        provenance=provenance,
        sampling=Sampling(
            n=n,
            generator=generator.name,
            model=getattr(generator, "model", None),
            temperature=_temperature_of(generator),
            measured_on=started.date().isoformat(),
        ),
        redistribution=Redistribution(
            verbatim_token_limit=verbatim_token_limit,
            verbatim_subsequence_limit=verbatim_subsequence_limit,
            worst_verbatim=worst.run,
            worst_verbatim_subsequence=worst.subsequence,
        ),
        displayed_sample_rule=DISPLAY_RULE,
        items=tuple(items),
    )


def _refuse_a_leak(
    found: VerbatimReading,
    *,
    question: ShowcaseQuestion,
    strategy: str,
    token_limit: int,
    subsequence_limit: int,
) -> None:
    """Stop the build and name the question, on either measure.

    Checked after every draw rather than at the end, so a leak costs one question instead of the
    whole run — the build must not spend another twenty calls on a record it is about to refuse to
    write.

    Both measures are reported when both fired, because they say different things: a long contiguous
    run is a quotation, and a long subsequence with a short run is a paraphrase-shaped copy. Which
    one it is decides whether the fix is rewriting the question or rewriting the prompt.
    """
    reasons: list[str] = []
    if found.run.tokens > token_limit:
        reasons.append(
            f"repeats {found.run.tokens} consecutive tokens of Tier A chunk "
            f"{found.run.chunk_id} (limit {token_limit})"
        )
    if found.subsequence.tokens > subsequence_limit:
        reasons.append(
            f"shares {found.subsequence.tokens} tokens in order, gaps allowed, with Tier A chunk "
            f"{found.subsequence.chunk_id} (limit {subsequence_limit})"
        )
    if not reasons:
        return
    raise VerbatimLeak(
        f"the answer to {question.question_id!r} under {strategy} "
        + "; and ".join(reasons)
        + ".\n"
        f"  question: {question.question}\n"
        "Committing it would redistribute the operator's material (ADR-0003). Nothing was written. "
        "Rewrite or drop the question, or raise --verbatim-token-limit / "
        "--verbatim-subsequence-limit deliberately and say why in the commit."
    )


def _one_query(
    retriever: Retriever,
    *,
    generator: Generator,
    question: str,
    corpus_hash: str,
    k: int,
    tiers: tuple[str, ...],
    contract: Contract,
) -> tuple[tuple[Candidate, ...], Any, dict[str, Any]]:
    """One full pipeline run, traced exactly as `POST /query` traces it.

    The span names, the attribute keys and the nesting are `app.query`'s, because the waterfall in
    the interface reads them by name and a showcase whose traces were shaped differently would need
    a second renderer. Retrieval runs on every draw rather than once, and that is deliberate: it is
    local and free, and a trace assembled from one stage measured now and another measured a minute
    ago would be a timing diagram of a query that never happened.
    """
    from garage.app import _answer

    tracer = Tracer()
    with tracer.span("query", **{"query.question": question, "corpus.hash": corpus_hash}) as root:
        with tracer.span(
            "retrieve",
            **{
                "retrieval.strategy": retriever.name,
                "retrieval.embedder": retriever.embedder_id,
                "retrieval.k": k,
                "retrieval.tiers": ",".join(tiers),
            },
        ) as retrieve:
            candidates = tuple(
                retriever.retrieve(question, k=k, filters=Filters(tiers=tuple(tiers)))
            )
            retrieve.set(**{"retrieval.candidates": len(candidates)})
        answer = _answer(
            tracer,
            generator=generator,
            question=question,
            candidates=candidates,
            contract=contract,
        )
        root.set(**{"query.candidates": len(candidates)})

    tree = tracer.tree()
    assert tree is not None  # the root span above always opens
    return candidates, answer, tree


def _temperature_of(generator: Generator) -> float:
    """The temperature the samples were drawn at.

    Read off `generation.TEMPERATURE`, which is where `GeminiGenerator` reads it, rather than
    accepted as an argument here. A showcase that could *claim* a temperature its generator did not
    use would be a file asserting the one property that explains its own spread.
    """
    from garage.generation import TEMPERATURE

    return float(getattr(generator, "temperature", TEMPERATURE))
