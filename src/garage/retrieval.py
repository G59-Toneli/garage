"""Retrieval: the first seam, and the first thing worth measuring.

`Retriever` is the interface the whole comparison rests on (design §7.1). Strategy is a *runtime*
axis (§9), so swapping lexical for dense or hybrid must be a different object behind this contract
and nothing else — no change to the endpoint, no change to the response shape. That is the property
this module exists to hold, and it is why the endpoint never learns which implementation it has.

Two implementations live here now, and the pair is the point: the benchmark cannot compare anything
until there is something to compare.

`LexicalRetriever` is the first implementation: Postgres full text over the stored `tsvector`, plus
trigram matching for what full text structurally cannot reach. Both are needed, though the division
of labour changed with #12. Full text now searches under `garage_bi`
(`database.CREATE_TEXT_SEARCH_CONFIG`), which folds accents through `unaccent` before stemming, so
`cabecote` written without its cedilla is a full-text hit today.

This paragraph used to say that dropped accents were the *trigram* arm's job, and that was true only
by accident. Trigram reaches an unaccented word when enough of the rest of the sentence matches and
not otherwise: `cabecote plainado` peaks at `word_similarity` 0.714 and clears the floor, the bare
word `cabecote` peaks at 0.500 and is dropped, and `torque do parafuso do cabeçote` peaks at 0.357
across all 53 chunks. One word, three outcomes, decided by sentence length rather than by spelling.
`unaccent` makes it a full-text hit with a rank behind it in all three cases. What trigram still
carries on its own is genuine misspelling — `kadet` with one `t` — and partial terms, which no
dictionary folds; it fires above the floor for 31 of the 76 fact questions, so it is a live signal
and not a decoration.

`DenseRetriever` is the second, and it exists to pay a specific debt — a debt #12 has now partly
settled from the lexical side, which changes what the pair is for rather than removing the reason
for it. The lexical arm scored 0.91 on keyword phrasings and **0.07** on natural-language ones; with
the query shape below it scores 1.00 and 0.76. Almost all of the remainder is one thing, and it is
the thing dense exists for. Take the 21 natural-language questions written in Portuguese and split
them by the language the *target document* is written in: corrected lexical finds 9 of the 11 whose
answer is in Portuguese material and 3 of the 10 whose answer is in the English manual, and the
three are cognates (`torque`, `motor`) rather than translation. No parser of tsqueries will ever
learn that `flywheel` and `volante do motor` are the same part. A multilingual embedder has, and
that is now the *whole* of the remaining gap rather than most of a general one.

Which makes the two arms complementary in a way worth stating, because the `hybrid` strategy is
supposed to exploit it: corrected lexical reaches **0.952** on natural English questions and
**0.571** on natural Portuguese ones — a spread of 0.38 in one direction — while dense's overall
natural recall of 0.810 is not built that way. They fail on different questions, which is the
premise fusion needs and the reason neither arm is on its way out.

The two are deliberately *not* the same shape underneath, and one difference matters enough to state
here rather than bury: **dense does not abstain.** `LexicalRetriever` returns nothing when neither
signal fires — no lexeme of the question is in any chunk, and no trigram match clears
`WORD_SIMILARITY_FLOOR` — which is what lets a question the corpus does not cover retrieve nothing
at all; nearest-neighbour search has no such notion and always returns its k least-distant vectors,
however distant. No floor is invented here, because there is no measurement to set one from and a
threshold picked to look right is a number the gate would then be defending. The consequence is
real and documented in `docs/retrieval.md`: the zero-cost abstention in `app._answer` is reachable
under `lexical` and unreachable under `dense`, and the gate is the thing that will show what that
costs.

That abstention is **weaker after #12 and deliberately kept**. Over the 76 fact questions it fired
42 times before and fires 3 times now, because the OR fallback changed the bar from "some term of
the question did not match" to "no term of the question matched anything". The 39 it lost were
mostly not abstentions at all — they were a working retriever refusing to answer questions the
corpus does answer, which is the defect #12 reports. What remains is the honest case, and it is
still a real one: nothing in this corpus mentions a turbocharger wastegate actuator, so no lexeme of
that question is in any tsvector and the retriever says so by returning nothing. Buying recall by
deleting the property would have been the easy version of this change; ADR-0010 argues why the
narrower version was worth the extra CTE.

No language model anywhere here, deliberately: retrieval that can only be judged through a generator
cannot be measured, and measuring it is the point (ADR-0004). An embedder is not an exception to
that — it is a fixed function with a digest (`garage.embedding`), it runs offline, and it returns
the same vector for the same bytes on every machine that has been checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import psycopg
from psycopg.rows import dict_row

from garage.database import TEXT_SEARCH_CONFIG
from garage.embedding import Embedder, EmbedderError, configured_embedder

# The two signals are fused by rank, not by adding their scores together, because they are not on
# the same scale and never will be: `ts_rank_cd` lands around 0.01 for a good full-text hit while
# `word_similarity` lands around 0.7 for a good trigram one. A weighted sum of those two numbers is
# a trigram-only ranker wearing a weight it does not obey. Reciprocal rank fusion asks each signal
# only for its *ordering*, which is the part both are actually good at — and it is the same fusion
# the `hybrid` strategy will use across retrievers (design §7.1), so there is one idea here, not two.
LEXICAL_WEIGHT = 0.7
TRIGRAM_WEIGHT = 0.3

# The RRF damping constant, at its conventional 60: large enough that positions 1 and 2 are close
# and a single signal cannot dominate on its own, small enough that the tail still decays. One
# constant, shared, so `hybrid` fuses on the same scale this does rather than on a second 60 that
# drifts.
RRF_K = 60

# A warning for whoever writes `hybrid`, because the two weights above sum to 1.0 today and that is
# exactly the trap. `hybrid` has **three** signals — full text, trigram and cosine — not two. It is
# not "lexical + dense" with a pair of weights, and treating it that way silently drops the trigram
# signal, which is the one that carries Portuguese written without its accents and the reason half
# the Tier B material is reachable at all.
#
# It is also where the argument for fusing by *rank* rather than by score gets much stronger. Cosine
# lives in 0..1 and is by far the largest of the three — `ts_rank_cd` lands around 0.01 — so a
# weighted sum of raw scores is a pure dense ranker wearing two weights it does not obey.

# `word_similarity(query, text)` asks how well the query matches *some run of words inside* the
# chunk, which is the right question when a five-word question meets a two-hundred-word paragraph;
# plain `similarity()` would divide by the length of the chunk and score every long one to zero.
# Below this, a match is coincidence — the trigrams shared by any two Portuguese sentences.
#
# **Left alone by #12, on purpose**, and the reason is not that the signal is inert — an earlier
# draft of this comment claimed that and it is false. Measured over the 76 fact questions, 31 of them
# have at least one chunk above 0.6, so the floor is decided and is deciding.
#
# What 0.6 does not have is a *measurement*. It was picked by eye, and the distribution it sits in is
# strange: the questions that clear it clear it enormously (five reach 1.000, because a keyword
# question can be a literal substring of a spec row) while the ones that need help most sit at 0.36
# to 0.50. So the value is doing real work and nobody knows whether it is doing the right work, which
# is an argument for measuring it rather than for nudging it — and #13 changes the distribution
# underneath it anyway. A floor picked today is a floor #13 invalidates, and in the meantime the gate
# would be defending it. When there is a measurement, this line changes with a baseline behind it.
WORD_SIMILARITY_FLOOR = 0.6

DEFAULT_K = 10
MAX_K = 50

TIERS = ("A", "B")

# Same sentinel argument as `ingest.build`: `None` means "lexical only" and must stay distinguishable
# from "nobody said, ask the environment".
_UNSET: Any = object()


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

    #: How a run record's `Configuration.embedder` names the build-time axis this strategy stands
    #: on, or None where it stands on none (ADR-0005). An attribute rather than something
    #: `evaluation` reaches in and computes, because only the implementation knows whether it
    #: depends on a stored index and which one — a harness that special-cased `DenseRetriever` would
    #: have made the comparison the harness's business again.
    embedder_id: str | None

    #: The `Embedder` itself, for the boot gate to compare against `embeddings_meta`, or None. Kept
    #: apart from `embedder_id` deliberately: one is a label a JSON record carries and the other is
    #: a live object with weights behind it, and a single attribute doing both jobs would be
    #: serialised into a run record by the first person who tried.
    embedder: object | None

    def retrieve(
        self, query: str, *, k: int = DEFAULT_K, filters: Filters | None = None
    ) -> tuple[Candidate, ...]:
        ...


# One statement per strategy, so ranking happens in the database. Pulling candidates out to score
# them in Python would mean reading every chunk in the corpus on every query, and would put the
# ranking somewhere a `dense` or `hybrid` implementation could not follow it.
#
# The two scoring stages below are *fragments*, spliced into the statements underneath, rather than
# two hand-maintained copies of the same joins. `hybrid` is a third statement that needs both of
# them at once, and the claim above — that RRF here is the same fusion `hybrid` will use across
# retrievers — is only true for as long as there is one definition of each signal to fuse. Copy and
# paste would have made it false on the first edit to either.

# Everything a `Candidate` needs, named once. The `SELECT *` in the stages below carries these
# through, so a column added here reaches both strategies.
_CHUNK_COLUMNS = """
        chunks.chunk_id,
        chunks.doc_id,
        documents.title AS doc_title,
        chunks.tier,
        chunks.page,
        chunks.section,
        chunks.kind,
        chunks.text
