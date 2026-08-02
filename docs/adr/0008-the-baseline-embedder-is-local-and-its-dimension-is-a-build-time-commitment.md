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
- Embeddings are **bit-identical across x86-64 and not across architectures**, and every number
  below is measured rather than argued. An earlier draft of this ADR got the argument wrong by two
  orders of magnitude and the correction is the reason the code changed; it is left visible here
  because the mistake is instructive.
  - Windows x86-64 against Linux x86-64: all 20,352 passage components and all 1,920 query
    components identical.
  - Emulated linux/arm64 against x86-64: **1,756 of 1,920 query components differ**, by at most
    9.7e-08.
  - The wrong conclusion drawn from that: "the smallest adjacent top-10 cosine gap is ~2e-4, two
    orders of magnitude above the perturbation, so ranking is safe." Two errors. **2e-4 is the
    median** of the per-question smallest gaps; the actual minimum over the fact suite is
    **1.414e-06** (`kw-cabecote-trabalhado`, positions 8 and 9; an independent float32
    recomputation puts it at 1.311e-06) — 145× smaller. And 9.7e-08 is a *per-component* delta
    compared directly against a *cosine* gap, where the honest bound is
    `|Δcos| ≤ ‖Δq‖₂`: **1.90e-06** worst case over 384 components, 1.05e-06 RMS, with the passage
    side drifting too. The real gap sits **inside** the envelope. Order was never safe across
    architectures.
  - This is not a hypothetical about a machine nobody uses. **Production runs on aarch64**
    (ADR-0001) while every published number is measured on x86-64, so a visitor re-running a query
    live against the deployed service is comparing an ARM-computed result with an x86-measured
    figure.
  - **The mitigation, and an honest account of what it does not do.**
    `retrieval._DENSE_SCORED` rounds the cosine to five decimals before anything orders by it, and
    the `chunk_id` tie-break — already present, already deterministic — settles the ties rounding
    creates. It changed **no metric and no per-item order** on x86-64, so it is free.

    It is *not* a construction that makes ordering architecture-independent, and this ADR is not
    going to claim it is. Rounding has boundaries, and a value landing within the perturbation
    envelope of one rounds to different sides on the two architectures. Counting the 760 adjacent
    top-10 pairs the fact suite actually produces:

    | ordering | pairs that can swap under a 1.9e-06 perturbation |
    | --- | --- |
    | raw cosine | 1 |
    | round to 7 or 6 decimals | 0 |
    | round to 5 decimals *(shipped)* | 2 |
    | round to 4 decimals | 0 |
    | round to 3 decimals | 1 |
    | round to 2 decimals | 2 |

    That column is a lottery, not a trend, and the reason is structural: coarsening the grid pulls
    in more pairs at the same rate that it lowers each pair's chance of straddling a boundary, so
    the product barely moves. The 0s are where this corpus's values happen to fall, not a property
    any grid has. **No rounding precision guarantees stable order across architectures**, and
    picking 4 decimals because it scores 0 here would be fitting a constant to 76 questions.

    Five decimals is kept because it is the value that collapses the one genuinely sub-envelope pair
    into a `chunk_id` tie about seven times in eight, costs nothing measurable, and stays far below
    the 2.2e-04 median gap so it swallows no distinction the suite makes. It is a mitigation with a
    known residual, which is a different and more defensible thing than a guarantee.
  - **What the residual actually costs, which is close to nothing.** The at-risk pairs sit at
    positions 8 and 9 of one question. A swap there changes `retrieved_chunk_ids` and changes
    `recall@k`, `mrr@10` and `nDCG@10` by exactly zero unless a *relevant* chunk is one of the two.
    So the exposure is confined to `measurement()`'s exact per-item comparison, not to any published
    number — which is why the operational rule below is cheap to keep.
  - **Run records are generated and re-measured on x86-64.** Still a convention, and still the right
    one. If the ARM VM ever has to produce a record, the fix is to compare metrics rather than
    `retrieved_chunk_ids` for the dense arm, decided then, with the failure in hand.
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
