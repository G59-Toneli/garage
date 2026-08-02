"""The showcase, tested without a database, without a network and without a model.

The build reaches Postgres in exactly two places — `verify_artifact` and `local_provenance` — and
both are patched here, for the same reason `test_app.py` patches the first: what is under test is
the record's shape, its two ADR-0003 gates and its throttle, none of which are properties of
Postgres. Retrieval quality is measured by the ADR-0004 gate against a real database, and generation
against a real provider is `-m live` and costs money.

The `Generator` used throughout raises if it is asked for something the test did not stage, and one
test uses a generator that raises on *any* call at all. That one is the acceptance criterion written
as an assertion: a curated question must render with zero model calls, and a test that merely
happens not to make one would still pass on the day something starts making them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from garage import app as app_module
from garage import showcase as showcase_module
from garage.app import GeneratedAnswer, create_app
from garage.evaluation import Provenance
from garage.generation import Answer, Citation, Claim, Contract
from garage.ingest import Artifact
from garage.retrieval import Candidate, StoredChunk
from garage.showcase import (
    DISPLAY_RULE,
    VERBATIM_TOKEN_LIMIT,
    Sample,
    ShowcaseArm,
    ShowcaseChunk,
    ShowcaseError,
    ShowcaseQuestion,
    VerbatimLeak,
    build_showcase,
    choose_displayed_sample,
    load_questions,
    load_showcase_record,
    longest_common_run,
    spread_of,
    spreads_from,
    tokens,
    verify_showcase_records,
    write_showcase_record,
)

from test_app import FakeRetriever, candidate, settings  # noqa: F401 — `settings` is a fixture

CORPUS_HASH = "0" * 64
ARTIFACT = Artifact(corpus_id="fixture", corpus_hash=CORPUS_HASH, ingest_version=1)

PROVENANCE = Provenance(
    git_sha="abcdef0123456789",
    git_dirty=False,
    corpus_id="fixture",
    corpus_hash=CORPUS_HASH,
    ingest_version=1,
    python_version="3.12.13",
    platform="test",
    postgres_version="16.0",
    pg_trgm_version="1.6",
    text_search_config="pg_catalog.portuguese",
    text_search_dictionaries="portuguese_stem",
)

QUESTION = ShowcaseQuestion(
    question_id="torque-cabecote",
    question="Qual o torque do cabeçote?",
    why="A pergunta que separa o braço lexical do denso.",
)


class CountingGenerator:
    """A generator that answers with a canned `Answer` and counts how often it was asked.

    `tokens_out` varies per call by default, because the whole point of storing n samples is that
    they are not identical — a fake returning one constant would let a broken `Spread` and a broken
    `displayed_sample` both pass.
    """

    name = "fake"
    model = "fake-1"

    def __init__(self, answers=None):
        self.answers = list(answers) if answers is not None else None
        self.calls = 0

    def generate(self, query, *, context, contract=Contract()):
        self.calls += 1
        if self.answers is not None:
            return self.answers[(self.calls - 1) % len(self.answers)]
        return _answer(tokens_out=90 + (self.calls % 3))


class ExplodingGenerator:
    """A `Generator` that fails the test if anything asks it for anything.

    This is the acceptance criterion, mechanised. "The showcase renders with no model call" is only
    a property if calling a model is an error rather than merely something that did not happen.
    """

    name = "must-not-be-called"
    model = "must-not-be-called"

    def generate(self, query, *, context, contract=Contract()):  # pragma: no cover - it must not run
        raise AssertionError(
            "the showcase screen called a generation provider. Rendering a precomputed record must "
            "cost nothing; something reintroduced a live call."
        )


def _answer(*, text="O torque é 41 N·m.", tokens_out=96, chunk_id="svc-kadett-1993#0001") -> Answer:
    return Answer(
        text=text,
        claims=(Claim(text=text, citations=(Citation(1, chunk_id),)),),
        support="supported",
        provider="fake",
        model="fake-1",
        tokens_in=812,
        tokens_out=tokens_out,
        cost_usd=0.0005,
        cost_estimated=True,
        pricing_as_of="2026-08-01",
    )


@pytest.fixture
def offline(monkeypatch):
    """The two database calls in the build, replaced by the answers a database would give."""
    monkeypatch.setattr(showcase_module, "local_provenance", lambda *_: PROVENANCE)
    import garage.ingest

    monkeypatch.setattr(garage.ingest, "verify_artifact", lambda *_, **__: ARTIFACT)


def build(generator, *, questions=(QUESTION,), n=3, retrievers=None, **kwargs):
    return build_showcase(
        "postgresql://unused",
        Path("corpus/fixture"),
        generator=generator,
        questions=questions,
        retrievers=retrievers or [FakeRetriever([candidate()])],
        scope="test",
        n=n,
        sleep=lambda _: None,
        **kwargs,
    )


# --- the acceptance criterion: zero model calls ---------------------------------------------------


def test_a_curated_question_renders_answer_citations_chunks_trace_and_cost_with_no_model_call(
    offline, monkeypatch, settings, tmp_path  # noqa: F811
):
    """Everything the screen needs, served from disk and from the artifact, with a generator that
    would raise if anything touched it.

    The record is built once with a working fake, then the app is booted with `ExplodingGenerator`
    and every read the screen performs is exercised against it. Nothing here is a proxy for the
    browser's behaviour — these are literally the three requests `showcasescreen.js` issues.
    """
    record = build(CountingGenerator())
    directory = tmp_path / "showcase"
    write_showcase_record(record, directory)

    # Through `Settings`, not by patching a module global: where the records live is deployment
    # configuration, and the endpoints resolve it the same way a container does.
    settings = settings.model_copy(update={"showcase_dir": directory})
    monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
    # `GET /chunks` is the hydration path and the only place text ever comes from. It is a database
    # read, so it is faked here — what matters is that it is *local*, not that it is Postgres.
    monkeypatch.setattr(
        app_module,
        "fetch_chunks",
        lambda url, ids: tuple(
            StoredChunk(
                chunk_id=chunk_id,
                doc_id="svc-kadett-1993",
                doc_title="Manual de Serviço",
                tier="A",
                page=12,
                section="Section 3.2",
                kind="spec",
                text="Torque (N·m): 41",
            )
            for chunk_id in ids
        ),
    )

    from fastapi.testclient import TestClient

    with TestClient(
        create_app(settings, retriever=FakeRetriever([candidate()]), generator=ExplodingGenerator())
    ) as client:
        listing = client.get("/showcase").json()
        assert listing["showcase_ids"] == [record.showcase_id]

        served = client.get(f"/showcase/{record.showcase_id}").json()
        item = served["items"][0]
        arm = item["arms"][0]
        sample = arm["samples"][arm["displayed_sample"]]

        # The answer, its claims and their citations.
        assert sample["answer"]["text"]
        assert sample["answer"]["claims"][0]["citations"][0]["chunk_id"]
        # The trace, in the shape the waterfall reads.
        assert sample["trace"]["name"] == "query"
        assert [child["name"] for child in sample["trace"]["children"]] == ["retrieve", "generate"]
        # The cost.
        assert sample["answer"]["cost_usd"] is not None
        assert sample["answer"]["pricing_as_of"]
        # The chunks — identifiers here, words from the endpoint below.
        ids = [chunk["chunk_id"] for chunk in arm["retrieval"]["chunks"]]
        assert ids

        hydration = client.get("/chunks", params=[("ids", chunk_id) for chunk_id in ids]).json()
        assert [chunk["text"] for chunk in hydration["chunks"]] == ["Torque (N·m): 41"] * len(ids)
        assert hydration["corpus_hash"] == CORPUS_HASH
        assert hydration["missing"] == []


def test_a_missing_chunk_degrades_to_an_identified_absence_rather_than_an_error(
    monkeypatch, settings  # noqa: F811
):
    """A clone without the operator's material must still render. ADR-0003 makes this the normal case."""
    monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
    monkeypatch.setattr(app_module, "fetch_chunks", lambda url, ids: ())

    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, retriever=FakeRetriever([candidate()]))) as client:
        body = client.get("/chunks", params=[("ids", "svc-kadett-1993#0001")])

    # 200 and a named absence, never a 404. The identifier survives; only the words are gone, and
    # the interface says which ones.
    assert body.status_code == 200
    assert body.json()["chunks"] == []
    assert body.json()["missing"] == ["svc-kadett-1993#0001"]


