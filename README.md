# Garage

Garage answers technical questions about one car — a 1993 Chevrolet Kadett GSi — and shows its own
work. Ask it something, and it does not just reply: it displays the documents it went looking in,
the passages it pulled out, the score each one got, and how long every step took. Change the way it
searches, and you see the answer change alongside it, side by side with the old one.

The point is not the answering. The point is that the quality of an answering system can be
**measured** rather than asserted. The working thesis of this project is that an AI feature is a
system with a quality contract — something you can put a number on, regress against in CI, and
audit after the fact — and not a prompt that seemed to work when someone tried it. A single car with
a small, awkward, partly Brazilian-Portuguese corpus is a good place to prove that, because nothing
about it is already in a model's training data.

## Status: Phase 1 of 5

The project ships in five vertical slices. Phase 1 is the honest baseline: ingest the verifiable
technical sources into Postgres, put two retrieval strategies head to head, gate the whole thing on
a deterministic evaluation run in CI, and deploy it publicly with the traces visible.

**Built so far:** the design spec, the domain vocabulary, and the architectural decisions. That is
all — this is early. **Not built yet:** ingestion, retrieval, the evaluation gate, the comparison
UI, and the public deployment. Later phases add hybrid retrieval and reranking (2), community
sources with a citation contract (3), a fine-tuned embedder (4), and a historical dashboard with the
consolidated write-up (5).

There are no benchmark numbers in this README yet, because there is no run record to derive them
from. When there is, they will link to the run that produced them.

## Running it locally

```sh
docker compose up --wait
```

That is the whole setup: Postgres with pgvector installed, dependencies built, and the service
answering on <http://localhost:8000/health>. Run the suite with `docker compose exec serve pytest`.
Every setting is read from the environment and every one has a working default; copy
`.env.example` to `.env` only when you want to change one. Nothing secret is committed.

Python is pinned to **3.12** in the container ([ADR-0006](docs/adr/0006-single-language-python-serving.md)),
so the container is the supported way to run the suite — a newer local interpreter outruns the
machine-learning wheels this project will need. The image builds for `linux/arm64` as well as
`linux/amd64`, because the deployment target is a free ARM VM
([ADR-0001](docs/adr/0001-architecture-characteristics.md)).

## Where the reasoning lives

- **[Design spec](docs/superpowers/specs/2026-08-01-garage-design.md)** — the full design: scope,
  corpus strategy, evaluation, UI, phases. Written in Brazilian Portuguese; everything public is in
  English.
- **[CONTEXT.md](CONTEXT.md)** — the domain vocabulary. Terms like *corpus*, *fact*, *recipe*, and
  *abstention* are used precisely throughout the code and docs, and this is where they are defined.
- **[docs/adr/](docs/adr/)** — the architectural decisions and what forced them:
  - [0001](docs/adr/0001-architecture-characteristics.md) — architecture characteristics and explicit non-goals
  - [0002](docs/adr/0002-database-as-derived-artifact.md) — the database is a derived artifact, not the source of truth
  - [0003](docs/adr/0003-no-redistribution-of-source-material.md) — the repository does not redistribute third-party source material
  - [0004](docs/adr/0004-two-layer-evaluation.md) — evaluation runs in two layers: a deterministic CI gate and an on-demand judge
  - [0005](docs/adr/0005-build-time-vs-runtime-axes.md) — configuration axes are split by whether they cost an index
  - [0006](docs/adr/0006-single-language-python-serving.md) — serving is a single-language Python monolith

## No source material is redistributed here

The most useful material about this car is a GM service manual and posts written by people on
forums. All of it is copyrighted, and a public repository that ships copies of it is infringing.
Facts are not copyrightable, though — only the way they are expressed is.

So this repository publishes a **manifest** of the sources (title, publisher, year, tier, rights,
and the `sha256` of the original file), the **facts extracted** from them with pointers back to
document and page, short **attributed excerpts** from community sources only, and the **ingestion
script**. It never publishes the documents. Cloning the repository and pointing the pipeline at your
own copies reproduces the corpus; the manifest verifies each hash and rejects a file that does not
match. See [ADR-0003](docs/adr/0003-no-redistribution-of-source-material.md).
