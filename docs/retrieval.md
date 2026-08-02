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
docker compose up -d postgres
docker compose run --rm serve python -m garage ingest
docker compose up --wait
```

`pg_trgm` is created by ingestion rather than by `docker/initdb/`, where `vector` lives, because
initdb only runs when the data directory is empty: a developer whose volume predates that line would
otherwise get a database the server cannot query. Ingestion already owns DDL and is always re-run.