"""

# Full text and trigram, per chunk, with the tier filter applied inside the SQL. `Filters` is part
# of the `Retriever` contract and applying it in Python would mean a retriever that returns fewer
# than k results for a tier-narrowed query — a comparison that means nothing.
#
# The full-text half is **strict AND with an OR fallback**, and the shape is the answer to #12 rather
# than a flourish. `plainto_tsquery` requires every lexeme, which is right when the question is
# keywords and catastrophic when it is a sentence: 42 of 76 fact questions retrieved literally
# nothing. Always OR-ing instead fixes recall (0.447 -> 0.842) and pays for it at the top of the
# list, because a one-word coincidence now ranks — r@1 0.612 against 0.704 for the shape below, and
# 7.0 candidates returned per question against 4.1. Trying AND first and falling back only when it
# matched nothing keeps the 34 keyword questions exactly as surgical as they were and spends the OR
# only where the alternative was an empty page (ADR-0010).
#
# It also keeps the abstention, which is the property that distinguishes this retriever from
# `DenseRetriever` and the reason the zero-cost path in `app._answer` is reachable at all. Weaker
# than it was, and honestly so: 42 empty results become 3. What survives is genuine — a question
# about a turbocharger wastegate actuator still returns nothing under OR, because *no* content word
# of it is anywhere in the corpus — but the bar moved from "not every term matched" to "not one term
# matched", and that is a real loss of margin recorded in ADR-0010 rather than glossed.
_LEXICAL_SCORED = f"""
parsed AS (
    -- Two readings of the same question, both under the same configuration the stored `tsvector`
    -- was built with, from the same constant. A query parsed under a different stemmer than the
    -- index would silently stop matching it.
    SELECT
        -- Every content word required. This is the precise reading and it stays the default.
        plainto_tsquery('{TEXT_SEARCH_CONFIG}', %(query)s) AS strict_tsq,
        -- Any content word will do. Built by unnesting `to_tsvector` under the *same*
        -- configuration rather than by a second parser, so the lexemes here are byte for byte the
        -- lexemes the index holds: stop words are already gone, stemming has already happened,
        -- accents are already folded. Nothing can diverge between the two readings, because only
        -- one of them does any parsing.
        (
            SELECT string_agg(quote_literal(lexeme), ' | ')
            FROM unnest(to_tsvector('{TEXT_SEARCH_CONFIG}', %(query)s))
        )::tsquery AS loose_tsq
),
strict AS (
    -- The fallback fires on **zero rows**, not on an empty tsquery, and the difference is the
    -- entire fix. A question like `what torque for the cylinder head bolts in stage 1` parses to a
    -- perfectly well-formed conjunction; it just happens to match nothing, because a spec table row
    -- reads `| Cylinder head bolt, stage 1 | M11 | 41 |` and contains no `for`. Testing the tsquery
    -- for emptiness would have declared that query fine and returned nothing, which is the bug.
    SELECT count(*) AS hits
    FROM chunks
    CROSS JOIN parsed
    WHERE chunks.tier = ANY(%(tiers)s)
      AND chunks.tsv @@ parsed.strict_tsq
),
chosen AS (
    -- One statement, one round trip, no parsing in Python. The whole ranking still lives in the
    -- database, which is the property that lets `dense` and `hybrid` be the same shape.
    SELECT
        CASE
            WHEN strict.hits > 0 THEN parsed.strict_tsq
            -- `COALESCE` because `string_agg` over an empty tsvector is NULL: a question made
            -- entirely of stop words has no loose reading either, and must fall back to the empty
            -- conjunction rather than to NULL, which would make `ts_rank_cd` null for every row.
            ELSE COALESCE(parsed.loose_tsq, parsed.strict_tsq)
        END AS tsq
    FROM parsed CROSS JOIN strict
),
scored AS (
    SELECT
        {_CHUNK_COLUMNS},
        ts_rank_cd(chunks.tsv, chosen.tsq) AS lexical,
        word_similarity(%(query)s, chunks.text) AS trigram
    FROM chunks
    JOIN documents ON documents.doc_id = chunks.doc_id
    CROSS JOIN chosen
    WHERE chunks.tier = ANY(%(tiers)s)
)
"""

# Cosine similarity against one embedder's vectors. `<=>` and not `<#>`, and the reason is the
# project's thesis rather than performance. For unit vectors the two order identically and `<#>` is
# marginally faster, but pgvector *negates* the inner product — Postgres only scans an index
# ascending — so `components["cosine"]` would arrive negative and need a `* -1` somewhere before a
# reader could believe it. In a demo whose product is the trace, a score that must be sign-corrected
# before it can be read is a permanent trap for whoever reads it next.
#
# `model_key` is a *parameter*. This is design §8's "switching embedder is a WHERE clause" written
# out: the Phase 4 fine-tuned embedder is a different value bound here and not one line of new SQL
# (ADR-0005).
#
# The score is **rounded before anything orders by it**, and the reasoning is spelled out because an
# earlier version of this comment got it wrong by two orders of magnitude. ONNX Runtime is not
# bit-reproducible across instruction sets. Embedding the whole fixture corpus and all 76 fact
# questions on x86-64 and again on emulated arm64: 18,679 of 20,352 passage components and 26,917 of
# 29,184 query components differ. The honest cosine bound is `|Dcos| <= ||Dq|| + ||Dp||`, measured at
# 1.162e-06; the largest cosine delta actually observed is 2.384e-07; the smallest adjacent gap
# between two top-ten cosines in the suite is 1.311e-06. Margin: 1.13x against the bound, 5.5x
# against reality. Measured end to end, **0 of 76 top-ten orders differ** — rounded or raw.
#
# So ordering does survive the trip today, and it survives it on a margin thin enough to write down
# rather than to trust. ADR-0001 puts production on aarch64 while every published number is measured
# on x86-64, so this is the live path, not a hypothetical.
#
# Rounding is insurance on that margin. Two cosines closer together than the grid become equal and
# the `chunk_id` tie-break below settles them identically on every machine. The cost is real and
# worth naming: a pair genuinely separated by less than 1e-5 is now ordered alphabetically rather
# than by similarity. In a project whose thesis is reproducibility that is the right trade, because
# a similarity difference smaller than the platform's own floating-point error is not signal, it is
# noise wearing the shape of signal.
#
# What rounding is **not** is a guarantee, and nobody should upgrade this comment into one. `round`
# is monotone, so it never creates an inversion the raw comparison did not already permit; what it
# does create is *ties*, which `chunk_id` then settles identically everywhere. That is the whole
# mechanism, and at five decimals it fires on **zero** of the 760 adjacent top-ten pairs this suite
# produces — including the tightest one, where the grid boundary at 0.834365 falls between the two
# values (0.834365457 and 0.834364044) and leaves them strictly ordered and exactly as exposed as
# before. A precision sweep wanders with no trend, and the exact counts do not even reproduce
# between measurements: they depend on where each value lands relative to a boundary, so cosines
# recomputed in float32 and cosines read from pgvector's float4 disagree on the integer while
# agreeing on the conclusion. ADR-0008 has both sweeps.
#
# So five decimals is kept on the two grounds that survived being measured, and not on a third that
# did not. It is demonstrably free — every metric and every per-item order identical to the
# unrounded run — and it sits 200x below the 2.6e-04 median gap, so it swallows no distinction the
# suite makes. It is cheap insurance against one class of perturbation on a denser corpus, bought at
# zero premium; on *this* corpus the x86-64/arm64 agreement is bought by the gap distribution, not
# by this line. Four decimals was measured and rejected: it creates 11 ties, 5 of which `chunk_id`
# resolves against cosine order — real ranking traded for a guarantee that still would not be one.
#
# The margin is 1.13x against the analytic bound. That is a reason to keep the cross-architecture
# measurement in the procedure when the corpus grows, not a reason to relax.
_DENSE_SCORED = """
scored AS (
    SELECT
        {columns},
        round(
            (1 - (embeddings.embedding <=> %(vector)s::vector))::numeric,
            {decimals}
        )::double precision AS cosine
    FROM chunks
    JOIN documents ON documents.doc_id = chunks.doc_id
    JOIN embeddings ON embeddings.chunk_id = chunks.chunk_id
    WHERE embeddings.model_key = %(model_key)s
      AND chunks.tier = ANY(%(tiers)s)
)
"""

# Where the grid is set, once. `dense_rank` is computed from the *rounded* cosine as well, so the
# rank a candidate reports and the order it arrives in cannot disagree — a panel showing rank 8 on a
# row sitting ninth would be the Glass Box lying about its own ranking.
DENSE_SCORE_DECIMALS = 5


_SEARCH = f"""
WITH {_LEXICAL_SCORED},
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


