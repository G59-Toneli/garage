# Garage

A reproducible retrieval benchmark and glass-box demo built on a corpus about a 1993 Chevrolet
Kadett GSi. Read `CONTEXT.md` for the domain vocabulary and `docs/adr/` for the decisions that
constrain the design. The full design lives in `docs/superpowers/specs/`.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues in `G59-Toneli/garage`, operated through the `gh` CLI. See
`docs/agents/issue-tracker.md`.

### Triage labels

Canonical vocabulary, unchanged — `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Corpus

The manifest format, the `corpus_hash`, and how to catalogue real material by hand are documented in
`docs/corpus-manifest.md`. `python -m garage corpus validate` is the gate — nothing downstream should
run against material it has not verified. The fixture Corpus in `corpus/fixture/` is permanent: tests
never depend on copyrighted material or on which PDFs happen to be on a given machine (ADR-0003).

## Database

`python -m garage ingest` rebuilds the whole database from a verified Corpus — always a full
rebuild, always safe to re-run, and nothing writes to the ingested tables at runtime (ADR-0002; the
schema enforces it). Structure-aware chunking, the Jargon vocabulary and the stored `corpus_hash`
are documented in `docs/ingestion.md`.

## Serving

`python -m garage serve` answers `POST /query` with ranked chunks and the span tree behind them —
no language model anywhere, so retrieval stays measurable on its own (ADR-0004). `Retriever` is the
interface everything else drops in behind; the endpoint must never learn which implementation it
holds. The lexical strategy, the rank fusion, the trace format and the boot gate are documented in
`docs/retrieval.md`. The service refuses to start against a database that is not this commit's
artifact, so `ingest` runs before `serve`.

## Generation

`POST /query` also returns an `answer` when a `Generator` is configured — prose assembled from
claims, every one of them citing chunks by number, every citation validated against the chunks that
were actually retrieved before it reaches the wire. Abstention is a first-class result served with
200, degradation is a separate flag from it, and neither is ever an error page. The citation
contract, the validation rules, the dated price table and what is deliberately **not** covered by
tests are documented in `docs/generation.md`. Generation is optional at every level: `google-genai`
is an optional extra imported late, no key means no generator and no `generate` span, and no test in
`tests/` may import the SDK or touch the network.