def test_asking_for_more_chunks_than_the_cap_is_refused_rather_than_truncated(
    monkeypatch, settings  # noqa: F811
):
    from garage.retrieval import MAX_CHUNK_IDS

    monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, retriever=FakeRetriever([candidate()]))) as client:
        response = client.get(
            "/chunks", params=[("ids", f"doc#{index:04d}") for index in range(MAX_CHUNK_IDS + 1)]
        )

    # Truncating would hand back a partial hydration indistinguishable from chunks the artifact
    # genuinely lacks, which is the one confusion `missing` exists to prevent.
    assert response.status_code == 422
    assert str(MAX_CHUNK_IDS) in response.json()["detail"]


# --- ADR-0003, part one: no text on disk ------------------------------------------------------------


def test_no_chunk_text_reaches_a_committed_showcase_record(offline, tmp_path):
    """The bytes on disk, read back and searched for the corpus.

    Asserted against the *file* rather than against the model, because the model is what this
    codebase believes it wrote and the file is what git will hold. A `model_dump` that grew a field,
    a hand-edited record, or a future `ShowcaseChunk` with a `text` on it are all caught here and
    none of them are caught by reading `showcase.py`.
    """
    text = "Torque (N·m): 41"
    record = build(CountingGenerator(), retrievers=[FakeRetriever([candidate()])])
    path = write_showcase_record(record, tmp_path)
    written = path.read_text(encoding="utf-8")

    assert text not in written
    # The identifier, by contrast, must be there — that is what makes the record hydratable at all.
    assert "svc-kadett-1993#0001" in written
    payload = json.loads(written)
    for item in payload["items"]:
        for arm in item["arms"]:
            for chunk in arm["retrieval"]["chunks"]:
                assert "text" not in chunk
    assert payload["redistribution"]["chunk_text_stored"] is False