_DENSE_SEARCH = f"""
WITH {_DENSE_SCORED.format(columns=_CHUNK_COLUMNS, decimals=DENSE_SCORE_DECIMALS)},
ranked AS (
    SELECT
        scored.*,
        -- `rank()`, not `row_number()`, and here it is load bearing rather than merely careful.
        -- Rounding deliberately *creates* ties, and two chunks at an equal rounded cosine must
        -- place equally rather than in whatever order the planner happened to read them. No `CASE`
        -- guard: every row in `scored` has a cosine, because dense retrieval has no notion of a
        -- signal that did not fire. That absence *is* the missing floor.
        rank() OVER (ORDER BY cosine DESC) AS dense_rank
    FROM scored
)
SELECT ranked.*, cosine AS score
FROM ranked
-- The single signal is the score, so there is no fusion to do and RRF would only compress a
-- readable 0..1 cosine into a rank reciprocal nobody can interpret. `chunk_id` breaks ties for the
-- same reason it does under `lexical`, and after rounding it is doing real work rather than
-- guarding a theoretical case: it is what settles the ties rounding deliberately creates, and it is
-- what makes the order agree between x86-64 and arm64 in every case but the residual ADR-0008
-- measures.
ORDER BY score DESC, chunk_id
LIMIT %(k)s
"""

