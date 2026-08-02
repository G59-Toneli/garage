"""Configuration, read exclusively from the environment.

There is no config file. Portability (ADR-0001) means the same image runs on the author's
Windows box and on the ARM VM with nothing but a different environment. Compose is what loads
`.env`; the application itself only ever reads the process environment, so a stray `.env` in a
working directory cannot change how a test or a container behaves.
"""

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from garage.corpus import FIXTURE_CORPUS
from garage.generation import DEFAULT_MODEL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GARAGE_", extra="ignore")

    # No default: a wrong database is worse than no database, so a missing URL is a boot failure.
    database_url: str

    # The manifest the boot check compares the database against (ADR-0002). Only the catalogue is
    # read, so pointing this at a real Corpus does not require the operator's material to be
    # mounted into the serving container (ADR-0003).
    corpus_dir: Path = FIXTURE_CORPUS

    # Absent by default, and absence is a supported configuration rather than a misconfiguration:
    # the service boots, retrieves, traces and abstains-by-degradation without ever holding one. The
    # boot gate is the `corpus_hash` alone (ADR-0002) — retrieval is the measurable layer and must
    # not be held hostage to a hosted model's credentials.
    # Two accepted names, and the alias is not indulgence: `GEMINI_API_KEY` is what the SDK's own
    # documentation tells an operator to export and what is actually sitting in the environment,
    # while `GARAGE_GEMINI_API_KEY` is this project's prefix convention. The project's own name wins
    # when both are set, so a machine can hold one key for Garage and another for anything else.
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GARAGE_GEMINI_API_KEY", "GEMINI_API_KEY"),
    )
    gemini_model: str = DEFAULT_MODEL

    host: str = "0.0.0.0"
    port: int = 8000