def test_the_chunk_model_refuses_a_text_field_outright():
    """`extra="forbid"` is the enforcement; this is the test that says so is deliberate.

    Without it, the day someone writes `ShowcaseChunk(**vars(candidate))` the corpus goes into git
    and every other test still passes.
    """
    fields = dict(vars(candidate()))
    with pytest.raises(Exception):
        ShowcaseChunk(**fields)
    # And the constructor that is supposed to be used drops it rather than failing.
    assert not hasattr(ShowcaseChunk.of(candidate()), "text")


def test_every_committed_showcase_record_holds_no_corpus_text():
    """The same check, against whatever is actually in `eval/showcase/` in this checkout.

    The test above proves the builder does not write text. This one proves nothing in the repository
    *has* text — including records committed by a future version of the builder, and including one
    edited by hand. It is the check that keeps being true after this file stops being read.
    """
    from garage.corpus import FIXTURE_CORPUS
    from garage.showcase import SHOWCASE_DIR

    records = sorted(SHOWCASE_DIR.glob("*.json")) if SHOWCASE_DIR.is_dir() else []
    if not records:
        pytest.skip("no showcase record is committed in this checkout")

    # Every distinct non-trivial line of the fixture Corpus. Long lines only: "GSi" appears in a
    # question and in a document title and proves nothing, while a whole table row does not occur by
    # coincidence.
    lines = {
        line.strip()
        for source in (FIXTURE_CORPUS / "sources").glob("*.md")
        for line in source.read_text(encoding="utf-8").splitlines()
        if len(line.strip()) > 40
    }
    for path in records:
        written = path.read_text(encoding="utf-8")
        leaked = sorted(line for line in lines if line in written)
        assert leaked == [], f"{path.name} carries corpus text: {leaked[:3]}"


