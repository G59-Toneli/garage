"""The metrics, the fact set and the comparison — all without a database.

Everything in this file is arithmetic over lists of strings. That is the whole reason the metrics
were written as pure functions: the numbers a build fails on must be checkable by hand, and a test
that needed Postgres to assert that `1 / log2(3)` is 0.63 would be testing psycopg. The parts that
genuinely need a database live in `tests/test_eval_gate.py`.
"""

import json
import math
from collections import Counter

import pytest
from pydantic import ValidationError

from garage.evaluation import (
    Arm,
    Baseline,
    BaselineArm,
    Configuration,
    EvaluationError,
    Fact,
    ItemResult,
    Provenance,
    RunRecord,
    compare,
    facts_digest,
    hit_rank,
    load_facts,
    load_run_record,
    measurement,
    ndcg_at_k,
    promote,
    recall_at_k,
    reciprocal_rank,
    ungated_arms,
    value_matches,
    write_run_record,
)

RETRIEVED = ("a", "b", "c", "d", "e")


@pytest.mark.parametrize(
    "relevant, k, expected",
    [
        # One relevant chunk, found at the top: perfect at every depth.
        (("a",), 1, 1.0),
        (("a",), 5, 1.0),
        # Found, but below the cut: recall at that depth is zero, not "nearly".
        (("c",), 1, 0.0),
        (("c",), 3, 1.0),
        # Two relevant chunks, one inside the window.
        (("a", "z"), 5, 0.5),
        # Nothing retrieved is relevant.
        (("y", "z"), 5, 0.0),
        # More relevant chunks than the window can hold: recall is capped by k, correctly.
        (("a", "b", "c"), 2, 2 / 3),
    ],
)
def test_recall_counts_the_relevant_chunks_inside_the_window(relevant, k, expected):
    assert recall_at_k(RETRIEVED, relevant, k) == pytest.approx(expected)


def test_recall_refuses_a_fact_with_no_relevant_chunks():
    # Undefined, not zero. `Fact` forbids this, and the metric says so rather than dividing.
    with pytest.raises(ValueError):
        recall_at_k(RETRIEVED, (), 5)


@pytest.mark.parametrize(
    "relevant, k, expected",
    [
        (("a",), 5, 1.0),
        (("b",), 5, 0.5),
        (("e",), 5, 0.2),
        # First relevant chunk wins; the second one contributes nothing.
        (("d", "b"), 5, 0.5),
        # Present in the list but outside k: truncated MRR scores this zero.
        (("e",), 3, 0.0),
        (("z",), 5, 0.0),
    ],
)
def test_reciprocal_rank_uses_the_first_relevant_position_and_is_zero_on_a_miss(relevant, k, expected):
    assert reciprocal_rank(RETRIEVED, relevant, k) == pytest.approx(expected)


def test_a_miss_scores_zero_rather_than_being_left_out_of_the_average():
    # The failure this pins: dropping misses would let a retriever that stopped answering the hard
    # question report a *higher* MRR than one that answered it at rank 5.
    answered = [reciprocal_rank(RETRIEVED, ("a",), 5), reciprocal_rank(RETRIEVED, ("e",), 5)]
    lost_one = [reciprocal_rank(RETRIEVED, ("a",), 5), reciprocal_rank(RETRIEVED, ("z",), 5)]

    assert sum(answered) / 2 == pytest.approx(0.6)
    assert sum(lost_one) / 2 == pytest.approx(0.5)


@pytest.mark.parametrize(
    "relevant, k, expected",
    [
        # Ideal ranking: the only relevant chunk is first.
        (("a",), 5, 1.0),
        # One relevant chunk at position 2: DCG = 1/log2(3), IDCG = 1/log2(2) = 1.
        (("b",), 5, 1 / math.log2(3)),
        (("c",), 5, 1 / math.log2(4)),
        # Two relevant chunks, both ideally placed.
        (("a", "b"), 5, 1.0),
        # Two relevant chunks at positions 1 and 3 against an ideal of 1 and 2.
        (("a", "c"), 5, (1 + 1 / math.log2(4)) / (1 + 1 / math.log2(3))),
        (("y", "z"), 5, 0.0),
        # Truncation shrinks the ideal too, so a fact whose second chunk cannot fit is not punished
        # for the window: one relevant chunk found at position 1 out of two, k=1, is still 1.0.
        (("a", "b"), 1, 1.0),
    ],
)
def test_ndcg_discounts_by_position_and_normalises_against_the_ideal(relevant, k, expected):
    assert ndcg_at_k(RETRIEVED, relevant, k) == pytest.approx(expected)


