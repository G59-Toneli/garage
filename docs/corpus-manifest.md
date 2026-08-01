# The Corpus manifest

A **Corpus** is the versioned, immutable set of source material an answer was derived from,
identified by a hash (`CONTEXT.md`). The manifest is its catalogue: one entry per document, enough to
cite the document, verify it, and reason about its rights status.

Garage does not redistribute third-party material (ADR-0003). So the manifest lives in git and the
documents do not — you point the tooling at your own copies. The manifest's job is to prove that the
copies you pointed at are the ones the catalogue describes.

## Layout

```
corpus/<name>/
  manifest.yaml        versioned in git
  sources/             the documents themselves — in git ONLY for the fixture
```

`sources/` is the default location. For real material, keep the documents wherever they already live
and pass `--sources`:

```bash
python -m garage corpus validate corpus/v1 --sources /mnt/manuais
```

## Validating

```bash
python -m garage corpus validate                 # the fixture Corpus
python -m garage corpus validate corpus/v1       # a real Corpus
```

Success prints the Corpus identity and exits `0`:

```
corpus_id:   fixture
documents:   5
corpus_hash: 21c4e571b96fefae82062b11d1cdd0f237b0b311d781d8a11f975d8b650b75d6
```

Validation is the gate, not the build. `python -m garage ingest` runs it again before writing
anything — see [ingestion.md](ingestion.md).

Failure lists *every* bad document — not just the first — on stderr and exits `1`:

```
corpus validation failed
2 of 5 documents failed verification:
  parts-gsi-1994: /mnt/manuais/parts-gsi-1994.md has sha256 9b89c194…, manifest expects dbe7481e…
  forum-swap-250s: missing source file /mnt/manuais/forum-swap-250s.md
```

## Format

```yaml
manifest_version: 1
corpus_id: fixture

documents:
  - doc_id: svc-kadett-1993
    title: Manual de Serviço — Kadett GSi 2.0 MPFI
    publisher: Garage fixture (invented)
    year: 1993
    tier: A
    provenance: Written for this repository as a stand-in for a service manual.
    filename: svc-kadett-1993.md
    sha256: 4682d9e89b3128d3e617471fc203099fe0be1c7c665d94af474abc18c768dbc8
    rights: original-work
```

### Top level

| Field              | Notes |
| ------------------ | ----- |
| `manifest_version` | Currently `1`. A future version is a hard failure, never a silent partial read. |
| `corpus_id`        | Short identifier, e.g. `fixture`, `v1`. |
| `documents`        | At least one entry. Order is irrelevant — see *Corpus hash*. |

### Per document

| Field        | Notes |
| ------------ | ----- |
| `doc_id`     | Stable, unique, URL-safe. Chunks and citations point at this forever; renaming it changes the Corpus. |
| `title`      | As printed on the document, in its own language. |
| `publisher`  | Publisher for Tier A; site or forum name for Tier B. |
| `year`       | Publication year (1900–2100). Edition year for a manual, post year for a thread. |
| `tier`       | `A` (checkable against a named publication) or `B` (community source). Nothing else. |
| `provenance` | Free text: where *this* copy came from — a URL, a scan, a purchase, an archive. This is the audit trail when the file itself cannot be shared. |
| `filename`   | Name inside the sources directory. Not a path — keep the sources directory flat. |
| `sha256`     | Lowercase hex digest of the original file, 64 characters. |
| `rights`     | Rights status, e.g. `original-work`, `copyright-gm-not-redistributed`, `forum-post-fair-use-excerpt-only`. Free text for now; what matters is that every document has an answer. |

Unknown fields are rejected. A typo'd key is a loud error rather than a value silently dropped from
the Corpus identity.

### Cataloguing real material by hand

For each document you own a copy of:

```bash
sha256sum "/mnt/manuais/manual-servico-kadett.pdf"
```

Add an entry with that digest, a `doc_id` you are willing to live with permanently, the honest
`provenance`, and the `rights` status. Then run `corpus validate --sources /mnt/manuais`. Tier A
material stays on your disk; only the catalogue, the extracted facts, and short attributed Tier B
excerpts are ever committed.

## Corpus hash

`corpus_hash` is a sha256 over a canonical JSON serialisation of the manifest: keys sorted, documents
sorted by `doc_id`, UTF-8. It is what a run record cites, and what the server checks at boot against
the database artifact it is serving (ADR-0002).

The fixture's digest is pinned in `tests/test_corpus.py`, which is what makes the cross-machine
guarantee testable rather than aspirational: CI runs on Linux, the author works on Windows, and both
must produce the same value.

It **is** sensitive to:

- any document's `sha256` — so editing a source file changes the Corpus;
- any catalogue metadata — retiering a document from B to A is a different Corpus, because it changes
  what the retrieval pipeline is allowed to do with it;
- a document appearing or disappearing.

It is **not** sensitive to:

- the order documents appear in the YAML file;
- whitespace, comments, or quoting style;
- the directory the Corpus was loaded from, or the machine loading it;
- files sitting in the sources directory that the manifest never claimed. Validation checks catalogued
  documents only, because a real Corpus points at a directory of the operator's own material that
  holds plenty this Corpus is not made of. The manifest defines the Corpus; the directory contains it.

Once ingestion produces derived artifacts — facts, attributed excerpts, chunks — those become part of
the identity too, so that re-chunking the same sources yields a different Corpus. That extension lands
with ingestion; today the catalogue is the whole story because nothing is derived from it yet.

## The fixture Corpus

`corpus/fixture/` holds five invented documents — three Tier A, two Tier B — with a specification
table, a numbered procedure, part numbers, and Jargon (`swap 250-S`, `projetinho de rua`). No real
person wrote any of it and every figure is fictional and deliberately implausible.

It is permanent, not scaffolding. Unit tests must never depend on copyrighted material or on which
PDFs happen to be on a given machine, so the fixture stays as the deterministic test base even after
the real Tier A corpus arrives.