# pgvector's default `hnsw.ef_search` is 40, which is *below* `MAX_K`: at k=50 the index would be
# asked for fewer candidates than the query wants and would return fewer than k rows. Set per
# session as a function of k, and never below the default, so a small k does not also quietly buy
# worse recall.
#
# It is deliberately absent from the run record's `Configuration`, which is a claim worth defending
# rather than an omission. It is a pure function of `k`, and `k` is already recorded — a derived
# field is a second place for one fact to be wrong. More decisively: the contracted semantics of
# this retriever are *exact* search and the HNSW index is an optimisation
# (`database.CREATE_EMBEDDING_INDEX`), so at this scale `ef_search` cannot change the result the
# record describes.
#
# That defence rests entirely on the planner choosing a sequential scan, which is a premise nothing
# in the running system checks. The day the corpus is large enough for the planner to reach for the
# index, `ef_search` starts deciding *which* neighbours come back, approximation becomes visible in
# the ranking, and the gate would compare two things that are no longer comparable — silently, with
# no line of code anywhere noticing that the assumption underneath it expired. So the premise is
# asserted rather than assumed: `test_dense_retrieval` reads the plan with `EXPLAIN` and fails when
# it stops being a scan. That turns a silent expiry into a red build, and the fix at that point is a
# `Configuration` field for `ef_search` plus a deliberately re-promoted baseline.
HNSW_EF_SEARCH_FLOOR = 40


