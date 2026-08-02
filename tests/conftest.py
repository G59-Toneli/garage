import shutil
from pathlib import Path

import pytest

from garage.corpus import FIXTURE_CORPUS


@pytest.fixture(autouse=True)
def the_anti_abuse_bucket_is_off_unless_a_test_asks_for_it(monkeypatch):
    """No test inherits the rate limiter by accident, because that makes the suite order-dependent.

    Issue #11 gave `POST /query` a per-address token bucket, default ten requests a minute, built in
    `create_app` and therefore one per app object. That is correct for a deployment and it turned
    every test that boots an app into a test with a hidden budget: exceed ten `POST /query` calls
    against one app and the eleventh is a 429, which surfaces as a failure in a test that never
    mentioned rate limiting, only in a full-suite run, and only in whatever order pytest happened to
    collect that day. It then vanishes when somebody runs the file alone to investigate. That is the
    worst shape a failure can have, and one reached `main`.

    Setting the environment variable rather than editing four `Settings(...)` call sites is
    deliberate. `Settings` reads the process environment, so this covers the app-building tests in
    `test_retrieval.py` and `test_dense_retrieval.py`, which construct their settings from scratch,
    **and every test written after this one**. A policy that has to be remembered at each new call
    site is a policy that will be missing from the next one.

    Zero disables the bucket — see `limits.Limiter.admit_request`, where zero is a disabling value
    for this limiter and refuses everything for the two generation budgets. The asymmetry is argued
    in `garage/limits.py`.

    **This does not remove coverage, and it must not.** A test that wants the bucket says so and
    wins, because an explicit constructor argument and `model_copy` both beat the environment:
    `test_cascade.py` runs it at two requests a minute against a freshly built app, and
    `test_limits.py` exercises the bucket directly as arithmetic. Both are order-independent by
    construction — one owns its app, the other owns its clock.
    """
    monkeypatch.setenv("GARAGE_REQUESTS_PER_MINUTE", "0")


@pytest.fixture
def corpus_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the fixture Corpus.

    Tests that need a *broken* Corpus damage this copy. The fixture in the repository is the
    deterministic test base and is never mutated.
    """
    destination = tmp_path / "corpus"
    shutil.copytree(FIXTURE_CORPUS, destination)
    return destination


@pytest.fixture
def detached_sources(corpus_copy: Path, tmp_path: Path) -> Path:
    """The fixture Corpus with its documents moved out of the Corpus directory.

    This is the shape of every real Corpus: manifest in git, material on the operator's own disk
    (ADR-0003).
    """
    elsewhere = tmp_path / "elsewhere"
    shutil.move(corpus_copy / "sources", elsewhere)
    return elsewhere
