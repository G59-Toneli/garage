"""The Corpus: its catalogue, its verification, and its identity.

A Corpus is the versioned, immutable set of source material an answer was derived from, identified
by a hash (CONTEXT.md). This module owns three things:

- the **manifest** format every Corpus must satisfy — one catalogue entry per document;
- **verification** — every document on disk hashes to the `sha256` the manifest recorded;
- the **corpus hash** — one digest standing for the whole Corpus, so a run record can point at
  exactly the material it was measured against (ADR-0002).

The documents themselves are deliberately *not* assumed to live in the repository. Garage does not
redistribute third-party material (ADR-0003), so a real Corpus keeps its manifest in git and its
documents on the operator's own disk; `sources_dir` is what joins the two. The fixture Corpus is the
one case where the documents happen to be checked in, because they were written for this repository.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Resolved from this file rather than the working directory: tests and CLI invocations must find the
# same fixture no matter where they were started from.
FIXTURE_CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixture"

MANIFEST_FILENAME = "manifest.yaml"
SOURCES_DIRNAME = "sources"


class CorpusError(Exception):
    """A Corpus is unusable: malformed catalogue, or material that does not match it."""


class Document(BaseModel):
    """One catalogue entry. Every field here is part of the Corpus identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    year: int = Field(ge=1900, le=2100)
    tier: Literal["A", "B"]
    provenance: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rights: str = Field(min_length=1)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # `Literal[1]` is the version gate: a manifest from the future fails to validate rather than
    # being read partially, because silently ignoring fields we do not understand would mean two
    # different Corpora sharing a corpus hash.
    manifest_version: Literal[1]
    corpus_id: str = Field(min_length=1)
    documents: tuple[Document, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _doc_ids_are_unique(self) -> Manifest:
        duplicates = sorted(
            doc_id for doc_id, count in Counter(d.doc_id for d in self.documents).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate doc_id: {', '.join(duplicates)}")
        return self


@dataclass(frozen=True)
class ValidationReport:
    """What a successful validation is allowed to claim."""

    corpus_id: str
    corpus_hash: str
    document_count: int


def load_manifest(corpus_dir: Path) -> Manifest:
    """Read and validate the catalogue. Does not touch the documents themselves."""
    path = Path(corpus_dir) / MANIFEST_FILENAME
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise CorpusError(f"no {MANIFEST_FILENAME} in {corpus_dir}") from missing
    except yaml.YAMLError as broken:
        raise CorpusError(f"{path} is not valid YAML: {broken}") from broken

    try:
        return Manifest.model_validate(raw)
    except ValidationError as invalid:
        raise CorpusError(f"{path} does not satisfy the manifest schema:\n{invalid}") from invalid


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        # Read in blocks so a scanned manual never has to fit in memory.
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_manifest_bytes(manifest: Manifest) -> bytes:
    """Serialise a manifest to the exact bytes the corpus hash is taken over.

    This function *is* the definition of Corpus identity: two Corpora share a hash if and only if
    they serialise identically here. Three choices make that identity trustworthy:

    - **JSON, not hand-rolled delimiters.** Titles and provenance are free text; any separator we
      invented could appear inside a value and let two different Corpora collide. JSON escaping
      removes the question.
    - **Sorted keys and documents sorted by `doc_id`.** The order documents happen to appear in the
      YAML file is an editing accident, not a property of the material.
    - **Field names included, via `model_dump()`.** The day a field is added to `Document` the hash
      changes — which is correct, because the catalogue then says something it did not say before.
    """
    payload = {
        "manifest_version": manifest.manifest_version,
        "corpus_id": manifest.corpus_id,
        "documents": sorted(
            (document.model_dump(mode="json") for document in manifest.documents),
            key=lambda document: document["doc_id"],
        ),
    }
    # `ensure_ascii=False` with an explicit UTF-8 encode: the vocabulary is Brazilian Portuguese, and
    # accented titles should hash as the characters they are rather than as escape sequences.
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def corpus_hash(manifest: Manifest) -> str:
    """The identity of a Corpus: one hex sha256 digest over its canonical catalogue.

    The digest covers the catalogue only. How that material was *processed* is a second number,
    `INGEST_VERSION`, stored beside this one in `corpus_meta`: the hash says which material this is,
    the ingest version says which rules turned it into chunks, and a run record cites both
    (ADR-0007).
    """
    return hashlib.sha256(_canonical_manifest_bytes(manifest)).hexdigest()


def validate_corpus(corpus_dir: Path, sources_dir: Path | None = None) -> ValidationReport:
    """Check the catalogue against the material on disk and report the Corpus identity.

    Raises `CorpusError` listing *every* document that failed, not just the first: an operator
    re-pointing the pipeline at their own material wants the whole list in one pass.

    Only catalogued documents are checked. Extra files in `sources_dir` are ignored deliberately —
    a real Corpus points at a directory of the operator's own material, which will hold plenty that
    this Corpus never claimed. The manifest defines the Corpus; the directory merely contains it.
    """
    corpus_dir = Path(corpus_dir)
    sources = Path(sources_dir) if sources_dir is not None else corpus_dir / SOURCES_DIRNAME

    manifest = load_manifest(corpus_dir)

    failures: list[str] = []
    for document in manifest.documents:
        path = sources / document.filename
        if not path.is_file():
            failures.append(f"{document.doc_id}: missing source file {path}")
            continue
        found = _file_digest(path)
        if found != document.sha256:
            failures.append(
                f"{document.doc_id}: {path} has sha256 {found}, manifest expects {document.sha256}"
            )

    if failures:
        raise CorpusError(
            f"{len(failures)} of {len(manifest.documents)} documents failed verification:\n  "
            + "\n  ".join(failures)
        )

    return ValidationReport(
        corpus_id=manifest.corpus_id,
        corpus_hash=corpus_hash(manifest),
        document_count=len(manifest.documents),
    )
