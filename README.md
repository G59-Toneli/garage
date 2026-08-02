# Garage

**English** · [Português (BR)](README.pt-BR.md)

A reproducible retrieval benchmark and glass-box demo built on a corpus about one car: a 1993
Chevrolet Kadett GSi.

---

## What this is

Garage answers technical questions about a single car and shows its own work. Ask it something and
it does not just reply: it displays the documents it searched, the passages it pulled out, the score
each one received, and how long every step took. Change the way it searches, and the new answer
appears beside the old one so you can see what the change bought you.

The point is not the answering. The point is that the quality of an answering system can be
**measured** rather than asserted. The thesis of this project is that an AI feature is a system with
a quality contract — something you can put a number on, regress against in CI, and audit after the
fact — not a prompt that seemed to work when someone tried it.

A single old car is a deliberately awkward subject. The useful material is a scanned service manual
and forum posts written in Brazilian Portuguese workshop slang; the questions real people ask
("dá pra fazer swap de 250-S?") share almost no vocabulary with the manuals that answer them. Nothing
about it is comfortably present in a general model's training data, which is exactly what makes it a
fair test.

**Not in scope**, permanently: more than one vehicle, user accounts, multi-turn chat history, visitor
document upload, a mobile app, or monetisation.

## Status: Phase 1 of 5

The project ships as five vertical slices, each ending in a public write-up.

| Phase | Slice | State |
|---|---|---|
| 0 | Corpus spike — gather Tier A material and hand-write 20 fact questions with exact answers. Project gate. | done |
| **1** | **Honest baseline** — ingestion → Postgres/pgvector → `lexical` vs `dense` → deterministic CI gate → two-column UI with traces → public deploy. | **in progress** |
| 2 | Hybrid retrieval and reranking — RRF in SQL, hosted reranker, more columns, advanced panel. | planned |
| 3 | Tier B and the citation contract — community ingestion with attribution, tier filter, calibrated judge, abstention set. | planned |
| 4 | Embedder fine-tune — synthetic pairs, hard-negative mining, second `model_key`, gain measured by the gate. | planned |
| 5 | Historical series and close-out — dashboard, consolidated README, retrospective. | planned |

**Built so far:** the design spec, the domain vocabulary, the architectural decisions, the project
skeleton — a FastAPI service, Postgres with pgvector in Compose, and CI running tests plus a
multi-arch image build; ingestion — one command rebuilds the whole database from a verified corpus
with structure-aware chunking ([docs/ingestion.md](docs/ingestion.md)); and the first end-to-end
path — `POST /query` returns ranked chunks with score, tier, document and page, plus the span tree
behind them, served by lexical retrieval with no language model anywhere
([docs/retrieval.md](docs/retrieval.md)); and the deterministic evaluation gate — 76 committed fact
questions scored on every push, with a baseline the build fails against
([docs/evaluation.md](docs/evaluation.md)).

**Not built yet:** dense and hybrid retrieval, the judge layer, the comparison UI, and the public
deployment. The only numbers in this README come from a run record in `eval/runs/`; nothing here is
typed by hand.

## How it works

```
                     ┌───────────────────────────────────────┐
  your local  ─────▶ │ ingest/   (Python, offline)           │
   material          │  manifest → hash verification         │
                     │  extraction → jargon normalisation    │
                     │  structure-aware chunking             │
                     │  embedding (baseline | fine-tuned)    │
                     └──────────────────┬────────────────────┘
                                        │ deterministic build
                                        ▼
                     ┌───────────────────────────────────────┐
                     │ Postgres + pgvector   (artifact)      │
                     │  read-only at runtime                 │
                     │  boot validates corpus_hash           │
                     └──────────────────┬────────────────────┘
                                        │
   ┌────────────────────────────────────┼────────────────────────────────┐
   │ serve/   (FastAPI, modular monolith)                                │
   │                                                                     │
   │   Retriever  ◀interface▶  lexical │ dense │ hybrid (RRF)            │
   │   Reranker   ◀interface▶  none │ hosted                             │
   │   Generator  ◀interface▶  citation contract, explicit abstention    │
   │   Tracer     ─ OpenTelemetry spans per stage                        │
   └────────────────────────────────────┬────────────────────────────────┘
                                        ▼
                     ┌───────────────────────────────────────┐
                     │ web/   (static build)                 │
                     │  comparison columns · chunks + scores │
                     │  trace · Evals tab · "re-run now"     │
                     └───────────────────────────────────────┘
```

