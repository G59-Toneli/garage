"""The read-only HTTP service: one question in, the chunks and the trace out.

There is no language model here, and that is the shape of the product rather than a stage of it.
Retrieval is what decides whether an answer *can* be right; a generator only decides how it reads.
So the retrieval layer is served, scored and traced on its own first, and stays separately
measurable after a generator lands on top of it (ADR-0004).

Two things the endpoint deliberately does not know: which `Retriever` it is holding (strategy is a
runtime axis — design §9 — so a dense or hybrid implementation must drop in with no change here),
and whether the database is the right one. The second is answered once, at boot: the service checks
`corpus_hash` and refuses to start on a mismatch (ADR-0002), because a per-request check would be a
service willing to run against the wrong artifact as long as nobody asks it anything.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from garage import __version__
from garage.config import Settings
from garage.ingest import verify_artifact
from garage.retrieval import (
    DEFAULT_K,
    MAX_K,
    TIERS,
    Candidate,
    Filters,
    LexicalRetriever,
    Retriever,
)
from garage.tracing import Tracer

Tier = Literal["A", "B"]


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    # The tier filter is a runtime axis (design §9). The default is both, because a Tier B forum
    # thread holds knowledge that exists nowhere else; narrowing to A is what makes the contrast
    # between the two visible.
    tiers: tuple[Tier, ...] = Field(default=TIERS, min_length=1)


class RetrievedChunk(BaseModel):
    """One chunk as the wire sees it.

    `tier` travels with every one of them: a manual and a forum post must never look alike on
    screen (design §13). So does `page`, null where the document genuinely has none.
    """

    chunk_id: str
    doc_id: str
    doc_title: str
    tier: Tier
    page: int | None
    section: str | None
    kind: str
    text: str
    score: float
    components: dict[str, float | None]

    @classmethod
    def of(cls, candidate: Candidate) -> RetrievedChunk:
        return cls(**vars(candidate))


class QueryResponse(BaseModel):
    question: str
    # Echoed on every response, not only checked at boot: an answer kept without the identity of the
    # material it came from cannot be reproduced, and a run record is assembled out of responses.
    corpus_hash: str
    strategy: str
    k: int
    tiers: tuple[Tier, ...]
    chunks: tuple[RetrievedChunk, ...]
    trace: dict[str, Any]


def create_app(settings: Settings | None = None, retriever: Retriever | None = None) -> FastAPI:
    # Reading settings here is the boot gate: a container with no GARAGE_DATABASE_URL dies at
    # startup rather than serving requests against a database it never had.
    settings = settings or Settings()
    # Constructing a retriever opens nothing. `retriever` is injectable so the endpoint's own
    # behaviour — shape, trace, filters — can be tested without a database standing behind it.
    retriever = retriever or LexicalRetriever(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Raising here is the refusal: uvicorn aborts the boot and the operator reads why. Building
        # the app is deliberately not enough to touch the database, so `--help` never needs one.
        app.state.artifact = verify_artifact(settings.database_url, settings.corpus_dir)
        yield

    app = FastAPI(title="Garage", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.retriever = retriever

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/query")
    def query(query_request: QueryRequest, http: Request) -> QueryResponse:
        # From `app.state`, not from a closure: it is written by the lifespan, so reading it here is
        # what makes it impossible to serve a query the boot gate never ran for.
        corpus_hash: str = http.app.state.artifact.corpus_hash

        tracer = Tracer()
        with tracer.span(
            "query", **{"query.question": query_request.question, "corpus.hash": corpus_hash}
        ) as root:
            with tracer.span(
                "retrieve",
                **{
                    "retrieval.strategy": retriever.name,
                    "retrieval.k": query_request.k,
                    "retrieval.tiers": ",".join(query_request.tiers),
                },
            ) as retrieve:
                candidates = retriever.retrieve(
                    query_request.question,
                    k=query_request.k,
                    filters=Filters(tiers=tuple(query_request.tiers)),
                )
                retrieve.set(**{"retrieval.candidates": len(candidates)})
            # `rerank` and `generate` are the other two stages the design names (§12). They are
            # absent rather than empty: a span reporting zero milliseconds for a stage that does not
            # exist would be a trace lying about the pipeline it describes.
            root.set(**{"query.candidates": len(candidates)})

        return QueryResponse(
            question=query_request.question,
            corpus_hash=corpus_hash,
            strategy=retriever.name,
            k=query_request.k,
            tiers=query_request.tiers,
            chunks=tuple(RetrievedChunk.of(candidate) for candidate in candidates),
            trace=tracer.tree(),
        )

    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
