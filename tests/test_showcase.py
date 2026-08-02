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
    VERBATIM_SUBSEQUENCE_LIMIT,
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
    longest_common_subsequence,
    spread_of,
    spreads_from,
    tokens,
    SHOWCASE_IDENTITY_FIELDS,
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

# What the boot gate compares against, built from `PROVENANCE` rather than typed out beside it:
# these two must agree field for field in the passing case, and a second literal is how they would
# stop agreeing. `showcase.artifact_identity` builds the real one off the live database.
IDENTITY = {name: getattr(PROVENANCE, name) for name in SHOWCASE_IDENTITY_FIELDS}


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
    # Boot now also reads the live text search configuration to build the artifact identity the
    # showcase gate compares against. Stubbed with the same values `PROVENANCE` carries, so a record
    # built by `offline` matches this build exactly, which is the passing case these tests need.
    monkeypatch.setattr(app_module, "artifact_identity", lambda *_: IDENTITY)
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
    # Boot now also reads the live text search configuration to build the artifact identity the
    # showcase gate compares against. Stubbed with the same values `PROVENANCE` carries, so a record
    # built by `offline` matches this build exactly, which is the passing case these tests need.
    monkeypatch.setattr(app_module, "artifact_identity", lambda *_: IDENTITY)
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
    # Boot now also reads the live text search configuration to build the artifact identity the
    # showcase gate compares against. Stubbed with the same values `PROVENANCE` carries, so a record
    # built by `offline` matches this build exactly, which is the passing case these tests need.
    monkeypatch.setattr(app_module, "artifact_identity", lambda *_: IDENTITY)
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


# How long a shared run of tokens stops being a coincidence. Seven, and the number was swept rather
# than guessed — the whole point of this guard is that the last one was chosen by eye and had a
# blind spot. Against the five fixture sources, the manifest, and the committed record:
#
#   n    exempt (also in manifest)   false positives   catches `Section 3.2 — Cylinder head,
#                                    on the record     tightening specifications` (7 tokens folded)
#   5            16                        15                        yes
#   6            11                         0                        yes
#   7             8                         0                        yes
#   8             5                         0                        no
#   12            0                         0                        no
#
# Twelve — the first choice — was too loose in the direction that matters: it cannot see a short
# Tier A heading, which is exactly the string a real service manual's `section` will hold. Five is
# too strict; it flags `doc_title` against its own document's H1 beyond what the manifest exempts.
# Six and seven are both clean, and seven is taken for the extra token of headroom over the
# strictest clean value.
#
# Note the `exempt` column: at twelve it is zero, so the manifest subtraction was inert and
# `doc_title` was passing on length alone. At seven it removes eight real n-grams and is doing work.
LEAK_NGRAM = 7


def _ngrams(text: str, n: int = LEAK_NGRAM) -> set[tuple[str, ...]]:
    words = tokens(text)
    return {tuple(words[start : start + n]) for start in range(len(words) - n + 1)}


def _strings(node) -> list[tuple[str, str]]:
    """Every string in a parsed JSON document, with the path that reaches it."""

    def walk(value, path):
        if isinstance(value, str):
            yield (path, value)
        elif isinstance(value, dict):
            for key, child in value.items():
                yield from walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, f"{path}[{index}]")

    return list(walk(node, "$"))


