"""The fingerprint, and the failures it is the only thing that can see.

No database, no weights, no network. Everything here is arithmetic over `EmbedderSpec`, which is
the point: the digest that decides whether a dense artifact may be served has to be testable without
the 470 MB of weights it describes, or it will only ever be exercised by the machines that already
have them.

The load-bearing test in this file is `test_the_fingerprint_changes_when_any_field_changes`. A
fingerprint that ignores one field is strictly worse than no fingerprint, because it looks like
protection while admitting exactly the divergence it was built to catch — so the parametrisation
reads the field list off the dataclass rather than restating it, and a field added to `EmbedderSpec`
without being folded into the digest fails here.
"""

from __future__ import annotations

import dataclasses
import os

import pytest
from fakes import FakeEmbedder

from garage.embedding import (
    BASELINE_MODEL_KEY,
    EMBEDDING_DIMENSION,
    NO_EMBEDDER,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    EmbedderError,
    EmbedderSpec,
    embedder_for,
)

# The real model, opt in. Same pattern the database tests use for `GARAGE_DATABASE_URL`: a suite
# that silently depended on half a gigabyte of downloaded weights would not be a suite anyone could
# run on a fresh checkout, and one that skipped without saying so would hide its own absence.
REAL_EMBEDDER = os.environ.get("GARAGE_TEST_REAL_EMBEDDER")


def spec(**overrides) -> EmbedderSpec:
    fields = dict(
        model_id="m",
        weights_sha256="a" * 64,
        tokenizer_sha256="b" * 64,
        dimension=EMBEDDING_DIMENSION,
        max_seq_len=512,
        pooling="mean",
        normalize=True,
        query_prefix=QUERY_PREFIX,
        passage_prefix=PASSAGE_PREFIX,
        graph_optimization="ORT_ENABLE_ALL",
        embed_version=1,
    )
    return EmbedderSpec(**{**fields, **overrides})


def test_the_fingerprint_is_a_function_of_the_spec_and_nothing_else():
    # Two independently constructed specs with equal fields must agree, or the digest would depend
    # on object identity and every restart would look like a different embedder.
    assert spec().fingerprint == spec().fingerprint
    assert len(spec().fingerprint) == 64


@pytest.mark.parametrize("field", [field.name for field in dataclasses.fields(EmbedderSpec)])
def test_the_fingerprint_changes_when_any_field_changes(field):
    """Every field, read off the dataclass. A field the digest ignores is a hole shaped like it."""
    base = spec()
    current = getattr(base, field)
    changed = {
        str: lambda value: value + "x",
        int: lambda value: value + 1,
        bool: lambda value: not value,
    }[type(current)](current)

    assert spec(**{field: changed}).fingerprint != base.fingerprint


def test_swapping_the_e5_prefixes_changes_the_fingerprint():
    """The divergence no other check can reach.

    An embedder that applies `passage: ` to a query has the same model, the same weights, the same
    tokenizer and the same 384 dimensions. Every vector it produces is a valid `vector(384)`, every
    cosine against the stored index is a real number, and retrieval is simply worse. No dimension
    check, no shape check and no type signature sees it. This does.
    """
    swapped = spec(query_prefix=PASSAGE_PREFIX, passage_prefix=QUERY_PREFIX)

    assert swapped.fingerprint != spec().fingerprint


def test_the_two_methods_apply_different_prefixes_to_the_same_text():
    # The reason the interface is `embed_query`/`embed_passages` and not `embed(texts)`: with one
    # method this equality would hold, and the asymmetric training the e5 family depends on would
    # be silently discarded at whichever call site forgot.
    embedder = FakeEmbedder()

    assert embedder.embed_query("torque") != embedder.embed_passages(["torque"])[0]


def test_an_embedder_is_deterministic_across_calls():
    embedder = FakeEmbedder()

    assert embedder.embed_query("torque do cabeçote") == embedder.embed_query("torque do cabeçote")


def test_vectors_are_unit_length_and_the_declared_width():
    embedder = FakeEmbedder()
    vector = embedder.embed_query("folga de válvulas")

    assert len(vector) == EMBEDDING_DIMENSION == embedder.dimension
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_two_differently_configured_embedders_do_not_share_a_fingerprint():
    # What makes the boot gate able to tell them apart at all.
    assert FakeEmbedder().fingerprint != FakeEmbedder(model_id="other").fingerprint
    assert FakeEmbedder().fingerprint != FakeEmbedder(embed_version=2).fingerprint


