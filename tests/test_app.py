import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from garage.app import create_app
from garage.config import Settings


def test_health_reports_the_running_version():
    settings = Settings(database_url="postgresql://u:p@db:5432/garage")

    response = TestClient(create_app(settings)).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_booting_without_configuration_fails_loudly(monkeypatch):
    monkeypatch.delenv("GARAGE_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        create_app()
