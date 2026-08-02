# Retrieval and traces

One endpoint, and no language model behind it:

```sh
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "torque do parafuso do cabeçote", "k": 5, "tiers": ["A", "B"]}'
```

```jsonc
{
  "question": "torque do parafuso do cabeçote",
  "corpus_hash": "21c4e571…",
  "strategy": "lexical",
  "k": 5,
  "tiers": ["A", "B"],
  "chunks": [
    {
      "chunk_id": "svc-kadett-1993#0001",
      "doc_id": "svc-kadett-1993",
      "doc_title": "Manual de Serviço — Kadett GSi 2.0 MPFI",
      "tier": "A",
      "page": null,
      "section": "Section 3.2 — Cylinder head, tightening specifications",
      "kind": "spec",
      "text": "Section 3.2 — … — Fastener: Cylinder head bolt, stage 1; Thread: M11; Torque (N·m): 41",
      "score": 0.0164,
      "components": {"lexical": 0.02, "lexical_rank": 1, "trigram": 0.73, "trigram_rank": 1}
    }
  ],
  "trace": { /* below */ }
}
```

There is no generated answer, and that is the ticket rather than a gap in it. Retrieval decides
whether an answer *can* be right; a generator only decides how it reads. Serving retrieval on its own
keeps it measurable on its own ([ADR-0004](adr/0004-two-layer-evaluation.md)), and the deterministic
CI gate scores exactly this response.

`tier` and `page` travel with every chunk because a citation is worth very little without them, and
because a manual and a forum post must never look alike on screen. `page` is `null` where the source
genuinely has no pages rather than carrying an invented number.

## Two signals, fused by rank

`lexical` is Postgres full text over the stored `tsvector` **and** trigram matching over the chunk
text. Both are needed, and for different failures:

| Signal | Finds | Misses |
| ------ | ----- | ------ |
| Full text (`to_tsvector('portuguese')`) | `torques` from `torque`, stop words handled | `cabecote` written without its cedilla, `kadet` with one `t` |
| Trigram (`word_similarity`) | misspellings, dropped accents, partial terms | nothing about stemming or word boundaries; matches noise if left unbounded |

Brazilian workshop writing drops accents constantly, so the second column is not hypothetical — it is
most of the Tier B material.

The two are combined by **reciprocal rank fusion**, not by adding their scores. They are not on the
same scale and never will be: a good `ts_rank_cd` hit lands around 0.01, a good `word_similarity` hit
around 0.7, so a weighted sum of the raw numbers is a trigram-only ranker wearing a weight it does
not obey. RRF asks each signal only for its *ordering* — the part both are good at — and it is the
same fusion the `hybrid` strategy will use across retrievers later, so there is one idea here rather
than two.

```
score = 0.7 / (60 + lexical_rank) + 0.3 / (60 + trigram_rank)
```

A signal that did not fire contributes nothing at all rather than a large rank that would penalise
chunks the other signal found. `components` carries both raw scores and both ranks, with a null where
a signal was silent: a chunk that ranked on trigram alone is a different kind of hit from one full
text agreed with, and the interface shows which.

Trigram matches below `word_similarity` 0.6 are dropped. That floor is what lets a question the
corpus does not cover retrieve **nothing** — and abstention depends on it, because a retriever that
always returns its ten least-bad chunks gives a generator nothing to abstain on.

Ranking is deterministic: ties break on `chunk_id`, so the same query against the same artifact
returns the same order. A benchmark whose ranking wobbled between runs would report noise as a
difference. `k` is capped at 50; the endpoint is public and a huge `k` is a way to make the service
read the whole corpus rather than a question anyone is asking.

## Dense: the second strategy

```sh
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "qual o torque do parafuso do volante do motor?", "strategy": "dense", "k": 5}'
```

`strategy` is a runtime axis ([ADR-0005](adr/0005-build-time-vs-runtime-axes.md)): both retrievers
are built at boot, both stand on the same artifact, and choosing between them is a dictionary lookup
in the same process — no rebuild, no redeploy, no second container. Omit the field and you get
`lexical`, so every request written before `dense` existed still means what it meant. A strategy this
build does not serve is a 422 naming the ones it does, never a silent fall back: a visitor comparing
two strategies who typos one and is quietly served the other reads a difference that is not there.

No existing field changes shape. `components` differs, because it is `dict[str, float | None]`
rather than four named fields, and one field is **added**:

```jsonc
{
  "strategy": "dense",
  "embedder": "baseline@6f852da7deb1",
  "chunks": [
    {"chunk_id": "svc-kadett-1993#0006", "score": 0.8376,
     "components": {"cosine": 0.8376, "dense_rank": 1}}
  ]
}
```

`embedder` is `<model_key>@<fingerprint prefix>`, null under `lexical`, and it also rides the
`retrieve` span as `retrieval.embedder`. It exists because `strategy` alone stops being an identity
the moment ADR-0005's second embedder does: Phase 4 puts `baseline` and `finetuned` in one
`embeddings` table under two `model_key`s, and two arms both labelled `dense` would be a comparison
a reader cannot name. An interface can then show which embedder answered without hard-coding it —
the same thing `Configuration.embedder` has done for run records all along.

