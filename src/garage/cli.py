"""Command line entry points.

The project has two modes — an offline build step that produces the database artifact, and a
read-only server that serves it (ADR-0002) — and one measurement that sits between them. `corpus
validate` is the first piece of the build step, `ingest` the second, `eval` the third: everything
downstream refuses to run against material it has not verified, and the evaluation refuses to run
against a database that is not this commit's artifact.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from garage.corpus import FIXTURE_CORPUS, CorpusError, validate_corpus

if TYPE_CHECKING:
    # Type-checking only, so the annotation costs nothing at import time and `corpus validate` still
    # runs on a machine that has never heard of psycopg.
    from garage.evaluation import RunRecord


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
    _add_database_argument(ingest)

    evaluation = commands.add_parser("eval", help="deterministic evaluation").add_subparsers(
        dest="eval_command", required=True
    )
    eval_run = evaluation.add_parser(
        "run", help="measure retrieval against eval/facts.jsonl and write a run record"
    )
    _add_corpus_arguments(eval_run)
    _add_database_argument(eval_run)

    eval_gate = evaluation.add_parser(
        "gate",
        help="measure and fail if retrieval regressed against eval/baseline.json (writes nothing)",
    )
    _add_corpus_arguments(eval_gate)
    _add_database_argument(eval_gate)

    eval_promote = evaluation.add_parser(
        "promote", help="make a recorded run the baseline the gate compares against"
    )
    eval_promote.add_argument(
        "run_id",
        help="the run_id of a record in eval/runs/ (its filename without .json)",
    )

    # A subcommand rather than a `curl` line in the Dockerfile and another in `ci.yml`. The sha256
    # digests are the load-bearing part of vendoring model weights, and a digest maintained in three
    # places is a digest that is wrong in two of them — the same argument that moved `pg_trgm` and
    # `vector` into `database.py`.
    embedder = commands.add_parser("embedder", help="the build-time embedder axis").add_subparsers(
        dest="embedder_command", required=True
    )
    embedder_fetch = embedder.add_parser(
        "fetch", help="download and sha256-verify the baseline embedder weights"
    )
    embedder_show = embedder.add_parser(
        "show", help="report the configured embedder's fingerprint and what the database holds"
    )
    _add_database_argument(embedder_show)

    # Every leaf names its own handler. Dispatching by falling through `if command == ...` would mean
    # a future subcommand silently inheriting whatever the last branch happened to do.
    validate.set_defaults(handler=_validate)
    ingest.set_defaults(handler=_ingest)
    eval_run.set_defaults(handler=_eval_run)
    eval_gate.set_defaults(handler=_eval_gate)
    eval_promote.set_defaults(handler=_eval_promote)
    embedder_fetch.set_defaults(handler=_embedder_fetch)
    embedder_show.set_defaults(handler=_embedder_show)
    commands.add_parser("serve", help="run the read-only HTTP server").set_defaults(handler=_serve)
    return parser


def _add_database_argument(command: argparse.ArgumentParser) -> None:
    """How every command that reaches the database names it — shared so they cannot drift apart."""
    command.add_argument(
        "--database-url",
        default=None,
        metavar="URL",
        help="Postgres URL (default: GARAGE_DATABASE_URL from the environment)",
    )


def _resolve_database_url(arguments: argparse.Namespace) -> str | None:
    """The URL from the flag, else from the environment, else None with the reason already printed.

    Settings are imported here rather than at module scope for the same reason the handlers import
    their own dependencies: `corpus validate` is an offline build step and must keep working on a
    machine with no database configured at all.
    """
    if arguments.database_url is not None:
        return arguments.database_url

    from pydantic import ValidationError

    from garage.config import Settings

    try:
        return Settings().database_url
    except ValidationError:
        print("no database URL: pass --database-url or set GARAGE_DATABASE_URL", file=sys.stderr)
        return None


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
    from garage.embedding import EmbedderError
    from garage.ingest import build

    database_url = _resolve_database_url(arguments)
    if database_url is None:
        return 1

    try:
        report = build(database_url, arguments.corpus_dir, sources_dir=arguments.sources)
    except CorpusError as failure:
        # Same wording as `corpus validate`: ingestion runs the same gate, and an operator who has
        # seen one failure should recognise the other.
        print(f"corpus validation failed\n{failure}", file=sys.stderr)
        return 1
    except EmbedderError as failure:
        # Distinct from the corpus failure and distinct from a database one, because the fix is
        # neither: nothing was written, the previous artifact is intact, and the message already
        # names the command that repairs it.
        print(f"ingestion failed\n{failure}", file=sys.stderr)
        return 1

    kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(report.chunks_by_kind.items()))
    print(f"corpus_id:      {report.corpus_id}")
    print(f"corpus_hash:    {report.corpus_hash}")
    print(f"ingest_version: {report.ingest_version}")
    print(f"documents:      {report.document_count}")
    print(f"chunks:         {report.chunk_count} ({kinds})")
    print(f"jargon terms:   {report.jargon_term_count}")
    if report.embedder_model_key is None:
        # Said out loud rather than left to silence. A lexical-only artifact is a supported build,
        # but it is also what an operator who forgot `embedder fetch` would produce, and the two
        # must not look the same in a terminal.
        print("embeddings:     none (GARAGE_EMBEDDER=none; this artifact has no dense arm)")
    else:
        print(
            f"embeddings:     {report.embedding_count} × {report.embedder_model_key} "
            f"(fingerprint {report.embedder_fingerprint[:12]}…)"
        )
    return 0


def _eval_run(arguments: argparse.Namespace) -> int:
    """Measure and write a record. The record is a repository artifact, so this is not a CI step —
    a developer runs it and commits the file alongside the change that moved the numbers.
    """
    # Imported inside the handler, like every other command that needs a database: `corpus validate`
    # must not acquire a psycopg dependency because a sibling subcommand has one.
    from garage.evaluation import EvaluationError, run_evaluation, write_run_record
    from garage.ingest import ArtifactMismatch

    database_url = _resolve_database_url(arguments)
    if database_url is None:
        return 1

    try:
        record = run_evaluation(database_url, arguments.corpus_dir)
    except (EvaluationError, ArtifactMismatch) as failure:
        print(f"evaluation failed\n{failure}", file=sys.stderr)
        return 1

    path = write_run_record(record)
    _print_metrics(record)
    print(f"run record:  {path}")
    if record.provenance.git_dirty:
        # Not a failure — the normal way to produce a record is to run it on the change you are
        # about to commit, which is a dirty tree by definition. Said out loud because a record whose
        # git_sha does not describe what was measured is only reproducible by the person holding it.
        print("note: measured from a dirty working tree; commit the record with the change it describes")
    return 0


def _eval_gate(arguments: argparse.Namespace) -> int:
    """The build gate. Writes nothing at all.

    Two independent questions, both of which fail the build. Did retrieval regress against the
    promoted baseline? And does the newest run record committed to the tree still describe this
    build? The second is what makes a committed record an assertion rather than a souvenir: a change
    that moves the numbers and does not regenerate the record is caught here (ADR-0002).
    """
    from garage.evaluation import (
        EvaluationError,
        compare,
        is_ancestor_of_head,
        latest_run_record,
        load_baseline,
        load_run_record,
        measurement,
        run_evaluation,
        ungated_arms,
    )
    from garage.ingest import ArtifactMismatch

    database_url = _resolve_database_url(arguments)
    if database_url is None:
        return 1

    try:
        baseline = load_baseline()
        record = run_evaluation(database_url, arguments.corpus_dir)
        committed_path = latest_run_record()
        committed = load_run_record(committed_path) if committed_path else None
        promoted_path = _run_record_path(baseline.run_id)
        promoted = load_run_record(promoted_path) if promoted_path.is_file() else None
    except (EvaluationError, ArtifactMismatch) as failure:
        print(f"evaluation gate failed\n{failure}", file=sys.stderr)
        return 1

    report = compare(baseline, record, promoted)
    failures = list(report.failures)

    if promoted is None:
        failures.append(
            f"the baseline names run {baseline.run_id}, which is not in eval/runs/. A baseline must "
            "point at a record someone can read."
        )
    if committed is None:
        failures.append(
            "no run record in eval/runs/. Records are generated and committed, never written by CI."
        )
    elif not is_ancestor_of_head(committed.provenance.git_sha):
        # `latest_run_record` picks the newest filename, and a record committed on an unmerged
        # branch with a later timestamp would otherwise become the thing this build is validated
        # against. Ancestry is the check that says "this record belongs to this history".
        failures.append(
            f"the newest run record in the tree ({committed_path.name}) names commit "
            f"{committed.provenance.git_sha[:12]}, which is not an ancestor of HEAD. It came from "
            "another branch; regenerate a record for this one."
        )
    elif measurement(committed) != measurement(record):
        failures.append(
            f"the newest run record in the tree ({committed_path.name}) does not match what this "
            "build measures. It was committed against a different corpus, engine, Configuration or "
            "retrieval behaviour."
        )

    _print_metrics(record)
    for note in report.notes:
        print(note)
    for strategy in ungated_arms(baseline):
        print(f"note: the {strategy} arm is in the baseline but gates nothing")

    if not failures:
        print("gate: pass")
        return 0

    print(f"\nevaluation gate failed: {len(failures)} problem(s)", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    for note in report.notes:
        print(f"  {note}", file=sys.stderr)
    # Last line, always the next action: a gate that only says "no" costs more time than it saves.
    print(
        "\nNext: run `python -m garage eval run`, inspect the new record, and either fix the "
        "regression or promote it deliberately with `python -m garage eval promote <run_id>`.",
        file=sys.stderr,
    )
    return 1


def _eval_promote(arguments: argparse.Namespace) -> int:
    """Deliberate, local, and never run by CI — the promoting commit is the human sign-off."""
    from garage.evaluation import EvaluationError, load_baseline, promote, ungated_arms

    try:
        path = promote(arguments.run_id)
    except EvaluationError as failure:
        print(f"promotion failed\n{failure}", file=sys.stderr)
        return 1

    print(f"baseline: {path} now points at {arguments.run_id}")
    for strategy in ungated_arms(load_baseline(path)):
        print(
            f"note: the {strategy} arm gates nothing. Add its metric names to gated_metrics in "
            f"{path} when you are ready to hold them."
        )
    return 0


def _embedder_fetch(_: argparse.Namespace) -> int:
    from garage.embedding import EmbedderError, cache_dir, fetch

    try:
        directory = fetch()
    except (EmbedderError, OSError) as failure:
        print(f"embedder fetch failed\n{failure}", file=sys.stderr)
        return 1
    print(f"embedder: {directory}")
    print(f"set GARAGE_EMBEDDER_DIR to override this location (default {cache_dir()})")
    return 0


def _embedder_show(arguments: argparse.Namespace) -> int:
    """What this build would query with, beside what the database was actually built with.

    The two lines exist to be read together. When dense retrieval is quietly bad, the first question
    is whether these two fingerprints are the same string, and an operator should be able to answer
    it without writing SQL.
    """
    from garage.embedding import EmbedderError, configured_embedder, embedder_name

    try:
        embedder = configured_embedder()
    except EmbedderError as failure:
        print(f"embedder unavailable\n{failure}", file=sys.stderr)
        return 1

    print(f"configured:  {embedder_name()}")
    if embedder is None:
        print("code:        no embedder; this build is lexical-only")
    else:
        print(f"code:        {embedder.model_key} {embedder.dimension}d fingerprint {embedder.fingerprint}")

    database_url = _resolve_database_url(arguments)
    if database_url is None:
        return 1

    from garage.ingest import stored_embedders

    held = stored_embedders(database_url)
    if not held:
        print("database:    no embeddings")
    for stored in held:
        print(
            f"database:    {stored.model_key} {stored.dimension}d fingerprint {stored.fingerprint}"
            + ("" if stored.normalized else " (not normalized)")
        )
    return 0


def _run_record_path(run_id: str) -> Path:
    from garage.evaluation import RUNS_DIR

    return RUNS_DIR / f"{run_id}.json"


def _print_metrics(record: RunRecord) -> None:
    print(f"corpus_hash: {record.provenance.corpus_hash}")
    print(f"postgres:    {record.provenance.postgres_version} (pg_trgm {record.provenance.pg_trgm_version})")
    print(f"facts:       {record.sample_count} (sha256 {record.facts_sha256[:12]}…)")
    for arm in record.arms:
        print(f"{arm.configuration.strategy}  k={arm.configuration.k}")
        for name, value in sorted(arm.metrics.items()):
            print(f"  {name:<18} {value:.6f}")
        # The rank histogram, printed every run. A suite where every hit lands at rank 1 has two
        # states per question and cannot tell a small improvement from none at all; seeing that in
        # the output is how the previous fact set was caught.
        print(f"  hit_rank           {_histogram(arm)}")


def _histogram(arm) -> str:
    from collections import Counter

    counted = Counter(
        "miss" if item.hit_rank is None else str(item.hit_rank) for item in arm.per_item
    )
    ordered = sorted(counted.items(), key=lambda pair: (pair[0] == "miss", _numeric(pair[0])))
    return "  ".join(f"{rank}:{count}" for rank, count in ordered)


def _numeric(rank: str) -> int:
    return 0 if rank == "miss" else int(rank)


def _serve(_: argparse.Namespace) -> int:
    # Imported here, not at module scope: `corpus validate` is an offline build step and must not
    # need the server's dependencies — or its GARAGE_DATABASE_URL — to run.
    from garage.app import main as run_server

    run_server()
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    return arguments.handler(arguments)
