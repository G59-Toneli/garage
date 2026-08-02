"""The evaluation end to end, against a real Postgres and the fixture Corpus.

What cannot be asserted without a database: that the fact set points at chunks that exist, that the
gate refuses to measure an artifact it does not recognise, and — the one that matters most — that
running the same evaluation twice produces the same record. Determinism is the property the whole
gate rests on: a benchmark whose ranking wobbled between runs would report noise as a regression and
train everyone to ignore it. Skipped when no database is reachable, so `pytest` stays green on a
bare checkout.
"""

import os

import pytest

from garage.corpus import FIXTURE_CORPUS
from garage.evaluation import (
    EVAL_K,
    EvaluationError,
    Fact,
    RECALL_DEPTHS,
    is_ancestor_of_head,
    latest_run_record,
    load_baseline,
    load_facts,
    load_run_record,
    measurement,
    run_evaluation,
    verify_chunk_ids,
)
from garage.chunking import INGEST_VERSION
from garage.database import TEXT_SEARCH_CONFIG
from garage.ingest import build

DATABASE_URL = os.environ.get("GARAGE_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="GARAGE_DATABASE_URL is unset; start `docker compose up`"
)


@pytest.fixture(scope="module")
def artifact():
    build(DATABASE_URL, FIXTURE_CORPUS)
    return DATABASE_URL


@pytest.fixture(scope="module")
def record(artifact):
    return run_evaluation(artifact, FIXTURE_CORPUS)


def test_every_chunk_id_the_committed_fact_set_names_exists_in_the_database(artifact):
    verify_chunk_ids(artifact, load_facts())


def test_a_fact_pointing_at_a_chunk_that_does_not_exist_fails_listing_all_of_them(artifact):
    stale = (
        Fact(
            fact_id="gone-one",
            question="q",
            phrasing="keyword",
            expected_value="v",
            chunk_ids=("svc-kadett-1993#9998",),
        ),
        Fact(
            fact_id="gone-two",
            question="q",
            phrasing="natural",
            expected_value="v",
            chunk_ids=("no-such-doc#0001",),
        ),
    )

    with pytest.raises(EvaluationError) as failure:
        verify_chunk_ids(artifact, stale)

    message = str(failure.value)
    assert "svc-kadett-1993#9998" in message and "no-such-doc#0001" in message


def test_the_run_record_cites_the_artifact_the_database_actually_holds(record):
    # From the `Artifact` the database returned, never from the manifest in the checkout: the
    # manifest is what we expected, the artifact is what we measured (ADR-0002, ADR-0007).
    assert record.provenance.corpus_id == "fixture"
    assert len(record.provenance.corpus_hash) == 64
    # The constant rather than a literal. It was `1` written out here, which meant ADR-0010's bump
    # broke a test whose subject is not the version number at all — and worse, a literal invites the
    # next person to "fix" it by editing this line, which is precisely the check being defeated.
    assert record.provenance.ingest_version == INGEST_VERSION
    assert record.provenance.postgres_version and record.provenance.pg_trgm_version
    # The configuration the search ran under, not the server default. Recording the default was a
    # defect: nothing in this pipeline reads it, so it could not detect a change that mattered.
    assert record.provenance.text_search_config.endswith(TEXT_SEARCH_CONFIG)
    assert "portuguese_stem" in record.provenance.text_search_dictionaries
    assert record.arms[0].configuration.strategy == "lexical"
    assert record.arms[0].configuration.k == EVAL_K


def test_the_run_record_reports_every_metric_the_suite_promises(record):
    expected = {f"recall@{depth}" for depth in RECALL_DEPTHS} | {
        f"mrr@{EVAL_K}",
        f"ndcg@{EVAL_K}",
        "value_match@1",
        f"recall@{EVAL_K}:keyword",
        f"recall@{EVAL_K}:natural",
        # Ungated, and in the record all the same (ADR-0010). Listed here because this test's job is
        # "every metric the suite promises", and a metric that is reported but not enumerated
        # anywhere is one an edit can delete without a red build.
        f"precision@{EVAL_K}",
        f"candidates@{EVAL_K}",
    }
    arm = record.arms[0]

    assert set(arm.metrics) == expected
    # `candidates@10` is a count and not a rate — it is the mean size of the returned list, bounded
    # by `k` rather than by 1. The range check is stated per metric rather than loosened for all of
    # them, because "every score is a fraction" is a real property of the other seven and dropping
    # it to accommodate the eighth would stop catching a fraction that isn't one.
    rates = {name: value for name, value in arm.metrics.items() if name != f"candidates@{EVAL_K}"}
    assert all(0.0 <= value <= 1.0 for value in rates.values())
    assert 0.0 <= arm.metrics[f"candidates@{EVAL_K}"] <= EVAL_K
    assert record.sample_count == len(arm.per_item) == len(load_facts())


