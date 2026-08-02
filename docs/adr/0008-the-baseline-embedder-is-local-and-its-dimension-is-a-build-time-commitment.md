# The baseline embedder is local, and its dimension is a build-time commitment

ADR-0005 fixes the embedder at build time and requires that the fine-tuned embedder of Phase 4 be
**derived from the baseline and preserve its dimension**, so the two can share one `vector(N)`
column and one `embeddings` table. That requirement is not a detail of the schema; it is what makes
`model_key` a real axis instead of a decorative column, and Phase 4 is the declared climax of the
project. The baseline therefore has to be a model this project can actually fine-tune.

A hosted embedding API was the obvious first choice — no weights to ship, no inference on a 1 GB ARM
VM. **Cohere was evaluated and rejected**: they have retired fine-tuning for Embed models. A
baseline that cannot be fine-tuned deletes Phase 4 and reduces ADR-0005 to a column nobody writes a
second value into. A `COHERE_API_KEY` is in the environment and is reserved for the Phase 2
reranker, which is a *runtime* axis and cacheable, and is not used here.

We run **`intfloat/multilingual-e5-small`, 384 dimensions, locally, under ONNX Runtime** — never
torch, never sentence-transformers. 384 becomes a **build-time commitment**: the schema declares
`vector(384)`, `embedding.EMBEDDING_DIMENSION` names it once, and every future embedder must
preserve it.

Multilingual because that is the debt the model exists to pay. The corpus is Brazilian workshop
material with English headings and the questions are Portuguese sentences; the lexical arm scores
`recall@10:keyword` 0.91 and `recall@10:natural` 0.07, and no stemmer will ever relate `volante do
motor` to `flywheel`. Small because the deployment target is an Oracle Ampere VM (ADR-0001).

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
- Embeddings are **bit-identical across x86-64 and not across architectures**, and both halves of
  that were measured rather than assumed.
  - Windows x86-64 against Linux x86-64: all 20,352 passage components and all 1,920 query
    components identical. This is the pair that matters for the gate — records are generated on a
    developer's Windows machine and re-measured by CI on Ubuntu — so `measurement()`, which compares
    per-item retrieved order, is safe as the pipeline is configured.
  - Emulated linux/arm64 against x86-64: **1,756 of 1,920 query components differ**, by at most
    9.7e-08. Ranking is very unlikely to move — the smallest adjacent cosine gap observed in any
    top-10 is around 2e-4, some two orders of magnitude above the worst cosine perturbation that
    delta can produce — but bit-identity does **not** hold and must not be relied on.
  - The operational rule that follows: **run records are generated and re-measured on x86-64.** The
    ARM VM is a deployment target, not a measurement machine. A record regenerated there would very
    probably still match and is not guaranteed to, and "probably" is not what a reproducibility
    claim is worth. If the ARM VM ever has to produce records, the fix is to round the score before
    `ORDER BY` (the `chunk_id` tie-break then settles it) or to exclude `retrieved_chunk_ids` from
    `measurement()` for the dense arm — decided then, with the failure in hand.
- The 384-dimension promise constrains Phase 4 and is worth restating: a fine-tuned embedder that
  changes the width is not a second `model_key`, it is a schema migration and a new baseline.
- **The serving process does not fit on the 1 GB VM of ADR-0001 alongside Postgres.** Measured as
  RSS inside a Linux container, which is the number that matters rather than a Windows working set:

  | stage | RSS | peak |
  | --- | --- | --- |
  | interpreter, before importing `garage` | 9 MB | — |
  | after `InferenceSession` is constructed | 510–790 MB | — |
  | after one short query (18 tokens, 11 ms) | ~790 MB | ~870 MB |
  | after one 512-token passage (340 ms) | ~820–845 MB | 870–1025 MB |

  The spread is run-to-run variance in how ONNX Runtime materialises initialisers, not measurement
  error; the floor and the peak are stable enough to decide on. **No configuration fixes it.** The
  CPU memory arena was the first suspect and moved steady RSS by a few megabytes; the graph
  optimisation level moved the peak by roughly its own noise while costing ~13% latency; a
  pre-optimised graph saved at build time landed within noise of `ORT_DISABLE_ALL`. The floor is the
  model: 470 MB of fp32 weights, of which about 384 MB is the 250,002 × 384 vocabulary embedding
  matrix, plus what the runtime holds while it loads them.

  Postgres wants 150–250 MB on top. **Plan for 2 GB, or split the processes**, and note that the
  ingestion path is the more expensive one (512-token passages) while serving is the cheaper one
  (short questions), so a VM that serves need not be a VM that ingests. Three ways out exist and
  none is free: `model_O4.onnx` at fp16 halves the weights and changes every number in the baseline;
  `model_qint8` quarters them and makes the cross-architecture determinism story much worse; pruning
  the vocabulary to what the Corpus actually uses would save ~300 MB and complicates deriving the
  Phase 4 fine-tune from the baseline. This is #11's decision to make, with these numbers in hand.
  It is recorded here rather than discovered on the VM.
- `fingerprint` (`embedding.EmbedderSpec`) is a **fourth identity number** beside `corpus_hash`,
  `INGEST_VERSION` and the facts digest, and it is deliberately not folded into any of them
  (ADR-0007): numbers that fail for different reasons are separate numbers. `EMBED_VERSION` is its
  escape hatch, exactly as `INGEST_VERSION` is chunking's.
