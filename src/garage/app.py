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

## The cascade (issue #11)

The moment this service sits behind a public URL, `POST /query` stops being "run the pipeline" and
becomes "decide *which* pipeline this request deserves", because one of the stages costs money out of
a free tier with a daily ceiling. Four origins, in this order, and the order is the design:

```
POST /query
  ├─ anti-abuse: 10 requests a minute per address ────────────── 429, and the only 429 here
  ├─ 0. `rerun: true`?  skip 1 and 2 — a re-run that is served from a record or a cache is not a
  │     re-run — and fall back to the record if the live attempt is refused
  ├─ 1. a committed showcase arm matches this exact request? ──── origin="precomputed"
  ├─ 2. the answer cache holds this exact request? ────────────── origin="cache"
  ├─ 3. a generator exists and both daily budgets allow it? ───── origin="live"
  └─ 4. refused, or the provider said 429 ────────────────────── origin="live_degraded"
        retrieval ran anyway, because retrieval is local and free and is the product
```

Four things about that are decisions rather than mechanics.

**The showcase lookup is before the budgets.** A curated question was paid for once, months ago, and
costs nothing now. Rationing it would be rationing a free resource, and it is precisely what makes
acceptance criterion four true: when the quota is gone, the curated questions are still there and
still identical. The anti-abuse bucket *is* in front of everything, including the curated path,
because that limiter is not about the provider's quota — it is about a loop hammering an endpoint
that still touches Postgres on every call.

**A refused budget is never a 429.** It degrades. The visitor gets the chunks, the scores, the trace
and a marked answer saying the model could not be asked. Returning an error would throw away the free
half of the product in order to report the unavailability of the paid half — and the free half is the
half this project is actually about.

**No sixth answer state.** `degraded=true` with a different `degradation_reason`, reusing the state
issue #8 argued into existence. "I could not ask" already covers "I could not ask because the day's
budget is spent", and adding a state would undo work that was done deliberately.

**`origin` is a response field, not a header.** A header would be cheaper and is the wrong choice:
`adapt.toView` consumes bodies, not responses, and putting a fact the screen must render outside the
body would punch through the issue #9 boundary on its first day. It costs one key on `QueryResponse`
and breaks `tests/test_ui_contract.py`, which is exactly what that test is for — an addition to this
payload is supposed to be a decision somebody makes, not a thing that happens.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal, Sequence

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from garage import __version__
from garage.cache import AnswerCache, CachedAnswer, cache_key
from garage.config import Settings
from garage.limits import Limiter, client_address
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
    MAX_CHUNK_IDS,
    MAX_K,
    TIERS,
    Candidate,
    Filters,
    Retriever,
    available_retrievers,
    fetch_chunks,
)
from garage.tracing import Tracer

Tier = Literal["A", "B"]
PromptContract = Literal["cited", "free"]
# Where the answer on this response came from. A closed set, because the interface renders a
# different band for each one and an unrecognised value would render as nothing.
Origin = Literal["live", "cache", "precomputed", "live_degraded"]

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
    # The re-run button (issue #11, criterion five), and an explicit flag rather than a guess.
    #
    # It means "do not serve me a record and do not serve me a cache — call the provider now". Both
    # skips are required for the button to mean anything: a re-run answered out of the cache is a
    # re-run of nothing, and the visitor is being invited to *falsify* a published number, which they
    # cannot do against a copy of it (ADR-0004).
    #
    # It is not a way around the budget. It goes through the same admission as any other generation
    # and, when refused, the endpoint answers with the recorded arm marked `rerun_refused` — the
    # honest outcome, and better than an error, because the recorded number is still the thing the
    # visitor came to look at.
    rerun: bool = False


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
    # Null is reachable on exactly one path and is a *designed* state there: a precomputed answer
    # stores `chunk_id`s and never a word of the material (ADR-0003), so a deployment whose database
    # does not hold the operator's Corpus hydrates the identity, the rank, the score and the tier and
    # leaves the paragraph absent. `adapt.chunkView` already turns that null into `textAbsent` and
    # `render.chunkText` already draws it as an identified absence — the vocabulary existed before
    # this field could produce it. A live retrieval always carries text.
    text: str | None
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
    # First field on the model and first key a reader sees, deliberately. Everything under it is a
    # measurement, and the single most important thing to know about a measurement on this site is
    # whether it was taken just now, read out of a cache, or copied from a file somebody committed in
    # April. See the cascade in the module docstring.
    origin: Origin
    # Whatever that origin needs said about itself, and nothing shared between them. A `showcase_id`
    # is meaningless on a live answer and `generations_remaining` is meaningless on a precomputed
    # one; a flat model holding the union would publish a null for every field that does not apply
    # and invite a screen to render it. Null when there is genuinely nothing to add.
    #
    # The key sets are per-origin and are asserted in `tests/test_ui_contract.py`, so this being a
    # bare dict is a shape the interface can rely on rather than a place to put anything.
    origin_detail: dict[str, Any] | None
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