# --- ADR-0003, part two: the verbatim gate ------------------------------------------------------------


def test_the_verbatim_gate_fails_the_build_on_a_synthetic_long_tier_a_chunk(offline):
    """The gate's only real exercise, and the reason it is mandatory.

    On the fixture Corpus this will never fire on its own: the chunks are short invented tables and
    a correct cited answer shares a handful of tokens with them. So the gate would otherwise be
    tested for the first time in production, against real licensed material, which is the worst
    possible moment to discover it does not work.

    The chunk here is a long Tier A paragraph and the "model" copies it whole — exactly the failure
    ADR-0003 exists for, and exactly what a model answering over a scanned manual can produce.
    """
    stolen = (
        "Afrouxe as cinco porcas dos mancais do comando um quarto de volta por vez trabalhando do "
        "mancal cinco para o mancal um e levante o comando esquadrejado sem alavancar contra as "
        "superficies de apoio dos mancais porque qualquer marca ali obriga a substituir o cabecote "
        "inteiro em vez de apenas retificar a superficie de contato"
    )
    long_chunk = candidate()
    object.__setattr__(long_chunk, "text", stolen)
    object.__setattr__(long_chunk, "tier", "A")

    copying = CountingGenerator([_answer(text=stolen, chunk_id=long_chunk.chunk_id)])

    with pytest.raises(VerbatimLeak) as leak:
        build(copying, retrievers=[FakeRetriever([long_chunk])])

    message = str(leak.value)
    # It names the question, so the operator knows which one to rewrite or drop.
    assert QUESTION.question_id in message
    assert long_chunk.chunk_id in message
    assert str(VERBATIM_TOKEN_LIMIT) in message
    # And it fails fast, on the first offending sample, rather than paying for the rest of the run.
    assert copying.calls == 1


def test_the_verbatim_gate_does_not_fire_on_an_ordinary_cited_answer(offline):
    record = build(CountingGenerator())
    assert record.redistribution.worst_verbatim.tokens <= VERBATIM_TOKEN_LIMIT
    # Recorded even when it passed. "The gate ran and saw 3" and "the gate never ran" are different
    # facts, and a record that reported only failures could not tell them apart.
    assert record.redistribution.verbatim_token_limit == VERBATIM_TOKEN_LIMIT


def test_only_tier_a_chunks_that_were_actually_cited_are_compared(offline):
    """Two narrowings, both deliberate, both load-bearing.

    Tier B is a forum post — a different problem with a different answer — and a chunk the claim did
    not cite is a chunk the claim did not copy, however much of it sat in the prompt.
    """
    stolen = " ".join(f"palavra{index}" for index in range(60))
    tier_b = candidate(chunk_id="forum-swap-250s#0003", tier="B")
    object.__setattr__(tier_b, "text", stolen)
    uncited = candidate(chunk_id="svc-kadett-1993#0099")
    object.__setattr__(uncited, "text", stolen)

    # The claim cites the Tier B chunk verbatim, and the Tier A chunk it never mentions holds the
    # same words. Neither is a redistribution of the operator's licensed material.
    copying = CountingGenerator([_answer(text=stolen, chunk_id=tier_b.chunk_id)])
    record = build(copying, retrievers=[FakeRetriever([tier_b, uncited])])
    assert record.redistribution.worst_verbatim.tokens == 0


def test_a_hand_edited_record_over_the_limit_will_not_load(offline, tmp_path):
    record = build(CountingGenerator())
    path = write_showcase_record(record, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["redistribution"]["worst_verbatim"]["tokens"] = payload["redistribution"][
        "verbatim_token_limit"
    ] + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ShowcaseError, match="should never have been written"):
        load_showcase_record(path)


