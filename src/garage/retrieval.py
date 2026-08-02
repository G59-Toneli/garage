"""Retrieval: the first seam, and the first thing worth measuring.

`Retriever` is the interface the whole comparison rests on (design §7.1). Strategy is a *runtime*
axis (§9), so swapping lexical for dense or hybrid must be a different object behind this contract
and nothing else — no change to the endpoint, no change to the response shape. That is the property
this module exists to hold, and it is why the endpoint never learns which implementation it has.

`LexicalRetriever` is the first implementation: Postgres full text over the stored `tsvector`, plus
trigram matching for what full text structurally cannot reach. Both are needed. Stemming finds
`torques` from `torque`; it does nothing at all for `cabecote` written without its cedilla, or for
`kadet` with one `t` — and Brazilian workshop text is full of both (`jargon._fold` exists for the
same reason). Trigram similarity is what recovers those, at the cost of matching some noise, which
is why the two scores are combined rather than either being used alone.

No language model anywhere here, deliberately: retrieval that can only be judged through a generator
cannot be measured, and measuring it is the point (ADR-0004).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import psycopg
from psycopg.rows import dict_row

# The two signals are fused by rank, not by adding their scores together, because they are not on
# the same scale and never will be: `ts_rank_cd` lands around 0.01 for a good full-text hit while
# `word_similarity` lands around 0.7 for a good trigram one. A weighted sum of those two numbers is
# a trigram-only ranker wearing a weight it does not obey. Reciprocal rank fusion asks each signal
# only for its *ordering*, which is the part both are actually good at — and it is the same fusion
# the `hybrid` strategy will use across retrievers (design §7.1), so there is one idea here, not two.
LEXICAL_WEIGHT = 0.7
TRIGRAM_WEIGHT = 0.3

# The RRF damping constant, at its conventional 60: large enough that positions 1 and 2 are close
# and a single signal cannot dominate on its own, small enough that the tail still decays.
RRF_K = 60

# `word_similarity(query, text)` asks how well the query matches *some run of words inside* the
# chunk, which is the right question when a five-word question meets a two-hundred-word paragraph;
# plain `similarity()` would divide by the length of the chunk and score every long one to zero.
# Below this, a match is coincidence — the trigrams shared by any two Portuguese sentences.
WORD_SIMILARITY_FLOOR = 0.6

DEFAULT_K = 10
MAX_K = 50

TIERS = ("A", "B")


@dataclass(frozen=True)
class Filters:
    """The runtime axes a query may narrow itself with (design §9).

    A dataclass rather than loose keyword arguments because every implementation must accept exactly
    the same filters — a `dense` retriever that quietly ignored the tier filter would produce a
    comparison that means nothing.
    """

    tiers: tuple[str, ...] = TIERS

    def __post_init__(self) -> None:
        unknown = [tier for tier in self.tiers if tier not in TIERS]
        if unknown or not self.tiers:
            raise ValueError(f"tiers must be a non-empty subset of {TIERS}, got {self.tiers!r}")


@dataclass(frozen=True)
class Candidate:
    """One retrieved chunk with everything a citation, a tier label and a score panel need.

    `components` carries the per-signal scores and ranks behind `score`, with a null where a signal
    did not fire. The demo shows them (Glass Box): a chunk that ranked on trigram alone is a
    different kind of hit from one full text agreed with, and collapsing both into a single number
    hides exactly the failure worth looking at.
    """

    chunk_id: str
    doc_id: str
    doc_title: str
    tier: str
    page: int | None
    section: str | None
    kind: str
    text: str
    score: float
    components: dict[str, float | None]


class Retriever(Protocol):
    """`retrieve(query, k, filters) -> candidates`, and nothing else (design §7.1)."""

    #: How a run record names this strategy. Part of the contract: a Configuration is unidentifiable
    #: without it.
    name: str

    def retrieve(
        self, query: str, *, k: int = DEFAULT_K, filters: Filters | None = None
    ) -> tuple[Candidate, ...]:
        ...


# One statement, so ranking happens in the database. Pulling candidates out to score them in Python
# would mean reading every chunk in the corpus on every query, and would put the ranking somewhere a
# `dense` or `hybrid` implementation could not follow it (both rank in SQL — pgvector `<=>` and RRF).
_SEARCH = f"""
WITH parsed AS (
    SELECT plainto_tsquery('portuguese', %(query)s) AS tsq
),
scored AS (
    SELECT
        chunks.chunk_id,
        chunks.doc_id,
        documents.title AS doc_title,
        chunks.tier,
        chunks.page,
        chunks.section,
        chunks.kind,
        chunks.text,
        ts_rank_cd(chunks.tsv, parsed.tsq) AS lexical,
        word_similarity(%(query)s, chunks.text) AS trigram
    FROM chunks
    JOIN documents ON documents.doc_id = chunks.doc_id
    CROSS JOIN parsed
    WHERE chunks.tier = ANY(%(tiers)s)
),
matched AS (
    SELECT * FROM scored WHERE lexical > 0 OR trigram >= %(floor)s
),
ranked AS (
    SELECT
        matched.*,
        -- `rank()`, not `row_number()`: two chunks that scored identically must place identically,
        -- or the ranking would depend on which one the planner happened to read first. Null where
        -- the signal did not fire at all, so a non-match contributes nothing rather than a large
        -- rank that would quietly penalise chunks the other signal found.
        CASE WHEN lexical > 0 THEN rank() OVER (ORDER BY lexical DESC) END AS lexical_rank,
        CASE WHEN trigram >= %(floor)s THEN rank() OVER (ORDER BY trigram DESC) END AS trigram_rank
    FROM matched
)
SELECT
    ranked.*,
    COALESCE({LEXICAL_WEIGHT} / ({RRF_K} + lexical_rank), 0)
        + COALESCE({TRIGRAM_WEIGHT} / ({RRF_K} + trigram_rank), 0) AS score