def test_no_ngram_of_any_source_document_reaches_a_committed_record():
    """The guard that actually holds ADR-0003, replacing one that had a demonstrated blind spot.

    The test this replaces matched whole markdown **lines** against the record. It never fired,
    because a heading is stored without its `## ` prefix and `line in written` was therefore false
    every time — sixteen fields of the committed record were carrying twelve-token runs of the
    fixture while the suite stayed green. `section` was the field, `chunking` sets it from
    `heading.group(2)`, and no gate in this module looked at it: the verbatim gate reads
    `claim.text` against cited Tier A chunks, and `extra="forbid"` only catches fields nobody
    enumerated.

    So this one knows nothing about fields, lines or prefixes. It tokenises every string in the
    record and rejects any `LEAK_NGRAM`-token run that appears in a source document.

    **Minus the manifest.** `doc_title` is the manifest's own `title`, which is committed by hand as
    the catalogue entry ADR-0002 says a Corpus is, and several titles are also the first heading of
    their document. Subtracting the manifest's own n-grams is what distinguishes "this text is
    already in git on purpose" from "this text escaped", and it is the only exemption.

    **Every committed retrieval artifact, not only the showcase directory.** `docs/examples/` arrived
    with ADR-0010 and holds a captured retrieval response — same provenance, same risk, and it was
    protected only by `set(chunk) & set(_SOURCE_TEXT_FIELDS)`, which is a field list. A field list is
    precisely what got `section` wrong and precisely why this test exists. Guarding a *new* artifact
    with the weak check while the strong one sat one glob away was the same mistake at one remove, so
    the glob covers both directories and will cover the next one by being the place that enumerates
    them.
    """
    from garage.capture import CAPTURE_PATH
    from garage.corpus import FIXTURE_CORPUS
    from garage.showcase import SHOWCASE_DIR

    records = sorted(SHOWCASE_DIR.glob("*.json")) if SHOWCASE_DIR.is_dir() else []
    records += sorted(CAPTURE_PATH.parent.glob("*.json")) if CAPTURE_PATH.parent.is_dir() else []
    if not records:
        pytest.skip("no committed record in this checkout")
    # The captured example is the artifact this test was widened for, so its presence is asserted
    # rather than left to a glob that would silently cover nothing if the file were renamed.
    assert CAPTURE_PATH in records

    from_sources: set[tuple[str, ...]] = set()
    for source in sorted((FIXTURE_CORPUS / "sources").glob("*.md")):
        from_sources |= _ngrams(source.read_text(encoding="utf-8"))
    forbidden = from_sources - _ngrams((FIXTURE_CORPUS / "manifest.yaml").read_text(encoding="utf-8"))
    assert forbidden, "the fixture is too small to make this check mean anything"

    leaks: list[str] = []
    for path in records:
        for where, value in _strings(json.loads(path.read_text(encoding="utf-8"))):
            shared = _ngrams(value) & forbidden
            if shared:
                leaks.append(f"{path.name}{where}: {' '.join(sorted(shared)[0])!r}")
    assert leaks == [], (
        f"{len(leaks)} committed field(s) carry a {LEAK_NGRAM}-token run of a source document "
        f"(ADR-0003):\n  " + "\n  ".join(leaks[:8])
    )


def test_the_ngram_guard_catches_the_leak_that_the_line_based_one_missed():
    """The guard, proved against the exact defect it was written for.

    A guard that has only ever passed is a guard nobody can trust, and the one this replaces passed
    for its whole life while sixteen fields leaked. So this reconstructs the leaks that were really
    on disk, plus the one that would matter on a real Corpus, and asserts all three are rejected —
    and that the manifest exemption is genuinely load-bearing rather than decoration.
    """
    from garage.corpus import FIXTURE_CORPUS

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((FIXTURE_CORPUS / "sources").glob("*.md"))
    )
    manifest = (FIXTURE_CORPUS / "manifest.yaml").read_text(encoding="utf-8")
    forbidden = _ngrams(sources) - _ngrams(manifest)

    # 1. The short Tier A heading. This is the one that matters on the day #10 lands — a real
    #    manual's `section` looks exactly like this — and it is the case an n of twelve could not
    #    see, because folded it is only seven tokens long.
    tier_a_heading = "Section 3.2 — Cylinder head, tightening specifications"
    assert f"## {tier_a_heading}" in sources
    assert _ngrams(tier_a_heading) & forbidden, "the short heading must be caught"

    # 2. What the old check could not see, spelled out: the record stores a heading without its
    #    markdown prefix, so matching whole lines never matched anything.
    lines = {line.strip() for line in sources.splitlines() if len(line.strip()) > 40}
    assert tier_a_heading not in lines

    # 3. A whole numbered step — prose rather than a heading.
    step = "Loosen the five camshaft bearing cap nuts a quarter turn at a time, working from cap 5 to cap 1."
    assert _ngrams(step) & forbidden

    # 4. And the exemption is real work, not decoration. This `doc_title` is a manifest entry *and*
    #    its document's own first heading, so without the subtraction it would be a false positive.
    catalogued = "Catálogo de Peças — Kadett / Ipanema, grupo 12 e 18"
    assert _ngrams(catalogued) & _ngrams(sources), "it is in a document, verbatim"
    assert not (_ngrams(catalogued) & forbidden), "and it is exempt, because it is in the manifest"


