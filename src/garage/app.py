"""ASGI entry point.

Carries no domain behaviour yet — only enough surface for Compose and CI to prove the
container is alive. The pipeline seams (Retriever, Embedder, Reranker, Generator; ADR-0006)
arrive with their first real implementation.
"""

from fastapi import FastAPI

from garage import __version__
from garage.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    # Reading settings here is the boot gate: a container with no GARAGE_DATABASE_URL dies at
    # startup rather than serving requests against a database it never had.
    settings = settings or Settings()

    app = FastAPI(title="Garage", version=__version__)
    app.state.settings = settings

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
