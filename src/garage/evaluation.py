"""The deterministic evaluation layer: retrieval quality as a number, and that number as a gate.

ADR-0004 splits evaluation in two. The *deterministic* layer — this module — asks only whether the
right chunk came back, which is a question with a known answer written down in `eval/facts.jsonl`.
The judge layer, later, asks whether a generated answer reads correctly, which is a question only a
model can answer and therefore a question no build should block on. Keeping them apart is what lets
retrieval be measured on its own: no language model here, no network, no API key, no non-determinism
worth the name. `pytest`, `corpus validate`, `ingest`, and then this — all offline.

Three things live here, in that order of dependence:

- **The facts.** One question per line, each naming the `chunk_id` that answers it and the value a
  correct answer would have to contain. They are written by hand against real chunk identifiers, and
  every identifier is checked against the database before a single query runs — a fact pointing at a
  chunk that no longer exists silently lowers recall, which is the one failure mode a gate must
  never absorb quietly.
- **The metrics.** `recall@k`, `mrr@k` and `nDCG@k` under binary relevance, as pure functions over a
  list of retrieved identifiers and a set of relevant ones. Pure because they must be testable
  without a database, and roughly fifteen lines of arithmetic because that is all they are — adding
  a scientific dependency to a gate that has to run on an ARM VM (ADR-0001) to avoid writing
  `1 / log2(i + 1)` would be a poor trade.
- **The comparison.** A run record measured now against a baseline promoted deliberately, with every
  reason for failure reported at once.

What this module never does is rank anything. `Retriever` already returned an order that the SQL
made total (`ORDER BY score DESC, chunk_id`); re-sorting those candidates in Python would reintroduce
exactly the tie-breaking non-determinism the database was written to remove. The candidate tuple is
consumed in the order it arrived, always.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform as platform_info
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# `_fold` is private to `jargon`, and imported anyway rather than copied: accent-insensitive matching
# has to mean the same thing when the gate looks for a value in a chunk as it did when ingestion
# detected a term in that chunk, or the two would disagree about whether `cabeçote` was present.
from garage.jargon import _fold as fold
from garage.retrieval import MAX_K, TIERS, Candidate, Filters, Retriever

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
FACTS_PATH = EVAL_DIR / "facts.jsonl"
BASELINE_PATH = EVAL_DIR / "baseline.json"
RUNS_DIR = EVAL_DIR / "runs"

# Recall is reported at three depths because they answer different questions: `recall@1` is whether
# the top hit is usable on its own, `recall@10` is whether a reader scrolling the panel would find
# it at all. All three come from a *single* retrieval at the deepest one — re-querying per depth
# would triple the work and, worse, would let the three numbers disagree if anything about the
# ranking were ever non-deterministic.
RECALL_DEPTHS = (1, 5, 10)
EVAL_K = max(RECALL_DEPTHS)

# The scoring depth is part of the Configuration, not a knob: `mrr@10` and `mrr@50` are different
# measurements and comparing one against the other would be meaningless.
assert EVAL_K <= MAX_K

# Version 2 because the record holds *arms*: one measurement per strategy, side by side in one file.
# A single-arm record could express `lexical` alone and could not express the comparison the whole
# benchmark exists to make — and worse, two single-arm files could be produced against two different
# databases and displayed beside each other as though they were a comparison. With `provenance`,
# `sample_count` and `facts_sha256` held once at the top of the record and the arms underneath, that
# is not a rule anyone has to remember: it is unrepresentable.
RUN_RECORD_VERSION = 2
BASELINE_VERSION = 2

# Numbers as they appear in workshop text: `63`, `0.18`, `3,9`. Thousands separated by spaces
# (`10 000 km`, `90 484 220`) deliberately split into separate matches — those are not quantities
# anyone compares numerically, and the facts that ask for them declare no tolerance and are matched
# as text instead.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class EvaluationError(Exception):
    """The evaluation cannot be run or cannot be trusted. Never a warning, always a non-zero exit."""


class Fact(BaseModel):
    """One question with a known answer, and the chunk that carries it.

    `chunk_ids` is a list even though it holds one entry in most cases. Two reasons, and the second
    is the real one: a value genuinely can live in more than one chunk (both cylinder head stages
    are M11), and with exactly one relevant chunk per question nDCG is a monotone function of the
    reciprocal rank and reports nothing MRR did not already say. The list is what makes the third
    metric earn its place.

    `tolerance` is present exactly when `expected_value` is a number. Its absence is what selects
    text matching, so a fact cannot accidentally get both.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    question: str = Field(min_length=1)
    # How the question is *phrased*, not how hard it is. Required, because the suite is a sample of
    # an input distribution and a sample that does not say what it sampled cannot be audited. Real
    # users type both: three keywords into a search box, and whole sentences with a question mark.
    # Against a conjunctive `plainto_tsquery` those two behave completely differently, so a suite
    # made only of one of them measures a corner of the problem and reports it as the whole. The
    # first version of this file was entirely `keyword`, scored 0.91, and was worthless: it could
    # not have detected the difference between good retrieval and an inverted index.
    phrasing: Literal["keyword", "natural"]
    expected_value: str = Field(min_length=1)
    chunk_ids: tuple[str, ...] = Field(min_length=1)
    tolerance: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _chunk_ids_are_distinct(self) -> Fact:
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("chunk_ids repeats an entry")
        return self

    @property
    def relevant(self) -> frozenset[str]:
        return frozenset(self.chunk_ids)


