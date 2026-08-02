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

The interface is served from here too, as a static build out of `garage/static/` (ADR-0006), and it
is a *client* of this endpoint like any other — it holds no privileged route. The two-column
comparison is two ordinary `POST /query` calls, one per strategy, which is why no `/compare`
endpoint exists: "two columns" is a decision the interface makes, and an endpoint shaped around it
would be that decision leaking into the API. `docs/ui.md` argues the whole of it.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
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
    Retriever,
    available_retrievers,
)
from garage.tracing import Tracer

Tier = Literal["A", "B"]
PromptContract = Literal["cited", "free"]

# The interface ships inside the package and is served by this same process (ADR-0006): one
# language, one container, one artifact. Resolved from `__file__` rather than from the working
# directory so that `pip install -e .`, a wheel and the image all find the same files.
STATIC_DIR = Path(__file__).resolve().parent / "static"


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    # The retrieval strategy, a runtime axis (ADR-0005, design §9) — and the field that makes that
    # sentence true rather than aspirational. Both strategies are built at boot and both stand on
    # the same artifact, so choosing between them is a dictionary lookup: no rebuild, no redeploy,
    # no second container. Null means whichever this build lists first, which keeps every request
    # written before `dense` existed meaning exactly what it meant then.
    #
    # A plain `str` and not a `Literal`, unlike `contract` above, and the asymmetry is deliberate:
    # `contract` has two values this code will always know, while the set of strategies is decided
    # by `available_retrievers` and changes with the build. A `Literal` here would have to be
    # rewritten every time a strategy is added and would still be wrong for a lexical-only build.
    # The endpoint validates against what it actually holds and says so, which is a better error.
    strategy: str | None = None
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


class Strategy(BaseModel):
    """One strategy this build can answer with, and which stored vectors it stands on."""

    name: str
    embedder: str | None


class StrategiesResponse(BaseModel):
    """What `GET /strategies` publishes: the runtime axis, enumerable rather than guessable.

    In the order `available_retrievers` returns them — the pipeline's order, never alphabetical —
    because the client that asks this question is choosing which arm to show first.
    """

    strategies: tuple[Strategy, ...]
    # Which one an omitted `strategy` field resolves to. Published rather than left implicit: a
    # request written before `dense` existed still means what it meant then, and a reader should be
    # able to see what that is instead of inferring it from the ordering.
    default: str


