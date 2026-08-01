# The repository does not redistribute third-party source material

The most valuable Tier A material for a 1993 Kadett GSi is a GM service manual, and Tier B material
is written by forum authors. Both are copyrighted, and a public repository that ships them is
infringing. Facts, however, are not copyrightable — only their expression is. The repository
therefore publishes a **manifest** (title, publisher, year, tier, rights, `sha256` of the original),
the **extracted facts** with pointers to document and page, short **attributed excerpts** for Tier B
only, and the **ingestion script** — but never the source documents themselves.

## Consequences

- Anyone cloning the repository points the pipeline at their own copy of the material; the manifest
  verifies hashes and rejects a divergent file.
- `corpus/` is a manifest plus derived facts, not a folder of PDFs. This forces a clean split
  between *acquisition* (manual, offline, the author's problem) and *ingestion* (deterministic,
  scripted, reproducible) that the project wanted anyway.
- Evaluation sets are safe to publish in full, since a question and an expected value are facts.
