"""Configuration, read exclusively from the environment.

There is no config file. Portability (ADR-0001) means the same image runs on the author's
Windows box and on the ARM VM with nothing but a different environment. Compose is what loads
`.env`; the application itself only ever reads the process environment, so a stray `.env` in a
working directory cannot change how a test or a container behaves.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GARAGE_", extra="ignore")

    # No default: a wrong database is worse than no database, so a missing URL is a boot failure.
    database_url: str

    host: str = "0.0.0.0"
    port: int = 8000