def _ef_search(k: int) -> int:
    return max(k, HNSW_EF_SEARCH_FLOOR)


class LexicalRetriever:
    """Full text plus trigram over the ingested chunks. Reads only; writes would be rejected anyway
    by the schema's guard (ADR-0002).
    """

    name = "lexical"
    # No stored index behind it beyond the chunks themselves, so no build-time embedder axis. Null
    # rather than absent: a Configuration field that appeared only when set would make a lexical
    # Configuration and a dense one look equal.
    embedder_id = None
    embedder = None

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


class DenseRetriever:
    """Nearest neighbours by cosine over one embedder's stored vectors. Reads only.

    Line for line the same shape as `LexicalRetriever` — a connection per query, `_clamp_k`, the
    whole ranking in one statement, the tier filter inside the SQL, ties broken on `chunk_id` — and
    the resemblance is deliberate. The two are compared arm against arm in a single run record, so
    every difference between them that is *not* the ranking signal is noise in that comparison.

    The constructor takes an `Embedder` **instance and never a model name**. A `str` parameter here
    would be a second place an embedder gets configured, and two independent constructions is
    precisely how the ingest side and the query side come to disagree. There is one factory,
    `embedding.embedder_for`, and both sides call it (acceptance criterion three).
    """

    name = "dense"

    def __init__(self, database_url: str, embedder: Embedder) -> None:
        self._database_url = database_url
        # Public, because the boot gate has to compare *this* object's fingerprint against the one
        # in `embeddings_meta` (`ingest._verify_embedder`). A private attribute would have forced
        # `app.py` to reach through the name mangling or to resolve a second embedder of its own —
        # and a second resolution is the divergence this whole design removes.
        self.embedder = embedder
        # What the run record's `Configuration.embedder` carries. The `model_key` alone would name
        # the `WHERE` clause and say nothing about what produced the vectors behind it — two runs
        # separated by a change to pooling or to the e5 prefixes would look comparable and would not
        # be. Twelve hex characters of the fingerprint is what makes the baseline stop comparing
        # across a build-time change, which is the whole job of a Configuration field (ADR-0005).
        self.embedder_id = f"{embedder.model_key}@{embedder.fingerprint[:12]}"

    def retrieve(
        self, query: str, *, k: int = DEFAULT_K, filters: Filters | None = None
    ) -> tuple[Candidate, ...]:
        filters = filters or Filters()
        k = _clamp_k(k)
        # `embed_query`, and the method name is the safety. The e5 prefixes are asymmetric and this
        # is the query side; with a single generic `embed` the passage prefix here would be a silent
        # recall loss that no test of shape, dimension or type could see.
        vector = self.embedder.embed_query(query)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            # `SET LOCAL`, so it expires with this transaction and cannot leak into a connection the
            # pool that does not exist yet might hand to someone else.
            connection.execute(f"SET LOCAL hnsw.ef_search = {_ef_search(k)}")
            rows = connection.execute(
                _DENSE_SEARCH,
                {
                    "vector": _as_vector(vector),
                    "model_key": self.embedder.model_key,
                    "tiers": list(filters.tiers),
                    "k": k,
                },
            ).fetchall()
        return _rows_to_dense_candidates(rows)