def test_the_record_stores_no_section_because_a_section_is_the_document_s_own_heading(offline):
    """`section` comes from `chunking.heading.group(2)`. It is the operator's prose, and it left.

    `doc_title` stays, and the pair is the whole rule: the manifest is a catalogue somebody wrote and
    committed, the headings are the document.
    """
    from garage.showcase import _SOURCE_TEXT_FIELDS

    assert set(_SOURCE_TEXT_FIELDS) == {"text", "section"}
    assert "section" not in set(ShowcaseChunk.model_fields)
    assert "doc_title" in set(ShowcaseChunk.model_fields)

    sectioned = candidate()
    assert sectioned.section, "the fixture candidate must carry one, or this proves nothing"
    assert not hasattr(ShowcaseChunk.of(sectioned), "section")

    record = build(CountingGenerator(), retrievers=[FakeRetriever([sectioned])])
    written = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    assert sectioned.section not in written


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


def test_the_two_measures_measure_different_things():
    chunk = tokens("aperte o parafuso do cabeçote a quarenta e um newton metro no primeiro estágio")
    scattered = tokens("o parafuso a um metro no estágio")

    # Contiguous: a scattered match is worth almost nothing.
    assert longest_common_run(scattered, chunk) < 3
    assert longest_common_run(tokens("do cabeçote a quarenta e um"), chunk) == 6
    assert longest_common_run((), chunk) == 0

    # In order with gaps: the same scattered match is worth all of it.
    assert longest_common_subsequence(scattered, chunk) == len(scattered)
    assert longest_common_subsequence((), chunk) == 0
    # And the subsequence is never shorter than the run, by construction.
    assert longest_common_subsequence(scattered, chunk) >= longest_common_run(scattered, chunk)


def test_the_subsequence_gate_catches_the_paragraph_a_run_gate_lets_through(offline):
    """The hole the QA round found, closed and pinned.

    A contiguous-run gate alone is evadable with one edit. Copy a Tier A paragraph in order and drop
    a linking word in every twenty tokens — "ou seja", "segundo o manual", which is *ordinary* model
    behaviour over a manual rather than an attack — and the run never exceeds twenty while the whole
    paragraph is redistributed word for word, in order.

    This asserts both halves: that the run measure genuinely does not fire, and that the build fails
    anyway.
    """
    paragraph = " ".join(f"palavra{index}" for index in range(44))
    padded = []
    for index, word in enumerate(paragraph.split()):
        if index and index % 20 == 0:
            padded.append("ou seja")
        padded.append(word)
    evasive = " ".join(padded)

    chunk = candidate()
    object.__setattr__(chunk, "text", paragraph)
    object.__setattr__(chunk, "tier", "A")

    # The run measure, on its own, does not fire. This is the defect, stated as a measurement.
    assert longest_common_run(tokens(evasive), tokens(paragraph)) <= VERBATIM_TOKEN_LIMIT
    # The subsequence measure recovers the entire paragraph.
    assert longest_common_subsequence(tokens(evasive), tokens(paragraph)) == 44

    copying = CountingGenerator([_answer(text=evasive, chunk_id=chunk.chunk_id)])
    with pytest.raises(VerbatimLeak) as leak:
        build(copying, retrievers=[FakeRetriever([chunk])])

    message = str(leak.value)
    assert "in order, gaps allowed" in message
    assert QUESTION.question_id in message
    # It says *which* measure fired, because a long run is a quotation and a long subsequence with a
    # short run is a paraphrase-shaped copy, and the two have different fixes.
    assert "consecutive tokens" not in message
    assert copying.calls == 1


