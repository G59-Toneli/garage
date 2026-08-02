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


def test_the_eval_gate_exits_non_zero_without_a_database(monkeypatch, capsys):
    # The gate is a CI step, so an unconfigured machine must fail loudly rather than skip quietly.
    monkeypatch.delenv("GARAGE_DATABASE_URL", raising=False)

    assert main(["eval", "gate"]) == 1

    assert "no database URL" in capsys.readouterr().err


def test_promoting_a_run_that_does_not_exist_exits_non_zero(capsys):
    assert main(["eval", "promote", "20260101T000000Z-000000000000"]) == 1

    assert "promotion failed" in capsys.readouterr().err
