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


class Settings(BaseSettings):
    # `populate_by_name` is required by the `validation_alias` on `gemini_api_key` below: a field
    # with an explicit alias otherwise refuses its own Python name, so `Settings(gemini_api_key=None)`
    # would be silently discarded and the environment would win. That is not theoretical — it let a
    # test that passes no key construct a real client and call a paid API.
    model_config = SettingsConfigDict(
        env_prefix="GARAGE_", extra="ignore", populate_by_name=True
    )

    # No default: a wrong database is worse than no database, so a missing URL is a boot failure.
    database_url: str

    # The manifest the boot check compares the database against (ADR-0002). Only the catalogue is
    # read, so pointing this at a real Corpus does not require the operator's material to be
    # mounted into the serving container (ADR-0003).
    corpus_dir: Path = FIXTURE_CORPUS

    # Where the precomputed showcase records live, checked against the artifact at boot exactly as
    # `corpus_dir` is (ADR-0002, `docs/showcase.md`). `None` means the repository's own
    # `eval/showcase/`, resolved by `garage.showcase` — spelled as a null rather than as that path
    # because naming it here would mean importing `showcase`, which reaches `app`, from the module
    # every other module reads. Configuration must stay the leaf.
    #
    # It is a setting at all, and not a constant, for the same reason `corpus_dir` is: which records
    # a container serves is a property of the deployment. It is also the seam that keeps a test of
    # the query endpoint from depending on whichever showcase happens to be committed — a test's
    # dependencies come from the test, which is the argument `gemini_api_key` below already makes.
    showcase_dir: Path | None = None

    # Absent by default, and absence is a supported configuration rather than a misconfiguration:
    # the service boots, retrieves and traces without ever holding one, and simply returns no
    # answer. Not an abstention and not a degradation — a stage that never ran, exactly as the trace
    # already expresses it. The boot gate is the `corpus_hash` alone (ADR-0002); retrieval is the
    # measurable layer and must not be held hostage to a hosted model's credentials.
    # Two accepted names, and the alias is not indulgence: `GEMINI_API_KEY` is what the SDK's own
    # documentation tells an operator to export and what is actually sitting in the environment,
    # while `GARAGE_GEMINI_API_KEY` is this project's prefix convention. The project's own name wins
    # when both are set, so a machine can hold one key for Garage and another for anything else.
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GARAGE_GEMINI_API_KEY", "GEMINI_API_KEY"),
    )
    # Written out rather than imported from `garage.generation`. Configuration is the layer
    # everything else reads; a module that imports one of its own consumers has the dependency arrow
    # backwards, and this one string is not worth inverting it for. The duplicate is kept honest by
    # a test asserting it equals `generation.DEFAULT_MODEL`, and by the price table living next to
    # that constant. The 2.5 family retires in late 2026, so this value is expected to change.
    gemini_model: str = "gemini-2.5-flash"

    host: str = "0.0.0.0"
    port: int = 8000