def test_the_metrics_are_not_six_names_for_one_number(record):
    # What the first fact set failed. Every hit landed at rank 1, so recall, MRR, nDCG and the
    # value rate collapsed onto a single value and the gate had two states per question. Distinct
    # numbers are the evidence that the suite spans more than one outcome.
    assert len(set(record.arms[0].metrics.values())) >= 4


def test_the_two_phrasings_are_measured_separately_and_disagree(record):
    # The headline number averages two populations that behave nothing alike. Reporting only the
    # average is how a retriever that cannot read a sentence looks acceptable.
    metrics = record.arms[0].metrics

    assert metrics[f"recall@{EVAL_K}:keyword"] > metrics[f"recall@{EVAL_K}:natural"]


def test_the_run_record_carries_no_chunk_text(record):
    # ADR-0003: records are committed, and against a real Corpus a record carrying text would be a
    # slow redistribution of the material this repository promises not to hold.
    fields = set(record.arms[0].per_item[0].model_dump())

    assert "text" not in fields and "chunks" not in fields


def test_two_evaluations_of_the_same_artifact_produce_the_same_record(artifact):
    # The determinism assertion. Everything except the clock, the stopwatch and the run identifier
    # must be bit-identical — including the per-item retrieved order, which is where a stray
    # `sorted()` in Python would show up.
    first = run_evaluation(artifact, FIXTURE_CORPUS)
    second = run_evaluation(artifact, FIXTURE_CORPUS)

    assert measurement(first) == measurement(second)
    assert first.run_id != second.run_id or first.started_at == second.started_at


def test_the_suite_is_neither_trivial_nor_impossible(record):
    # Both bounds matter and both have been violated in this file's history. A suite everything
    # passes cannot detect an improvement; a suite nothing passes cannot detect a regression. The
    # gate itself compares against the promoted baseline — this only asserts the suite has room in
    # both directions.
    # **Every arm**, not `arms[0]`. It read the first arm, which is always `lexical`, so `dense` has
    # never been covered since the day it landed — and `dense` has scored 0.894737 from that day,
    # which means it would have failed the old 0.85 ceiling immediately had anything looked. A
    # triviality check that only inspects the weaker arm cannot detect the thing it is for, because
    # the fact set being too easy shows up in the *strongest* arm first.
    #
    # The ceiling moved from 0.85 to 0.95 with ADR-0010, which took `lexical` from 0.447 to 0.868 and
    # pushed it through the old bound. Raising a bound because the code got better is exactly the
    # move this file should be suspicious of, so the reasoning is written down rather than assumed:
    # this is a check for a *fact set shaped to fit the retriever*, which is what happened before #6.
    # `facts_sha256` and `sample_count` are byte-identical across the change, so the questions did not
    # move — the retriever did. The suite is not trivial at 0.868 either: ten of the 76 questions miss
    # entirely and the arm scores 0.571 on natural Portuguese ones. 0.95 still fires on a rewritten
    # question set while no longer firing on a genuine improvement, and it now fires for any arm.
    for arm in record.arms:
        recall = arm.metrics["recall@10"]
        assert 0.15 < recall < 0.95, f"{arm.configuration.strategy}: recall@10 {recall}"


def test_the_committed_baseline_describes_a_run_record_that_is_in_the_tree():
    baseline = load_baseline()
    record = load_run_record(latest_run_record().parent / f"{baseline.run_id}.json")

    assert record.sample_count == baseline.sample_count
    assert record.facts_sha256 == baseline.facts_sha256
    for baselined in baseline.arms:
        arm = record.arm(baselined.configuration.strategy)
        assert arm is not None
        assert arm.metrics == baselined.metrics
        assert arm.configuration == baselined.configuration


def test_the_committed_run_record_belongs_to_this_history():
    # Guards the orphan `latest_run_record` cannot see: a record from an unmerged branch with a
    # later timestamp would otherwise become the thing this build is validated against.
    assert is_ancestor_of_head(load_run_record(latest_run_record()).provenance.git_sha)


def test_the_newest_committed_run_record_still_describes_this_build(record):
    # The same assertion CI makes. A run record is generated and committed with the change that
    # moved it; if this fails locally, regenerate with `python -m garage eval run`.
    committed = load_run_record(latest_run_record())

    assert measurement(committed) == measurement(record)
