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

    # The showcase, and deliberately a sibling of `eval` rather than a verb under it. `eval` is the
    # deterministic layer — offline, free, and the thing CI re-measures; this one calls a paid
    # provider on every line it writes (ADR-0004, ADR-0007). Putting it under `eval` would put a
    # command that spends money one tab-completion away from the command that gates the build.
    showcase = commands.add_parser(
        "showcase", help="the precomputed showcase (calls a paid provider)"
    ).add_subparsers(dest="showcase_command", required=True)
    showcase_build = showcase.add_parser(
        "build",
        help="sample the curated questions against every strategy and write a showcase record",
    )
    _add_corpus_arguments(showcase_build)
    _add_database_argument(showcase_build)
    # Every default below is `None` and is resolved in the handler, which is not the usual argparse
    # style and is required here: naming the real default would mean importing `garage.showcase` at
    # parser-construction time, and that module reaches `garage.app` — so `corpus validate`, an
    # offline build step, would acquire fastapi and psycopg because a sibling subcommand has them.
    # The numbers live in exactly one place; this file points at it.
    showcase_build.add_argument(
        "-n",
        "--samples",
        type=int,
        default=None,
        metavar="N",
        help="draws per question per strategy (default: showcase.DEFAULT_SAMPLE_COUNT)",
    )
    showcase_build.add_argument(
        "--questions",
        type=Path,
        default=None,
        metavar="PATH",
        help="the curated question set (default: eval/showcase/questions.jsonl)",
    )
    showcase_build.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="use only the first N curated questions — how a small proving run is built cheaply",
    )
    showcase_build.add_argument(
        "--scope",
        required=True,
        metavar="TEXT",
        help=(
            "what this record is for, written into it. A proving run and a curated release share a "
            "schema and have nothing else in common, and a reader who cannot tell them apart will "
            "cite the wrong one"
        ),
    )
    showcase_build.add_argument(
        "--throttle",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "pause between provider calls (default: showcase.THROTTLE_SECONDS). The free tier is "
            "~10 RPM; without a pause the build eats its own 429s and records them as results"
        ),
    )
    showcase_build.add_argument(
        "--verbatim-token-limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "fail the build when a claim repeats this many consecutive tokens of a cited Tier A "
            "chunk (default: showcase.VERBATIM_TOKEN_LIMIT; ADR-0003)"
        ),
    )
    showcase_build.add_argument(
        "--verbatim-subsequence-limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "fail the build when a claim shares this many tokens in order, gaps allowed, with a "
            "cited Tier A chunk (default: showcase.VERBATIM_SUBSEQUENCE_LIMIT). This is the half "
            "that catches a paragraph copied with a linking word every twenty tokens"
        ),
    )
    showcase_build.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "build from an uncommitted tree. Off by default: showcase_id promises that its git_sha "
            "identifies the code that produced the record, and a dirty tree breaks that promise"
        ),
    )
    showcase_build.add_argument(
        "--yes",
        action="store_true",
        help="actually spend the money. Without it the command prints the plan and stops",
    )

    # The documentation's one measured example, produced by the same retriever the service runs.
    # A subcommand and not a script in `tools/`, because the property it holds — the block in
    # `docs/retrieval.md` is a capture and not a composition (#12) — is a property of this build,
    # and a script nobody installs is a property nobody re-establishes.
    docs = commands.add_parser("docs", help="documentation artifacts").add_subparsers(
        dest="docs_command", required=True
    )
    docs_capture = docs.add_parser(
        "capture",
        help="run the real retriever and rewrite the captured example in docs/retrieval.md",
    )
    _add_database_argument(docs_capture)

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
    showcase_build.set_defaults(handler=_showcase_build)
    docs_capture.set_defaults(handler=_docs_capture)
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