def test_the_longest_common_run_is_contiguous_and_not_a_scattered_subsequence():
    """The documented deviation from the issue text, asserted so it cannot be undone by accident.

    A scattered subsequence is two sentences about the same torque figure sharing articles and
    prepositions, which every correct answer does. A contiguous run is a copy.
    """
    chunk = tokens("aperte o parafuso do cabeçote a quarenta e um newton metro no primeiro estágio")
    scattered = tokens("o parafuso a um metro no estágio")
    assert longest_common_run(scattered, chunk) < 3
    assert longest_common_run(tokens("do cabeçote a quarenta e um"), chunk) == 6
    assert longest_common_run((), chunk) == 0


def test_tokenising_folds_accents_so_a_copy_without_a_cedilla_is_still_a_copy():
    assert tokens("cabeçote") == tokens("cabecote")
    assert longest_common_run(tokens("o cabeçote plainado"), tokens("o cabecote plainado")) == 3


# --- spread, and the absence of a point value ---------------------------------------------------------


def test_the_stored_spread_recomputes_from_the_samples(offline):
    """The stored spread is a convenience for the screen. A convenience allowed to disagree with the
    data it summarises is a defect waiting for a reader to trust it."""
    record = build(CountingGenerator(), n=5)
    for item in record.items:
        for arm in item.arms:
            assert arm.spread == spreads_from(arm.samples)


def test_a_spread_holds_no_mean_and_no_standard_deviation():
    """ADR-0004 made structural: the interface cannot draw an error bar because there is nothing to
    draw one from. Asserted on the serialized keys, which is what a renderer actually reads."""
    keys = set(spread_of([1.0, 2.0, 3.0]).model_dump().keys())
    assert keys == {"values", "n", "minimum", "maximum", "distinct"}
    assert not keys & {"mean", "average", "stddev", "std", "sigma", "variance", "median"}


def test_a_spread_keeps_nulls_rather_than_shortening_itself():
    # `cost_usd` is null for a model with no published price and for a call that never happened. A
    # spread that dropped them would report n=2 for three samples with nothing on screen saying so.
    spread = spread_of([0.5, None, 1.5])
    assert spread.n == 3 and spread.values == (0.5, None, 1.5)
    assert (spread.minimum, spread.maximum, spread.distinct) == (0.5, 1.5, 2)


def test_an_all_null_spread_has_no_minimum_and_no_maximum():
    spread = spread_of([None, None])
    assert (spread.minimum, spread.maximum, spread.distinct) == (None, None, 0)


def test_the_displayed_sample_is_the_median_by_tokens_out_and_ties_go_to_the_lowest_index():
    def sample(index, tokens_out):
        return Sample(index=index, answer=GeneratedAnswer.of(_answer(tokens_out=tokens_out)), trace={})

    # Odd n: the middle of the sorted order, not the largest and not the first.
    assert choose_displayed_sample([sample(0, 300), sample(1, 100), sample(2, 200)]) == 2
    # Even n: the lower median, as `DISPLAY_RULE` states.
    assert choose_displayed_sample([sample(0, 40), sample(1, 10), sample(2, 30), sample(3, 20)]) == 3
    # A tie goes to the lowest index, so the rule is total and the choice is reproducible.
    assert choose_displayed_sample([sample(0, 50), sample(1, 50), sample(2, 50)]) == 1
    with pytest.raises(ShowcaseError):
        choose_displayed_sample([])


def test_the_rule_that_chose_the_displayed_sample_travels_in_the_record(offline):
    record = build(CountingGenerator())
    assert record.displayed_sample_rule == DISPLAY_RULE
    assert "median" in record.displayed_sample_rule


# --- the build ---------------------------------------------------------------------------------------