def _as_vector(vector: Sequence[float]) -> str:
    """pgvector's text input form. See `ingest._write_embeddings` for why it is not an adapter."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


# The cap on `GET /chunks?ids=...`. It is not a rate limit and is not about load: it is the same
# argument as `MAX_K`, which is that a public read-only endpoint must not offer "return the whole
# corpus in one request" as an ordinary parameter. A hundred is four times the largest hydration any
# showcase arm can ask for at `MAX_K = 50` per column, so nothing legitimate meets it.
MAX_CHUNK_IDS = 100


@dataclass(frozen=True)
class StoredChunk:
    """One chunk read by identifier: the same fields a `Candidate` carries, minus the ranking.

    No `score` and no `components`, and their absence is the whole distinction. A `Candidate` is the
    result of a *query* — this chunk, found this way, at this position. A `StoredChunk` is the
    artifact's own record of a paragraph, and it is the same paragraph whoever asks and whyever.
    Giving it a score would mean inventing one for a lookup that ranked nothing.
    """

    chunk_id: str
    doc_id: str
    doc_title: str
    tier: str
    page: int | None
    section: str | None
    kind: str
    text: str


_BY_ID = f"""
SELECT {_CHUNK_COLUMNS}
FROM chunks
JOIN documents ON documents.doc_id = chunks.doc_id
WHERE chunks.chunk_id = ANY(%(ids)s)
ORDER BY chunks.chunk_id
"""


def fetch_chunks(database_url: str, ids: Sequence[str]) -> tuple[StoredChunk, ...]:
    """The text behind a list of identifiers, straight off the artifact.

    This is what makes ADR-0003 survivable for a *precomputed* record. A showcase commits
    `chunk_id`s and never a word of the material, and the words are read back here at render time —
    local, free, deterministic, and no model anywhere near it (`docs/showcase.md`).

    Identifiers that do not exist are simply not returned, and that is the contract rather than a
    shortcut: a clone of this repository with no database, or with a Corpus that does not hold the
    operator's manual, must be able to render the showcase with those chunks marked absent. The
    caller compares what it asked for against what came back and says so on screen. A 404 here would
    turn a legitimate partial artifact into an error page.

    No tier filter and no `k`. The caller is hydrating identifiers it already holds from a record
    this build verified at boot, so a filter would only be able to *hide* a chunk the reader was
    already looking at the citation for.
    """
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return ()
    if len(wanted) > MAX_CHUNK_IDS:
        raise ValueError(f"at most {MAX_CHUNK_IDS} chunk ids per request, got {len(wanted)}")
    with psycopg.connect(database_url) as connection:
        rows = connection.cursor(row_factory=dict_row).execute(_BY_ID, {"ids": wanted}).fetchall()
    return tuple(
        StoredChunk(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            doc_title=row["doc_title"],
            tier=row["tier"],
            page=row["page"],
            section=row["section"],
            kind=row["kind"],
            text=row["text"],
        )
        for row in rows
    )


def available_retrievers(database_url: str, embedder: Embedder | None = _UNSET) -> tuple[Retriever, ...]:
    """Every strategy this build can measure, in the order a report should show them.

    This tuple, and not the evaluation harness, is the list the deterministic gate iterates over.
    Strategy is a runtime axis (design §9) and comparing strategies is the entire point of the
    benchmark, so `dense` belongs here — one line, in the module that owns retrieval — and the gate
    picks it up with no change to `evaluation.py` at all. A harness that named `LexicalRetriever`
    itself would have made the comparison the harness's business rather than retrieval's.

    `dense` is present exactly when an embedder is configured, and its absence is never quiet: with
    `GARAGE_EMBEDDER=none` the build is lexical-only *by declaration*, and with a broken or missing
    weights file `embedder_for` raises rather than returning None. A retriever list that silently
    shrank when the model could not be loaded would report a configuration mistake as a strategy
    that scores nothing, which is the single most expensive way for this gate to be wrong.
    """
    if embedder is _UNSET:
        embedder = configured_embedder()
    if embedder is None:
        return (LexicalRetriever(database_url),)
    return (LexicalRetriever(database_url), DenseRetriever(database_url, embedder))


def _clamp_k(k: int) -> int:
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    # A cap rather than an error: the endpoint is public and `k=100000` is a way to make the service
    # read the whole corpus, not a question anyone is asking.
    return min(k, MAX_K)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _candidate(row: Any, score: float, components: dict[str, float | None]) -> Candidate:
    return Candidate(
        chunk_id=row["chunk_id"],
        doc_id=row["doc_id"],
        doc_title=row["doc_title"],
        tier=row["tier"],
        page=row["page"],
        section=row["section"],
        kind=row["kind"],
        text=row["text"],
        score=score,
        components=components,
    )


def _rows_to_dense_candidates(rows: Sequence[Any]) -> tuple[Candidate, ...]:
    # Two entries and no nulls, against the lexical panel's four. The Glass Box shows the score
    # panel as the retriever reports it, so a `dense` candidate carrying a null `lexical` key would
    # be this strategy pretending to have a signal it never computed.
    return tuple(
        _candidate(
            row,
            float(row["score"]),
            {"cosine": float(row["cosine"]), "dense_rank": float(row["dense_rank"])},
        )
        for row in rows
    )


def _rows_to_candidates(rows: Sequence[Any]) -> tuple[Candidate, ...]:
    return tuple(
        _candidate(
            row,
            float(row["score"]),
            {
                "lexical": float(row["lexical"]),
                "trigram": float(row["trigram"]),
                "lexical_rank": _optional_float(row["lexical_rank"]),
                "trigram_rank": _optional_float(row["trigram_rank"]),
            },
        )
        for row in rows
    )