def test_ndcg_returns_zero_by_convention_when_nothing_could_have_been_ideal():
    assert ndcg_at_k(RETRIEVED, (), 5) == 0.0


def test_the_two_binary_gain_formulations_agree():
    # Under binary relevance `rel` and `2**rel - 1` are the same function, which is why the module
    # picks one without comment elsewhere. Asserted once so the claim is not just a docstring.
    relevant = frozenset({"b", "d"})
    graded = sum(
        (2 ** (1 if chunk in relevant else 0) - 1) / math.log2(position + 1)
        for position, chunk in enumerate(RETRIEVED, start=1)
    )
    linear = sum(
        (1 if chunk in relevant else 0) / math.log2(position + 1)
        for position, chunk in enumerate(RETRIEVED, start=1)
    )

    assert graded == pytest.approx(linear)


@pytest.mark.parametrize(
    "relevant, k, expected",
    [(("a",), 5, 1), (("c",), 5, 3), (("c",), 2, None), (("z",), 5, None)],
)
def test_hit_rank_reports_the_first_relevant_position_or_nothing(relevant, k, expected):
    assert hit_rank(RETRIEVED, relevant, k) == expected


@pytest.mark.parametrize(
    "expected_value, texts, tolerance, matched",
    [
        # Numeric, with the tolerance a fact declares.
        ("63", ["Flywheel bolt; Torque (N·m): 63"], 0.5, True),
        ("63", ["Spare wheel inflated to 163 psi"], 0.5, False),
        ("0.18", ["Valve: Intake; Clearance (mm): 0.18"], 0.005, True),
        ("0.18", ["Valve: Exhaust; Clearance (mm): 0.31"], 0.005, False),
        # A comma decimal separator is how the Portuguese half of the Corpus writes it.
        ("3.9", ["Capacidade: 3,9 litros"], 0.05, True),
        # Textual, accent- and case-insensitive: forum Portuguese drops accents constantly.
        ("cabeçote plainado", ["Já vi CABECOTE PLAINADO demais comendo pistão"], None, True),
        ("SAE 20W-50", ["Engine oil, with filter; 3.9; SAE 20W-50"], None, True),
        ("coroa curta", ["a coroa original do GSi é curta demais"], None, False),
    ],
)
def test_value_matching_separates_numbers_from_text(expected_value, texts, tolerance, matched):
    assert value_matches(expected_value, texts, tolerance) is matched


def test_a_tolerance_on_something_that_is_not_a_number_is_a_mistake_not_a_text_match():
    with pytest.raises(ValueError):
        value_matches("SAE 20W-50", ["SAE 20W-50"], 0.5)


def test_a_fact_may_not_name_the_same_chunk_twice():
    with pytest.raises(ValidationError):
        Fact(
            fact_id="x",
            question="q",
            phrasing="keyword",
            expected_value="v",
            chunk_ids=("svc-kadett-1993#0001", "svc-kadett-1993#0001"),
        )


def test_a_fact_must_say_how_it_is_phrased():
    # Not optional and not defaulted: a suite that does not record what it sampled cannot be
    # audited, and the first version of this fact set was 100% keyword without anyone noticing.
    with pytest.raises(ValidationError):
        Fact(fact_id="x", question="q", expected_value="v", chunk_ids=("a#0001",))


