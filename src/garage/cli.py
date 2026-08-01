"""Command line entry points.

Two commands, because the project has two modes: an offline build step that produces the database
artifact, and a read-only server that serves it (ADR-0002). `corpus validate` is the first piece of
the build step — everything downstream refuses to run against material it has not verified.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from garage.corpus import FIXTURE_CORPUS, CorpusError, validate_corpus


def _add_corpus_arguments(command: argparse.ArgumentParser) -> None:
    """How every command that reads a Corpus names it — shared so they cannot drift apart."""
    command.add_argument(
        "corpus_dir",
        nargs="?",
        default=FIXTURE_CORPUS,
        type=Path,
        help=f"directory holding manifest.yaml (default: the fixture Corpus at {FIXTURE_CORPUS})",
    )
    command.add_argument(
        "--sources",
        type=Path,
        default=None,
        metavar="DIR",
        help="where the source documents live (default: <corpus_dir>/sources)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garage", description="Garage: retrieval benchmark tooling")
    commands = parser.add_subparsers(dest="command", required=True)

    corpus = commands.add_parser("corpus", help="Corpus tooling").add_subparsers(
        dest="corpus_command", required=True
    )
    validate = corpus.add_parser(
        "validate",
        help="verify a Corpus against its manifest and report its corpus_hash",
    )
    _add_corpus_arguments(validate)

    ingest = commands.add_parser(
        "ingest",
        help="rebuild the database from a Corpus (safe to re-run; drops and reloads)",
    )
    _add_corpus_arguments(ingest)
    ingest.add_argument(
        "--database-url",
        default=None,
        metavar="URL",
        help="Postgres URL (default: GARAGE_DATABASE_URL from the environment)",
    )

    # Every leaf names its own handler. Dispatching by falling through `if command == ...` would mean
    # a future subcommand silently inheriting whatever the last branch happened to do.
    validate.set_defaults(handler=_validate)
    ingest.set_defaults(handler=_ingest)
    commands.add_parser("serve", help="run the read-only HTTP server").set_defaults(handler=_serve)
    return parser


def _validate(arguments: argparse.Namespace) -> int:
    try:
        report = validate_corpus(arguments.corpus_dir, sources_dir=arguments.sources)
    except CorpusError as failure:
        # stderr, non-zero: this is the gate CI hangs off, so a failure must be impossible to miss.
        print(f"corpus validation failed\n{failure}", file=sys.stderr)
        return 1

    print(f"corpus_id:   {report.corpus_id}")
    print(f"documents:   {report.document_count}")
    print(f"corpus_hash: {report.corpus_hash}")
    return 0


def _ingest(arguments: argparse.Namespace) -> int:
    # Imported here so `corpus validate` keeps working with no database driver configured and no
    # database in sight — the gate must never depend on the thing it gates.
    from garage.ingest import build

    database_url = arguments.database_url
    if database_url is None:
        from pydantic import ValidationError

        from garage.config import Settings

        try:
            database_url = Settings().database_url
        except ValidationError:
            print(
                "no database URL: pass --database-url or set GARAGE_DATABASE_URL", file=sys.stderr
            )
            return 1

    try:
        report = build(database_url, arguments.corpus_dir, sources_dir=arguments.sources)
    except CorpusError as failure:
        # Same wording as `corpus validate`: ingestion runs the same gate, and an operator who has
        # seen one failure should recognise the other.
        print(f"corpus validation failed\n{failure}", file=sys.stderr)
        return 1

    kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(report.chunks_by_kind.items()))
    print(f"corpus_id:      {report.corpus_id}")
    print(f"corpus_hash:    {report.corpus_hash}")
    print(f"ingest_version: {report.ingest_version}")
    print(f"documents:      {report.document_count}")
    print(f"chunks:         {report.chunk_count} ({kinds})")
    print(f"jargon terms:   {report.jargon_term_count}")
    return 0


def _serve(_: argparse.Namespace) -> int:
    # Imported here, not at module scope: `corpus validate` is an offline build step and must not
    # need the server's dependencies — or its GARAGE_DATABASE_URL — to run.
    from garage.app import main as run_server

    run_server()
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    return arguments.handler(arguments)