def test_both_worst_values_are_recorded_even_when_neither_gate_fired(offline):
    record = build(CountingGenerator())
    redistribution = record.redistribution
    assert redistribution.verbatim_token_limit == VERBATIM_TOKEN_LIMIT
    assert redistribution.verbatim_subsequence_limit == VERBATIM_SUBSEQUENCE_LIMIT
    # A threshold with no observed value beside it is a policy nobody can calibrate.
    assert redistribution.worst_verbatim.tokens >= 0
    assert redistribution.worst_verbatim_subsequence.tokens >= 0
    assert redistribution.worst_verbatim_subsequence.tokens >= redistribution.worst_verbatim.tokens


def test_the_worst_of_each_measure_is_kept_separately_not_the_worst_answer_s_pair():
    """Per measure, not per answer. The answer with the longest run is frequently not the one with
    the longest subsequence, and keeping one answer's pair discards the other's worse half — which
    is the half somebody calibrating a threshold needs."""
    from garage.showcase import VerbatimFinding, VerbatimReading

    quoting = VerbatimReading(
        run=VerbatimFinding(tokens=18, question_id="a", chunk_id="c1"),
        subsequence=VerbatimFinding(tokens=18, question_id="a", chunk_id="c1"),
    )
    paraphrasing = VerbatimReading(
        run=VerbatimFinding(tokens=4, question_id="b", chunk_id="c2"),
        subsequence=VerbatimFinding(tokens=40, question_id="b", chunk_id="c2"),
    )
    merged = quoting.merge(paraphrasing)
    assert (merged.run.tokens, merged.run.question_id) == (18, "a")
    assert (merged.subsequence.tokens, merged.subsequence.question_id) == (40, "b")


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


def test_a_dirty_tree_is_refused_because_showcase_id_promises_the_sha_identifies_the_build(
    monkeypatch,
):
    """The promise `showcase_id` makes, enforced rather than imitated.

    `<timestamp>-<git_sha[:12]>` is `run_id`'s format and `run_id` keeps its guarantee. Built from a
    dirty tree the sha names the last commit and the code that produced the numbers exists nowhere,
    so the id looks like a `run_id` without being one.

    `eval run` only warns about this, and the asymmetry is deliberate rather than an inconsistency:
    a run record is regenerated by one free local command, and this one costs 160 provider calls and
    is then cited for months. So the default is a refusal, `--allow-dirty` is the deliberate
    exception, and the record carries `git_dirty` either way — which the screen reads and prints
    beside the id.
    """
    import garage.ingest

    monkeypatch.setattr(garage.ingest, "verify_artifact", lambda *_, **__: ARTIFACT)
    dirty = PROVENANCE.model_copy(update={"git_dirty": True})
    monkeypatch.setattr(showcase_module, "local_provenance", lambda *_: dirty)

    generator = CountingGenerator()
    with pytest.raises(ShowcaseError, match="working tree is dirty"):
        build(generator)
    # Refused before a single call, so a mistake costs nothing.
    assert generator.calls == 0

    record = build(CountingGenerator(), allow_dirty=True)
    # Not hidden once allowed: the record says it, and that is what the banner renders.
    assert record.provenance.git_dirty is True


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

    # The same artifact it was built against: silence, and the ids come back.
    assert verify_showcase_records(IDENTITY, tmp_path) == (record.showcase_id,)

    with pytest.raises(ShowcaseError) as refusal:
        verify_showcase_records({**IDENTITY, "corpus_hash": "f" * 64}, tmp_path)

    message = str(refusal.value)
    assert record.showcase_id in message
    assert CORPUS_HASH in message and "f" * 64 in message
    # And it says what to do about it, because a refusal that only says no costs more time than it
    # saves.
    assert "showcase build" in message


