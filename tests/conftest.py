import shutil
from pathlib import Path

import pytest

from garage.corpus import FIXTURE_CORPUS


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