def load_facts(path: Path | None = None) -> tuple[Fact, ...]:
    """Read `facts.jsonl`, reporting the line number of every malformed line at once.

    Blank lines are rejected rather than skipped. A tolerant reader would let a fact be deleted by a
    stray keystroke and the suite quietly shrink, and a suite that shrinks is the cheapest way there
    is to make a quality gate pass.
    """
    path = Path(path) if path is not None else FACTS_PATH
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except FileNotFoundError as missing:
        raise EvaluationError(f"no fact set at {path}") from missing

    # A trailing newline ends the last record; anything after it would be a real blank line.
    if lines and lines[-1] == "":
        lines.pop()

    facts: list[Fact] = []
    failures: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            failures.append(f"line {number}: blank")
            continue
        try:
            facts.append(Fact.model_validate(json.loads(line)))
        except json.JSONDecodeError as broken:
            failures.append(f"line {number}: not JSON: {broken}")
        except ValidationError as invalid:
            failures.append(f"line {number}: {_one_line(invalid)}")

    duplicates = sorted({fact.fact_id for fact in facts if _count(facts, fact.fact_id) > 1})
    failures.extend(f"duplicate fact_id: {fact_id}" for fact_id in duplicates)

    if failures:
        raise EvaluationError(f"{path} is not a valid fact set:\n  " + "\n  ".join(failures))
    if not facts:
        raise EvaluationError(f"{path} holds no facts")
    return tuple(facts)


def _count(facts: Sequence[Fact], fact_id: str) -> int:
    return sum(1 for fact in facts if fact.fact_id == fact_id)


def _one_line(invalid: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in invalid.errors()
    )


def facts_digest(path: Path | None = None) -> str:
    """The digest of the fact set exactly as it sits on disk.

    Recorded on every run so that a changed number can be attributed. Without it, "recall went from
    0.91 to 0.78" is ambiguous between *retrieval got worse* and *someone added eight hard
    questions* — and those two call for opposite responses.
    """
    path = Path(path) if path is not None else FACTS_PATH
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------------
# Metrics. Pure functions over identifiers: no database, no retriever, no clock.
# --------------------------------------------------------------------------------------------


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the relevant chunks that appear in the first `k` retrieved."""
    relevant = frozenset(relevant)
    if not relevant:
        raise ValueError("recall is undefined with no relevant chunks")
    return len(relevant.intersection(retrieved[:k])) / len(relevant)


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """`1 / rank` of the first relevant chunk in the first `k` retrieved, 1-based; 0.0 if none.

    Zero rather than *skipped*. Dropping a miss from the average would let a change that stops
    finding the hardest questions at all report a *higher* MRR than the version that found them at
    rank 9 — the arithmetic of averaging over a smaller, easier set. This is truncated MRR, which is
    why the run record names the metric `mrr@10` and not `mrr`.
    """
    relevant = frozenset(relevant)
    for position, chunk_id in enumerate(retrieved[:k], start=1):
        if chunk_id in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Normalised discounted cumulative gain over the first `k` retrieved, binary relevance.

    Relevance is binary here — a chunk either answers the question or it does not — and under binary
    relevance the two textbook gain formulations agree: `rel` and `2**rel - 1` are both 0 for 0 and
    both 1 for 1. This is the first thing a reviewer checks, so: there is no choice being made here,
    and switching formulations would change nothing while graded judgements do not exist.

    The ideal ranking puts every relevant chunk first, so `IDCG` sums the discount over
    `min(|relevant|, k)` positions. It can only be zero when there are no relevant chunks at all,
    which `Fact` forbids; the guard returns 0.0 by explicit convention rather than dividing, because
    an evaluation harness that raises `ZeroDivisionError` on an edge case is worse than one that
    reports the honest score of a query nothing could have answered.
    """
    relevant = frozenset(relevant)
    gain = sum(
        1.0 / math.log2(position + 1)
        for position, chunk_id in enumerate(retrieved[:k], start=1)
        if chunk_id in relevant
    )
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(relevant), k) + 1))
    if ideal == 0.0:
        return 0.0
    return gain / ideal