class ProvenanceResponse(BaseModel):
    """What this build is standing on, answerable before a visitor asks anything.

    It exists for one requirement that could not be met any other way: while the corpus is the
    fixture, **every screen** must say so, permanently and above everything else, because the
    documents in it were invented for this project and a demo that falsifies a claim about invented
    material is an exercise (ADR-0003, `corpus/fixture/`). That banner has to be on the page at load,
    before any query, on the metrics screen and on the showcase screen too — so the fact has to be
    readable from a GET that costs nothing.

    `POST /query` was not an option: it needs a question. `/health` was not an option either — a
    health check that grows fields is a health check that starts failing for reasons unrelated to
    health, and orchestrators read it.

    `corpus_id` rather than a `fixture: true` boolean. The interface asks "is this the fixture", but
    the *fact* is which Corpus is loaded, and a boolean would have to be recomputed here the day a
    second synthetic corpus exists. `banner.js` compares the string; the string is the truth.
    """

    model_config = ConfigDict(extra="forbid")

    corpus_id: str
    corpus_hash: str
    ingest_version: int
    # The commit this container was built from, or `unknown` in a build that has no git and was given
    # no `GARAGE_GIT_SHA`. Never invented: `unknown` is printed as `unknown`.
    git_sha: str
    version: str
    # Whether this build holds a `Generator` at all. Published beside the budget and not folded into
    # it, because a retrieval-only deployment is a *supported configuration* rather than a quota of
    # zero, and the two must not read alike. Without this field the site announced "200 de 200
    # gerações restantes hoje" on a service that cannot generate anything — a budget for a thing it
    # does not do.
    generation_configured: bool
    # The generation budget as it stands right now, published to anyone who asks. A visitor watching
    # the number fall is a visitor who is not surprised when the answers stop being live — and an
    # operator can read the state of the quota without shelling into the VM. Meaningless, and
    # labelled as such by the flag above, when no generator exists.
    budget: dict[str, int | str]


class HydratedChunk(BaseModel):
    """One chunk read back by identifier, for a screen that holds ids and needs words.

    `RetrievedChunk` minus `score` and `components`, and the two omissions are the point: this chunk
    was not ranked by anything, so a score here would be a number invented for a lookup. Same field
    names as `RetrievedChunk` wherever they overlap, because the interface renders both through the
    same component and a second spelling of `doc_title` would be a second thing to keep in step.
    """

    chunk_id: str
    doc_id: str
    doc_title: str
    tier: Tier
    page: int | None
    section: str | None
    kind: str
    text: str


