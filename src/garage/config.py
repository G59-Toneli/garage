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

    # --- the public deployment (issue #11) -------------------------------------------------------
    #
    # Every value below has a default that is correct for a laptop, so nothing here has to be set to
    # run the project. They exist because the ARM VM puts a paid provider's free tier behind a URL
    # anyone can open, and the numbers that protect it are a property of *that* deployment rather
    # than of this code. `docs/deploy.md` is where they are argued; `garage/limits.py` is where they
    # are enforced.

    # Whether `X-Forwarded-For` may be believed. **Off by default and on only behind the reverse
    # proxy that sets it.** Direct on the internet, that header is a string the client chose, so
    # honouring it would let one address present as a new visitor on every request and walk through
    # both per-IP limiters. The production overlay turns it on beside the Caddy that makes it true.
    trust_forwarded_for: bool = False

    # The anti-abuse bucket, the only limiter in this system that produces a 429. Zero disables it.
    requests_per_minute: int = 20

    # The two generation budgets. Neither ever produces an error: over either one the request is
    # rebased onto retrieval, which is local and free, and the answer is marked `degraded` with a
    # reason of its own (`app.query`). A 429 here would throw away the free half of the product to
    # punish a visitor for the paid half.
    generations_per_day_per_client: int = 60
    # Around 250 requests a day is what the free tier publishes for `gemini-2.5-flash`. Two hundred
    # leaves headroom for retries, for the operator's own `showcase build`, and for the fact that
    # the provider's day boundary need not be ours. Configurable because that number is not ours to
    # promise and has moved before.
    generation_budget_per_day: int = 200

    cache_max_entries: int = 512
    cache_ttl_seconds: int = 86400

    # The commit this container was built from, baked in by the Dockerfile. It is a *cache key
    # component* first and a display string second: without it, a deploy that changes the prompt
    # keeps serving answers the previous build produced, under this build's version stamp. Empty
    # means "ask git", which works in a checkout and returns `unknown` in an image built from a
    # tarball — and `unknown` is stated on screen rather than hidden.
    git_sha: str = ""