class QueryResponse(BaseModel):
    question: str
    # Echoed on every response, not only checked at boot: an answer kept without the identity of the
    # material it came from cannot be reproduced, and a run record is assembled out of responses.
    corpus_hash: str
    strategy: str
    # Which stored vectors answered, as `<model_key>@<fingerprint prefix>`; null under `lexical`,
    # exactly as `Configuration.embedder` is null there. Added because `strategy` alone stops being
    # an identity the moment ADR-0005's second embedder exists: Phase 4 puts `baseline` and
    # `finetuned` in one `embeddings` table under two `model_key`s, and comparing them is the point
    # of the phase — two arms both labelled `dense` would be a comparison a reader cannot name.
    # It also lets an interface show the embedder without hard-coding it, which the run record has
    # been able to do all along and the HTTP surface could not.
    embedder: str | None
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
    retrievers: Sequence[Retriever] | None = None,
) -> FastAPI:
    # Reading settings here is the boot gate: a container with no GARAGE_DATABASE_URL dies at
    # startup rather than serving requests against a database it never had.
    settings = settings or Settings()
    # Two injection points and they are not redundant. `retriever` is the single-strategy one every
    # test that cares about the endpoint's own behaviour uses — one object, and it is the only thing
    # this app can retrieve with. `retrievers` is the several-strategy one, for asserting that
    # choosing between them is a runtime axis. Pass neither and the strategies are built in the
    # lifespan below from `available_retrievers`, which is what a real deployment does.
    if retriever is not None and retrievers is not None:
        raise ValueError("pass retriever or retrievers, not both")
    injected = tuple(retrievers) if retrievers is not None else (
        None if retriever is None else (retriever,)
    )
    # No key, no generator, and no failure: generation is the optional layer and the service is
    # complete without it. The construction is guarded rather than attempted-and-caught because
    # `GeminiGenerator` imports an optional dependency, and a machine with neither the package nor a
    # key must not have to pay for a traceback to find that out.
    if generator is None and settings.gemini_api_key:
        from garage.generation import GeminiGenerator

        generator = GeminiGenerator(api_key=settings.gemini_api_key, model=settings.gemini_model)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Retrievers are built here rather than above, and the reason is `dense`: it holds an
        # `Embedder`, and an `Embedder` is 470 MB of weights read off disk and hashed. Constructing
        # a retriever must stay free — `--help` and `/health` do not deserve a model load — so the
        # cost moves to boot, which is where the artifact is verified anyway and where an operator
        # already expects to wait.
        strategies = injected if injected is not None else available_retrievers(settings.database_url)
        # **Every** embedder any strategy holds, not the first one found. With one dense retriever
        # the two are the same; with the Phase 4 fine-tuned embedder alongside the baseline
        # (ADR-0005) they are not, and stopping at the first would serve a whole set of vectors the
        # boot gate never looked at — the exact failure this check exists for, reintroduced by the
        # check. `[None]` when there are none, so a lexical-only build still runs the corpus_hash
        # half of the gate.
        #
        # Each embedder comes off the retriever that will actually answer queries with it, never
        # resolved a second time here: a second resolution is the divergence
        # `embedding.embedder_for` exists to make unwriteable.
        embedders = [held for held in (strategy.embedder for strategy in strategies) if held]
        # Raising here is the refusal: uvicorn aborts the boot and the operator reads why. Building
        # the app is deliberately not enough to touch the database, so `--help` never needs one.
        for embedder in embedders or [None]:
            app.state.artifact = verify_artifact(
                settings.database_url, settings.corpus_dir, embedder
            )
        app.state.retrievers = {strategy.name: strategy for strategy in strategies}
        app.state.default_strategy = strategies[0].name
        yield

    app = FastAPI(title="Garage", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.generator = generator

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/strategies")
    def strategies(http: Request) -> StrategiesResponse:
        """What this build serves, in the order the pipeline lists it.

        It exists because the interface needs the list and the only place it was published was the
        `msg` of a validation error — so the interface was provoking a deliberate 422 at load and
        reading a human sentence out of it with a regular expression. Two defects buying one: prose
        parsing, and an error in every visitor's console on a page that is working correctly.

        The order is `available_retrievers`' order and is deliberately **not** sorted. That tuple is
        "every strategy this build can measure, in the order a report should show them", so it is
        the pipeline's own ordering and it is what decides which arm a comparison opens with.
        Alphabetising it here would be a presentation decision taken in the wrong layer — and it was:
        the sorted list in the 422 below is what forced the interface to re-order it back.

        `embedder` travels with each one, null under `lexical`, for the same reason `QueryResponse`
        carries it: `strategy` alone stops being an identity the moment ADR-0005's second embedder
        exists.
        """
        available: dict[str, Retriever] = http.app.state.retrievers
        return StrategiesResponse(
            strategies=tuple(
                Strategy(name=name, embedder=retriever.embedder_id)
                for name, retriever in available.items()
            ),
            default=http.app.state.default_strategy,
        )

    @app.post("/query")
    def query(query_request: QueryRequest, http: Request) -> QueryResponse:
        # From `app.state`, not from a closure: it is written by the lifespan, so reading it here is
        # what makes it impossible to serve a query the boot gate never ran for.
        corpus_hash: str = http.app.state.artifact.corpus_hash
        strategies: dict[str, Retriever] = http.app.state.retrievers
        name = query_request.strategy or http.app.state.default_strategy
        if name not in strategies:
            # 422 and the list, not a silent fall back to the default. A visitor comparing two
            # strategies who typos one and is quietly served the other reads a difference that is
            # not there, which in a benchmark is worse than an error.
            raise RequestValidationError(
                [
                    {
                        "type": "enum",
                        "loc": ("body", "strategy"),
                        "msg": f"this build serves {', '.join(sorted(strategies))}",
                        "input": query_request.strategy,
                        # The same list again, structurally, in the place Pydantic puts an enum's
                        # permitted values. Added beside the sentence and never in place of it: the
                        # sentence is what a human reads in a terminal, and changing its wording
                        # would break whoever is already reading it.
                        #
                        # Pipeline order here, unlike the message above, which is sorted for
                        # readability. A client picking column order out of an alphabetised list
                        # gets `dense` on the left, which is a presentation decision made by a
                        # `sorted()` call in an error handler.
                        "ctx": {"strategies": list(strategies)},
                    }
                ]
            )
        retriever = strategies[name]

        tracer = Tracer()
        with tracer.span(
            "query", **{"query.question": query_request.question, "corpus.hash": corpus_hash}
        ) as root:
            with tracer.span(
                "retrieve",
                **{
                    "retrieval.strategy": retriever.name,
                    # Beside the strategy rather than folded into it, and null under `lexical`. A
                    # trace that says `dense` without saying *which* vectors is a trace that cannot
                    # tell the Phase 4 arms apart (ADR-0005) — and the trace is the product.
                    "retrieval.embedder": retriever.embedder_id,
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
            embedder=retriever.embedder_id,
            k=query_request.k,
            tiers=query_request.tiers,
            chunks=tuple(RetrievedChunk.of(candidate) for candidate in candidates),
            contract=query_request.contract,
            answer=None if answer is None else GeneratedAnswer.of(answer),
            trace=tracer.tree(),
        )

    # The evaluation surface, read-only and deliberately unmodelled. Three endpoints that do nothing
    # but hand back the bytes on disk, because the interface's job is to *show the committed
    # numbers* and re-serialising them through a Pydantic model here would be a second place for the
    # record format to be described — and therefore a place for it to drift from `evaluation.py`
    # without any test noticing. `0.911765` must reach the screen from `eval/baseline.json` or not
    # at all: nothing the interface displays may be typed by hand (acceptance criterion six).
    #
    # They are GETs on the serving app rather than files under `static/` because the records are
    # written by `eval run` into `eval/runs/` and promoted by a human commit; copying them into the
    # package would make the interface show a stale copy of a number that has a canonical location.

    @app.get("/eval/baseline")
    def eval_baseline() -> dict[str, Any]:
        return _read_json(_baseline_path())

    @app.get("/eval/runs")
    def eval_runs() -> dict[str, list[str]]:
        # Newest first: `run_id` starts with a UTC timestamp, so lexicographic order is chronological
        # order, which is a property of the id format and not an accident worth re-deriving here.
        return {"run_ids": sorted(_run_ids(), reverse=True)}

    @app.get("/eval/runs/{run_id}")
    def eval_run(run_id: str) -> dict[str, Any]:
        # Checked against the listing rather than sanitised. A denylist of `..` and separators is a
        # thing to get wrong; membership in the set of records that actually exist cannot be.
        if run_id not in _run_ids():
            raise HTTPException(status_code=404, detail=f"no run record {run_id!r}")
        return _read_json(_runs_dir() / f"{run_id}.json")

    # Mounted **last**, after every route above. `StaticFiles` at the root is a catch-all: mounted
    # earlier it would swallow `/health`, `/query` and `/eval/*` and the service would answer every
    # one of them with a 404 page. Absent directory means no mount rather than a boot failure — the
    # API is the product's contract and a missing interface must never keep it from serving.
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    return app


def _baseline_path() -> Path:
    from garage.evaluation import BASELINE_PATH

    return BASELINE_PATH


def _runs_dir() -> Path:
    from garage.evaluation import RUNS_DIR

    return RUNS_DIR


def _run_ids() -> set[str]:
    directory = _runs_dir()
    return {path.stem for path in directory.glob("*.json")} if directory.is_dir() else set()


def _read_json(path: Path) -> dict[str, Any]:
    """The file, parsed and nothing else.

    404 rather than 500 when it is missing, and the distinction is not pedantry: a build with no
    promoted baseline is a legitimate state (`eval promote` is a deliberate human act, never
    automatic), so the interface must be able to tell "not measured yet" from "the service is
    broken" and say the right one on screen.
    """
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{path.name} has not been written yet")
    return json.loads(path.read_bytes())


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
    little — and it carries no cost and no tokens, because nothing was answered and nothing was
    billed. **A citation that does not resolve**: the answer is refused, marked as this codebase's
    own contract violation rather than as anything the model or the network did, and it carries the
    *full* cost on both the span and the wire, because that call was answered and charged for.
    **A good answer**: served with its tokens and its cost on the span.

    Our own bugs are the one thing that is *not* handled here. The `try` covers the provider call
    alone, so a `TypeError` in this function is a 500 rather than a polite note blaming a provider
    that answered correctly.
    """
    if generator is None:
        return None
    if not candidates:
        # Says only what actually happened — the retriever returned nothing — and deliberately not
        # *why*. The old wording named a similarity floor, which is `lexical`'s mechanism and
        # `lexical`'s alone: `dense` has no floor and cannot reach this branch at all today, and if
        # it ever gains one the reason will not be the same reason. An abstention reason that
        # asserts the internals of one strategy is a message that is either unreachable or wrong.
        return abstain_without_asking(
            "nenhum trecho do Corpus foi recuperado para esta pergunta",
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
            # The rejected answer is built *first*, and the span is then written from it. That
            # ordering is the fix for a real defect rather than a stylistic preference: the span and
            # the response used to be assembled independently from the same facts, and they drifted.
            # The span carried the cost from the day this state was introduced; the response carried
            # the defaults, so the same rejection was billed in the trace and free on the wire. Two
            # copies of one fact is one copy too many, so there is now a single object and the span
            # reads it.
            #
            # The asymmetry with the degradation block above is deliberate — do not "tidy" the two
            # into agreement. There the provider never answered, so there is nothing to bill and
            # nothing to record. Here it answered, on time, and charged for it; the answer is refused
            # by us, not unbilled by them. Dropping the cost would let a configuration that reliably
            # breaks the citation contract show up as the cheap one in the comparison the demo puts
            # on screen.
            # The wire carries a short sentence in the page's language, and the clause list stays
            # on the span beside every other raw detail — the same split degradation already uses,
            # where `degradation_reason` names the exception type and `exception.message` holds what
            # the provider actually said.
            #
            # Two sentences, chosen by the count, because one of them was a contradiction. A claim
            # marked supported with no citations at all produced "o gerador produziu citações que
            # não resolvem" followed by a clause stating there was no citation to resolve. The
            # docstring on `ContractViolation` already called that a different violation; the
            # sentence on screen did not.
            rejected = reject_unverifiable(
                "o gerador citou trechos que não foram recuperados"
                if violation.invalid_citations
                else "o gerador marcou uma afirmação como sustentada sem nenhuma citação",
                provider=generator.name,
                model=model,
                contract=contract,
                tokens_in=answer.tokens_in,
                tokens_out=answer.tokens_out,
                cost_usd=answer.cost_usd,
                cost_estimated=answer.cost_estimated,
                pricing_as_of=answer.pricing_as_of,
                # The adapter's own discarded citations plus the ones this re-assertion caught. Both
                # halves are needed and neither is the whole: the adapter counts what it dropped on
                # its way out, and this check exists precisely because an adapter can miss some.
                invalid_citations=answer.invalid_citations + violation.invalid_citations,
                unsupported_claims=answer.unsupported_claims,
            )
            span.set(
                error=True,
                **{
                    "exception.type": type(violation).__name__,
                    "exception.message": str(violation),
                    "generation.contract.violated": True,
                    "generation.abstained": False,
                    "generation.degraded": False,
                    "generation.support": rejected.support,
                    "generation.tokens.input": rejected.tokens_in,
                    "generation.tokens.output": rejected.tokens_out,
                    "generation.tokens.total": rejected.tokens_total,
                    "generation.cost.usd_estimated": rejected.cost_usd,
                    "generation.cost.estimated": rejected.cost_estimated,
                    "generation.pricing.as_of": rejected.pricing_as_of,
                    "generation.citations.invalid": rejected.invalid_citations,
                    "generation.claims.unsupported": rejected.unsupported_claims,
                },
            )
            return rejected

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