class ChunksResponse(BaseModel):
    """The words behind a list of identifiers, and — separately — the ones this artifact lacks.

    `missing` is a field rather than a 404 because a partial answer is the *designed* state, not an
    error. A showcase record commits `chunk_id`s and never text (ADR-0003, `docs/showcase.md`), so a
    clone without the operator's material renders the metrics, the answer and the trace with those
    chunks marked absent and identified. An absence travels as an absence; a 404 would turn the
    whole page into an error over material it was never promised.

    `corpus_hash` is echoed for the same reason `QueryResponse` echoes it: text hydrated from one
    artifact into a record built against another is the exact failure the boot gate exists to
    prevent, and a client that can see both can refuse to draw.
    """

    corpus_hash: str
    chunks: tuple[HydratedChunk, ...]
    missing: tuple[str, ...]


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

    # One limiter and one cache per process, constructed here rather than at module scope. Module
    # scope would give two `TestClient`s in one test session a shared budget, which is a test
    # depending on the order the suite happens to run in — and would make "the counters reset on
    # restart" untrue in the one place it is convenient for it to be true.
    limiter = Limiter(
        requests_per_minute=settings.requests_per_minute,
        generations_per_day_per_client=settings.generations_per_day_per_client,
        generation_budget_per_day=settings.generation_budget_per_day,
    )
    cache = AnswerCache(
        max_entries=settings.cache_max_entries, ttl_seconds=settings.cache_ttl_seconds
    )

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
        # The same refusal, one artifact further out. A showcase record is precomputed prose citing
        # `chunk_id`s that `GET /chunks` will hydrate out of *this* database, so a record built
        # against a different Corpus would put the wrong paragraph under a real citation and nothing
        # on screen could tell. Loud, at boot, exactly like the database check above — and here
        # rather than inside `showcase.py` because refusing to *serve* is this layer's decision.
        from garage.showcase import precomputed_index, verify_showcase_records

        app.state.showcase_ids = verify_showcase_records(
            app.state.artifact.corpus_hash, settings.showcase_dir
        )
        # Indexed once, here, immediately after the gate that just agreed every record stands on this
        # artifact. Rebuilding it per request would put file reads and Pydantic validation on the hot
        # path of the one endpoint that has to answer a curated question instantly — which is
        # acceptance criterion two, and would be defeated by the very mechanism meant to serve it.
        app.state.precomputed = precomputed_index(_showcase_dir(settings))
        # Resolved once, for the same reason and one more: `git_provenance` shells out to git, and a
        # subprocess on every request would be a subprocess on every request. The environment wins
        # over the checkout, because the image is built from a tarball where git knows nothing.
        app.state.git_sha = settings.git_sha or _git_sha()
        app.state.retrievers = {strategy.name: strategy for strategy in strategies}
        app.state.default_strategy = strategies[0].name
        yield

    app = FastAPI(title="Garage", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.generator = generator
    app.state.limiter = limiter
    app.state.cache = cache

    @app.get("/health")
    def health() -> dict[str, str]:
        # Deliberately unchanged and deliberately never extended. Compose and systemd read this, and
        # a health check that grows fields is a health check that starts failing for reasons that
        # have nothing to do with health. Everything issue #11 wanted published went to
        # `/provenance` below instead.
        return {"status": "ok", "version": __version__}

    @app.get("/provenance")
    def provenance(http: Request) -> ProvenanceResponse:
        """Which Corpus, which commit, and how much of today's generation budget is left."""
        artifact = http.app.state.artifact
        return ProvenanceResponse(
            corpus_id=artifact.corpus_id,
            corpus_hash=artifact.corpus_hash,
            ingest_version=artifact.ingest_version,
            git_sha=http.app.state.git_sha,
            version=__version__,
            generation_configured=generator is not None,
            budget=limiter.snapshot(_utcnow()),
        )

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

        now = _utcnow()
        client = client_address(
            http.headers.get("x-forwarded-for"),
            http.client.host if http.client else None,
            trust_forwarded_for=settings.trust_forwarded_for,
        )

        # The anti-abuse bucket, in front of everything including the free paths. It is not the
        # provider's quota — it is a loop hammering an endpoint that touches Postgres on every call,
        # and the correct answer to that is the one 429 this endpoint produces. See the cascade in
        # the module docstring for why every *other* refusal degrades instead.
        admitted = limiter.admit_request(client, now)
        if not admitted.allowed:
            raise HTTPException(
                status_code=429,
                detail=(
                    "muitas requisições em pouco tempo. Nada foi consultado; tente de novo em "
                    f"{admitted.retry_after_seconds}s."
                ),
                headers={"Retry-After": str(admitted.retry_after_seconds)},
            )

        # Step 1. A curated question, answered months ago, costing nothing now — so it is looked up
        # before the budgets and never rationed by them. A re-run skips it by definition: a re-run
        # served from the record is a re-run of nothing (ADR-0004 — the published number is a claim
        # to falsify, and you cannot falsify a copy of it).
        from garage.showcase import find_precomputed

        recorded = find_precomputed(
            http.app.state.precomputed,
            question=query_request.question,
            strategy=name,
            k=query_request.k,
            tiers=query_request.tiers,
            contract=query_request.contract,
            corpus_hash=corpus_hash,
        )
        if recorded is not None and not query_request.rerun:
            return _precomputed_response(
                recorded, query_request, settings, rerun_refused=False
            )

        key = cache_key(
            question=query_request.question,
            strategy=name,
            k=query_request.k,
            tiers=list(query_request.tiers),
            contract=query_request.contract,
            corpus_hash=corpus_hash,
            embedder=retriever.embedder_id,
            model=getattr(generator, "model", None) if generator is not None else None,
            git_sha=http.app.state.git_sha,
        )

        # Step 2. Same skip, same reason.
        if not query_request.rerun:
            hit = cache.get(key, now)
            if hit is not None:
                return hit.payload.model_copy(
                    update={
                        # The question **this** visitor typed, not the one the first visitor typed.
                        # Two strings that normalise to the same key can differ in case and spacing,
                        # and the cached payload carries whoever asked first. Echoing that back
                        # would print `torque do cabeçote` to somebody who typed
                        # `TORQUE  DO  CABEÇOTE` — a small dishonesty in exactly the place
                        # `cache.py` promises the raw string is preserved, and the reason accents
                        # are deliberately not folded in the first place.
                        "question": query_request.question,
                        "origin": "cache",
                        "origin_detail": {
                            "key": key[:16],
                            "stored_at": _iso(hit.stored_at),
                            "age_seconds": int((now - hit.stored_at).total_seconds()),
                            **cache.stats(),
                        },
                    }
                )

        # Step 3. Admission, and the one place a generation is debited. Checked *before* the
        # retriever runs — not after — because the trace has to be able to say honestly that the
        # model was never consulted, and a `generate` span opened and then abandoned would be a trace
        # describing a call that did not happen.
        #
        # No generator at all is not a refusal and is not a degradation: it is the supported
        # retrieval-only configuration this service has always had, and it reports `origin="live"`
        # with a null answer, exactly as it reported a null answer before this cascade existed.
        decision = None
        if generator is not None:
            decision = limiter.admit_generation(client, now)

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
            #
            # Step 4 lives in this `if`. A refused budget passes `generator=None` down, so `_answer`
            # opens no `generate` span and the trace says truthfully that nothing was asked; the
            # degradation is then built here, from the refusal, in the page's language.
            refused = decision is not None and not decision.allowed
            answer = _answer(
                tracer,
                generator=None if refused else generator,
                question=query_request.question,
                candidates=candidates,
                contract=Contract(mode=query_request.contract),
            )
            if refused:
                answer = degrade(
                    _budget_sentence(decision.reason),
                    provider=generator.name,
                    model=getattr(generator, "model", None),
                    contract=Contract(mode=query_request.contract),
                )
                root.set(
                    **{
                        "generation.admitted": False,
                        "generation.refusal": decision.reason,
                    }
                )
            elif decision is not None and (answer is None or answer.provider is None):
                # Admitted, debited, and then never spent. The zero-cost abstention is the case: no
                # candidates means `_answer` asks nobody, and a budget that counted those would spend
                # the day's quota on the questions the corpus does not cover — which are exactly the
                # ones that are supposed to be free. `provider is None` is the wire-level marker for
                # "the model was never called" that `adapt.answerView` already reads.
                limiter.refund_generation(client, now)
            root.set(**{"query.candidates": len(candidates)})

        origin: Origin = "live_degraded" if refused else "live"
        response = QueryResponse(
            origin=origin,
            origin_detail=_live_detail(
                key=key,
                decision=decision,
                limiter=limiter,
                now=now,
                refused=refused,
                rerun=query_request.rerun,
                recorded=recorded,
            ),
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

        # A re-run that was refused falls back to the record rather than to an error. The visitor
        # asked to test a published number against a live call; we could not make the call, and the
        # published number is still the thing they came to look at. `rerun_refused` is what lets the
        # screen say so instead of silently showing them the same figures again.
        if refused and query_request.rerun and recorded is not None:
            return _precomputed_response(
                recorded, query_request, settings, rerun_refused=True
            )

        # Cached only when a provider was actually called and answered. Three exclusions, each for a
        # different reason. A **degradation** must never be cached: the budget resets at midnight UTC
        # and a cached refusal would go on refusing for twenty-four hours after the quota came back.
        # A **null answer** is the retrieval-only build and there is nothing to store. A **zero-cost
        # abstention** cost nothing, so caching it saves nothing and would only add a way for the
        # cheapest correct behaviour in the system to be served stale.
        if answer is not None and not answer.degraded and answer.provider is not None:
            cache.put(key, CachedAnswer(payload=response, stored_at=now))
        return response

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

    @app.get("/chunks")
    def chunks(
        http: Request,
        # No `max_length` here on purpose, even though FastAPI would happily enforce one. The cap is
        # `retrieval.MAX_CHUNK_IDS` and it is checked by `fetch_chunks`, so it holds for every caller
        # of that function rather than for HTTP alone — and there is one number, not two that can
        # drift. What this layer decides is the *status code*, below.
        ids: Annotated[list[str] | None, Query()] = None,
    ) -> ChunksResponse:
        """Read-only hydration: identifiers in, the artifact's own words out.

        This endpoint is what lets a committed showcase record hold **no chunk text at all**
        (ADR-0003). The acceptance criterion behind the showcase says "no *model* call" — it does
        not say "no database", and the words already live in `chunks.text`, in the derived artifact
        that ADR-0002 makes the one legitimate home for third-party material. So the record commits
        `chunk_id`s, this hands back the paragraphs, and neither one costs a cent or leaves the box.

        Repeated `ids` parameters rather than one comma-separated value, because a `chunk_id` is an
        opaque string this endpoint does not get to impose a grammar on. `doc#0007` is the shape
        today; a separator baked into the query format would be a separator some future corpus_id
        cannot contain.

        Nothing here is filtered by tier or by anything else. The caller is looking at a citation it
        already holds, and a filter could only hide the chunk behind it.
        """
        wanted = list(dict.fromkeys(ids or []))
        try:
            found = fetch_chunks(http.app.state.settings.database_url, wanted)
        except ValueError as too_many:
            # 422 and the real number, not a truncation. Silently answering the first hundred of a
            # hundred and twenty would hand a screen a partial hydration indistinguishable from
            # chunks the artifact genuinely lacks — the one confusion `missing` exists to prevent.
            raise HTTPException(status_code=422, detail=str(too_many)) from too_many
        by_id = {chunk.chunk_id: chunk for chunk in found}
        return ChunksResponse(
            corpus_hash=http.app.state.artifact.corpus_hash,
            # In the order they were asked for, not the order the SQL returned. The caller is
            # hydrating a ranked list and re-sorting it here would make every client sort it back.
            chunks=tuple(
                HydratedChunk(**vars(by_id[chunk_id])) for chunk_id in wanted if chunk_id in by_id
            ),
            missing=tuple(chunk_id for chunk_id in wanted if chunk_id not in by_id),
        )

    # The showcase surface, beside `GET /eval/*` and unmodelled for exactly the same reason: these
    # endpoints hand back the bytes on disk, because re-serialising a record through a Pydantic model
    # here would be a second description of the format and therefore a place for it to drift from
    # `showcase.py` with no test noticing. The screen shows the committed file or nothing.

    @app.get("/showcase")
    def showcase() -> dict[str, list[str]]:
        # Newest first, and by the same property as `GET /eval/runs`: `showcase_id` opens with a UTC
        # timestamp, so lexicographic order is chronological order.
        #
        # Re-listed off disk rather than served from `app.state.showcase_ids`, which the boot gate
        # already computed. The two would agree at boot and could diverge afterwards, and this is
        # the side to be on: the endpoint hands back what is on disk *now*, so a record dropped into
        # the directory of a running service is either served or not, never listed-but-404.
        return {"showcase_ids": list(_showcase_ids(settings))}

    @app.get("/showcase/{showcase_id}")
    def showcase_record(showcase_id: str) -> dict[str, Any]:
        # Membership in the set that exists, never a denylist of `..` and separators. Same reasoning
        # as `GET /eval/runs/{run_id}`: a denylist is a thing to get wrong.
        if showcase_id not in _showcase_ids(settings):
            raise HTTPException(status_code=404, detail=f"no showcase record {showcase_id!r}")
        return _read_json(_showcase_dir(settings) / f"{showcase_id}.json")

    # Mounted **last**, after every route above. `StaticFiles` at the root is a catch-all: mounted
    # earlier it would swallow `/health`, `/query` and `/eval/*` and the service would answer every
    # one of them with a 404 page. Absent directory means no mount rather than a boot failure — the
    # API is the product's contract and a missing interface must never keep it from serving.
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    return app


def _utcnow() -> datetime:
    """The clock, in one place, so a test can freeze it by patching one name.

    Timezone-aware and UTC. A naive datetime subtracted from an aware one raises, and the day
    boundary the generation budget rolls over on is defined in UTC (`limits.utc_day`).
    """
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _git_sha() -> str:
    """The commit this checkout is on, or `unknown`.

    Delegates to `evaluation.git_provenance`, which is where the subprocess and its failure mode
    already live. A second implementation would be a second thing to get wrong, and this one already
    answers `unknown` rather than inventing a sha for a tarball with no `.git`.
    """
    from garage.evaluation import git_provenance

    return git_provenance()[0]


_BUDGET_SENTENCES = {
    # Two sentences, not one, because they are two different facts and only one of them is about the
    # visitor. Both say what still works, because what still works is the product.
    "client_daily": (
        "você já usou sua cota de gerações de hoje neste endereço — os trechos recuperados abaixo "
        "continuam válidos e continuam sendo gratuitos"
    ),
    "global_daily": (
        "o orçamento diário de geração deste site acabou — os trechos recuperados abaixo continuam "
        "válidos, e as perguntas curadas continuam idênticas porque não custam nada"
    ),
}


def _budget_sentence(reason: str | None) -> str:
    return _BUDGET_SENTENCES.get(reason or "", "a geração não foi admitida")


def _live_detail(
    *,
    key: str,
    decision: Any,
    limiter: Limiter,
    now: datetime,
    refused: bool,
    rerun: bool,
    recorded: Any,
) -> dict[str, Any]:
    """What a live or degraded response says about the budget it was decided against.

    Published on the successful path too, not only on the refusal. A visitor who can watch
    `generations_remaining` fall is a visitor who is not surprised when it reaches zero — and the
    entire argument of this deployment is that its failure modes are legible before they happen
    rather than after.

    Only sixteen characters of the cache key. It is a diagnostic handle for an operator comparing two
    responses, not an identifier anything should be built on, and the full digest on the wire would
    invite exactly that.
    """
    return {
        "key": key[:16],
        # `decision is None` means no `Generator` was ever constructed, so the budget numbers below
        # describe a resource this build cannot spend. The band reads this and says "geração não
        # configurada neste build" instead of counting down a quota that will never move.
        "generation_configured": decision is not None,
        "refusal": decision.reason if (refused and decision is not None) else None,
        "rerun": rerun,
        # True only when the visitor asked for a live re-run and the budget said no *and* there was
        # no record to fall back to. The record case returns a `precomputed` response instead, and
        # carries its own flag.
        "rerun_refused": refused and rerun and recorded is None,
        **limiter.snapshot(now),
    }


def _precomputed_response(
    recorded: Any,
    query_request: QueryRequest,
    settings: Settings,
    *,
    rerun_refused: bool,
) -> QueryResponse:
    """A committed showcase arm, served as an answer to a live question, with zero model calls.

    The chunks are hydrated out of `chunks.text` by the same `fetch_chunks` `GET /chunks` uses —
    local, free, deterministic, no provider. The record itself stores `chunk_id`s and never a word of
    the material (ADR-0003), which is why a deployment whose database does not hold the operator's
    Corpus gets a `text` of null here and the interface draws an identified absence.

    Retrieval is **not** re-run. The recorded ranking is what the record asserts and is what the
    published spread was measured against; re-running it would produce a response whose chunks and
    whose answer came from two different executions, which is the same fabrication the cache
    deliberately refuses (`cache.CachedAnswer`). The visitor who wants a fresh ranking has the re-run
    button, and that is exactly what it is for.

    `question` echoes the string the visitor actually typed, not the record's phrasing. Two questions
    that normalise to the same key can differ in case and spacing, and echoing the record's wording
    back would put words on screen the visitor did not write.
    """
    from garage.showcase import Precomputed

    assert isinstance(recorded, Precomputed)
    arm = recorded.arm
    sample = arm.samples[arm.displayed_sample]
    wanted = [chunk.chunk_id for chunk in arm.retrieval.chunks]
    hydrated = {
        chunk.chunk_id: chunk for chunk in fetch_chunks(settings.database_url, wanted)
    }
    chunks = tuple(
        RetrievedChunk(
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            doc_title=chunk.doc_title,
            tier=chunk.tier,  # type: ignore[arg-type]
            page=chunk.page,
            # Both come from the artifact or from neither. `section` is the source document's own
            # heading and is source prose, so the record does not carry it either.
            section=hydrated[chunk.chunk_id].section if chunk.chunk_id in hydrated else None,
            kind=chunk.kind,
            text=hydrated[chunk.chunk_id].text if chunk.chunk_id in hydrated else None,
            score=chunk.score,
            components=chunk.components,
        )
        for chunk in arm.retrieval.chunks
    )
    return QueryResponse(
        origin="precomputed",
        origin_detail={
            "showcase_id": recorded.record.showcase_id,
            "scope": recorded.record.scope,
            "question_id": recorded.item.question_id,
            "why": recorded.item.why,
            "measured_on": recorded.record.sampling.measured_on,
            "generator": recorded.record.sampling.generator,
            "model": recorded.record.sampling.model,
            "temperature": recorded.record.sampling.temperature,
            "n": recorded.record.sampling.n,
            "displayed_sample": arm.displayed_sample,
            "display_rule": recorded.record.displayed_sample_rule,
            "git_sha": recorded.record.provenance.git_sha,
            "git_dirty": recorded.record.provenance.git_dirty,
            # The full spread, beside the single draw in `answer`, and this is a requirement rather
            # than generosity. `answer.tokens_out` and `answer.cost_usd` are **one draw of n** of a
            # stochastic quantity, and ADR-0004 forbids publishing a point estimate for one of those
            # without the distribution it came from. The showcase *record* enforces that structurally
            # by storing no scalars; this response would break it if it shipped the draw alone.
            "spread": {
                metric: spread.model_dump(mode="json") for metric, spread in arm.spread.items()
            },
            # True when the visitor pressed the re-run button and the budget refused the live call.
            # They asked to falsify this number and could not; saying so is the whole difference
            # between a degraded re-run and a page that quietly redisplays the same figures.
            "rerun_refused": rerun_refused,
            "chunks_absent": tuple(
                chunk_id for chunk_id in wanted if chunk_id not in hydrated
            ),
        },
        question=query_request.question,
        corpus_hash=recorded.record.provenance.corpus_hash,
        strategy=arm.strategy,
        embedder=arm.embedder,
        k=arm.k,
        tiers=tuple(arm.tiers),  # type: ignore[arg-type]
        chunks=chunks,
        contract=arm.contract,  # type: ignore[arg-type]
        answer=sample.answer,
        trace=sample.trace,
    )


def _showcase_dir(settings: Settings) -> Path:
    """Where this deployment's showcase records live.

    Null in `Settings` means the repository's own `eval/showcase/`, resolved here rather than in
    `config.py`: that module is the leaf everything else reads and must not import `showcase`, which
    imports this one.
    """
    from garage.showcase import SHOWCASE_DIR

    return settings.showcase_dir or SHOWCASE_DIR


def _showcase_ids(settings: Settings) -> tuple[str, ...]:
    from garage.showcase import showcase_ids

    return showcase_ids(_showcase_dir(settings))


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