`score` **is** the cosine, `1 - (embedding <=> :q)`, **rounded to five decimals before anything
orders by it**. That rounding is insurance on a thin margin rather than cosmetics. ONNX Runtime is
not bit-reproducible across instruction sets, and production runs on aarch64
([ADR-0001](adr/0001-architecture-characteristics.md)) while every published number is measured on
x86-64. Embedding the whole corpus and all 76 fact questions on both: most components differ, the
worst observed cosine delta is 2.384e-07, and the smallest adjacent top-ten gap in the suite is
1.311e-06 — so the order does hold, on a margin of 5.5×, and **0 of 76 top-ten orders differ**.

Rounding collapses sub-grid differences into ties that the `chunk_id` tie-break settles identically
everywhere. It is **cheap insurance against one class of perturbation, not a construction that makes
order architecture-independent** — `round` is monotone, so all it can add are ties, and at five
decimals this suite gets none: even the tightest pair straddles a grid boundary and stays strictly
ordered. On this corpus the x86-64/arm64 agreement is bought by the gap distribution, not by the
rounding. It is kept because it is measurably free (no metric and no per-item order moved) and sits
200× below the median gap, so it swallows no distinction the suite makes, while covering a denser
corpus later. Four decimals was measured and rejected: it creates ties that `chunk_id` resolves
against cosine order, trading real ranking for a guarantee that still would not be one.

The margin against the analytic bound is 1.13×. That is why the cross-architecture measurement stays
part of the procedure as the corpus grows. See
[ADR-0008](adr/0008-the-baseline-embedder-is-local-and-its-dimension-is-a-build-time-commitment.md)
for the full measurement and the precision sweep.

There is nothing to fuse — one signal, and reciprocal-rank fusion would only compress a readable
0..1 number into a rank reciprocal nobody can interpret. The `<=>` operator rather than the marginally faster `<#>`, even though the vectors are
unit length and the two order identically, because pgvector *negates* the inner product (Postgres
only scans an index ascending) and `components["cosine"]` would arrive negative needing a `* -1`
before a reader could believe it. In a demo whose product is the trace, a score that must be
sign-corrected before it can be read is a permanent trap.

### Dense does not abstain

This is the one behavioural difference between the arms that matters, and it is not a bug:

| | Question the corpus does not cover |
| --- | --- |
| `lexical` | returns **nothing** — `word_similarity` below 0.6 is dropped |
| `dense` | returns its **k least-distant vectors**, however distant |

Nearest-neighbour search has no notion of "no match". The zero-cost abstention — the model is never
called, there is no `generate` span — is therefore reachable under `lexical` and unreachable under
`dense`. **No floor is invented here**, because there is no measurement to set one from, and a
threshold picked to look right is a number the gate would then be defending. The evaluation gate is
what will show what this costs; deciding it is a later commit with a number behind it.

### The embedder

Vectors are stored in `embeddings(chunk_id, model_key, embedding vector(384))` — one row per chunk
per embedder, so a second embedder is a second `model_key` and not a second table
([ADR-0008](adr/0008-the-baseline-embedder-is-local-and-its-dimension-is-a-build-time-commitment.md)).
`model_key` is a query parameter, so Phase 4's fine-tuned embedder costs zero lines of SQL.

The HNSW index is partial on `model_key`, because pgvector applies the `WHERE` *after* walking the
index: a shared index with `ef_search = 40` and a predicate matching half the rows returns about
twenty candidates, not forty. It is created **after** the rows are inserted, and it is HNSW rather
than IVFFlat because IVFFlat trains centroids during `CREATE INDEX` and is structurally useless when
built over an empty table. **The contracted semantics are exact search; the index is an
optimisation** — at fifty-three chunks the planner scans and the search really is exact, which is
what lets the deterministic gate rest on it.

Ingestion and query provably use the same embedder, and this is the failure the whole design is
arranged around. Nothing about a 384-float vector says which model produced it: a database embedded
by one model and queried by another boots, serves, ranks, and is merely *quietly worse*. Two
mechanisms make it unwriteable rather than unlikely — a single factory, `embedding.embedder_for`,
that both `ingest.build` and `available_retrievers` call, and a `fingerprint` (sha256 over model id,
weights digest, tokenizer digest, dimension, sequence length, pooling, normalisation, both prefixes
and `EMBED_VERSION`) which ingestion stores in `embeddings_meta` and the boot gate compares:

```
the database was embedded by a different embedder than this code would query with.
  database: fingerprint 37211055… (model_key baseline, 384 dimensions)
  code:     fingerprint 9c2ab410… (model_key baseline, 384 dimensions)
Run `python -m garage ingest` to rebuild it.
```

A dimension check would not catch that, and the most likely divergence of all is the reason why: an
embedder that applies e5's `"passage: "` prefix on the query side has the same model, the same
weights and the same 384 dimensions, and simply retrieves worse. That is why `Embedder` has
`embed_query` and `embed_passages` and no generic `embed(texts)` — the design (§7.1) writes one
method, and with one method calling the wrong side is the *default* mistake rather than a discouraged
one.

