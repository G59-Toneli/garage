"""Integration check: the database the app is pointed at already carries vectors.

The extension is installed by the environment that provisions the database, never by this test —
a test that creates the state it verifies proves nothing about a fresh `docker compose up`.
Skipped when no database is reachable, so `pytest` stays green on a bare checkout.
"""

import os

import psycopg
import pytest

DATABASE_URL = os.environ.get("GARAGE_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="GARAGE_DATABASE_URL is unset; start `docker compose up`"
)


def test_pgvector_extension_is_installed():
    with psycopg.connect(DATABASE_URL) as conn:
        installed = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        vector = conn.execute("SELECT '[1,2,3]'::vector").fetchone()

    assert installed is not None
    assert vector is not None