Each interface is a deep module: a small contract, a swappable implementation, testable in isolation
against a synthetic corpus.

- **`Retriever`** — `retrieve(query, k, filters) -> list[Candidate]`. `lexical` (`tsvector` +
  `pg_trgm`), `dense` (pgvector `<=>`), `hybrid` (RRF in SQL).
- **`Embedder`** — `embed(texts) -> matrix`. The *same* implementation runs at ingestion and at query
  time; divergence there is the classic source of silent retrieval bugs.
- **`Reranker`** — `rerank(query, candidates) -> list[Candidate]`.
- **`Generator`** — `generate(query, context, contract) -> Answer`, returning claims with mandatory
  citations and an explicit abstention signal.

**The database is a derived artifact, not the source of truth.** `corpus/v1/` is the truth; the
database is built from it by a deterministic pipeline and nothing writes to it at runtime. The
service validates `corpus_hash` at boot and refuses to start if it diverges from the commit. That is
what makes a "real database" compatible with reproducibility: it is hashable because it is
rebuildable. See [ADR-0002](docs/adr/0002-database-as-derived-artifact.md).

## The corpus, and why no source material is here

The most useful material about this car is a GM service manual and posts written by people on
forums. All of it is copyrighted, and a public repository that ships copies is infringing. Facts are
not copyrightable, though — only the way they are expressed is.

So this repository publishes:

- `corpus/v1/manifest.yaml` — per document: identifier, title, publisher, year, provenance, `sha256`
  of the original file, tier, and rights status.
- `corpus/v1/facts/` — extracted facts in structured form, each pointing back to document and page.
- `corpus/v1/excerpts/` — short attributed excerpts, community sources only.
- `ingest/` — the deterministic script that rebuilds the corpus from your local files.

It never publishes the documents themselves. Clone the repository, point the pipeline at your own
copies, and the manifest verifies each hash and rejects a file that does not match. See
[ADR-0003](docs/adr/0003-no-redistribution-of-source-material.md).

Sources are graded by **tier**, and the tier label is visible on every citation in the interface — a
service manual and a forum post must never look alike on screen:

- **Tier A** — checkable against a named publication: service manual, owner's manual, parts
  catalogue, published specification sheet.
- **Tier B** — community: forum thread, blog post, group discussion. Real knowledge that exists
  nowhere else, with lower authority.

Chunking is **structure-aware**. A specification table is sliced by row, so a single spec is never
cut in half; a procedure is sliced by step; prose is sliced by paragraph with overlap. `python -m
garage ingest` rebuilds the entire database from a verified corpus in one transaction — see
[docs/ingestion.md](docs/ingestion.md).

## Configuration axes

A **configuration** is one concrete combination of pipeline choices. They split by whether changing
one costs you an index:

**Build-time** (expensive — rebuilds the index)

- chunking strategy — one in the MVP
- embedder — `baseline` | `finetuned`

**Runtime** (free — a `WHERE` clause, not a redeploy)

- strategy — `lexical` | `dense` | `hybrid`
- reranker — `none` | hosted
- tier filter — `A` | `A+B`
- prompt contract — `mandatory citation` | `free` (the second exists only to demonstrate the
  contrast, and is never the default)

Two embedders coexist in one `embeddings` table keyed by `model_key`, which is only possible because
the fine-tuned model is derived from the baseline and preserves its dimension. The interface offers
four named **presets** up front and an advanced panel for manual combinations. See
[ADR-0005](docs/adr/0005-build-time-vs-runtime-axes.md).

