"""The read-only HTTP service: one question in, the chunks, the answer and the trace out.

The chunks come first in that list and in the response, because retrieval is what decides whether an
answer *can* be right while a generator only decides how it reads. The retrieval layer was served,
scored and traced on its own before generation existed, and it stays separately measurable now that
a generator sits on top of it (ADR-0004): the fields generation added are *additional*, and the
deterministic gate scores the same response it always scored.

Generation is optional at every level. No `Generator` configured means no `answer` and no `generate`
span, and the service is entirely usable that way. That is not a fallback path bolted on — it is the
same absence the trace already expresses for any stage that did not run.

Two policies live here rather than in `generation.py`, both deliberately. **Degradation**: the
adapter raises honestly and this endpoint decides what a visitor sees, which is 200 with the chunks
intact and an answer marked `degraded` — never a blank error page, and never confused with an
abstention. **The zero-cost abstention**: no candidates means the model is not called at all, so
there is no `generate` span to show.

Three things the endpoint deliberately does not know: which `Retriever` it is holding, which
`Generator` it is holding (both are runtime axes — design §9 — so a dense retriever or a different
provider must drop in with no change here), and whether the database is the right one. The last is
answered once, at boot: the service checks
`corpus_hash` and refuses to start on a mismatch (ADR-0002), because a per-request check would be a
service willing to run against the wrong artifact as long as nobody asks it anything.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from garage import __version__
from garage.config import Settings
from garage.generation import (
    Answer,
    Contract,
    ContractViolation,
    Generator,
    abstain_without_asking,
    degrade,
    reject_unverifiable,
    verify_citations,
)
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
PromptContract = Literal["cited", "free"]


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    # The tier filter is a runtime axis (design §9). The default is both, because a Tier B forum
    # thread holds knowledge that exists nowhere else; narrowing to A is what makes the contrast
    # between the two visible.
    tiers: tuple[Tier, ...] = Field(default=TIERS, min_length=1)
    # The prompt contract, a runtime axis (ADR-0005, design §9). `cited` is the default here and in
    # `generation.Contract`, and both defaults are asserted by tests: `free` exists only so the demo
    # can show what the citation contract is buying, and a system whose central property could be
    # switched off by omitting a field would not have that property. A `Literal` rather than a plain
    # string so that `extra="forbid"` and an unknown value both produce a 422 rather than a guess.
    contract: PromptContract = "cited"


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


class CitedChunk(BaseModel):
    """A citation as the wire sees it: the number the prose reads, and the chunk it resolved to.

    Both, always. The number is what `[2]` in the answer means to a reader; the `chunk_id` is what
    the interface links to and what makes a stored run record replayable. Every one of these was
    checked against the chunks in this same response — the model is never taken at its word.
    """

    index: int
    chunk_id: str


class AnsweredClaim(BaseModel):
    text: str
    citations: tuple[CitedChunk, ...]
    # False when every citation offered for this claim was discarded as invalid. Shown, marked,
    # rather than deleted: hiding it would hide the failure, and the ADR-0004 judge is better served
    # by a flagged sentence than by a silently shorter answer.
    supported: bool


class GeneratedAnswer(BaseModel):
    """The generation result, present whenever generation was attempted — including when it refused.

    `abstained` and `degraded` are separate booleans because they are separate facts, and the whole
    abstention metric depends on not adding them together: the first says the corpus does not cover
    the question, the second says the model could not be asked.
    """

    text: str
    claims: tuple[AnsweredClaim, ...]
    abstained: bool
    abstention_reason: str | None
    degraded: bool
    degradation_reason: str | None
    # A third, rarer state, and never folded into the two above: the provider answered but the
    # `Generator` in front of it published a citation that resolves to nothing. That is our bug, not
    # the corpus's silence and not the network's, and it is reported as itself.
    contract_violation: str | None
    support: str
    provider: str | None
    model: str | None
    contract: str
    tokens_in: int
    tokens_out: int
    # Null for a model with no published price, never a zero: a free-looking row in a cost
    # comparison is worse than a missing one. `pricing_as_of` dates the table it came from.
    cost_usd: float | None
    cost_estimated: bool
    pricing_as_of: str | None
    invalid_citations: int
    unsupported_claims: int
    contradictory: bool

    @classmethod
    def of(cls, answer: Answer) -> GeneratedAnswer:
        # `asdict` recurses through the nested claim and citation dataclasses, so the wire shape is
        # derived from the domain object rather than restated field by field beside it.
        return cls(**asdict(answer))


class QueryResponse(BaseModel):
    question: str
    # Echoed on every response, not only checked at boot: an answer kept without the identity of the
    # material it came from cannot be reproduced, and a run record is assembled out of responses.
    corpus_hash: str
    strategy: str
    k: int
    tiers: tuple[Tier, ...]
    chunks: tuple[RetrievedChunk, ...]
    # Added beside the existing fields, never in place of them: the ADR-0004 gate scores this exact
    # response and a retrieval-only deployment must still produce the shape it was written against.
    contract: PromptContract
    # Null when no generator is configured at all — the honest shape for a stage that did not run,
    # matching the trace, which has no `generate` span either.
    answer: GeneratedAnswer | None
    trace: dict[str, Any]


def create_app(
    settings: Settings | None = None,
    retriever: Retriever | None = None,
    generator: Generator | None = None,
) -> FastAPI:
    # Reading settings here is the boot gate: a container with no GARAGE_DATABASE_URL dies at
    # startup rather than serving requests against a database it never had.
    settings = settings or Settings()
    # Constructing a retriever opens nothing. `retriever` is injectable so the endpoint's own
    # behaviour — shape, trace, filters — can be tested without a database standing behind it.
    retriever = retriever or LexicalRetriever(settings.database_url)
    # No key, no generator, and no failure: generation is the optional layer and the service is
    # complete without it. The construction is guarded rather than attempted-and-caught because
    # `GeminiGenerator` imports an optional dependency, and a machine with neither the package nor a
    # key must not have to pay for a traceback to find that out.
    if generator is None and settings.gemini_api_key:
        from garage.generation import GeminiGenerator

        generator = GeminiGenerator(api_key=settings.gemini_api_key, model=settings.gemini_model)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Raising here is the refusal: uvicorn aborts the boot and the operator reads why. Building
        # the app is deliberately not enough to touch the database, so `--help` never needs one.
        app.state.artifact = verify_artifact(settings.database_url, settings.corpus_dir)
        yield

    app = FastAPI(title="Garage", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.retriever = retriever
    app.state.generator = generator

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
            # `rerank` is the other stage the design names (§12), and it is absent rather than
            # empty: a span reporting zero milliseconds for a stage that does not exist would be a
            # trace lying about the pipeline it describes. `generate` obeys the same rule below.
            answer = _answer(
                tracer,
                generator=generator,
                question=query_request.question,
                candidates=candidates,
                contract=Contract(mode=query_request.contract),
            )
            root.set(**{"query.candidates": len(candidates)})

        return QueryResponse(
            question=query_request.question,
            corpus_hash=corpus_hash,
            strategy=retriever.name,
            k=query_request.k,
            tiers=query_request.tiers,
            chunks=tuple(RetrievedChunk.of(candidate) for candidate in candidates),
            contract=query_request.contract,
            answer=None if answer is None else GeneratedAnswer.of(answer),
            trace=tracer.tree(),
        )

    return app


def _answer(
    tracer: Tracer,
    *,
    generator: Generator | None,
    question: str,
    candidates: tuple[Candidate, ...],
    contract: Contract,
) -> Answer | None:
    """Run generation, or say honestly why it did not run.

    Five exits, and the trace differs in each. **No generator**: nothing happened, no span, no
    answer. **No candidates**: an abstention that cost nothing and asked nobody, and still no span —
    the retriever's similarity floor is what makes this reachable (`docs/retrieval.md`), and
    inventing a zero-millisecond `generate` here would claim a model call that never occurred. **A
    provider failure**: the span exists, carrying its duration and its error, because the attempt is
    a real thing that happened and a trace which goes quiet exactly when something broke is worth
    little. **A citation that does not resolve**: the answer is refused, marked as this codebase's
    own contract violation rather than as anything the model or the network did. **A good answer**:
    served with its tokens and its cost on the span.

    Our own bugs are the one thing that is *not* handled here. The `try` covers the provider call
    alone, so a `TypeError` in this function is a 500 rather than a polite note blaming a provider
    that answered correctly.
    """
    if generator is None:
        return None
    if not candidates:
        return abstain_without_asking(
            "nenhum trecho do Corpus passou o piso de similaridade para esta pergunta",
            contract=contract,
        )

    model = getattr(generator, "model", None)
    with tracer.span(
        "generate",
        **{
            "generation.provider": generator.name,
            "generation.model": model,
            "generation.contract": contract.mode,
            "generation.context.chunks": len(candidates),
        },
    ) as span:
        try:
            # The `try` wraps the provider call and nothing else. Widening it by one line would mean
            # a bug in the code *below* — an attribute error of ours — reaching the visitor as "the
            # generation provider failed", which is this service slandering a dependency for its own
            # mistake. Our bugs are ours: they propagate, and FastAPI reports them as a 500.
            answer = generator.generate(question, context=candidates, contract=contract)
        except Exception as failure:
            # Recorded on the span from inside it, so the attributes land before the span closes and
            # its duration is the real one. The full provider message goes here, in the trace, where
            # an operator reads it — and deliberately not into the HTTP response, which gets the
            # exception's type and nothing else. A provider's raw error text is free surface area.
            span.set(
                error=True,
                **{
                    "exception.type": type(failure).__name__,
                    "exception.message": str(failure),
                    "generation.degraded": True,
                },
            )
            return degrade(
                f"o provedor de geração não respondeu ({type(failure).__name__})",
                provider=generator.name,
                model=model,
                contract=contract,
            )

        try:
            # The endpoint's own re-assertion of the first acceptance criterion. `Generator` is a
            # runtime axis, so the implementation that forgets to validate is the next one, not this
            # one — and "every citation resolves" has to be true of the system rather than of a
            # single adapter's diligence.
            verify_citations(answer, context=candidates, contract=contract)
        except ContractViolation as violation:
            # The full cost record, and the asymmetry with the degradation block above is
            # deliberate — do not "tidy" the two into agreement. There, the provider never answered,
            # so there is nothing to bill and nothing to record. Here it answered, on time, and
            # charged for it; the answer is refused by us, not unbilled by them. A span that dropped
            # the cost would let a configuration which reliably breaks the citation contract show up
            # as the cheap one in the comparison the demo puts on screen.
            span.set(
                error=True,
                **{
                    "exception.type": type(violation).__name__,
                    "exception.message": str(violation),
                    "generation.contract.violated": True,
                    "generation.abstained": False,
                    "generation.degraded": False,
                    "generation.support": "rejected",
                    "generation.tokens.input": answer.tokens_in,
                    "generation.tokens.output": answer.tokens_out,
                    "generation.tokens.total": answer.tokens_total,
                    "generation.cost.usd_estimated": answer.cost_usd,
                    "generation.cost.estimated": answer.cost_estimated,
                    "generation.pricing.as_of": answer.pricing_as_of,
                },
            )
            return reject_unverifiable(
                f"o gerador produziu citações que não resolvem: {violation}",
                provider=generator.name,
                model=model,
                contract=contract,
            )

        span.set(
            **{
                "generation.tokens.input": answer.tokens_in,
                "generation.tokens.output": answer.tokens_out,
                "generation.tokens.total": answer.tokens_total,
                "generation.cost.usd_estimated": answer.cost_usd,
                "generation.cost.estimated": answer.cost_estimated,
                "generation.pricing.as_of": answer.pricing_as_of,
                "generation.abstained": answer.abstained,
                "generation.support": answer.support,
                "generation.citations": sum(len(claim.citations) for claim in answer.claims),
                "generation.citations.invalid": answer.invalid_citations,
                "generation.claims.unsupported": answer.unsupported_claims,
                "generation.contradictory": answer.contradictory,
                "generation.contract.violated": False,
                "generation.degraded": False,
            }
        )
        return answer


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