## The interface, not the implementation

`Retriever` is `retrieve(query, k, filters) -> tuple[Candidate, ...]` and nothing else. Strategy is a
*runtime* axis ([ADR-0005](adr/0005-build-time-vs-runtime-axes.md)), so `dense` and `hybrid` arrive
as different objects behind that contract with no change to the endpoint and no change to the
response shape. The endpoint never learns which implementation it holds — the tests prove it by
running the whole HTTP path against a retriever that answers from memory.

The tier filter is part of the contract rather than something each implementation is trusted to
remember. An implementation that quietly ignored it would produce a comparison that means nothing.

## The trace

Every response carries an OpenTelemetry-compatible span tree — the trace **is** the product
(design §12), rendered beside the chunks in the demo:

```jsonc
{
  "traceId": "9f2c…", "spanId": "3b71…", "parentSpanId": null,
  "name": "query",
  "startTimeUnixNano": "1785…", "endTimeUnixNano": "1785…", "durationMs": 7.41,
  "attributes": {"query.question": "…", "corpus.hash": "21c4e571…", "query.candidates": 5},
  "children": [
    {
      "name": "retrieve", "parentSpanId": "3b71…", "durationMs": 7.02,
      "attributes": {
        "retrieval.strategy": "lexical", "retrieval.k": 5,
        "retrieval.tiers": "A,B", "retrieval.candidates": 5
      },
      "children": []
    }
  ]
}
```

`rerank` and `generate` are the other two stages the design names. They are **absent** rather than
empty: a span reporting zero milliseconds for a stage that does not exist would be a trace lying
about the pipeline it describes.

Compatible where compatibility is load-bearing — identifiers are OTel-shaped (16 hex characters for a
span, 32 for a trace), times are Unix nanoseconds, attribute keys are dotted namespaces. The nesting
is the one difference: OTLP is a flat list joined by `parentSpanId`, and this tree flattens to exactly
that. No SDK and no collector, because the VM runs neither (design §14); an exporter can be written
against this shape the day a Jaeger is actually running.

Durations are measured with a monotonic clock while the wall clock supplies the timestamps, so an NTP
correction mid-query cannot produce a negative duration. A stage that raised keeps its duration and
stays in the tree, with `error`, `exception.type` and `exception.message` on it — a trace that goes
silent exactly when something went wrong is worth very little.

## The boot gate

The service verifies the database is this commit's artifact before it serves anything, and refuses to
start otherwise ([ADR-0002](adr/0002-database-as-derived-artifact.md)):

```
the database holds a different Corpus than this checkout describes.
  database: corpus_hash 8ab3… (corpus_id fixture)
  manifest: corpus_hash 21c4… (from corpus/fixture)
Run `python -m garage ingest` to rebuild it.
```

Two numbers have to agree, and they fail differently
([ADR-0007](adr/0007-corpus-hash-and-ingest-version-are-separate.md)). A wrong `corpus_hash` means
the database holds *different material* than the manifest in this checkout describes — every citation
would name a document the reader cannot check. A wrong `ingest_version` means the same material
processed by *rules this code no longer implements* — the chunks are real, but they are not the chunks
this code would build, so a `chunk_id` in an evaluation set quietly points somewhere else.

Only the manifest is read, never the source documents: a real Corpus keeps its material on the
operator's disk and the serving container has none of it
([ADR-0003](adr/0003-no-redistribution-of-source-material.md)). That is sound because the hash is
taken over the catalogue, and ingestion verified the material against that catalogue before it wrote
a single row. `GARAGE_CORPUS_DIR` points at the manifest to check against; it defaults to the fixture
Corpus.

The check runs once, at boot, not per request. A service that checked per request would be one
willing to run against the wrong artifact as long as nobody asked it anything.

## Consequences for running it

`serve` now requires an ingested database and will crash-loop against an empty one, which is the
intended behaviour. Ingest first:

```sh
python -m garage embedder fetch     # once: 470 MB, sha256-verified, never committed
docker compose up -d postgres
docker compose run --rm serve python -m garage ingest
docker compose up --wait
```

`pg_trgm` **and** `vector` are created by ingestion rather than by `docker/initdb/`, because initdb
only runs when the data directory is empty: a developer whose volume predates a line added there
would get a database the server cannot query. Ingestion already owns DDL and is always re-run.
`vector` moved out of `docker/initdb/001-extensions.sql` — and out of the hand-copied duplicate in
`ci.yml` — when it stopped being optional, which is the commit that added `embeddings`.

Without the weights, `ingest` and `serve` refuse and name the command that fixes it. To build a
lexical-only artifact deliberately, set `GARAGE_EMBEDDER=none`: there are then no vectors, no dense
strategy on the endpoint and no dense arm in the run record, and `ingest` says so in its output. A
missing model is never silently degraded to lexical — that would report a configuration mistake as
a strategy that scores nothing.