def test_a_fact_set_reports_every_bad_line_with_its_number(tmp_path):
    path = tmp_path / "facts.jsonl"
    path.write_text(
        '{"fact_id": "ok", "question": "q", "expected_value": "v", "chunk_ids": ["a#0001"]}\n'
        "not json\n"
        "\n"
        '{"fact_id": "no-chunks", "question": "q", "expected_value": "v", "chunk_ids": []}\n'
        '{"fact_id": "extra", "question": "q", "expected_value": "v", "chunk_ids": ["a#0001"],'
        ' "surprise": 1}\n',
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError) as failure:
        load_facts(path)

    message = str(failure.value)
    # All of them at once, in the manner of `validate_corpus`: repairing a fact set one error per
    # run is the kind of chore that gets fixed by deleting the file.
    assert "line 2" in message and "line 3" in message and "line 4" in message and "line 5" in message


def test_a_blank_line_is_a_broken_fact_set_not_a_skipped_one(tmp_path):
    path = tmp_path / "facts.jsonl"
    path.write_text(
        "\n" + '{"fact_id": "ok", "question": "q", "expected_value": "v", "chunk_ids": ["a#0001"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError, match="line 1: blank"):
        load_facts(path)


def test_the_same_fact_id_twice_is_rejected(tmp_path):
    line = (
        '{"fact_id": "twice", "question": "q", "phrasing": "keyword", "expected_value": "v",'
        ' "chunk_ids": ["a#0001"]}\n'
    )
    path = tmp_path / "facts.jsonl"
    path.write_text(line * 2, encoding="utf-8")

    with pytest.raises(EvaluationError, match="duplicate fact_id: twice"):
        load_facts(path)


def test_the_committed_fact_set_loads_and_covers_every_fixture_document():
    facts = load_facts()
    documents = {chunk_id.split("#")[0] for fact in facts for chunk_id in fact.chunk_ids}

    assert len(facts) >= 25
    # Both strata are populated, and neither dominates. A suite that drifted back to all-keyword
    # would report a high number that means nothing (see `Fact.phrasing`).
    by_phrasing = Counter(fact.phrasing for fact in facts)
    assert by_phrasing["keyword"] >= 20 and by_phrasing["natural"] >= 20
    assert documents == {
        "svc-kadett-1993",
        "owner-kadett-1993",
        "parts-gsi-1994",
        "forum-swap-250s",
        "blog-projetinho-de-rua",
    }


def test_every_committed_fact_names_a_well_formed_chunk_id():
    # `<doc_id>#<ordinal:04d>` is positional and produced by chunking; a hand-written fact set is
    # exactly where a typo in one would go unnoticed until it silently cost recall.
    for fact in load_facts():
        for chunk_id in fact.chunk_ids:
            doc_id, _, ordinal = chunk_id.partition("#")
            assert doc_id and len(ordinal) == 4 and ordinal.isdigit(), chunk_id


def test_the_fact_digest_changes_with_the_file(tmp_path):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    line = '{"fact_id": "one", "question": "q", "expected_value": "v", "chunk_ids": ["a#0001"]}\n'
    first.write_text(line, encoding="utf-8")
    second.write_text(line.replace("one", "two"), encoding="utf-8")

    assert facts_digest(first) != facts_digest(second)


# --------------------------------------------------------------------------------------------
# The comparison. Records are built by hand here because the point is the policy, not the search.
# --------------------------------------------------------------------------------------------

LEXICAL = Configuration(strategy="lexical", k=10, tiers=("A", "B"))
DENSE = Configuration(strategy="dense", k=10, tiers=("A", "B"), embedder="e5-small")
PROVENANCE = Provenance(
    git_sha="0" * 40,
    git_dirty=False,
    corpus_id="fixture",
    corpus_hash="f" * 64,
    ingest_version=1,
    python_version="3.12.13",
    platform="test",
    postgres_version="16.14",
    pg_trgm_version="1.6",
    text_search_config="pg_catalog.portuguese",
    text_search_dictionaries="portuguese_stem, simple",
)


def _item(fact_id, rank):
    return ItemResult(
        fact_id=fact_id,
        question="q",
        expected_chunk_ids=("a#0001",),
        expected_value="v",
        hit_rank=rank,
        reciprocal_rank=0.0 if rank is None else 1 / rank,
        ndcg=0.0,
        value_matched=False,
        retrieved_chunk_ids=(),
    )


def _arm(metrics, *, configuration=LEXICAL, items=()):
    return Arm(configuration=configuration, metrics=metrics, per_item=items)


def _record(*arms, sample_count=2, facts_sha256="a" * 64):
    return RunRecord(
        run_id="20260101T000000Z-000000000000",
        started_at="2026-01-01T00:00:00Z",
        duration_ms=1,
        provenance=PROVENANCE,
        sample_count=sample_count,
        facts_sha256=facts_sha256,
        arms=arms,
    )


def _baseline(*arms, tolerance=0.01, noise_floor=0.0, sample_count=2, facts_sha256="a" * 64):
    return Baseline(
        run_id="20260101T000000Z-000000000000",
        sample_count=sample_count,
        facts_sha256=facts_sha256,
        arms=arms,
        tolerance=tolerance,
        noise_floor=noise_floor,
    )


def _baselined(metrics, *, configuration=LEXICAL, gated=("recall@1", "mrr@10")):
    return BaselineArm(configuration=configuration, metrics=metrics, gated_metrics=gated)


METRICS = {"recall@1": 0.9, "mrr@10": 0.95}


def test_a_run_that_matches_the_baseline_passes():
    assert compare(_baseline(_baselined(METRICS)), _record(_arm(METRICS))).passed


def test_a_loss_inside_the_tolerance_passes_and_a_loss_beyond_it_fails():
    baseline = _baseline(_baselined(METRICS), tolerance=0.01)

    assert compare(baseline, _record(_arm({"recall@1": 0.895, "mrr@10": 0.95}))).passed

    report = compare(baseline, _record(_arm({"recall@1": 0.87, "mrr@10": 0.95})))
    assert not report.passed
    assert "lexical: recall@1 regressed" in report.failures[0]


def test_a_loss_of_exactly_the_tolerance_passes_because_the_rule_is_strictly_greater():
    baseline = _baseline(_baselined(METRICS), tolerance=0.01)

    assert compare(baseline, _record(_arm({"recall@1": 0.89, "mrr@10": 0.95}))).passed


def test_an_improvement_passes_and_asks_to_be_promoted():
    baseline = _baseline(_baselined(METRICS), noise_floor=0.001)

    report = compare(baseline, _record(_arm({"recall@1": 0.94, "mrr@10": 0.95})))

    assert report.passed
    assert any("improved" in note and "Promote" in note for note in report.notes)


def test_an_improvement_below_the_noise_floor_is_not_worth_a_promotion_commit():
    baseline = _baseline(_baselined(METRICS), noise_floor=0.01)

    report = compare(baseline, _record(_arm({"recall@1": 0.905, "mrr@10": 0.95})))

    assert report.passed and not report.notes


def test_a_metric_that_is_ungated_may_regress_without_failing_the_build():
    metrics = METRICS | {"value_match@1": 0.8}
    baseline = _baseline(_baselined(metrics, gated=("recall@1",)))

    assert compare(baseline, _record(_arm(metrics | {"value_match@1": 0.1}))).passed


def test_a_gated_metric_that_disappeared_fails_rather_than_being_skipped():
    report = compare(_baseline(_baselined(METRICS)), _record(_arm({"recall@1": 0.9})))

    assert not report.passed
    assert "lexical: mrr@10 is gated" in report.failures[0]


def test_a_different_configuration_refuses_to_be_compared_rather_than_being_compared_anyway():
    elsewhere = Configuration(strategy="lexical", k=5, tiers=("A", "B"))

    report = compare(
        _baseline(_baselined(METRICS)),
        _record(_arm({"recall@1": 1.0, "mrr@10": 1.0}, configuration=elsewhere)),
    )

    assert not report.passed
    assert "lexical Configuration changed" in report.failures[0]
    # And it did not additionally claim the metrics were fine — comparability comes first.
    assert len(report.failures) == 1


def test_a_changed_fact_set_refuses_to_be_compared():
    report = compare(_baseline(_baselined(METRICS)), _record(_arm(METRICS), facts_sha256="b" * 64))

    assert not report.passed
    assert "fact set changed" in report.failures[0]


def test_losing_questions_fails_even_when_every_metric_went_up():
    baseline = _baseline(_baselined(METRICS), sample_count=40)

    report = compare(baseline, _record(_arm({"recall@1": 1.0, "mrr@10": 1.0}), sample_count=30))

    assert not report.passed
    assert "lost questions" in report.failures[0]


def test_a_regression_names_the_questions_that_moved():
    before = _arm(METRICS, items=(_item("torque-volante-motor", 1), _item("coroa-curta", 2)))
    after = _arm(METRICS, items=(_item("torque-volante-motor", None), _item("coroa-curta", 7)))

    report = compare(_baseline(_baselined(METRICS)), _record(after), _record(before))

    joined = "\n".join(report.notes)
    assert "torque-volante-motor: rank 1 -> none" in joined
    assert "coroa-curta: rank 2 -> 7" in joined


# --------------------------------------------------------------------------------------------
# Arms: the shape issue #7 needs, and the guarantee it buys.
# --------------------------------------------------------------------------------------------


def test_every_arm_of_a_record_shares_one_corpus_and_one_fact_set_by_construction():
    # The reason provenance, sample_count and facts_sha256 live above the arms rather than inside
    # them: two strategies measured against different databases cannot be written down as one
    # record, so "we compared them against different corpora" is not a mistake anyone can make.
    record = _record(_arm(METRICS), _arm(METRICS, configuration=DENSE))

    assert {"provenance", "sample_count", "facts_sha256"} <= set(RunRecord.model_fields)
    assert "provenance" not in Arm.model_fields
    assert "facts_sha256" not in Arm.model_fields
    assert len(record.arms) == 2


def test_a_record_may_not_hold_two_arms_of_the_same_strategy():
    with pytest.raises(ValidationError):
        _record(_arm(METRICS), _arm(METRICS))


def test_each_arm_is_gated_against_its_own_baseline_arm():
    baseline = _baseline(_baselined(METRICS), _baselined(METRICS, configuration=DENSE))

    report = compare(
        baseline,
        _record(_arm(METRICS), _arm({"recall@1": 0.5, "mrr@10": 0.5}, configuration=DENSE)),
    )

    assert not report.passed
    assert all("dense" in failure for failure in report.failures)


def test_a_baseline_arm_the_run_no_longer_measures_fails():
    baseline = _baseline(_baselined(METRICS), _baselined(METRICS, configuration=DENSE))

    report = compare(baseline, _record(_arm(METRICS)))

    assert not report.passed
    assert "baseline holds a dense arm" in report.failures[0]


def test_a_brand_new_arm_is_reported_and_never_fails_the_build():
    # Dense arriving for the first time must not turn CI red. It is reported so a human can look at
    # it and promote it deliberately.
    report = compare(
        _baseline(_baselined(METRICS)), _record(_arm(METRICS), _arm(METRICS, configuration=DENSE))
    )

    assert report.passed
    assert any(note.startswith("new arm dense") for note in report.notes)


def test_a_promoted_arm_gates_nothing_until_someone_says_so(tmp_path):
    runs, baseline_path = tmp_path / "runs", tmp_path / "baseline.json"
    record = _record(_arm(METRICS), _arm(METRICS, configuration=DENSE))
    write_run_record(record, runs)

    promote(record.run_id, runs, baseline_path)

    promoted = Baseline.model_validate_json(baseline_path.read_bytes())
    assert [arm.gated_metrics for arm in promoted.arms] == [(), ()]
    assert ungated_arms(promoted) == ("lexical", "dense")


def test_promotion_copies_the_measurement_and_keeps_the_policy_per_arm(tmp_path):
    runs, baseline_path = tmp_path / "runs", tmp_path / "baseline.json"
    record = _record(_arm({"recall@1": 0.99, "mrr@10": 0.99}))
    write_run_record(record, runs)
    baseline_path.write_bytes(
        _baseline(_baselined({"recall@1": 0.5, "mrr@10": 0.5}), tolerance=0.02, noise_floor=0.003)
        .model_dump_json()
        .encode("utf-8")
    )

    promote(record.run_id, runs, baseline_path)

    promoted = Baseline.model_validate_json(baseline_path.read_bytes())
    assert promoted.arms[0].metrics == record.arms[0].metrics
    assert promoted.arms[0].gated_metrics == ("recall@1", "mrr@10")
    # Policy is a separate decision from the measurement and survives the promotion untouched.
    assert promoted.tolerance == 0.02 and promoted.noise_floor == 0.003


def test_promoting_a_run_that_is_not_in_the_tree_fails(tmp_path):
    with pytest.raises(EvaluationError):
        promote("20260101T000000Z-000000000000", tmp_path / "runs", tmp_path / "baseline.json")


def test_a_baseline_may_not_gate_a_metric_it_does_not_hold():
    with pytest.raises(ValidationError):
        _baselined({"recall@1": 0.9}, gated=("recall@1", "ndcg@10"))


def test_a_record_from_a_future_version_fails_to_load_rather_than_loading_partially(tmp_path):
    payload = json.loads(_record(_arm(METRICS)).model_dump_json())
    payload["run_record_version"] = 99
    path = tmp_path / "future.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationError):
        load_run_record(path)


def test_a_version_1_record_fails_to_load_rather_than_being_read_as_single_arm(tmp_path):
    # The bump happened before anything shipped, deliberately. Had it happened later, this is the
    # error every committed record and the promoted baseline would have started raising.
    payload = json.loads(_record(_arm(METRICS)).model_dump_json())
    payload["run_record_version"] = 1
    path = tmp_path / "old.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationError):
        load_run_record(path)


def test_a_run_record_is_written_as_stable_readable_bytes(tmp_path):
    record = _record(_arm(METRICS))

    first = write_run_record(record, tmp_path).read_bytes()
    second = write_run_record(record, tmp_path).read_bytes()

    assert first == second
    assert first.endswith(b"\n")
    # Indented and key-sorted, because unlike the manifest digest these files are read in diffs.
    assert b'\n  "run_id"' in first


def test_the_measurement_view_excludes_the_clock_the_machine_and_the_commit():
    fast = _record(_arm(METRICS))
    slow = RunRecord.model_validate(
        fast.model_dump()
        | {
            "run_id": "20991231T235959Z-ffffffffffff",
            "started_at": "2099-12-31T23:59:59Z",
            "duration_ms": 999999,
            "provenance": PROVENANCE.model_dump()
            | {"git_sha": "1" * 40, "git_dirty": True, "platform": "other"},
        }
    )

    assert measurement(fast) == measurement(slow)


@pytest.mark.parametrize(
    "field",
    ["postgres_version", "pg_trgm_version", "text_search_config", "text_search_dictionaries"],
)
def test_the_measurement_view_does_not_excuse_a_different_database_engine(field):
    # The ranking is `ts_rank_cd`, the portuguese stemmer and `word_similarity`, all inside
    # Postgres. A server or pg_trgm upgrade moves every number in the file, so unlike the laptop's
    # OS these are part of what was measured, not of who ran it.
    before = _record(_arm(METRICS))
    after = RunRecord.model_validate(
        before.model_dump() | {"provenance": PROVENANCE.model_dump() | {field: "different"}}
    )

    assert measurement(before) != measurement(after)


def test_the_measurement_view_notices_a_different_ranking():
    before = _record(_arm(METRICS, items=(_item("one", 1),)))
    after = _record(_arm(METRICS, items=(_item("one", 2),)))

    assert measurement(before) != measurement(after)


def test_the_measurement_view_notices_an_arm_that_disappeared():
    assert measurement(_record(_arm(METRICS), _arm(METRICS, configuration=DENSE))) != measurement(
        _record(_arm(METRICS))
    )
