"""The command line contract.

Exit codes matter more than output here: `corpus validate` is the gate CI hangs off, so a broken
Corpus must be a non-zero exit, not a warning somebody reads.
"""

from pathlib import Path

from garage.cli import main


def test_validating_the_fixture_succeeds_and_reports_the_hash(capsys):
    assert main(["corpus", "validate"]) == 0

    output = capsys.readouterr().out
    assert "corpus_id:   fixture" in output
    assert "documents:   5" in output
    assert "corpus_hash: " in output


def test_a_broken_corpus_exits_non_zero_and_explains_on_stderr(corpus_copy: Path, capsys):
    (corpus_copy / "sources" / "owner-kadett-1993.md").unlink()

    assert main(["corpus", "validate", str(corpus_copy)]) == 1

    captured = capsys.readouterr()
    assert "owner-kadett-1993" in captured.err
    assert captured.out == ""


def test_sources_can_be_pointed_elsewhere(corpus_copy: Path, detached_sources: Path):
    assert main(["corpus", "validate", str(corpus_copy), "--sources", str(detached_sources)]) == 0
