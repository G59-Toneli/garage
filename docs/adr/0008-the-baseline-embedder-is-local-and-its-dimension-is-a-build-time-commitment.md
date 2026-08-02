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
- Embeddings were checked to be **bit-identical across Windows x86-64 and Linux x86-64** — all
  20,352 passage components and 1,920 query components — with the smallest adjacent cosine gap in
  any top-10 around 2e-4, twelve orders of magnitude above float32 epsilon. arm64 is untested.
- The 384-dimension promise constrains Phase 4 and is worth restating: a fine-tuned embedder that
  changes the width is not a second `model_key`, it is a schema migration and a new baseline.
- `fingerprint` (`embedding.EmbedderSpec`) is a **fourth identity number** beside `corpus_hash`,
  `INGEST_VERSION` and the facts digest, and it is deliberately not folded into any of them
  (ADR-0007): numbers that fail for different reasons are separate numbers. `EMBED_VERSION` is its
  escape hatch, exactly as `INGEST_VERSION` is chunking's.
