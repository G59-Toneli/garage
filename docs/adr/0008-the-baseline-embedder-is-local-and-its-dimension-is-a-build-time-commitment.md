# The baseline embedder is local, and its dimension is a build-time commitment

ADR-0005 fixes the embedder at build time and requires that the fine-tuned embedder of Phase 4 be
**derived from the baseline and preserve its dimension**, so the two can share one `vector(N)`
column and one `embeddings` table. That requirement is not a detail of the schema; it is what makes
`model_key` a real axis instead of a decorative column, and Phase 4 is the declared climax of the
project. The baseline therefore has to be a model this project can actually fine-tune.

A hosted embedding API was the obvious first choice — no weights to ship, no inference to run, no
470 MB in the image. **Cohere was evaluated and rejected**: they have retired fine-tuning for Embed
models. A baseline that cannot be fine-tuned deletes Phase 4 and reduces ADR-0005 to a column
nobody writes a second value into. A `COHERE_API_KEY` is in the environment and is reserved for the
Phase 2 reranker, which is a *runtime* axis and cacheable, and is not used here.

We run **`intfloat/multilingual-e5-small`, 384 dimensions, locally, under ONNX Runtime** — never
torch, never sentence-transformers. 384 becomes a **build-time commitment**: the schema declares
`vector(384)`, `embedding.EMBEDDING_DIMENSION` names it once, and every future embedder must
preserve it.

Multilingual because that is the debt the model exists to pay. The corpus is Brazilian workshop
material with English headings and the questions are Portuguese sentences; the lexical arm scores
`recall@10:keyword` 0.91 and `recall@10:natural` 0.07, and no stemmer will ever relate `volante do
motor` to `flywheel`.

Small for latency and image size, **not** for memory: the target is an Ampere A1 with 24 GB
(ADR-0001), and running the model locally costs about 800 MB, which is not a constraint there. The
architecture is the constraint, not the capacity — see the determinism consequence below.

## Consequences

- The weights are **fetched, not committed**: 470 MB, pinned by sha256 in `garage/embedding.py`,
  verified on every load with the same rigour the manifest applies to `documents.sha256`. One
  command, `python -m garage embedder fetch`, used by developers, the `Dockerfile` and CI alike, so
  the digests exist in one place rather than three.
- **ONNX Runtime, never torch.** The `Dockerfile` is pinned to `python:3.12-slim` because the torch
  and sentence-transformers wheels narrow the arm64 target. A test asserts that no `garage` module
  imports torch and that no declared dependency pulls it in. It is cheap and it is permanent.
- Vectors are produced **one text per session call, never batched**, because GEMM kernels select by
  shape and a text embedded alone can otherwise differ in its last bits from the same text embedded
  inside a padded batch. Ingestion embeds in bulk and the server embeds one query at a time, which
  is exactly the pair that would diverge, and ADR-0004's `measurement()` compares per-item retrieved
  order across machines. Revisit with a measurement when the corpus is large, not before.
- **Cross-architecture reproducibility, measured end to end.** Every number here comes from
  embedding the whole fixture corpus and all 76 fact questions twice: once on x86-64, once in an
  emulated `linux/arm64` container, same weights, same digests.

  | | result |
  | --- | --- |
  | Windows x86-64 vs Linux x86-64 | every component identical, passages and queries alike |
  | Linux x86-64 vs emulated arm64, components | 18,679/20,352 passage and 26,917/29,184 query components differ |
  | largest single component delta | 1.155e-07 |
  | `‖Δq‖₂` max / `‖Δp‖₂` max | 6.081e-07 / 5.540e-07 → cosine bound **1.162e-06** |
  | largest cosine delta actually observed | **2.384e-07** |
  | smallest adjacent top-10 cosine gap in the suite | **1.311e-06** |
  | **top-10 order differences over 76 questions** | **0, raw and rounded** |

  So the ordering does hold across architectures on this corpus — but the margin is 1.13× against
  the analytic bound and 5.5× against the worst delta actually seen, not the two orders of magnitude
  an earlier draft of this ADR claimed. That draft was wrong twice over and the corrections are
  worth keeping visible: **2e-4 was the median** of the per-question smallest gaps rather than the
  minimum (the minimum is 145× smaller), and a *per-component* delta was being compared directly
  against a *cosine* gap when the honest bound is `|Δcos| ≤ ‖Δq‖₂ + ‖Δp‖₂`.

  This is not a hypothetical about a machine nobody uses. **Production runs on aarch64** (ADR-0001)
  while every published number is measured on x86-64, so a visitor re-running a query live against
  the deployed service compares an ARM-computed result with an x86-measured figure.

