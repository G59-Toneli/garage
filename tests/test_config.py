import pytest
from pydantic import ValidationError

from garage.config import Settings


def test_reads_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("GARAGE_DATABASE_URL", "postgresql://u:p@db:5432/garage")
    monkeypatch.setenv("GARAGE_PORT", "9000")

    settings = Settings()

    assert settings.database_url == "postgresql://u:p@db:5432/garage"
    assert settings.port == 9000


def test_refuses_to_start_without_a_database_url(monkeypatch):
    monkeypatch.delenv("GARAGE_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings()


def test_defaults_the_bind_address_but_not_the_database(monkeypatch):
    monkeypatch.setenv("GARAGE_DATABASE_URL", "postgresql://u:p@db:5432/garage")
    monkeypatch.delenv("GARAGE_HOST", raising=False)
    monkeypatch.delenv("GARAGE_PORT", raising=False)

    settings = Settings()

    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