def _showcase_build(arguments: argparse.Namespace) -> int:
    """Sample the curated questions and write a record. The only command in this file that spends money.

    So it is the only one that refuses to act on its own. It prints the plan — how many provider
    calls, at what throttle, for how long — and stops unless `--yes` was passed. A dry run is not a
    courtesy here: 8 questions x 2 arms x n=10 is 160 calls against a free tier of 250 a day, and a
    mistyped `-n` is a whole day of quota spent on the wrong thing. `eval run` needs no such guard
    because re-running it costs a minute of a local database.

    The generator is built here rather than inside `build_showcase`, for the same reason `create_app`
    builds it: a missing key must be a clear sentence about configuration, not a traceback from an
    optional dependency deep inside a build.
    """
    from pydantic import ValidationError

    from garage.config import Settings
    from garage.ingest import ArtifactMismatch
    from garage.showcase import (
        DEFAULT_SAMPLE_COUNT,
        THROTTLE_SECONDS,
        VERBATIM_SUBSEQUENCE_LIMIT,
        VERBATIM_TOKEN_LIMIT,
        ShowcaseError,
        build_showcase,
        load_questions,
        write_showcase_record,
    )

    database_url = _resolve_database_url(arguments)
    if database_url is None:
        return 1

    samples = DEFAULT_SAMPLE_COUNT if arguments.samples is None else arguments.samples
    throttle = THROTTLE_SECONDS if arguments.throttle is None else arguments.throttle
    token_limit = (
        VERBATIM_TOKEN_LIMIT
        if arguments.verbatim_token_limit is None
        else arguments.verbatim_token_limit
    )
    subsequence_limit = (
        VERBATIM_SUBSEQUENCE_LIMIT
        if arguments.verbatim_subsequence_limit is None
        else arguments.verbatim_subsequence_limit
    )
    if samples < 1:
        print(f"--samples must be at least 1, got {samples}", file=sys.stderr)
        return 1

    try:
        settings = Settings(database_url=database_url)
    except ValidationError as invalid:
        print(f"configuration failed\n{invalid}", file=sys.stderr)
        return 1
    if not settings.gemini_api_key:
        print(
            "no generation key: set GEMINI_API_KEY (or GARAGE_GEMINI_API_KEY).\n"
            "A showcase is prose sampled from a hosted model; there is nothing to precompute "
            "without one.",
            file=sys.stderr,
        )
        return 1

    try:
        curated = load_questions(arguments.questions)
    except ShowcaseError as failure:
        print(f"showcase build failed\n{failure}", file=sys.stderr)
        return 1
    if arguments.limit is not None:
        # First N, never a random N. A proving run has to be re-runnable and comparable against the
        # one before it, and a sample that moves between invocations is not either.
        curated = curated[: arguments.limit]
    if not curated:
        print("--limit selected no questions", file=sys.stderr)
        return 1

    from garage.retrieval import available_retrievers

    try:
        strategies = available_retrievers(database_url)
    except Exception as failure:  # a missing embedder is `EmbedderError`, a bad URL is psycopg's
        print(f"showcase build failed\n{failure}", file=sys.stderr)
        return 1

    planned = len(curated) * len(strategies) * samples
    print(f"questions:   {len(curated)} ({', '.join(question.question_id for question in curated)})")
    print(f"strategies:  {len(strategies)} ({', '.join(strategy.name for strategy in strategies)})")
    print(f"samples:     {samples} per question per strategy")
    print(f"provider:    gemini {settings.gemini_model}")
    # An upper bound, said as one. A question the retriever comes back empty on is abstained without
    # anybody being asked and costs nothing, so the real total is this or less — and a plan that
    # printed the smaller number would be the one that surprises an operator.
    print(
        f"calls:       at most {planned} paid calls, ~{throttle:g}s apart "
        f"≈ {planned * throttle / 60:.1f} min (a question that retrieves nothing abstains free)"
    )
    print(f"scope:       {arguments.scope}")
    if not arguments.yes:
        # Deliberately not an interactive prompt. This runs in a terminal today and in whatever a
        # future operator wraps it in tomorrow, and a command that blocks on stdin is a command that
        # hangs in the second case.
        print("\nNothing was called. Re-run with --yes to spend it.")
        return 0

    from garage.generation import GeminiGenerator

    generator = GeminiGenerator(api_key=settings.gemini_api_key, model=settings.gemini_model)
    try:
        record = build_showcase(
            database_url,
            arguments.corpus_dir,
            generator=generator,
            questions=curated,
            retrievers=strategies,
            scope=arguments.scope,
            n=samples,
            verbatim_token_limit=token_limit,
            verbatim_subsequence_limit=subsequence_limit,
            allow_dirty=arguments.allow_dirty,
            throttle_seconds=throttle,
            # Progress on stdout, not through `logging`. The build is minutes long and mostly
            # sleeping; a silent command that spends money is a command an operator kills.
            report=print,
        )
    except (ShowcaseError, ArtifactMismatch) as failure:
        print(f"showcase build failed\n{failure}", file=sys.stderr)
        return 1

    path = write_showcase_record(record)
    print(f"\nshowcase_id: {record.showcase_id}")
    print(f"corpus_hash: {record.provenance.corpus_hash}")
    print(
        f"verbatim:    worst contiguous run {record.redistribution.worst_verbatim.tokens} "
        f"(limit {record.redistribution.verbatim_token_limit}) · worst subsequence "
        f"{record.redistribution.worst_verbatim_subsequence.tokens} "
        f"(limit {record.redistribution.verbatim_subsequence_limit})"
    )
    if record.provenance.git_dirty:
        # `--allow-dirty` was passed, so this is not a surprise — it is a receipt. The record's
        # git_sha does not identify the code that produced it, and the screen says so too.
        print(
            "note: built from a dirty tree with --allow-dirty; this record's git_sha does not "
            "identify the code that produced it, and the showcase screen says so"
        )
    for item in record.items:
        for arm in item.arms:
            spread = arm.spread["tokens_out"]
            print(
                f"  {item.question_id:<28} {arm.strategy:<8} tokens_out "
                f"{spread.minimum:g}–{spread.maximum:g} ({spread.distinct} distinct) · "
                f"displayed #{arm.displayed_sample}"
            )
    print(f"record:      {path}")
    return 0


def _docs_capture(arguments: argparse.Namespace) -> int:
    """Re-measure the documented example and write it into the document. Free, local, no model.

    Run it after anything that could move a lexical ranking, and commit the two files it touches
    together — `tests/test_capture.py` fails on the pair being out of step, which is the point.
    """
    from garage.capture import CaptureError, refresh

    database_url = _resolve_database_url(arguments)
    if database_url is None:
        return 1

    try:
        artifact, document = refresh(database_url)
    except CaptureError as failure:
        print(f"capture failed\n{failure}", file=sys.stderr)
        return 1

    print(f"captured: {artifact}")
    print(f"rewrote:  {document}")
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