def hit_rank(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> int | None:
    """1-based position of the first relevant chunk within the first `k`, or None."""
    relevant = frozenset(relevant)
    for position, chunk_id in enumerate(retrieved[:k], start=1):
        if chunk_id in relevant:
            return position
    return None


def value_matches(expected_value: str, texts: Sequence[str], tolerance: float | None) -> bool:
    """Whether any of these texts states the value a correct answer would have to contain.

    With a `tolerance`, numbers are extracted and compared numerically. Substring matching on digits
    is the bug this avoids: `"63" in text` is true of `163`, of `0.63` and of `Section 6.3`, and a
    torque gate that accepts `163 N·m` for `63 N·m` is measuring nothing. Without a tolerance the
    value is text, matched accent- and case-insensitively because half the Corpus is forum
    Portuguese with the accents left off.

    Which texts get passed in is the caller's decision, and it decides what the metric means. See
    `value_match_rate` in `_aggregate`.
    """
    if tolerance is None:
        needle = fold(expected_value)
        return any(needle in fold(text) for text in texts)

    expected = _as_number(expected_value)
    if expected is None:
        raise ValueError(f"expected_value {expected_value!r} declares a tolerance but is not a number")
    return any(
        math.isclose(expected, found, rel_tol=0.0, abs_tol=tolerance)
        for text in texts
        for found in _numbers(text)
    )


def _as_number(text: str) -> float | None:
    match = _NUMBER.fullmatch(text.strip())
    return float(match.group().replace(",", ".")) if match else None


def _numbers(text: str) -> tuple[float, ...]:
    return tuple(float(match.replace(",", ".")) for match in _NUMBER.findall(text))


# The number of decimals every metric is rounded to before it is written and before it is compared.
# Rounding at the boundary rather than at the comparison means the file and the check agree by
# construction: a gate that compared unrounded floats against numbers it had rounded on the way out
# would fail on the last bit of a double and no one would ever work out why.
METRIC_PRECISION = 6


def _round(value: float) -> float:
    return round(value, METRIC_PRECISION)


# --------------------------------------------------------------------------------------------
# The run record.
# --------------------------------------------------------------------------------------------


class Provenance(BaseModel):
    """Everything needed to say *which* build produced these numbers.

    `corpus_hash` and `ingest_version` appear together or not at all (ADR-0007): the first says which
    material was measured, the second says which rules turned it into the chunks the facts name. A
    record citing only the first would be reproducible in principle and wrong in practice, because
    the same Corpus rechunked produces different `chunk_id`s.

    The three database fields are here because the ranking is not in Python — it is `ts_rank_cd`,
    the `portuguese` snowball stemmer and `word_similarity`, all of them inside Postgres. A minor
    server upgrade that retunes the stemmer, or a `pg_trgm` release that changes how similarity is
    computed, moves every number in this file, and a record that recorded the laptop's OS but not
    the version of the engine that did the ranking would be describing the wrong machine. They are
    properties of the artifact that was measured, not of the person who ran it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    git_sha: str
    git_dirty: bool
    corpus_id: str
    corpus_hash: str
    ingest_version: int
    python_version: str
    platform: str
    postgres_version: str
    pg_trgm_version: str
    # The configuration the search actually runs under — `database.TEXT_SEARCH_CONFIG`, resolved to
    # its schema-qualified name — and *not* the server's `default_text_search_config`. Recording the
    # server default was a real defect: `chunks.tsv` and `plainto_tsquery` both name `portuguese`
    # explicitly, so the default is a number this pipeline never reads. A record citing it would
    # miss a change to the configuration that matters and mislead whoever read it.
    text_search_config: str
    # And the dictionaries behind that name, because the name is a pointer. `portuguese` is a label
    # for a snowball stemmer plus a stop word list; a server upgrade that reissues either one moves
    # every `ts_rank_cd` in the file while the configuration is still called `portuguese`. This is
    # the field that would actually catch it.
    text_search_dictionaries: str


class Configuration(BaseModel):
    """The axes that make two runs comparable, or don't (design §9).

    `reranker` and `embedder` are null under the lexical strategy and are still written down, because
    the day one of them exists the baseline must stop comparing rather than compare across it. A
    field that appears only when it is set would make the two Configurations look equal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: str = Field(min_length=1)
    k: int = Field(ge=1)
    tiers: tuple[str, ...] = Field(min_length=1)
    reranker: str | None = None
    embedder: str | None = None


class ItemResult(BaseModel):
    """One question's outcome, kept so a regression can be read question by question.

    The chunk *text* is deliberately absent. A run record is committed, and ADR-0003 forbids
    redistributing source material — against a real Corpus of scanned manuals a record carrying text
    would be a slow leak of the very thing the repository promises not to hold. `chunk_id` is enough
    to reproduce any line of it against the artifact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    question: str
    expected_chunk_ids: tuple[str, ...]
    expected_value: str
    hit_rank: int | None
    reciprocal_rank: float
    ndcg: float
    value_matched: bool
    retrieved_chunk_ids: tuple[str, ...]


class Arm(BaseModel):
    """One strategy's measurement inside a run.

    `metrics` is per-arm rather than a single flat dictionary with prefixed keys, because prefixed
    keys are a namespace pretending not to be one: `lexical.recall@1` and `dense.recall@1` would
    have to be split apart again by every reader, and nothing would stop a third arm from colliding
    with either. An arm is the namespace.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    configuration: Configuration
    metrics: dict[str, float]
    per_item: tuple[ItemResult, ...]


def _distinct_strategies(arms: Sequence[Any], what: str) -> None:
    names = [arm.configuration.strategy for arm in arms]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        # One arm per strategy, so `strategy` alone identifies an arm in a message and in the
        # baseline. Two arms of the same strategy differing only in `k` would be a comparison across
        # a Configuration axis, which is a different run, not a second arm of this one.
        raise ValueError(f"{what} holds more than one arm for: {', '.join(duplicates)}")


class RunRecord(BaseModel):
    """One measurement of one artifact by every strategy, complete enough to reproduce and to argue
    with.

    The shape carries an argument. `provenance`, `sample_count` and `facts_sha256` sit at the top of
    the record and the arms sit underneath, so every arm in a record is by construction the same
    database, the same `corpus_hash`, the same chunking rules and the same questions. That is what
    makes the arms comparable *to each other*, which is the comparison the whole demo rests on —
    and with the fields held once, "these two strategies were measured against different corpora"
    stops being a mistake anyone can make and becomes a sentence this format cannot express.

    `run_record_version` is a `Literal` for the same reason `Manifest.manifest_version` is: a record
    written by a future version must fail to load rather than load partially, because a gate that
    silently ignored a field it did not understand would compare two things it had no business
    comparing. That is also why this bumped to 2 before anything shipped rather than after: a
    version bump costs an afternoon today and costs the entire committed history later.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_record_version: Literal[2] = RUN_RECORD_VERSION
    run_id: str
    started_at: str
    duration_ms: int
    # Names the layer of ADR-0004 this record belongs to. The judge layer will write records with the
    # same shape and `layer: "judged"`, and nothing should ever compare one against the other.
    layer: Literal["deterministic"] = "deterministic"
    suite: Literal["facts"] = "facts"
    provenance: Provenance
    sample_count: int = Field(ge=1)
    facts_sha256: str
    arms: tuple[Arm, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_arm_per_strategy(self) -> RunRecord:
        _distinct_strategies(self.arms, "run record")
        return self

    def arm(self, strategy: str) -> Arm | None:
        return next((arm for arm in self.arms if arm.configuration.strategy == strategy), None)


class BaselineArm(BaseModel):
    """One strategy's floor, and which of its metrics the build may fail on.

    `gated_metrics` is explicit and may be empty. A newly promoted arm gates nothing until someone
    lists its metrics by hand — a measurement should be watched for a while before it is allowed to
    fail everyone's build, and the alternative (gate whatever the record happened to report) turns
    every new metric into an unreviewed build dependency.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    configuration: Configuration
    metrics: dict[str, float]
    gated_metrics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _gated_metrics_are_present(self) -> BaselineArm:
        missing = sorted(set(self.gated_metrics) - set(self.metrics))
        if missing:
            raise ValueError(
                f"{self.configuration.strategy}: gated_metrics names metrics the baseline does not "
                f"hold: {missing}"
            )
        return self


class Baseline(BaseModel):
    """The numbers the gate compares against, and the policy for comparing them.

    `run_id` points at a real record in `eval/runs/`. That indirection is the point: a baseline of
    hand-typed numbers is a wish, while a baseline that names a run is a claim someone can go and
    check. Promotion copies from the record, never from a keyboard.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_version: Literal[2] = BASELINE_VERSION
    run_id: str
    sample_count: int = Field(ge=1)
    facts_sha256: str
    arms: tuple[BaselineArm, ...] = Field(min_length=1)
    # A *policy* number, not a statistical one. Nothing in this pipeline is random — the same commit
    # against the same artifact produces bit-identical metrics — so there is no variance to estimate
    # and no confidence interval to derive. The tolerance says how much loss a human is willing to
    # wave through. Deciding it is a judgement; writing it in a committed file is what makes the
    # judgement reviewable. One policy for every arm, because "how much regression is acceptable" is
    # a statement about this project, not about a strategy.
    #
    # It is compared against a metric's own delta, so what it buys depends on the population that
    # metric averages over — a subtlety worth stating because it is easy to promise slack the code
    # does not give. At 0.014 over 76 questions, one question is 0.0132 and passes on an aggregate
    # metric while two do not. Over the 34-question `keyword` stratum one question is 0.0294 and
    # fails. That asymmetry is deliberate: the strata exist to stop a change from trading one
    # phrasing against the other, and a stratum with slack in it could not do that job.
    tolerance: float = Field(ge=0.0)
    # Below this, an improvement is not worth a promotion commit. Also policy, and also here rather
    # than in an environment variable, so the gate is reproducible from the checkout alone.
    noise_floor: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _one_arm_per_strategy(self) -> Baseline:
        _distinct_strategies(self.arms, "baseline")
        return self

    def arm(self, strategy: str) -> BaselineArm | None:
        return next((arm for arm in self.arms if arm.configuration.strategy == strategy), None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    # `Z` rather than `+00:00`: shorter, unambiguous, and it sorts the same as the compact form used
    # in the filename, so the newest record is also the lexicographically last one.
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _git(*arguments: str) -> str:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        # A checkout without git — an extracted tarball, a container built from a copy — can still
        # be measured. It just cannot claim which commit it is, and `unknown` says so out loud
        # rather than inventing a sha.
        return ""


def git_provenance() -> tuple[str, bool]:
    """The commit these numbers describe, and whether the tree it was measured from was clean.

    `--porcelain` over `diff --quiet`, because untracked files count: a run measured against a fact
    set that exists only on one laptop is not reproducible, and that is exactly what an untracked
    `eval/facts.jsonl` would be.

    `eval/runs/` is excluded from that question, and the exclusion is a fix rather than a
    convenience. Writing a record dirties the tree, so without it the *next* run is born
    `dirty: true` because the previous run existed — the flag would report its own side effect and
    every record after the first would claim a dirty tree whether or not anything was actually
    uncommitted. Records are outputs; the flag is about the inputs.
    """
    sha = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--", ".", ":(exclude)eval/runs"))
    return (sha or "unknown", dirty)


def is_ancestor_of_head(sha: str) -> bool:
    """Whether this commit is in the history of HEAD.

    The guard on the record the gate validates against. `latest_run_record` picks the newest
    filename, and a record from an unmerged branch with a later timestamp would otherwise hijack
    that choice silently — the numbers would be checked against a run that describes a build this
    one is not descended from. Asking for ancestry is satisfiable (a record is committed after the
    sha it names, so its sha is always HEAD or an ancestor of it) and catches exactly the orphan.
    """
    if sha in ("", "unknown"):
        return False
    try:
        return (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
            ).returncode
            == 0
        )
    except OSError:
        # No git at all. The ancestry claim cannot be checked, so it is not made; `git_sha` in the
        # record will already say `unknown` and the reader can see why.
        return True


# The dictionaries `TEXT_SEARCH_CONFIG` maps its token types to, in a stable order. `::regconfig`
# resolves the bare name exactly as `to_tsvector` and `plainto_tsquery` resolve it, through the same
# `search_path`, so this reports the configuration the query really used rather than one that merely
# shares its name.
_TEXT_SEARCH_DICTIONARIES = """
SELECT
    -- Always schema-qualified, unlike `regconfig::text`, which drops the schema whenever the
    -- configuration happens to sit on the search_path. A record saying `portuguese` would not say
    -- *which* `portuguese`, and a configuration of that name in another schema is exactly the kind
    -- of shadowing this field exists to make visible.
    namespaces.nspname || '.' || configurations.cfgname AS config,
    coalesce(string_agg(DISTINCT dictionaries.dictname, ', ' ORDER BY dictionaries.dictname), 'none')
        AS dictionaries
FROM pg_ts_config AS configurations
JOIN pg_namespace AS namespaces ON namespaces.oid = configurations.cfgnamespace
LEFT JOIN pg_ts_config_map AS mapping ON mapping.mapcfg = configurations.oid
LEFT JOIN pg_ts_dict AS dictionaries ON dictionaries.oid = mapping.mapdict
WHERE configurations.oid = %(config)s::regconfig
GROUP BY namespaces.nspname, configurations.cfgname
"""


def database_provenance(database_url: str) -> tuple[str, str, str, str]:
    """The engine that did the ranking: server, `pg_trgm`, and the text search config it searched under."""
    import psycopg

    from garage.database import TEXT_SEARCH_CONFIG

    with psycopg.connect(database_url) as connection:
        server = connection.execute("SHOW server_version").fetchone()[0]
        trgm = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'"
        ).fetchone()
        config, dictionaries = connection.execute(
            _TEXT_SEARCH_DICTIONARIES, {"config": TEXT_SEARCH_CONFIG}
        ).fetchone()
    return (server, trgm[0] if trgm else "absent", config, dictionaries)


def verify_chunk_ids(database_url: str, facts: Sequence[Fact]) -> None:
    """Refuse to measure against facts that point at chunks the artifact does not hold.

    Every missing identifier is reported at once, in the manner of `validate_corpus`: someone
    repairing a fact set after a chunking change wants the whole list, not one per run. This is the
    check that keeps recall honest — a fact whose chunk was renamed scores zero forever, looks
    exactly like a retrieval regression, and would be blamed on the retriever for a week.
    """
    import psycopg

    wanted = sorted({chunk_id for fact in facts for chunk_id in fact.chunk_ids})
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id = ANY(%s)", (wanted,)
        ).fetchall()
    missing = sorted(set(wanted) - {row[0] for row in rows})
    if missing:
        raise EvaluationError(
            f"{len(missing)} of {len(wanted)} chunk_ids named by the fact set are not in the "
            "database:\n  "
            + "\n  ".join(missing)
            + "\nEither the facts are stale or the database is: run `python -m garage ingest`, then "
            "fix eval/facts.jsonl against the chunk_ids it produced."
        )


def local_provenance(database_url: str, corpus_id: str, corpus_hash: str, ingest_version: int) -> Provenance:
    """Provenance for a run happening right here, right now, against this database."""
    sha, dirty = git_provenance()
    postgres_version, pg_trgm_version, config, dictionaries = database_provenance(database_url)
    return Provenance(
        git_sha=sha,
        git_dirty=dirty,
        corpus_id=corpus_id,
        corpus_hash=corpus_hash,
        ingest_version=ingest_version,
        python_version=platform_info.python_version(),
        platform=platform_info.platform(),
        postgres_version=postgres_version,
        pg_trgm_version=pg_trgm_version,
        text_search_config=config,
        text_search_dictionaries=dictionaries,
    )


def run_evaluation(
    database_url: str,
    corpus_dir: Path,
    *,
    facts_path: Path | None = None,
    retrievers: Sequence[Retriever] | None = None,
    k: int = EVAL_K,
    tiers: tuple[str, ...] = TIERS,
) -> RunRecord:
    """The whole measurement, in the only order that is safe.

    Verify the artifact, then the fact set, then measure every strategy. `verify_artifact` comes
    first and comes before anything is written, because a run record naming a `corpus_hash` the
    database does not actually hold is worse than no record at all — it is a reproducible-looking
    claim about material that was never measured (ADR-0002). The numbers written down come from the
    `Artifact` the database returned, never from the manifest in the checkout: the manifest is what
    we *expected*, the artifact is what we *measured*.

    `retrievers` defaults to `retrieval.available_retrievers`, so which strategies exist is decided
    by the module that owns retrieval and not by this one.
    """
    from garage.ingest import verify_artifact
    from garage.retrieval import available_retrievers

    artifact = verify_artifact(database_url, corpus_dir)
    facts = load_facts(facts_path)
    verify_chunk_ids(database_url, facts)

    return evaluate(
        retrievers if retrievers is not None else available_retrievers(database_url),
        facts,
        provenance=local_provenance(
            database_url, artifact.corpus_id, artifact.corpus_hash, artifact.ingest_version
        ),
        facts_sha256=facts_digest(facts_path),
        k=k,
        tiers=tiers,
    )


def evaluate(
    retrievers: Sequence[Retriever],
    facts: Sequence[Fact],
    *,
    provenance: Provenance,
    facts_sha256: str,
    k: int = EVAL_K,
    tiers: tuple[str, ...] = TIERS,
) -> RunRecord:
    """Run every fact through every strategy once and assemble the record.

    Takes `Retriever`s, never a database URL or an HTTP client: strategy is a runtime axis
    (design §9) and the whole reason the gate is worth building is that a dense or hybrid retriever
    can be dropped into this sequence and measured against the same facts, in the same record, with
    nothing else changing.
    """
    if not retrievers:
        raise EvaluationError("no retrievers to measure")

    started = _now()
    arms = tuple(_measure(retriever, facts, k=k, tiers=tiers) for retriever in retrievers)
    finished = _now()
    return RunRecord(
        run_id=f"{_compact(started)}-{provenance.git_sha[:12]}",
        started_at=_iso(started),
        duration_ms=int((finished - started).total_seconds() * 1000),
        provenance=provenance,
        sample_count=len(facts),
        facts_sha256=facts_sha256,
        arms=arms,
    )


def _measure(retriever: Retriever, facts: Sequence[Fact], *, k: int, tiers: tuple[str, ...]) -> Arm:
    filters = Filters(tiers=tiers)

    items: list[ItemResult] = []
    for fact in facts:
        # One retrieval per fact at the deepest k, and the candidates are read in the order they
        # arrived. Not re-sorted, not filtered, not de-duplicated: the SQL made this order total.
        candidates: tuple[Candidate, ...] = retriever.retrieve(fact.question, k=k, filters=filters)
        retrieved = tuple(candidate.chunk_id for candidate in candidates)
        items.append(
            ItemResult(
                fact_id=fact.fact_id,
                question=fact.question,
                expected_chunk_ids=fact.chunk_ids,
                expected_value=fact.expected_value,
                hit_rank=hit_rank(retrieved, fact.relevant, k),
                reciprocal_rank=_round(reciprocal_rank(retrieved, fact.relevant, k)),
                ndcg=_round(ndcg_at_k(retrieved, fact.relevant, k)),
                # Top-1 only. Scanning all ten made this a strictly weaker restatement of recall —
                # it agreed with `mrr@10` to six decimals on every question, which is a metric
                # reporting nothing. Asked of the single chunk a reader is shown first, it disagrees
                # with both: a hit at rank 3 states the value and scores zero here, and a wrong
                # top-1 that happens to carry the number scores one. That gap is the measurement.
                value_matched=value_matches(
                    fact.expected_value,
                    [candidate.text for candidate in candidates[:1]],
                    fact.tolerance,
                ),
                retrieved_chunk_ids=retrieved,
            )
        )

    return Arm(
        # `embedder` is read off the retriever rather than derived here, because only the
        # implementation knows whether it stands on a stored index and which one (ADR-0005). Null
        # for `lexical` and written down anyway: a field that appeared only when set would make two
        # Configurations measured under different build-time axes look equal, which is exactly the
        # comparison the baseline must refuse to make.
        #
        # A plain attribute read and not `getattr(..., None)`. `Retriever` declares `embedder_id`,
        # so a retriever without one is a broken implementation and must say so here — a default
        # would turn a misspelled attribute into a silent `null` in a committed record, which is a
        # run measured under an unrecorded build-time axis and the precise thing this field exists
        # to prevent.
        configuration=Configuration(
            strategy=retriever.name,
            k=k,
            tiers=tiers,
            embedder=retriever.embedder_id,
        ),
        metrics=_aggregate(facts, items, k),
        per_item=tuple(items),
    )


def _aggregate(facts: Sequence[Fact], items: Sequence[ItemResult], k: int) -> dict[str, float]:
    """Macro-averages: every question weighs the same, whatever its document or tier.

    Micro-averaging would let the service manual — twenty of the fifty-three chunks — decide the
    score, and the questions worth watching are the forum ones nobody expects to work.
    """
    count = len(facts)
    metrics = {
        f"recall@{depth}": _round(
            sum(
                recall_at_k(item.retrieved_chunk_ids, fact.relevant, depth)
                for fact, item in zip(facts, items)
            )
            / count
        )
        for depth in RECALL_DEPTHS
    }
    metrics[f"mrr@{k}"] = _round(sum(item.reciprocal_rank for item in items) / count)
    metrics[f"ndcg@{k}"] = _round(sum(item.ndcg for item in items) / count)
    # Fraction of questions whose *first* chunk states the expected value. Deliberately not a
    # ranking metric and never averaged into the three above: it asks whether the single chunk a
    # reader is shown would let them answer, which recall does not ask and MRR does not either.
    metrics["value_match@1"] = _round(sum(item.value_matched for item in items) / count)
    # The two numbers that watch the *cost* of recall, added by #12 and deliberately not gated yet.
    #
    # Every metric above this line rewards finding the right chunk and none of them notices anything
    # else that came back with it. That was survivable while the retriever returned 0.7 candidates
    # per question; the OR fallback takes it to 4.1, and the same change on a corpus of fifty
    # thousand chunks could take it to fifty while `recall@10` climbs and the gate stays green the
    # whole way. A benchmark whose thesis is honesty should be able to see its own precision rot.
    #
    # Two numbers rather than one because neither is sufficient. `precision@10` is the textbook
    # quantity — relevant chunks found, over `k` — and on a fact set where most questions have a
    # single correct chunk it is capped near 0.1 and moves almost exactly like recall. `candidates@10`
    # is the one with the news in it: the mean size of the returned list, which is what a precision
    # collapse actually looks like from here and which no other field in the record carries.
    #
    # Ungated on purpose. Gating a metric means committing to a direction, and the honest direction
    # for `candidates@10` is not "lower" — an abstaining retriever scores a perfect zero. It belongs
    # in the record now, watched by a human across a few corpus sizes, and in `gated_metrics` when
    # somebody can say what a bad value is. Putting it in today would mean inventing that threshold.
    metrics[f"precision@{k}"] = _round(
        sum(
            len(set(item.retrieved_chunk_ids[:k]) & set(fact.relevant)) / k
            for fact, item in zip(facts, items)
        )
        / count
    )
    metrics[f"candidates@{k}"] = _round(
        sum(len(item.retrieved_chunk_ids) for item in items) / count
    )
    # Split by phrasing, and both halves are gated by the committed baseline. The headline
    # `recall@10` is one number over two populations that behave nothing alike, and averaging them
    # hides the only question anyone actually wants answered about a retriever: can it handle a
    # sentence, or only a bag of words? Gating the average alone would let a change buy keyword
    # recall with sentence recall and pass, which is the specific trade this project must not make.
    for phrasing in ("keyword", "natural"):
        selected = [(fact, item) for fact, item in zip(facts, items) if fact.phrasing == phrasing]
        if selected:
            metrics[f"recall@{k}:{phrasing}"] = _round(
                sum(recall_at_k(item.retrieved_chunk_ids, fact.relevant, k) for fact, item in selected)
                / len(selected)
            )
    return metrics


def _canonical_bytes(payload: Any) -> bytes:
    """The exact bytes a record or a baseline is written as.

    The same four choices as `corpus._canonical_manifest_bytes`, for the same reason and one more:
    sorted keys and `ensure_ascii=False` so the file is a function of its content rather than of the
    dict ordering that happened to build it, `indent=2` and a trailing newline because unlike a
    hashed manifest these files are *read* — in a pull request diff, by someone deciding whether a
    regression is acceptable. A one-line JSON blob would make every run look like a total rewrite.
    """
    return (
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def write_run_record(record: RunRecord, runs_dir: Path | None = None) -> Path:
    """Write one record to one new file. Never appends, never rewrites an existing one.

    A file per run rather than an append-only JSONL, because these accumulate in git across branches
    that merge: two branches each adding a file merge cleanly, while two branches each appending a
    line conflict on that line every single time. The history is the directory listing.
    """
    runs_dir = Path(runs_dir) if runs_dir is not None else RUNS_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{record.run_id}.json"
    path.write_bytes(_canonical_bytes(record.model_dump(mode="json")))
    return path


def load_run_record(path: Path) -> RunRecord:
    try:
        return RunRecord.model_validate_json(Path(path).read_bytes())
    except FileNotFoundError as missing:
        raise EvaluationError(f"no run record at {path}") from missing
    except ValidationError as invalid:
        raise EvaluationError(f"{path} is not a valid run record:\n{_one_line(invalid)}") from invalid


def load_baseline(path: Path | None = None) -> Baseline:
    path = Path(path) if path is not None else BASELINE_PATH
    try:
        return Baseline.model_validate_json(path.read_bytes())
    except FileNotFoundError as missing:
        raise EvaluationError(
            f"no baseline at {path}.\n"
            "Run `python -m garage eval run`, then promote the record it wrote with "
            "`python -m garage eval promote <run_id>`."
        ) from missing
    except ValidationError as invalid:
        raise EvaluationError(f"{path} is not a valid baseline:\n{_one_line(invalid)}") from invalid


def latest_run_record(runs_dir: Path | None = None) -> Path | None:
    """The newest record in the tree, or None.

    `started_at` leads the filename in a form that sorts chronologically as text, so `max` over the
    names is the newest run without opening any of them.
    """
    runs_dir = Path(runs_dir) if runs_dir is not None else RUNS_DIR
    records = sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []
    return records[-1] if records else None


def measurement(record: RunRecord) -> dict[str, Any]:
    """The part of a run record that a re-run on another machine must reproduce exactly.

    Three groups of fields are excluded, and the third is the one worth defending:

    - `run_id`, `started_at`, `duration_ms` — a clock and a stopwatch, different by definition.
    - `python_version`, `platform` — the gate runs on a developer's Windows laptop and on CI's
      Ubuntu; requiring these to match would assert that the two are the same machine, which is the
      opposite of what reproducibility means. They stay in the record because they are exactly what
      you want to read when a number *does* differ. This is the weakest of the three exclusions,
      which is why the database that actually did the ranking — its version, its `pg_trgm`, its
      text search config — *is* compared: those are properties of the artifact, and they are where
      an unexplained difference would really come from.
    - `git_sha` and `git_dirty` — a record cannot name the commit that contains it. It is generated,
      then committed, so the sha it carries is always its parent's. Requiring a match would make the
      check unsatisfiable by construction rather than strict. Ancestry is checked separately and
      is satisfiable: see `is_ancestor_of_head`.

    What is left is the measurement: which Corpus, which chunking rules, which engine, which
    Configurations, which questions, and what came back for each one. Those must be identical, and
    if they are not, the record in the tree is describing a build that no longer exists.
    """
    return {
        "corpus_id": record.provenance.corpus_id,
        "corpus_hash": record.provenance.corpus_hash,
        "ingest_version": record.provenance.ingest_version,
        "postgres_version": record.provenance.postgres_version,
        "pg_trgm_version": record.provenance.pg_trgm_version,
        "text_search_config": record.provenance.text_search_config,
        "text_search_dictionaries": record.provenance.text_search_dictionaries,
        "sample_count": record.sample_count,
        "facts_sha256": record.facts_sha256,
        "arms": [arm.model_dump(mode="json") for arm in record.arms],
    }


@dataclass(frozen=True)
class GateReport:
    """Every reason this build should fail, and everything worth saying if it should not."""

    failures: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def compare(
    baseline: Baseline,
    record: RunRecord,
    baseline_record: RunRecord | None = None,
) -> GateReport:
    """Judge a run against the promoted baseline. Collects every failure; never stops at the first.

    Comparability is checked before quality, and a mismatch is a *failure* rather than a comparison
    made anyway. A baseline measured at `k=10` says nothing about a run at `k=5`, and a baseline
    measured on thirty questions says nothing about a run on forty-three. Answering "did it get
    worse?" across either of those is not a conservative approximation, it is a wrong answer — so the
    gate stops and asks for a deliberate promotion instead.
    """
    failures: list[str] = []
    notes: list[str] = []

    # Checked once, at the top, because they are held once at the top: every arm of a record shares
    # the fact set and the suite size, which is exactly what makes the arms comparable to each other.
    if record.facts_sha256 != baseline.facts_sha256:
        failures.append(
            "the fact set changed since the baseline was promoted, so the numbers are not "
            f"comparable.\n    baseline facts_sha256 {baseline.facts_sha256}\n"
            f"    this run          facts_sha256 {record.facts_sha256}"
        )
    if record.sample_count < baseline.sample_count:
        # Called out on its own rather than folded into the metric comparison, because a shrunken
        # suite raises every macro-average it touches. Losing questions is the easiest way there is
        # to make this gate report an improvement.
        failures.append(
            f"the suite lost questions: {record.sample_count} now against "
            f"{baseline.sample_count} at the baseline. Metrics over a smaller set are not a "
            "comparison."
        )

    measured = {arm.configuration.strategy for arm in record.arms}
    for baselined in baseline.arms:
        strategy = baselined.configuration.strategy
        arm = record.arm(strategy)
        if arm is None:
            failures.append(
                f"the baseline holds a {strategy} arm and this run did not measure one. A strategy "
                "cannot be retired by no longer running it."
            )
            continue
        if arm.configuration != baselined.configuration:
            # Per arm, so `dense` arriving at a different `k` does not make the `lexical` comparison
            # unavailable. A baseline measured at k=10 says nothing about a run at k=5; answering
            # "did it get worse?" across that is not a conservative approximation, it is a wrong
            # answer, and the gate stops rather than gives one.
            failures.append(
                f"the {strategy} Configuration changed since the baseline was promoted, so its "
                f"numbers are not comparable.\n    baseline "
                f"{baselined.configuration.model_dump(mode='json')}\n    this run "
                f"{arm.configuration.model_dump(mode='json')}"
            )
            continue
        # Only compare quality once comparability holds. Deltas between two things already known to
        # be measuring differently would bury the real failure under six numbers of noise.
        if not failures:
            failures.extend(_metric_failures(baseline, baselined, arm, notes))
            before = baseline_record.arm(strategy) if baseline_record else None
            if before is not None:
                notes.extend(_moved_items(strategy, before, arm))

    # A new strategy is reported, never failed on. Someone has to look at it and promote it before
    # it is allowed to break anyone's build (ADR-0004: the gate protects a floor, it does not
    # discover one).
    notes.extend(
        f"new arm {strategy} is not in the baseline yet: "
        + ", ".join(
            f"{name} {value:.6f}" for name, value in sorted(record.arm(strategy).metrics.items())
        )
        + ". Promote to gate it."
        for strategy in sorted(measured - {arm.configuration.strategy for arm in baseline.arms})
    )

    return GateReport(failures=tuple(failures), notes=tuple(notes))


def _metric_failures(
    baseline: Baseline, baselined: BaselineArm, arm: Arm, notes: list[str]
) -> list[str]:
    strategy = arm.configuration.strategy
    failures: list[str] = []
    for name in baselined.gated_metrics:
        if name not in arm.metrics:
            failures.append(
                f"{strategy}: {name} is gated by the baseline but this run did not report it. A "
                "metric cannot be retired by deleting it; retire it from gated_metrics deliberately."
            )
            continue
        delta = _round(arm.metrics[name] - baselined.metrics[name])
        if -delta > baseline.tolerance:
            failures.append(
                f"{strategy}: {name} regressed: {baselined.metrics[name]:.6f} -> "
                f"{arm.metrics[name]:.6f} ({delta:+.6f}, tolerance {baseline.tolerance:.6f})"
            )
        elif delta > baseline.noise_floor:
            # An improvement never fails a build. It is worth saying out loud anyway, because a
            # baseline nobody promotes stops being a floor and becomes a memory.
            notes.append(
                f"{strategy}: {name} improved: {baselined.metrics[name]:.6f} -> "
                f"{arm.metrics[name]:.6f} ({delta:+.6f}). Promote when you are ready to hold this."
            )
    return failures


def _moved_items(strategy: str, before: Arm, after: Arm) -> list[str]:
    """Which individual questions changed rank, read off both records.

    This is what makes the gate a debugging tool rather than a red light. "recall@1 fell by 0.04" is
    unactionable; "`torque-volante-motor`: rank 1 -> none" names the query to paste into `/query`.
    """
    previous = {item.fact_id: item.hit_rank for item in before.per_item}
    moved = [
        f"{item.fact_id}: rank {_rank(previous[item.fact_id])} -> {_rank(item.hit_rank)}"
        for item in after.per_item
        if item.fact_id in previous and previous[item.fact_id] != item.hit_rank
    ]
    if not moved:
        return []
    return [f"{strategy}: {len(moved)} question(s) moved", *(f"  {line}" for line in moved)]


def _rank(rank: int | None) -> str:
    return "none" if rank is None else str(rank)


def promote(run_id: str, runs_dir: Path | None = None, baseline_path: Path | None = None) -> Path:
    """Copy the measurement of one recorded run into the baseline.

    Deliberate and never automatic, never run in CI. The commit that promotes a baseline is the only
    durable record that a human looked at a change in retrieval quality and decided to keep it; a CI
    job that promoted on green would erase that record and turn the gate into a ratchet that only
    ever agrees with whatever landed last.

    Policy — `gated_metrics`, `tolerance`, `noise_floor` — is carried over from the existing baseline
    rather than reset, because promoting a measurement is not the same decision as changing what the
    build is allowed to fail on. An arm the baseline has never seen is promoted **ungated**, and the
    caller is told so: a strategy should be watched before it is allowed to break builds, and
    auto-gating whatever a new arm happened to report is how an unreviewed number becomes a
    dependency.
    """
    runs_dir = Path(runs_dir) if runs_dir is not None else RUNS_DIR
    baseline_path = Path(baseline_path) if baseline_path is not None else BASELINE_PATH

    record = load_run_record(runs_dir / f"{run_id}.json")
    try:
        previous: Baseline | None = load_baseline(baseline_path)
    except EvaluationError:
        previous = None

    baseline = Baseline(
        run_id=record.run_id,
        sample_count=record.sample_count,
        facts_sha256=record.facts_sha256,
        arms=tuple(
            BaselineArm(
                configuration=arm.configuration,
                metrics=arm.metrics,
                gated_metrics=_carried_gates(previous, arm),
            )
            for arm in record.arms
        ),
        tolerance=previous.tolerance if previous else DEFAULT_TOLERANCE,
        noise_floor=previous.noise_floor if previous else DEFAULT_NOISE_FLOOR,
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_bytes(_canonical_bytes(baseline.model_dump(mode="json")))
    return baseline_path


def _carried_gates(previous: Baseline | None, arm: Arm) -> tuple[str, ...]:
    baselined = previous.arm(arm.configuration.strategy) if previous else None
    if baselined is None:
        return ()
    # A gated metric the new record no longer reports is dropped here rather than carried into a
    # baseline that could not validate. The gate still catches the disappearance on the *next* run
    # only if someone re-adds it, so promotion says so out loud in the CLI.
    return tuple(name for name in baselined.gated_metrics if name in arm.metrics)


def ungated_arms(baseline: Baseline) -> tuple[str, ...]:
    """Strategies the baseline records but does not gate. Reported, never silently accepted."""
    return tuple(
        arm.configuration.strategy for arm in baseline.arms if not arm.gated_metrics
    )


# Seeds for the very first baseline only; after that the committed file is the authority. Zero, so
# that the first thing anyone does is decide what loss is acceptable rather than inherit a number
# nobody chose.
DEFAULT_TOLERANCE = 0.0
DEFAULT_NOISE_FLOOR = 0.0
