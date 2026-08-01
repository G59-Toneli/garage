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