FROM ranked
-- `chunk_id` breaks ties, so the same query against the same artifact returns the same order. A
-- benchmark whose ranking wobbled between runs would report noise as a difference.
ORDER BY score DESC, chunk_id
LIMIT %(k)s
"""


class LexicalRetriever:
    """Full text plus trigram over the ingested chunks. Reads only; writes would be rejected anyway
    by the schema's guard (ADR-0002).
    """

    name = "lexical"

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def retrieve(
        self, query: str, *, k: int = DEFAULT_K, filters: Filters | None = None
    ) -> tuple[Candidate, ...]:
        filters = filters or Filters()
        k = _clamp_k(k)
        # A connection per query, no pool. Honest about where this is: one demo service on one small
        # ARM VM, and a pool is a dependency plus a lifecycle to get wrong. The day the benchmark
        # measures throughput rather than ranking, this is the line that changes.
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                _SEARCH,
                {
                    "query": query,
                    "tiers": list(filters.tiers),
                    "floor": WORD_SIMILARITY_FLOOR,
                    "k": k,
                },
            ).fetchall()
        return _rows_to_candidates(rows)


def available_retrievers(database_url: str) -> tuple[Retriever, ...]:
    """Every strategy this build can measure, in the order a report should show them.

    This tuple, and not the evaluation harness, is the list the deterministic gate iterates over.
    Strategy is a runtime axis (design §9) and comparing strategies is the entire point of the
    benchmark, so the day `dense` exists it belongs here — one line, in the module that owns
    retrieval — and the gate picks it up with no change to `evaluation.py` at all. A harness that
    named `LexicalRetriever` itself would have made the comparison the harness's business rather
    than retrieval's.
    """
    return (LexicalRetriever(database_url),)


def _clamp_k(k: int) -> int:
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    # A cap rather than an error: the endpoint is public and `k=100000` is a way to make the service
    # read the whole corpus, not a question anyone is asking.
    return min(k, MAX_K)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _rows_to_candidates(rows: Sequence[Any]) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            doc_title=row["doc_title"],
            tier=row["tier"],
            page=row["page"],
            section=row["section"],
            kind=row["kind"],
            text=row["text"],
            score=float(row["score"]),
            components={
                "lexical": float(row["lexical"]),
                "trigram": float(row["trigram"]),
                "lexical_rank": _optional_float(row["lexical_rank"]),
                "trigram_rank": _optional_float(row["trigram_rank"]),
            },
        )
        for row in rows
    )