def test_the_build_throttles_between_provider_calls_and_not_before_the_first(offline):
    """Without a pause the build eats its own 429s and records degradations as though they were
    results — the most misleading thing this file could contain. With a pause before the *first*
    call, the smallest possible run costs six seconds for nothing."""
    slept: list[float] = []
    generator = CountingGenerator()
    build_showcase(
        "postgresql://unused",
        Path("corpus/fixture"),
        generator=generator,
        questions=(QUESTION,),
        retrievers=[FakeRetriever([candidate()])],
        scope="test",
        n=4,
        throttle_seconds=6.0,
        sleep=slept.append,
    )
    assert generator.calls == 4
    # Three gaps between four calls. Never four.
    assert slept == [6.0, 6.0, 6.0]


def test_the_throttle_spans_arms_and_questions_because_the_rate_limit_does(offline):
    slept: list[float] = []
    second = ShowcaseQuestion(question_id="segunda", question="E a folga?", why="outra")
    left, right = FakeRetriever([candidate()]), FakeRetriever([candidate()])
    # Distinct names because the record forbids two arms of one strategy in an item — a rule that
    # exists so `strategy` alone identifies a column, and that this test tripped over on the way in.
    left.name, right.name = "lexical", "dense"
    build_showcase(
        "postgresql://unused",
        Path("corpus/fixture"),
        generator=CountingGenerator(),
        questions=(QUESTION, second),
        retrievers=[left, right],
        scope="test",
        n=1,
        throttle_seconds=2.5,
        sleep=slept.append,
    )
    # Two questions x two arms x one sample = four calls, three gaps. The provider's quota does not
    # reset because the build moved on to a different question.
    assert slept == [2.5, 2.5, 2.5]


def test_a_zero_cost_abstention_consumes_neither_quota_nor_a_pause(offline):
    """The retriever came back empty, so `app._answer` refused without asking anybody.

    Nothing was billed, so nothing needs protecting from a rate limit — and sleeping six seconds
    anyway would make the arm that behaves *best* the slowest part of the build. The sample is still
    recorded, because a correct refusal is a result and is exactly what this question is curated to
    show.
    """
    slept: list[float] = []
    generator = CountingGenerator()
    record = build_showcase(
        "postgresql://unused",
        Path("corpus/fixture"),
        generator=generator,
        questions=(QUESTION,),
        retrievers=[FakeRetriever([])],
        scope="test",
        n=3,
        throttle_seconds=6.0,
        sleep=slept.append,
    )
    assert generator.calls == 0
    assert slept == []
    arm = record.items[0].arms[0]
    assert len(arm.samples) == 3
    assert arm.samples[0].answer.abstained is True
    assert arm.samples[0].answer.provider is None
    assert arm.samples[0].answer.cost_usd is None
    # No `generate` span at all, so the latency spread is all nulls rather than a column of zeros.
    assert arm.spread["generate_ms"].values == (None, None, None)


def test_the_record_holds_one_ranking_per_arm_and_n_samples_under_it(offline):
    record = build(CountingGenerator(), n=3)
    arm = record.items[0].arms[0]
    assert len(arm.samples) == 3
    assert [sample.index for sample in arm.samples] == [0, 1, 2]
    assert arm.retrieval.chunks[0].chunk_id == "svc-kadett-1993#0001"
    assert record.sampling.n == 3
    assert record.sampling.generator == "fake" and record.sampling.model == "fake-1"
    assert record.sampling.temperature == 0.0
    assert record.layer == "showcase" and record.scope == "test"


def test_a_retriever_that_is_not_deterministic_stops_the_build(offline):
    """The premise behind storing one ranking for n samples, checked instead of assumed."""

    class Wobbling(FakeRetriever):
        def retrieve(self, query, *, k=10, filters=None):
            self.calls.append((query, k, filters))
            return (candidate(chunk_id=f"svc-kadett-1993#{len(self.calls):04d}"),)

    with pytest.raises(ShowcaseError, match="not deterministic"):
        build(CountingGenerator(), retrievers=[Wobbling()], n=2)


