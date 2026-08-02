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


def test_either_spelling_of_the_gemini_key_is_read_and_the_project_prefix_wins(monkeypatch):
    monkeypatch.setenv("GARAGE_DATABASE_URL", "postgresql://u:p@db:5432/garage")
    monkeypatch.setenv("GEMINI_API_KEY", "from-the-sdk-convention")

    assert Settings().gemini_api_key == "from-the-sdk-convention"

    monkeypatch.setenv("GARAGE_GEMINI_API_KEY", "from-the-project-prefix")
    assert Settings().gemini_api_key == "from-the-project-prefix"


def test_an_explicit_key_argument_beats_the_environment(monkeypatch):
    # The field carries a `validation_alias`, and a field with an alias refuses its own Python name
    # unless the model says otherwise. Without `populate_by_name`, this argument was discarded in
    # silence and the ambient key won — which is how a test that passes no key ends up paying for a
    # real API call on a developer's machine.
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-be-used")

    settings = Settings(database_url="postgresql://u:p@db:5432/garage", gemini_api_key=None)

    assert settings.gemini_api_key is None


def test_defaults_the_bind_address_but_not_the_database(monkeypatch):
    monkeypatch.setenv("GARAGE_DATABASE_URL", "postgresql://u:p@db:5432/garage")
    monkeypatch.delenv("GARAGE_HOST", raising=False)
    monkeypatch.delenv("GARAGE_PORT", raising=False)

    settings = Settings()

    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