## Evaluation

Evaluation is a first-class citizen here, not a loose script. It runs in two layers
([ADR-0004](docs/adr/0004-two-layer-evaluation.md)):

| Layer | Measures | Uses an LLM? | When |
|---|---|---|---|
| **Deterministic gate** | `recall@k`, MRR, nDCG — retrieval only | no | every commit, in CI, seconds, zero cost |
| **Generation evaluation** | groundedness, citation accuracy, correct abstention | yes, a judge | on demand, locally; output committed |

The gate breaks the build on a retrieval regression. It is where the fine-tuning argument lives, and
it needs no API call to run. `python -m garage eval gate` is that gate, and it runs in CI after
`ingest`; the fact format, the run record format and the promotion procedure are documented in
[docs/evaluation.md](docs/evaluation.md).

The `lexical` baseline over the fixture corpus, from
[`eval/runs/20260802T011346Z-d8dcb021b722.json`](eval/runs/20260802T011346Z-d8dcb021b722.json):

| `recall@1` | `recall@5` | `recall@10` | `mrr@10` | `nDCG@10` |
|---|---|---|---|---|
| 0.427632 | 0.447368 | 0.447368 | 0.440789 | 0.442512 |

Over 76 hand-written questions, and the average hides the finding. Split by how the question is
phrased, `recall@10` is **0.912** for keyword queries and **0.071** for whole sentences: the lexical
strategy answers a bag of words almost perfectly and answers a question almost never. That is the
honest state of the baseline, and it is why the number is low rather than flattering — an earlier
version of the fact set was all keyword queries, scored 0.91, and would have frozen that bug in place
as a floor. See [docs/evaluation.md](docs/evaluation.md).

Three evaluation sets:

- `eval/facts.jsonl` — question → exact value plus the correct `chunk_id`. Scored by numeric match
  within a declared tolerance, plus `recall@k`. **Built** — see
  [docs/evaluation.md](docs/evaluation.md).
- `eval/recipes.jsonl` — open question → rubric. Scored on groundedness, citation accuracy, and
  correct abstention.
- `eval/abstention.jsonl` — questions deliberately *outside* the corpus. Success is refusing to
  answer. **Abstention is a first-class success, not a failure.**

**Non-determinism is reported, never hidden.** No single point value is ever published for the
stochastic layer: each question runs `k` times and the interface shows mean and spread. Every result
is a **run record** — `run_id`, `git_sha`, `corpus_hash`, config, model id, temperature, judge model,
prompt version, `n`, timestamps, metrics, and per-item detail — generated by execution and never
written by hand. Any number in the interface without a trace back to a run record is a bug.

**Two commitments about the judge, stated here because they are the ones easiest to quietly break:**

1. The judge is **cross-family** with respect to the generator — a different vendor's model grades
   the answers, to reduce self-preference bias.
2. **An uncalibrated judge does not count.** Roughly 20 items are hand-labelled by the author and the
   judge↔human agreement rate is published. Without that number, no generation metric is reported at
   all.

And one about contamination: **evaluation sets are written by a human and are never generated by the
same model that produces training data.** Provenance is recorded in the file itself.

## Running it locally

```sh
docker compose up -d postgres
docker compose run --rm serve python -m garage ingest   # build the artifact first
docker compose up --wait
```

Postgres with pgvector installed, the fixture corpus ingested, and the service answering on
<http://localhost:8000/health> and <http://localhost:8000/query>
([docs/retrieval.md](docs/retrieval.md)). The ingest step is not optional: the service verifies
`corpus_hash` at boot and refuses to start against a database that is not this commit's artifact.
Run the suite with `docker compose exec serve pytest`, and the evaluation gate with
`docker compose exec serve python -m garage eval gate` — it needs no API key and no network.
Every setting is read from the environment and every one has a working default; copy `.env.example`
to `.env` only when you want to change one. Nothing secret is committed.