def test_the_showcase_id_has_the_same_shape_as_a_run_id(offline):
    record = build(CountingGenerator())
    stamp, sha = record.showcase_id.split("-")
    assert len(stamp) == 16 and stamp.endswith("Z")
    assert sha == PROVENANCE.git_sha[:12]


def test_the_record_refuses_to_claim_a_sampling_n_its_arms_do_not_hold(offline, tmp_path):
    """`sampling` is a promise made once at the top of the file about every arm underneath it.

    Unchecked it would be a claim rather than a fact about the contents, and a reader comparing two
    items drawn a different number of times would have no way to see it. Asserted by editing the
    written record, because that is the only way the two can actually disagree.
    """
    from garage.showcase import ShowcaseRecord

    record = build(CountingGenerator(), n=2)
    payload = json.loads(write_showcase_record(record, tmp_path).read_text(encoding="utf-8"))
    payload["sampling"]["n"] = 7

    with pytest.raises(Exception, match="sampling.n is 7"):
        ShowcaseRecord.model_validate(payload)


# --- the boot gate ---------------------------------------------------------------------------------


def test_a_stale_corpus_hash_invalidates_the_record_loudly(offline, tmp_path):
    record = build(CountingGenerator())
    write_showcase_record(record, tmp_path)

    # The same hash it was built against: silence, and the ids come back.
    assert verify_showcase_records(CORPUS_HASH, tmp_path) == (record.showcase_id,)

    with pytest.raises(ShowcaseError) as refusal:
        verify_showcase_records("f" * 64, tmp_path)

    message = str(refusal.value)
    assert record.showcase_id in message
    assert CORPUS_HASH in message and "f" * 64 in message
    # And it says what to do about it, because a refusal that only says no costs more time than it
    # saves.
    assert "showcase build" in message


def test_the_service_refuses_to_boot_against_a_record_built_on_another_corpus(
    offline, monkeypatch, settings, tmp_path  # noqa: F811
):
    """The boot gate, at the layer that decides whether to serve. A per-request check would be a
    service willing to hold a stale record as long as nobody opened that page."""
    record = build(CountingGenerator())
    directory = tmp_path / "showcase"
    write_showcase_record(record, directory)
    settings = settings.model_copy(update={"showcase_dir": directory})
    monkeypatch.setattr(
        app_module,
        "verify_artifact",
        lambda *_: Artifact(corpus_id="fixture", corpus_hash="9" * 64, ingest_version=1),
    )

    from fastapi.testclient import TestClient

    with pytest.raises(ShowcaseError):
        with TestClient(create_app(settings, retriever=FakeRetriever([candidate()]))):
            pass  # pragma: no cover - the lifespan raises before the body runs


def test_where_the_records_live_is_deployment_configuration(offline, monkeypatch, settings, tmp_path):  # noqa: F811
    """`GARAGE_SHOWCASE_DIR` decides what a container serves, exactly as `GARAGE_CORPUS_DIR` does.

    A constant would have made the endpoints serve whatever is committed in the checkout the image
    was built from — and it would have made every test of the query endpoint depend on that too,
    which is how this seam was found.
    """
    from garage.config import Settings

    # Null means the repository's own directory, which is what a developer running from a checkout
    # gets and what the default has to be.
    assert Settings(database_url="postgresql://u:p@d/g", gemini_api_key=None).showcase_dir is None

    record = build(CountingGenerator())
    elsewhere = tmp_path / "elsewhere"
    write_showcase_record(record, elsewhere)
    monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)

    from fastapi.testclient import TestClient

    with TestClient(
        create_app(
            settings.model_copy(update={"showcase_dir": elsewhere}),
            retriever=FakeRetriever([candidate()]),
        )
    ) as client:
        assert client.get("/showcase").json()["showcase_ids"] == [record.showcase_id]

    # And the fixture's own empty directory serves nothing, rather than falling back to the
    # repository's — a fallback would make the setting advisory.
    with TestClient(create_app(settings, retriever=FakeRetriever([candidate()]))) as client:
        assert client.get("/showcase").json()["showcase_ids"] == []


