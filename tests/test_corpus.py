"""Tests for manifest validation and the corpus hash.

The fixture Corpus is the deterministic base for these tests, so most of them run against the real
`corpus/fixture` directory rather than a temporary one. Tests that need a *broken* Corpus copy the
fixture into `tmp_path` and damage the copy; the fixture in the repository is never mutated.
"""

from pathlib import Path

import pytest

from garage.corpus import (
    FIXTURE_CORPUS,
    CorpusError,
    corpus_hash,
    load_manifest,
    validate_corpus,
)

# The fixture Corpus is frozen material, so its identity is a constant that can be checked rather
# than merely recomputed. This is the cross-machine guarantee: CI runs on Linux, the author works on
# Windows, and both must produce this digest or the Corpus is not reproducible after all. It changes
# only when the fixture or the canonical serialisation deliberately changes.
FIXTURE_CORPUS_HASH = "21c4e571b96fefae82062b11d1cdd0f237b0b311d781d8a11f975d8b650b75d6"


class TestLoadManifest:
    def test_reads_every_fixture_document(self):
        manifest = load_manifest(FIXTURE_CORPUS)

        assert manifest.corpus_id == "fixture"
        assert [document.doc_id for document in manifest.documents] == [
            "svc-kadett-1993",
            "owner-kadett-1993",
            "parts-gsi-1994",
            "forum-swap-250s",
            "blog-projetinho-de-rua",
        ]

    def test_the_fixture_covers_both_tiers(self):
        manifest = load_manifest(FIXTURE_CORPUS)
        tiers = {document.tier for document in manifest.documents}

        assert tiers == {"A", "B"}

    def test_the_fixture_carries_a_specification_table_and_jargon(self):
        """The fixture only earns its keep if it exercises what the pipeline finds hard."""
        sources = FIXTURE_CORPUS / "sources"
        text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(sources.glob("*.md")))

        assert "| Torque (N·m) |" in text  # a specification table, sliced one spec per chunk
        assert "swap 250-S" in text  # Jargon absent from any formal text
        assert "projetinho de rua" in text

    def test_rejects_an_unknown_tier(self, corpus_copy: Path):
        path = corpus_copy / "manifest.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("tier: A", "tier: C", 1), encoding="utf-8")

        with pytest.raises(CorpusError, match="tier"):
            load_manifest(corpus_copy)

    def test_rejects_a_duplicate_doc_id(self, corpus_copy: Path):
        path = corpus_copy / "manifest.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("doc_id: parts-gsi-1994", "doc_id: svc-kadett-1993"),
            encoding="utf-8",
        )

        with pytest.raises(CorpusError, match="svc-kadett-1993"):
            load_manifest(corpus_copy)

    def test_reports_a_missing_manifest_by_path(self, tmp_path: Path):
        with pytest.raises(CorpusError, match="manifest.yaml"):
            load_manifest(tmp_path)


class TestValidateCorpus:
    def test_the_fixture_validates(self):
        report = validate_corpus(FIXTURE_CORPUS)

        assert report.document_count == 5
        assert report.corpus_hash == corpus_hash(load_manifest(FIXTURE_CORPUS))

    def test_a_tampered_source_file_fails_and_names_the_document(self, corpus_copy: Path):
        source = corpus_copy / "sources" / "svc-kadett-1993.md"
        source.write_text(source.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")

        with pytest.raises(CorpusError) as failure:
            validate_corpus(corpus_copy)

        assert "svc-kadett-1993" in str(failure.value)

    def test_a_missing_source_file_fails_and_names_the_document(self, corpus_copy: Path):
        (corpus_copy / "sources" / "forum-swap-250s.md").unlink()

        with pytest.raises(CorpusError) as failure:
            validate_corpus(corpus_copy)

        assert "forum-swap-250s" in str(failure.value)

    def test_reports_every_bad_document_not_only_the_first(self, corpus_copy: Path):
        (corpus_copy / "sources" / "forum-swap-250s.md").unlink()
        (corpus_copy / "sources" / "parts-gsi-1994.md").unlink()

        with pytest.raises(CorpusError) as failure:
            validate_corpus(corpus_copy)

        assert "forum-swap-250s" in str(failure.value)
        assert "parts-gsi-1994" in str(failure.value)

    def test_sources_may_live_outside_the_corpus_directory(self, corpus_copy: Path, detached_sources: Path):
        """A real Corpus keeps its documents on the operator's disk, never in git (ADR-0003)."""
        report = validate_corpus(corpus_copy, sources_dir=detached_sources)

        assert report.document_count == 5

    def test_ignores_files_the_manifest_never_claimed(self, corpus_copy: Path):
        """The operator's material directory holds plenty this Corpus is not made of."""
        (corpus_copy / "sources" / "unrelated-invoice.md").write_text("not ours", encoding="utf-8")

        assert validate_corpus(corpus_copy).document_count == 5


class TestCorpusHash:
    def test_the_fixture_hashes_to_its_recorded_identity(self):
        """The digest is a property of the Corpus, not of the machine that computed it."""
        assert corpus_hash(load_manifest(FIXTURE_CORPUS)) == FIXTURE_CORPUS_HASH

    def test_is_stable_across_repeated_runs(self):
        manifest = load_manifest(FIXTURE_CORPUS)

        assert corpus_hash(manifest) == corpus_hash(manifest)

    def test_is_stable_across_reloads_of_the_same_manifest(self, corpus_copy: Path):
        """A different directory on a different machine is still the same Corpus."""
        assert corpus_hash(load_manifest(corpus_copy)) == corpus_hash(load_manifest(FIXTURE_CORPUS))

    def test_ignores_the_order_documents_happen_to_appear_in(self, corpus_copy: Path):
        manifest = load_manifest(corpus_copy)
        reversed_manifest = manifest.model_copy(update={"documents": list(reversed(manifest.documents))})

        assert corpus_hash(reversed_manifest) == corpus_hash(manifest)

    def test_ignores_yaml_formatting(self, corpus_copy: Path):
        path = corpus_copy / "manifest.yaml"
        text = path.read_text(encoding="utf-8").replace("year: 1993", "year: 1993   # reformatted")
        path.write_text("\n\n" + text, encoding="utf-8")

        assert corpus_hash(load_manifest(corpus_copy)) == corpus_hash(load_manifest(FIXTURE_CORPUS))

    def test_changes_when_a_document_content_hash_changes(self, corpus_copy: Path):
        path = corpus_copy / "manifest.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("4682d9e8", "0000d9e8"), encoding="utf-8")

        assert corpus_hash(load_manifest(corpus_copy)) != corpus_hash(load_manifest(FIXTURE_CORPUS))

    def test_changes_when_catalogue_metadata_changes(self, corpus_copy: Path):
        """Metadata is part of the Corpus identity: a retiered document is a different Corpus."""
        path = corpus_copy / "manifest.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("tier: B", "tier: A"), encoding="utf-8")

        assert corpus_hash(load_manifest(corpus_copy)) != corpus_hash(load_manifest(FIXTURE_CORPUS))

    def test_changes_when_a_document_is_removed(self, corpus_copy: Path):
        manifest = load_manifest(corpus_copy)
        smaller = manifest.model_copy(update={"documents": manifest.documents[:-1]})

        assert corpus_hash(smaller) != corpus_hash(manifest)

    def test_is_a_hex_sha256_digest(self):
        digest = corpus_hash(load_manifest(FIXTURE_CORPUS))

        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
