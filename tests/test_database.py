"""Integration check: ingestion leaves behind a database that can actually be searched.

Both extensions are created by `ingest`, never by this test and — since the dense retriever landed —
never by `docker/initdb/` either. That move is the thing being verified here. `initdb` runs exactly
once, when the data directory is empty, so a developer whose volume predates a line added there gets
a database the server cannot query; ingestion already owns DDL and is always re-run, which makes it
the only place a required extension can be created and stay true of every artifact. `vector` used to
be optional and lived in `docker/initdb/001-extensions.sql`, with a hand-copied duplicate in
`ci.yml`. It is not optional any more — `embeddings.embedding` is a `vector(384)` column — so it
moved to `database.CREATE_EXTENSIONS` beside `pg_trgm` and both copies went away.

Skipped when no database is reachable, so `pytest` stays green on a bare checkout.
"""

import os

import psycopg
import pytest

from garage.corpus import FIXTURE_CORPUS
from garage.ingest import build

DATABASE_URL = os.environ.get("GARAGE_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="GARAGE_DATABASE_URL is unset; start `docker compose up`"
)


@pytest.fixture(scope="module")
def ingested():
    # Deliberately the whole build rather than a bare `CREATE EXTENSION`: the claim under test is
    # that the command an operator actually runs is sufficient on a volume nobody prepared.
    build(DATABASE_URL, FIXTURE_CORPUS)
    return DATABASE_URL


@pytest.mark.parametrize("extension", ["vector", "pg_trgm"])
def test_ingestion_installs_the_extensions_retrieval_needs(ingested, extension):
    with psycopg.connect(DATABASE_URL) as connection:
        installed = connection.execute(
            "SELECT 1 FROM pg_extension WHERE extname = %s", (extension,)
        ).fetchone()

    assert installed is not None


def test_the_vector_type_is_usable(ingested):
    with psycopg.connect(DATABASE_URL) as connection:
        (width,) = connection.execute("SELECT vector_dims('[1,2,3]'::vector)").fetchone()

    assert width == 3