def test_an_unknown_showcase_record_is_a_404_and_never_a_path_traversal(
    monkeypatch, settings  # noqa: F811
):
    monkeypatch.setattr(app_module, "verify_artifact", lambda *_: ARTIFACT)
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, retriever=FakeRetriever([candidate()]))) as client:
        assert client.get("/showcase/nao-existe").status_code == 404
        assert client.get("/showcase/..%2F..%2Fbaseline").status_code == 404


# --- the curated questions -------------------------------------------------------------------------


def test_the_committed_question_set_loads_and_every_question_says_why_it_is_there():
    questions = load_questions()
    assert len(questions) >= 1
    for question in questions:
        # `why` is required by the model; this asserts the committed file actually says something,
        # because a one-character `why` would satisfy the schema and nothing else.
        assert len(question.why) > 40
    assert len({question.question_id for question in questions}) == len(questions)


def test_every_committed_record_says_the_same_why_as_the_committed_question_set():
    """The editorial text on screen must agree with the curated set it came from.

    This is a real defect caught in a browser, not a hypothetical. The first record built here
    carried a `why` claiming the dense arm closes the Portuguese-phrasing gap — and the *measurement
    inside that same record* showed dense returning ten Tier B chunks and abstaining. The question
    set was corrected; the record kept asserting the disproved claim, on screen, above the evidence
    against it.

    `why` is stored in the record rather than looked up, and that is right: a record must be
    readable on its own. This is what keeps the copy from rotting. The two ways to satisfy it are
    both fine and one of them is free — edit the record's editorial text, or rebuild it — and only
    the second one costs money, which is why the copy is allowed to be edited in place at all.
    """
    from garage.showcase import SHOWCASE_DIR

    records = sorted(SHOWCASE_DIR.glob("*.json")) if SHOWCASE_DIR.is_dir() else []
    if not records:
        pytest.skip("no showcase record is committed in this checkout")

    curated = {question.question_id: question.why for question in load_questions()}
    for path in records:
        record = load_showcase_record(path)
        for item in record.items:
            assert item.question_id in curated, (
                f"{path.name} shows question {item.question_id!r}, which is no longer curated. "
                "Either restore it to eval/showcase/questions.jsonl or delete the record."
            )
            assert item.why == curated[item.question_id], (
                f"{path.name} explains {item.question_id!r} differently from "
                "eval/showcase/questions.jsonl. Copy the corrected text into the record (free) or "
                "rebuild it (paid) — but the demo must not argue against its own measurement."
            )


def test_a_duplicate_question_id_is_reported_rather_than_silently_kept(tmp_path):
    path = tmp_path / "questions.jsonl"
    line = '{"question_id": "a", "question": "q", "why": "porque"}'
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(ShowcaseError, match="duplicate question_id"):
        load_questions(path)


def test_every_bad_line_in_a_question_set_is_reported_at_once(tmp_path):
    path = tmp_path / "questions.jsonl"
    path.write_text('{"question_id": "a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ShowcaseError) as failure:
        load_questions(path)
    assert "line 1" in str(failure.value) and "line 2" in str(failure.value)


def test_an_arm_whose_displayed_sample_does_not_exist_will_not_validate():
    sample = Sample(index=0, answer=GeneratedAnswer.of(_answer()), trace={})
    with pytest.raises(Exception, match="displayed_sample"):
        ShowcaseArm(
            strategy="lexical",
            embedder=None,
            k=10,
            tiers=("A", "B"),
            contract="cited",
            retrieval={"chunks": ()},
            samples=(sample,),
            spread=spreads_from([sample]),
            displayed_sample=3,
        )