- **Rounding: what it does and what it does not.** `retrieval._DENSE_SCORED` rounds the cosine to
  five decimals before anything orders by it, and the `chunk_id` tie-break — already present, already
  deterministic — settles the ties that creates. It changed no metric and no per-item order, so it
  is free.

  It is a mitigation with a measured residual, **not** a construction that guarantees
  architecture-independent ordering, and this ADR will not claim otherwise. Rounding has boundaries,
  and a value landing within the perturbation envelope of one rounds to different sides on the two
  machines. Counting the 760 adjacent top-10 pairs the suite produces, against the analytic
  1.162e-06 bound: raw ordering has **0** pairs that could swap and five-decimal rounding has **2**.
  A sweep across precisions wanders between 0 and 2 with no trend, because a coarser grid pulls in
  more pairs at the same rate it makes each one less likely to straddle. Against the *observed*
  2.384e-07 delta both are 0, which is why the empirical run shows no difference.

  Read plainly: on this corpus rounding is not what is buying the agreement — the gap distribution
  is. Five decimals is kept because it costs nothing measurable, because it stays 200× below the
  2.6e-04 median gap so it swallows no distinction the suite makes, and because it turns the tightest
  pair into a deterministic tie under most perturbations rather than leaving it to luck. It is
  insurance against a corpus with a denser gap distribution than this one, bought at zero premium.
  It is not a proof, and the day the corpus grows is the day to re-run this measurement.

- **What a residual would cost, which is close to nothing.** The tightest pairs sit at positions 8
  and 9 of one question (`kw-cabecote-trabalhado`). A swap there changes `retrieved_chunk_ids` and
  changes `recall@k`, `mrr@10` and `nDCG@10` by exactly zero unless a *relevant* chunk is one of the
  two. The exposure is confined to `measurement()`'s exact per-item comparison and touches no
  published number.

- **Run records are generated and re-measured on x86-64.** A convention now rather than a
  load-bearing assumption, and cheap to keep. If the ARM VM ever has to produce a record, compare
  metrics rather than `retrieved_chunk_ids` for the dense arm — decided then, with a failure in hand.

- The 384-dimension promise constrains Phase 4 and is worth restating: a fine-tuned embedder that
  changes the width is not a second `model_key`, it is a schema migration and a new baseline.
- **Resource cost, measured, for #11 to plan with.** RSS inside a Linux container, which is the
  number the VM will actually see rather than a Windows working set:

  | stage | RSS | peak |
  | --- | --- | --- |
  | interpreter, before importing `garage` | 9 MB | — |
  | after `InferenceSession` is constructed | 510–790 MB | — |
  | after one short query (18 tokens, 11 ms) | ~790 MB | ~870 MB |
  | after one 512-token passage (340 ms) | ~820–845 MB | 870–1025 MB |

  The image is 1.3 GB. Against the target's 24 GB (ADR-0001) none of this is a constraint, and it is
  recorded as an observation rather than a concern — the number is here so #11 can size things
  without measuring again. The spread is run-to-run variance in how ONNX Runtime materialises
  initialisers. The floor is the model: 470 MB of fp32 weights, about 384 MB of which is the
  250,002 × 384 vocabulary embedding matrix.

  Nothing is being done to reduce it, deliberately. `model_O4.onnx` at fp16 and `model_qint8` at
  int8 would halve and quarter the weights respectively, and both would introduce numeric divergence
  in precisely the place this ADR is working to remove it. **fp32 stays.** Latency is ~11 ms for a
  query and ~340 ms for a 512-token passage on one thread, and the whole fifty-three-chunk corpus
  embeds in well under a second.
- `fingerprint` (`embedding.EmbedderSpec`) is a **fourth identity number** beside `corpus_hash`,
  `INGEST_VERSION` and the facts digest, and it is deliberately not folded into any of them
  (ADR-0007): numbers that fail for different reasons are separate numbers. `EMBED_VERSION` is its
  escape hatch, exactly as `INGEST_VERSION` is chunking's.
