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
