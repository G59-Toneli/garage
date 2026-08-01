# Configuration axes are split by whether they cost an index

The demo lets a visitor change the pipeline and watch the answer change, and the plausible axes
(chunking, embedder, retrieval strategy, reranker, tier filter, prompt contract) combine into around
ninety configurations — none of which can all be built. The split is made by **cost, not by taste**:
axes that alter the stored index (**chunking** and **embedder**) are fixed at build time, and axes
that operate over a finished index (**strategy**, **reranker**, **tier filter**, **prompt
contract**) are free at runtime.

## Consequences

- The MVP builds one chunking against two embedders — baseline and fine-tuned — giving two
  `model_key` values in one table. Switching embedder is a `WHERE` clause, not a redeploy.
- The fine-tuned embedder must be derived from the baseline and preserve its dimension, otherwise
  the two cannot share a `vector(N)` column. This constrains the choice of base model.
- Adding chunking as a second build axis doubles ingestion and storage. It is deferred, not refused.
- The interface presents four named Presets, with a secondary panel for manual combination.
