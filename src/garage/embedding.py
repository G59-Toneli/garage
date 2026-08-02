"""The embedder seam, and the fingerprint that makes ingestion and query provably the same one.

Embedder is a **build-time** axis (ADR-0005): the vectors it produces are stored, so changing it
means rebuilding the index rather than flipping a flag. That is exactly why it is dangerous in a way
`strategy` is not. A wrong strategy is visible in one query; a wrong embedder is invisible — the
service boots, the SQL runs, `<=>` returns a number for every pair of 384-float vectors whether or
not the two sides were produced by the same model, and the only symptom is that retrieval is quietly
mediocre. Nothing about a vector says which model made it.

So this module exists to make that failure impossible to write rather than merely unlikely:

- **One factory.** `embedder_for(name)` is the only way an `Embedder` is constructed. `ingest.build`
  calls it and `retrieval.available_retrievers` calls it, and because they call the *same* function
  with the same name they cannot disagree about pooling, prefixes, sequence length or weights. Two
  independent constructions is how divergence gets written; one factory is how it stops being
  expressible.
- **One digest.** `EmbedderSpec.fingerprint` is a sha256 over every parameter that changes a vector.
  Ingestion stores the fingerprint *of the object that actually ran* into `embeddings_meta`, read off
  that object rather than off the configuration that was meant to produce it, and `verify_artifact`
  refuses to boot when the stored digest and the live one disagree. A dimension check would not
  catch this: the most likely divergence — swapping the two e5 prefixes — has the same model, the
  same weights and the same 384 dimensions, and simply retrieves worse.

## Two methods, not one

The design (§7.1) writes the interface as `embed(texts)`. For the e5 family that is a defect and
this module deliberately diverges: e5 is trained with **asymmetric prefixes**, `"query: "` on the
question and `"passage: "` on the document, and those strings are part of the contract rather than
decoration. With a single generic `embed`, applying the passage prefix to a query is the *default*
mistake — the call site has no way to say which side it is on. With `embed_query` and
`embed_passages` the wrong side is not merely discouraged, it is unnameable. `test_embedding.py`
pins the fingerprint's sensitivity to a prefix swap for the case where someone reintroduces one.

## Local, ONNX, never torch

`intfloat/multilingual-e5-small` runs under ONNX Runtime. Two reasons, and the second is the one
that decides it. The `Dockerfile` is pinned to `python:3.12-slim` because torch and
sentence-transformers wheels narrow the arm64 target the deployment actually has (ADR-0001) — and
ADR-0008 records the other: the fine-tuned embedder of Phase 4 must be *derived from this baseline
and preserve its 384 dimensions*, so a hosted baseline that cannot be fine-tuned would make
`model_key` a decorative column and delete the phase the project is built towards.

## Batch size one, always

`_encode` runs exactly one text per session call and never pads. This looks wasteful and is the
point: GEMM kernels select by shape, so the same text embedded alone and embedded inside a padded
batch of thirty-two can differ in the last bits of a float. Ingestion embeds passages in bulk and
the server embeds one query at a time, which is precisely the pair that would diverge — and the
`measurement()` of ADR-0004 compares per-item retrieved order across machines, so a near-tie flipping
on the last bit is a red build nobody can reproduce. Fifty-three chunks at fixture scale cost
nothing; the day the corpus is a shelf of scanned manuals, this is the line to revisit *with a
measurement*, not before.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, Sequence

# The dimension the whole schema is built around. It is a `vector(384)` column, an ADR-0008
# commitment and a constraint on every future embedder, not a property of today's model that happens
# to be 384 — which is why it is named here and asserted against the loaded graph rather than read
# off it.
EMBEDDING_DIMENSION = 384

# The `model_key` the baseline embedder writes under. Phase 4's fine-tuned embedder adds a second
# value in the same table and the same column (ADR-0005); nothing here is singular.
BASELINE_MODEL_KEY = "baseline"

# The escape hatch, and the exact analogue of `chunking.INGEST_VERSION` in role: a number that goes
# up when this file changes how a vector is produced in a way none of the other fingerprint fields
# would notice — a different pooling implementation with the same name, a fixed normalisation bug.
# It is deliberately *not* folded into `INGEST_VERSION` or `corpus_hash` (ADR-0007): numbers that
# fail for different reasons are separate numbers, and re-chunking because the pooling changed would
# be a lie about what moved.
EMBED_VERSION = 1

# Pinned by digest, not by tag. A Hugging Face branch is mutable and `main` is not a version; the
# whole fingerprint argument collapses if the bytes behind `model_id` can change under it. These are
# the sha256 of the two files `embedder fetch` downloads, checked on every load, and they are what a
# `weights_sha256` in the fingerprint is actually derived from.
BASELINE_MODEL_ID = "intfloat/multilingual-e5-small"
BASELINE_FILES = {
    "model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
}
BASELINE_URL = f"https://huggingface.co/{BASELINE_MODEL_ID}/resolve/main/onnx/"

# Where `embedder fetch` puts them and where the loader looks. Outside the repository on purpose:
# 470 MB of weights is not a thing to commit (and ADR-0003's instinct — the repo holds catalogues,
# not payloads — applies to model weights as readily as to scanned manuals). `GARAGE_EMBEDDER_DIR`
# overrides it, which is how the container points at the copy baked into the image.
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "garage" / "embedders"

# `none` is a real, supported configuration and not a typo for "unset". It builds a lexical-only
# artifact: no vectors, no `embeddings_meta` row, no `dense` arm. Named rather than implied by an
# empty string, because "the operator chose not to build dense" and "the operator forgot to set the
# variable" must not look the same in a log.
NO_EMBEDDER = "none"


class EmbedderError(Exception):
    """The embedder cannot be loaded or cannot be trusted. Never a warning, always a refusal."""


@dataclass(frozen=True)
class EmbedderSpec:
    """Every parameter that changes a vector, and nothing that does not.

    The membership rule is exact and worth stating, because a fingerprint that omits one field is
    *worse* than no fingerprint at all — it looks like protection while permitting the divergence it
    was built to catch. A field belongs here if two embedders differing only in it can produce
    different vectors for the same input. `max_seq_len` qualifies (a truncated passage is a
    different passage). A batch size would not, which is why there is none.

    `test_embedding.py` parametrises over `dataclasses.fields(EmbedderSpec)` rather than over a
    hand-written list, so a field added here without changing the digest fails the suite.
    """

    model_id: str
    weights_sha256: str
    tokenizer_sha256: str
    dimension: int
    max_seq_len: int
    pooling: str
    normalize: bool
    query_prefix: str
    passage_prefix: str
    # The runtime's graph rewriting level. Included on the *possibility* rule above rather than on
    # evidence: operator fusion reorders floating point work, so two sessions differing only in this
    # can in principle disagree in the last bits. Measured across all four ONNX Runtime levels on
    # this graph they agree exactly — which is a fact about this model today, not a property, and is
    # precisely the kind of thing that stops being true after a runtime upgrade nobody reads the
    # notes for.
    graph_optimization: str
    embed_version: int

    @property
    def fingerprint(self) -> str:
        """sha256 over the canonical JSON of this spec.

        The same four `json.dumps` choices as `corpus._canonical_manifest_bytes` and
        `evaluation._canonical_bytes`, and for the same reason: sorted keys and fixed separators so
        the digest is a function of the *content* and not of the dict ordering that happened to
        build it. Unlike those two this one is never read by a human, so there is no `indent`.
        """
        payload = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Embedder(Protocol):
    """`embed_passages`, `embed_query`, and the identity that ties the two to a stored index.

    Deliberately not `embed(texts)`; see the module docstring. The four attributes are as much of the
    contract as the two methods: `fingerprint` is what `embeddings_meta` stores and what the boot
    gate compares, `model_key` is what the `WHERE` clause selects, and `dimension` has to match the
    column the vectors are written into.
    """

    #: Which set of vectors in `embeddings` this embedder owns. A `WHERE`, not a table (ADR-0005).
    model_key: str
    #: The digest of everything that changes a vector. See `EmbedderSpec`.
    fingerprint: str
    dimension: int
    normalized: bool

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        ...

    def embed_query(self, text: str) -> tuple[float, ...]:
        ...


class E5OnnxEmbedder:
    """`intfloat/multilingual-e5-small` under ONNX Runtime: mean pooling, L2 normalised, 384 dims.

    The weights are verified against `BASELINE_FILES` on every load, at the same rigour the manifest
    applies to `documents.sha256` — a corpus this project will not chunk without checking its bytes
    should not be searched with a model it will.
    """

    def __init__(self, directory: Path, *, model_key: str = BASELINE_MODEL_KEY) -> None:
        import numpy
        import onnxruntime
        from tokenizers import Tokenizer

        self.model_key = model_key
        self._numpy = numpy

        weights = _verified(directory, "model.onnx")
        tokenizer_file = _verified(directory, "tokenizer.json")

        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))
        # Truncation is set here rather than left to the tokenizer's own config, because it is a
        # fingerprint field: a loader that inherited 128 from a config file while the spec claimed
        # 512 would produce different vectors and an identical digest.
        self._tokenizer.enable_truncation(max_length=MAX_SEQ_LEN)
        self._tokenizer.no_padding()

        # One thread, and the session options are pinned rather than defaulted. Thread count changes
        # reduction order in a parallel GEMM, so a machine with more cores would otherwise produce
        # different last bits from the same weights — the exact cross-machine wobble `measurement()`
        # would report as a retrieval regression. Determinism is worth more than a benchmark that
        # embeds fifty-three chunks.
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        # Off, and the comment is here because the *measurement* is the interesting part. The arena
        # was the first suspect for this process's memory footprint and it is not the culprit:
        # toggling it moved steady RSS by a few megabytes against a total near 800 MB. It stays off
        # anyway, because an arena exists to amortise allocation across many concurrent inferences
        # and this workload has one query at a time on one thread, so all it can contribute is
        # retained fragmentation. See ADR-0008 for where the memory actually goes.
        options.enable_cpu_mem_arena = False
        options.graph_optimization_level = getattr(
            onnxruntime.GraphOptimizationLevel, GRAPH_OPTIMIZATION
        )
        self._session = onnxruntime.InferenceSession(
            str(weights), options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {tensor.name for tensor in self._session.get_inputs()}

        self.spec = EmbedderSpec(
            model_id=BASELINE_MODEL_ID,
            weights_sha256=BASELINE_FILES["model.onnx"],
            tokenizer_sha256=BASELINE_FILES["tokenizer.json"],
            dimension=EMBEDDING_DIMENSION,
            max_seq_len=MAX_SEQ_LEN,
            pooling=POOLING,
            normalize=True,
            query_prefix=QUERY_PREFIX,
            passage_prefix=PASSAGE_PREFIX,
            graph_optimization=GRAPH_OPTIMIZATION,
            embed_version=EMBED_VERSION,
        )
        self.fingerprint = self.spec.fingerprint
        self.dimension = EMBEDDING_DIMENSION
        self.normalized = True

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._encode(QUERY_PREFIX + text)

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._encode(PASSAGE_PREFIX + text) for text in texts)

    def _encode(self, text: str) -> tuple[float, ...]:
        numpy = self._numpy
        encoding = self._tokenizer.encode(text)
        ids = numpy.asarray([encoding.ids], dtype=numpy.int64)
        mask = numpy.asarray([encoding.attention_mask], dtype=numpy.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        # Present on some exports of this graph and absent on others. Fed only when the graph asks
        # for it, rather than assumed either way: a missing input is a hard error and an extra one is
        # silently ignored by some providers, so guessing has a wrong answer in both directions.
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = numpy.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]

        # Mean pooling over the *unmasked* positions. With batch size one nothing is padded, so the
        # mask is all ones and this is an unweighted mean — written against the mask anyway, because
        # the day batching returns this is the line that would silently start averaging in padding.
        weights = mask[..., None].astype(hidden.dtype)
        pooled = (hidden * weights).sum(axis=1) / numpy.clip(weights.sum(axis=1), 1e-9, None)
        # L2 normalised, which is what makes `1 - (a <=> b)` a cosine a reader can compare across
        # queries, and what lets the same vectors serve an inner-product index later without a
        # rebuild.
        pooled = pooled / numpy.clip(
            numpy.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None
        )
        return tuple(float(value) for value in pooled[0])


# Named constants rather than literals inside the class, because each one is a fingerprint field and
# a fingerprint field that is spelled twice is a fingerprint field that can be changed once.
MAX_SEQ_LEN = 512
POOLING = "mean"
# The e5 contract. Not decoration and not a prompt: the model was trained with these exact strings,
# trailing space included, and swapping them costs recall while changing nothing a dimension or a
# shape check could see.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
# Left at ONNX Runtime's fullest rewriting. `ORT_DISABLE_ALL` was measured as a way out of this
# process's memory footprint and is not one — the difference sits inside the run-to-run variance —
# while costing about 13% on a 512-token passage. Named as a constant and folded into the
# fingerprint because it is a knob that could move a vector, not because it currently does.
GRAPH_OPTIMIZATION = "ORT_ENABLE_ALL"


def _verified(directory: Path, name: str) -> Path:
    """The file, or a refusal naming both digests. Never a warning and never a re-download."""
    path = Path(directory) / name
    if not path.is_file():
        raise EmbedderError(
            f"the baseline embedder is missing {name}: no file at {path}.\n"
            "Run `python -m garage embedder fetch` to download and verify it, or set "
            "GARAGE_EMBEDDER=none to build a lexical-only artifact."
        )
    found = _sha256(path)
    expected = BASELINE_FILES[name]
    if found != expected:
        raise EmbedderError(
            f"{path} is not the file this build pins.\n"
            f"  expected sha256 {expected}\n"
            f"  found    sha256 {found}\n"
            "Delete it and run `python -m garage embedder fetch` again."
        )
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        # Chunked because `model.onnx` is 470 MB and reading it whole to hash it would be a
        # half-gigabyte allocation on a 1 GB ARM VM (ADR-0001).
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_dir() -> Path:
    """Where the weights live. `GARAGE_EMBEDDER_DIR` wins, so the image can bake them in."""
    override = os.environ.get("GARAGE_EMBEDDER_DIR")
    if override:
        return Path(override)
    return DEFAULT_CACHE_DIR / BASELINE_MODEL_ID.split("/")[-1]


def embedder_name() -> str:
    """The embedder this build is configured with. `none` is a choice, not an absence."""
    return os.environ.get("GARAGE_EMBEDDER", BASELINE_MODEL_KEY).strip() or BASELINE_MODEL_KEY


def embedder_for(name: str, *, directory: Path | None = None) -> Embedder | None:
    """The one construction site for an `Embedder`. None only for the explicit `none`.

    Both `ingest.build` and `retrieval.available_retrievers` come through here, which is the whole
    mechanism behind acceptance criterion three: two call sites that construct the same object by
    calling the same function with the same argument cannot configure it differently. A `str` model
    name threaded into `DenseRetriever.__init__` would have reintroduced exactly the second
    construction this removes, which is why that constructor takes the instance instead.

    A missing or altered weights file raises rather than returning None. Silently degrading to
    lexical would turn "the operator forgot to fetch the model" into "dense scored zero", and a
    benchmark that reports a configuration error as a quality result is worse than one that stops.
    """
    if name == NO_EMBEDDER:
        return None
    if name != BASELINE_MODEL_KEY:
        raise EmbedderError(
            f"unknown embedder {name!r}. This build knows {BASELINE_MODEL_KEY!r} and "
            f"{NO_EMBEDDER!r}. The fine-tuned embedder of Phase 4 adds a third name here and a "
            "second model_key in the same table (ADR-0005), not a new column."
        )
    return E5OnnxEmbedder(directory or cache_dir())


def configured_embedder(directory: Path | None = None) -> Embedder | None:
    """`embedder_for` applied to the environment. What ingestion, serving and the gate all read."""
    return embedder_for(embedder_name(), directory=directory)


def fetch(directory: Path | None = None) -> Path:
    """Download the pinned weights and verify them. Idempotent, and never trusted on faith.

    A download step in Python rather than a `curl` line duplicated into the `Dockerfile` and
    `ci.yml`: the digests are the load-bearing part and a digest maintained in three places is a
    digest that is wrong in two of them. The same argument the repository already makes about
    `pg_trgm` living in `database.py` rather than in `docker/initdb/` and the CI workflow.
    """
    import urllib.request

    directory = Path(directory) if directory is not None else cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name in BASELINE_FILES:
        path = directory / name
        if path.is_file() and _sha256(path) == BASELINE_FILES[name]:
            print(f"{name}: already present and verified")
            continue
        print(f"{name}: downloading from {BASELINE_URL}{name}")
        urllib.request.urlretrieve(BASELINE_URL + name, path)
        _verified(directory, name)
        print(f"{name}: verified sha256 {BASELINE_FILES[name]}")
    return directory