@pytest.mark.parametrize(
    "field, wrong",
    [
        ("ingest_version", 99),
        ("text_search_config", "public.something_else"),
        ("text_search_dictionaries", "portuguese_stem, simple"),
    ],
)
def test_every_identity_field_and_not_only_the_hash_invalidates_the_record(
    offline, tmp_path, field, wrong
):
    """The three fields the gate did not check, and the failure that proved it had to.

    ADR-0010 changed the text search configuration and the query shape. Not one document moved, so
    `corpus_hash` was identical and the committed record booted — and `POST /query` then served its
    recorded `chunks: [], abstained: true` for the curated question while the same question with one
    extra space fell through to live retrieval and came back with ten chunks. One process, one
    artifact, two contradictory answers, and the wrong one was the one the comparison screen
    published. The provenance panel read `ingest_version: 1` beside a `GET /provenance` reading `2`.

    Parametrised over each field on its own so the test says *which* field is load bearing rather
    than passing because some other one happened to differ too. `ingest_version` is the one that
    would have caught the real incident; the two text fields catch the class one step out, where the
    chunks are byte-identical and only the stemmer behind them moved.
    """
    record = build(CountingGenerator())
    write_showcase_record(record, tmp_path)

    with pytest.raises(ShowcaseError) as refusal:
        verify_showcase_records({**IDENTITY, field: wrong}, tmp_path)

    message = str(refusal.value)
    # The refusal names the field, both values and the way out. An operator reading it should not
    # have to diff two JSON files to learn what happened.
    assert field in message
    assert str(wrong) in message and str(getattr(PROVENANCE, field)) in message
    assert "showcase build" in message


def test_the_identity_deliberately_excludes_the_commit_that_contains_the_record():
    """A record cannot name the commit it is committed in, so `git_sha` is not comparable.

    Stated as a test because the list is tempting to "complete": every other `Provenance` field is
    in it, and adding these two would make every committed record permanently stale by construction
    — the sha in the file is the sha *before* the file existed. Issue #6 settled this for the run
    record and the reasoning is identical here.
    """
    assert "git_sha" not in SHOWCASE_IDENTITY_FIELDS
    assert "git_dirty" not in SHOWCASE_IDENTITY_FIELDS
    # And the ones that are in it are all real `Provenance` fields, so a typo is a failure here
    # rather than a `getattr` raising at boot in front of an operator.
    for name in SHOWCASE_IDENTITY_FIELDS:
        assert name in Provenance.model_fields


def test_a_record_that_fails_the_identity_is_not_served_as_a_precomputed_answer(offline, tmp_path):
    """The per-request half, which is the one that actually reached a reader.

    The boot gate refuses the whole process. This is the window it does not cover: a record dropped
    into the directory of a service already running. It compared `corpus_hash` alone, through a
    second hand-written comparison, which is exactly how the two checks came to have different
    widths. Both now go through `record_diverges`.
    """
    from garage.showcase import find_precomputed, precomputed_index, record_diverges

    record = build(CountingGenerator())
    write_showcase_record(record, tmp_path)
    index = precomputed_index(tmp_path)
    item = record.items[0]
    arm = item.arms[0]
    ask = dict(
        question=item.question,
        strategy=arm.strategy,
        k=arm.k,
        tiers=arm.tiers,
        contract=arm.contract,
    )

    assert find_precomputed(index, identity=IDENTITY, **ask) is not None
    # One field wrong — the one the real incident turned on — and the lookup declines rather than
    # publishing a stale answer that looks live.
    stale = {**IDENTITY, "ingest_version": 99}
    assert record_diverges(stale, record) == ("ingest_version",)
    assert find_precomputed(index, identity=stale, **ask) is None


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
    monkeypatch.setattr(
        app_module, "artifact_identity", lambda *_: {**IDENTITY, "corpus_hash": "9" * 64}
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
    # Boot now also reads the live text search configuration to build the artifact identity the
    # showcase gate compares against. Stubbed with the same values `PROVENANCE` carries, so a record
    # built by `offline` matches this build exactly, which is the passing case these tests need.
    monkeypatch.setattr(app_module, "artifact_identity", lambda *_: IDENTITY)

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
    # Boot now also reads the live text search configuration to build the artifact identity the
    # showcase gate compares against. Stubbed with the same values `PROVENANCE` carries, so a record
    # built by `offline` matches this build exactly, which is the passing case these tests need.
    monkeypatch.setattr(app_module, "artifact_identity", lambda *_: IDENTITY)
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
