# Corpus identity and chunking rules are two numbers, not one

A run record has to be reproducible (ADR-0002), which means it must pin both *which material* was
searched and *how that material was turned into chunks* — retrieval results change if either moves.
The obvious design folds both into `corpus_hash`, so one digest determines everything downstream.

We keep them **separate**: `corpus_hash` covers the catalogue, and `INGEST_VERSION` covers the
chunking rules. Both are stored in `corpus_meta`, and a run record cites both.

The reason is that they answer to different owners. The catalogue changes when material is acquired
or re-catalogued — an operator action. The chunking rules change when this repository's code changes
— a commit. Folding the second into the first would mean every chunking tweak re-issues the identity
of a Corpus whose documents nobody touched, and the identity of a Corpus is exactly the thing that is
supposed to be stable across code changes.

## Consequences

- `INGEST_VERSION` in `src/garage/chunking.py` must go up whenever the rules change. A stored chunk
  built by the old rules is not the chunk the new code would produce, and nothing else notices.
- A run record that cites only `corpus_hash` is under-specified. Both numbers or neither.
- `corpus/jargon.yaml` is deliberately outside both: the terms it detects are stored on the chunk,
  so a vocabulary edit is a rules change and takes `INGEST_VERSION` with it — while editing a `notes`
  line, which changes nothing derived, does not invalidate a run record.