Python is pinned to **3.12** in the container
([ADR-0006](docs/adr/0006-single-language-python-serving.md)), so the container is the supported way
to run the suite — a newer local interpreter outruns the machine-learning wheels this project will
need. The image builds for `linux/arm64` as well as `linux/amd64`, because the deployment target is a
free ARM VM ([ADR-0001](docs/adr/0001-architecture-characteristics.md)).

## Repository layout

```
compose.yaml            Postgres + the service, the only supported way to run it
Dockerfile              multi-arch image, Python pinned to 3.12
src/garage/             the service: config, ASGI app, pipeline modules
tests/                  pytest suite, run in CI against a real Postgres
docker/initdb/          extensions installed on first database boot
corpus/                 manifest, extracted facts, excerpts — never source documents
corpus/jargon.yaml      the curated workshop vocabulary, term → canonical
eval/facts.jsonl        the committed fact questions the CI gate scores
eval/baseline.json      the numbers to beat and the thresholds, both reviewable in a diff
eval/runs/              run records, one file per run, generated and never hand-written
docs/adr/               architectural decisions and what forced them
docs/superpowers/specs/ the full design document
CONTEXT.md              the domain vocabulary
```

## What drives the design

Every technical decision here should be derivable from this list. A decision that is not derivable
from it is personal preference and has to be labelled as such
([ADR-0001](docs/adr/0001-architecture-characteristics.md)).

1. **Reproducibility and auditability** — every number on display traces back to a `corpus_hash`, a
   commit SHA, and a run record. This is the thesis.
2. **Testability** — evaluation runs as a CI gate.
3. **Modifiability** — swap retriever, embedder, or reranker without touching anything else.
4. **Observability** — every query emits a span tree with timing, tokens, cost, and candidates per
   stage, rendered in the interface itself. The trace *is* the product.
5. **Portability** — the same image runs on the author's Windows machine and on a free ARM VM. No
   proprietary managed services.
6. **Cost** — a hard ceiling of zero. Cost is a first-class requirement, not a limitation.

**Explicitly not characteristics of this system:** scalability, high availability, elasticity,
multi-user security, and latency as an SLO. Latency here is *observable*, not a commitment.

## Where the reasoning lives

- **[Design spec](docs/superpowers/specs/2026-08-01-garage-design.md)** — the full design: scope,
  corpus strategy, evaluation, interface, phases. Written in Brazilian Portuguese; everything public
  is in English.
- **[CONTEXT.md](CONTEXT.md)** — the domain vocabulary. Terms like *corpus*, *fact*, *recipe*, and
  *abstention* are used precisely throughout the code and docs, and this is where they are defined.
- **[docs/adr/](docs/adr/)** — the architectural decisions:
  - [0001](docs/adr/0001-architecture-characteristics.md) — architecture characteristics and explicit non-goals
  - [0002](docs/adr/0002-database-as-derived-artifact.md) — the database is a derived artifact, not the source of truth
  - [0003](docs/adr/0003-no-redistribution-of-source-material.md) — the repository does not redistribute third-party source material
  - [0004](docs/adr/0004-two-layer-evaluation.md) — evaluation runs in two layers: a deterministic CI gate and an on-demand judge
  - [0005](docs/adr/0005-build-time-vs-runtime-axes.md) — configuration axes are split by whether they cost an index
  - [0006](docs/adr/0006-single-language-python-serving.md) — serving is a single-language Python monolith
  - [0007](docs/adr/0007-corpus-hash-and-ingest-version-are-separate.md) — corpus identity and chunking rules are two numbers, not one

## Language

Code, README, ADRs, and write-ups are in English. The corpus, the jargon, and the evaluation
questions are in Brazilian Portuguese — there, the vocabulary is the object of study, not noise. This
README is also available in [Português (BR)](README.pt-BR.md); the English version is canonical.

## Licence

Code is [MIT](LICENSE). Derived datasets carry a declared open licence. Third-party source material
is not redistributed — see [ADR-0003](docs/adr/0003-no-redistribution-of-source-material.md).