def test_the_factory_is_the_only_construction_and_none_is_explicit():
    # `none` is a supported configuration and not an absence: it builds a lexical-only artifact.
    assert embedder_for(NO_EMBEDDER) is None
    with pytest.raises(EmbedderError) as failure:
        embedder_for("multilingual-e5-large")
    assert BASELINE_MODEL_KEY in str(failure.value)


def test_missing_weights_refuse_rather_than_degrade(tmp_path):
    # Never a silent fall back to lexical. "The operator forgot to fetch the model" and "dense
    # scored zero" must not be the same observation, or a configuration mistake is reported as a
    # quality result.
    with pytest.raises(EmbedderError) as failure:
        embedder_for(BASELINE_MODEL_KEY, directory=tmp_path)

    assert "embedder fetch" in str(failure.value)


def test_weights_that_do_not_match_their_pinned_digest_refuse(tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"not the model")
    (tmp_path / "tokenizer.json").write_bytes(b"{}")

    with pytest.raises(EmbedderError) as failure:
        embedder_for(BASELINE_MODEL_KEY, directory=tmp_path)

    # The same rigour the manifest applies to `documents.sha256` (ADR-0002): a project that will not
    # chunk a document without checking its bytes should not search with a model it did not check.
    assert "sha256" in str(failure.value)


@pytest.mark.parametrize("forbidden", ["torch", "sentence_transformers", "transformers"])
def test_nothing_in_the_package_reaches_for_torch(forbidden):
    """The permanent, cheap assertion behind ADR-0008's "local, ONNX, never torch".

    Written as "no `garage` module imports it" rather than "it is not installed", and the difference
    matters: a developer may have torch in their environment for a hundred unrelated reasons, and a
    test that failed on its mere presence would be deleted within a week. What must never happen is
    this package *reaching* for it — that is the change that would break the arm64 image the
    `Dockerfile` is pinned to `python:3.12-slim` for (ADR-0001), and it would be found at deploy
    time rather than here.
    """
    import sys

    for module in list(sys.modules):
        if module == forbidden or module.startswith(forbidden + "."):
            del sys.modules[module]

    import garage.app  # noqa: F401  The widest import in the package: everything is behind it.
    import garage.embedding  # noqa: F401
    import garage.ingest  # noqa: F401
    import garage.retrieval  # noqa: F401

    assert forbidden not in sys.modules


def test_the_declared_dependencies_do_not_carry_a_deep_learning_stack():
    # The other half: an optional extra or a transitive pin would put the wheel back on the arm64
    # build even if no module imported it.
    from pathlib import Path

    import tomllib

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    # Parsed rather than grepped, so the comment above the dependency list explaining *why* torch is
    # absent does not itself fail the test that torch is absent.
    declared = " ".join(
        pyproject["project"]["dependencies"]
        + [entry for extra in pyproject["project"]["optional-dependencies"].values() for entry in extra]
    )

    assert "torch" not in declared and "sentence-transformers" not in declared
    assert "onnxruntime" in declared and "tokenizers" in declared


@pytest.mark.skipif(not REAL_EMBEDDER, reason="GARAGE_TEST_REAL_EMBEDDER is unset")
def test_the_real_embedder_matches_the_dimension_the_schema_commits_to():
    # ADR-0008: 384 is a build-time promise, not an observation about today's model. If this ever
    # fails, the `vector(384)` column and every stored vector are wrong together.
    embedder = embedder_for(BASELINE_MODEL_KEY)

    assert embedder.dimension == EMBEDDING_DIMENSION
    assert len(embedder.embed_query("torque")) == EMBEDDING_DIMENSION
    assert len(embedder.embed_passages(["torque"])[0]) == EMBEDDING_DIMENSION
    assert embedder.normalized


@pytest.mark.skipif(not REAL_EMBEDDER, reason="GARAGE_TEST_REAL_EMBEDDER is unset")
def test_the_real_embedder_is_multilingual_where_the_lexical_arm_is_not():
    """The one quality claim asserted outside the gate, because it is the reason #7 exists.

    A Portuguese question against an English heading is the exact shape of the `recall@10:natural`
    debt the lexical arm cannot pay — no stemmer relates `volante do motor` to `flywheel`. This is
    a floor on the property, not a measurement of it: the measurement is the gate's.
    """
    embedder = embedder_for(BASELINE_MODEL_KEY)
    question = embedder.embed_query("qual o torque do parafuso do volante do motor?")
    related = embedder.embed_passages(["Fastener: Flywheel bolt; Thread: M10; Torque (N·m): 63"])[0]
    unrelated = embedder.embed_passages(["Receita de brigadeiro de colher com leite condensado"])[0]

    assert sum(a * b for a, b in zip(question, related)) > sum(
        a * b for a, b in zip(question, unrelated)
    )
