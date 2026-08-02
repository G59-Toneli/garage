"""Generation: prose that is only ever allowed to say what was retrieved.

`Generator` is the second seam, and it is deliberately shaped like the first one (design §7.1):
`generate(query, context, contract) -> Answer`, a structural `Protocol`, a `name` that makes a
Configuration identifiable (§10.3), and everything after the query keyword-only. A hosted model sits
behind it. The endpoint must no more learn that it is talking to Gemini than it learns that
retrieval is lexical — swapping the provider is a runtime axis and must cost one constructor call.

Three properties this module exists to hold, none of which a prompt can be trusted with:

**Every claim carries a citation, and every citation resolves.** The chunks go into the prompt
numbered `[1]..[n]`, and the same pass that numbers them builds the `int -> Candidate` map that makes
validation possible afterwards. The `chunk_id` is never shown to the model: `svc-kadett-1993#0001` is
exactly the kind of token a language model invents plausibly, while a small integer in a closed range
is checkable. After the model answers, every citation is checked against that range and resolved back
to a real `chunk_id` — a citation the response carries is one that was verified against the chunks the
retriever actually returned, not one the model asserted.

**Abstention is a result, not an error** (design §6). When the context does not cover the question the
answer is an `Answer` with `abstained=True` and no claims, served with HTTP 200, because a correct
refusal is the behaviour we want and is routinely misread as a failure. The retriever's
`WORD_SIMILARITY_FLOOR` is what makes the cheapest case possible at all (`docs/retrieval.md`): zero
candidates means abstention *without asking the model*, which costs nothing and cannot hallucinate.

**Degradation is not abstention.** "The corpus does not cover this" and "I could not reach the model"
are different facts about the system, and collapsing them would destroy the abstention rate ADR-0004
measures. So this module's adapter is honest and raises; the *policy* of degrading to
retrieval-only lives in the endpoint, in one place, where it can be seen.

The prompt contract itself is a runtime axis (ADR-0005, design §9): `cited` and `free`. The second
exists only so the demo can show what the first is buying, and is never the default — the difference
between them is one system instruction and nothing else, which is the point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Protocol, Sequence

from garage.retrieval import Candidate

CONTRACTS = ("cited", "free")

# The model reads the tier letter, not just the UI (design §13). A manual and a forum thread must
# never look alike, and a generator that cannot tell them apart cannot prefer the manufacturer's
# torque figure over someone's recollection of it.
TIER_LABELS = {
    "A": "Tier A — documentação técnica do fabricante",
    "B": "Tier B — relato de comunidade, não verificado",
}

# Temperature zero because two runs of the same Configuration over the same artifact must produce the
# same run record; a benchmark whose answers wobble reports sampling noise as a difference. It does
# not make a hosted model deterministic, but it removes the part we control.
TEMPERATURE = 0.0

# Enough for a paragraph with citations and no more. The cap matters twice: it bounds cost per query
# on a free tier, and a truncated answer is invalid JSON rather than a plausible half-sentence — a
# parse failure degrades visibly, which is the failure mode worth having.
MAX_OUTPUT_TOKENS = 1024

# A demo that hangs for a minute is worse than one that degrades in two seconds, and the visitor
# cannot tell a slow model from a broken service. The SDK's own retry ladder is disabled for the same
# reason: four backed-off attempts against an exhausted free-tier quota is a minute of nothing.
REQUEST_TIMEOUT_SECONDS = 20.0

# Default model, and the comment is load-bearing: the 2.5 family is on a retirement path announced
# for late 2026. This constant is expected to change, and nothing outside this line should have to.
DEFAULT_MODEL = "gemini-2.5-flash"

# USD per million tokens, (input, output), read from the published price list on this date. The
# number exists to give an order of magnitude to a comparison *between configurations* — "the hybrid
# run costs four times the lexical one" — not to reconcile with an invoice. It is stamped with a date
# and travels into the span so a stale figure is visible rather than silently believed.
PRICING_AS_OF = "2026-08-01"
PRICES_USD_PER_MTOK = {
    "gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50")),
    "gemini-2.5-flash-lite": (Decimal("0.10"), Decimal("0.40")),
}

CITED_SYSTEM_INSTRUCTION = """\
Você responde perguntas sobre um Chevrolet Kadett GSi 1993 usando exclusivamente os trechos
numerados fornecidos no contexto da pergunta.

Regras, todas obrigatórias:
1. Responda apenas a partir dos trechos numerados. Conhecimento prévio seu sobre o veículo não é
   fonte e não pode aparecer na resposta.
2. Toda afirmação carrega ao menos uma citação, pelo número do trecho que a sustenta.
3. Cite apenas números que aparecem no contexto desta pergunta. Nunca invente um número.
4. Se os trechos não cobrem a pergunta, responda com "abstained": true, "reason" explicando o que
   falta, e nenhuma afirmação. Abster-se é a resposta certa nesse caso, não uma falha.
5. Cobertura parcial: responda a parte coberta, com citação, e declare explicitamente, em uma
   afirmação própria citando o trecho relevante, o que os trechos não cobrem. Nunca preencha a
   lacuna com suposição.
6. Trechos Tier A vêm da documentação do fabricante e Tier B de relatos de comunidade. Prefira
   Tier A quando os dois discordarem, e diga que discordam.

Responda em português do Brasil, em JSON conforme o schema."""

# The contrast variant, and it exists only to be shown next to the one above (ADR-0005). It is never
# the default: the whole claim of the system is the first instruction, and a build where "free"
# could be reached by omitting a field would be a system whose central property is opt-in.
FREE_SYSTEM_INSTRUCTION = """\
Você responde perguntas sobre um Chevrolet Kadett GSi 1993. Os trechos numerados no contexto são
material de apoio; você pode usar também o que sabe sobre o veículo. Não é necessário citar.

Responda em português do Brasil, em JSON conforme o schema."""

# The parseable shape, forced by the provider rather than recovered by a regular expression. Parsing
# `[3]` out of free prose is brittle in exactly the ways that matter: a model writes `[1,2]`, `[1][2]`
# and `[1-3]` for the same idea, and every one of those is a different bug. Carrying the citations
# structurally on each claim removes the problem instead of handling it.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "abstained": {"type": "boolean"},
        "reason": {"type": "string", "nullable": True},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "citations"],
            },
        },
    },
    "required": ["abstained", "claims"],
}


class GenerationError(RuntimeError):
    """The model could not be asked, or could not be understood.

    Raised by adapters and by the parser, never caught here. Degradation policy belongs to the
    service (see `app.query`), so that a `Generator` stays testable as a unit and there is exactly
    one place in the codebase that decides what a provider failure looks like to a visitor.
    """


@dataclass(frozen=True)
class Contract:
    """The prompt contract, as a validated value rather than loose keyword arguments.

    Same argument as `retrieval.Filters`: every implementation must accept exactly the same knobs,
    and a provider that quietly ignored `mode` would make the `cited`/`free` comparison meaningless.
    The default is `cited` here as well as at the HTTP edge — a citation contract that could be
    switched off by leaving a field out is not a contract.
    """

    mode: str = "cited"
    max_output_tokens: int = MAX_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        if self.mode not in CONTRACTS:
            raise ValueError(f"mode must be one of {CONTRACTS}, got {self.mode!r}")
        if self.max_output_tokens < 1:
            raise ValueError(f"max_output_tokens must be at least 1, got {self.max_output_tokens}")

    @property
    def system_instruction(self) -> str:
        return CITED_SYSTEM_INSTRUCTION if self.mode == "cited" else FREE_SYSTEM_INSTRUCTION

    @property
    def enforced(self) -> bool:
        """Whether post-hoc validation may call an uncited claim unsupported."""
        return self.mode == "cited"


@dataclass(frozen=True)
class Citation:
    """A verified pointer: the number the model wrote, and the chunk it resolved to.

    Both travel. The number is what the prose reads as; the `chunk_id` is what the interface links
    and what makes a stored run record reproducible against the same artifact.
    """

    index: int
    chunk_id: str


@dataclass(frozen=True)
class Claim:
    """One assertion and the citations that survived validation.

    `supported` is false when every citation the model offered for this claim was discarded. Such a
    claim is still shown, marked — dropping the whole answer to abstention over one bad index is too
    blunt, and it would also hide the failure. Keeping it visible preserves the Glass Box and hands
    the ADR-0004 judge strictly more information than a blank page would.
    """

    text: str
    citations: tuple[Citation, ...] = ()
    supported: bool = True


@dataclass(frozen=True)
class Answer:
    """What generation produced, including the two cases that are not prose.

    An abstention has `abstained=True`, no claims and an empty `text`. A degradation has
    `degraded=True`, no claims and an empty `text`. They are separate fields because they are
    separate facts: "the corpus does not cover this" is the system working, "I could not reach the
    model" is the system failing, and a metric that added them together would be measuring nothing.
    """

    text: str = ""
    claims: tuple[Claim, ...] = ()
    abstained: bool = False
    abstention_reason: str | None = None
    degraded: bool = False
    degradation_reason: str | None = None
    support: str = "not_applicable"
    provider: str | None = None
    model: str | None = None
    contract: str = "cited"
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    # Never a silent zero. A model with no published price is `None` with this flag false, because a
    # zero cost in a trace is a lie, and the trace is the product.
    cost_estimated: bool = False
    pricing_as_of: str | None = None
    invalid_citations: int = 0
    unsupported_claims: int = 0
    # `abstained: true` arriving together with claims is a contradictory state. It is resolved in
    # favour of abstention and recorded rather than smoothed over: a model that does this is a fact
    # about the configuration under test.
    contradictory: bool = False

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


class Generator(Protocol):
    """`generate(query, context, contract) -> Answer`, and nothing else (design §7.1)."""

    #: How a run record names this provider. A Configuration is unidentifiable without it.
    name: str
    #: The specific hosted model. Separate from `name` because cost, tokens and retirement dates
    #: belong to the model, not to the vendor behind it.
    model: str

    def generate(
        self, query: str, *, context: Sequence[Candidate], contract: Contract = Contract()
    ) -> Answer:
        ...


@dataclass(frozen=True)
class Validation:
    """The result of checking a raw model payload against the chunks that were actually retrieved."""

    claims: tuple[Claim, ...] = ()
    text: str = ""
    abstained: bool = False
    abstention_reason: str | None = None
    support: str = "not_applicable"
    invalid_citations: int = 0
    unsupported_claims: int = 0
    contradictory: bool = False


def index_map(context: Sequence[Candidate]) -> dict[int, Candidate]:
    """Citation number to chunk, numbered from 1.

    One function, used by the prompt builder and by validation both, because a citation is only
    checkable if the numbering the model read and the numbering we check against are the same
    numbering. Starting at 1 is not cosmetic: an off-by-one here would resolve every citation in
    every stored run record to the wrong chunk, consistently and invisibly.
    """
    return dict(enumerate(context, start=1))


def numbered_context(context: Sequence[Candidate]) -> tuple[str, dict[int, Candidate]]:
    """Render the chunks as `[1]..[n]` and return the map that makes validation possible.

    One pass, two outputs, on purpose: the numbering the model sees and the numbering validation
    checks against cannot be built separately without inviting them to disagree. Numbering starts at
    1 because that is what prose citations look like, and off-by-one here would silently shift every
    citation in the corpus by one chunk.
    """
    index_to_candidate = index_map(context)
    blocks = []
    for index, candidate in index_to_candidate.items():
        where = " — ".join(
            part
            for part in (
                TIER_LABELS.get(candidate.tier, f"Tier {candidate.tier}"),
                candidate.doc_title,
                candidate.section,
                None if candidate.page is None else f"p. {candidate.page}",
            )
            if part
        )
        blocks.append(f"[{index}] ({where})\n{candidate.text}")
    return "\n\n".join(blocks), index_to_candidate


def build_prompt(query: str, context: Sequence[Candidate]) -> tuple[str, dict[int, Candidate]]:
    """The user turn. The contract itself is *not* here — it lives in the system instruction, so the
    `free` variant is literally one different instruction and nothing else changes."""
    block, index_to_candidate = numbered_context(context)
    prompt = f"Trechos disponíveis:\n\n{block}\n\nPergunta: {query}"
    return prompt, index_to_candidate


def parse_payload(text: str) -> Mapping[str, Any]:
    """JSON in, mapping out; anything else is a `GenerationError`.

    Invalid JSON is the expected shape of a truncated answer — `max_output_tokens` cuts the model off
    mid-object — and it must degrade, not 500. Raising the module's own error is what lets the
    endpoint treat a parse failure and a quota failure with the same policy.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as failure:
        raise GenerationError(f"the model did not return valid JSON: {failure}") from failure
    if not isinstance(payload, Mapping):
        raise GenerationError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def validate_payload(
    payload: Mapping[str, Any], *, context: Sequence[Candidate], contract: Contract = Contract()
) -> Validation:
    """Check every citation against the chunks that were really retrieved.

    This function is the first acceptance criterion. The prompt asks for grounded citations; only
    this defends against getting ungrounded ones. An index outside `1..len(context)` is discarded
    and counted — never repaired, never guessed at — and a claim left with no surviving citation is
    marked `supported=False` rather than deleted or promoted.
    """
    # The same numbering the prompt used, from the same function, so the two cannot drift apart.
    index_to_candidate = index_map(context)
    abstained = bool(payload.get("abstained", False))
    reason = payload.get("reason")
    reason = str(reason) if reason else None

    claims: list[Claim] = []
    invalid = 0
    for raw_claim in _as_sequence(payload.get("claims")):
        if not isinstance(raw_claim, Mapping):
            continue
        text = str(raw_claim.get("text", "")).strip()
        if not text:
            continue
        citations: list[Citation] = []
        seen: set[int] = set()
        for raw_index in _as_sequence(raw_claim.get("citations")):
            index = _as_index(raw_index)
            if index is None or index not in index_to_candidate:
                invalid += 1
                continue
            if index in seen:
                continue
            seen.add(index)
            citations.append(Citation(index=index, chunk_id=index_to_candidate[index].chunk_id))
        supported = bool(citations) or not contract.enforced
        claims.append(Claim(text=text, citations=tuple(citations), supported=supported))

    contradictory = abstained and bool(claims)
    if abstained:
        # Abstention cancels the claims. A refusal that also asserts things is not a refusal, and
        # serving both would let an unsupported sentence through under the safest-looking flag.
        return Validation(
            abstained=True,
            abstention_reason=reason or "os trechos recuperados não cobrem a pergunta",
            support="not_applicable",
            invalid_citations=invalid,
            contradictory=contradictory,
        )

    unsupported = sum(1 for claim in claims if not claim.supported)
    if not claims:
        # No abstention flag and no claims either: nothing was said, so nothing is being asserted.
        # Reported as an abstention because that is what it is from the reader's side, with the
        # reason naming what happened rather than inventing one.
        return Validation(
            abstained=True,
            abstention_reason=reason or "o modelo não produziu nenhuma afirmação",
            support="not_applicable",
            invalid_citations=invalid,
            contradictory=contradictory,
        )

    if not contract.enforced:
        support = "unenforced"
    elif unsupported == 0:
        support = "supported"
    elif unsupported < len(claims):
        support = "partially_supported"
    else:
        support = "unsupported"

    return Validation(
        claims=tuple(claims),
        text=" ".join(claim.text for claim in claims),
        support=support,
        invalid_citations=invalid,
        unsupported_claims=unsupported,
        contradictory=contradictory,
    )


def estimate_cost_usd(model: str, *, tokens_in: int, tokens_out: int) -> float | None:
    """USD for one call, or `None` for a model with no price on record.

    Computed in `Decimal` and handed out as `float` only at the boundary, because span attributes
    carry native OTLP scalars (`tracing.AttributeValue`). An unknown model is `None` and never zero:
    a free-looking line in a cost comparison is worse than a missing one.
    """
    price = PRICES_USD_PER_MTOK.get(model)
    if price is None:
        return None
    million = Decimal(1_000_000)
    cost = (Decimal(tokens_in) / million) * price[0] + (Decimal(tokens_out) / million) * price[1]
    return float(cost)


def tokens_from_usage(usage: Any) -> tuple[int, int]:
    """`usage_metadata` to `(tokens_in, tokens_out)`.

    Pulled out as a pure function taking a duck-typed object precisely so it can be tested without
    the SDK installed and without a network — it is the one part of the adapter that has behaviour
    worth asserting. Missing counters read as zero: the provider omits them on some error paths and a
    zero token count is honest there, unlike a zero cost for a priced model.
    """
    return (
        _as_count(getattr(usage, "prompt_token_count", None)),
        _as_count(getattr(usage, "candidates_token_count", None)),
    )


def answer_from_validation(
    validation: Validation,
    *,
    provider: str,
    model: str,
    contract: Contract,
    tokens_in: int,
    tokens_out: int,
) -> Answer:
    """Attach identity and cost to a validated payload. Shared by every adapter, so the cost rule and
    the pricing date are written once."""
    cost = estimate_cost_usd(model, tokens_in=tokens_in, tokens_out=tokens_out)
    return Answer(
        text=validation.text,
        claims=validation.claims,
        abstained=validation.abstained,
        abstention_reason=validation.abstention_reason,
        support=validation.support,
        provider=provider,
        model=model,
        contract=contract.mode,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        cost_estimated=cost is not None,
        pricing_as_of=PRICING_AS_OF,
        invalid_citations=validation.invalid_citations,
        unsupported_claims=validation.unsupported_claims,
        contradictory=validation.contradictory,
    )


def abstain_without_asking(reason: str) -> Answer:
    """The zero-cost abstention: no candidates, so no call.

    `WORD_SIMILARITY_FLOOR` in the retriever is what makes this reachable at all — a retriever that
    always returned its ten least-bad chunks would leave nothing to abstain on. Nothing was asked of
    any model here, so no model, no tokens, and no `generate` span in the trace.
    """
    return Answer(abstained=True, abstention_reason=reason, support="not_applicable")


def degrade(reason: str, *, provider: str | None = None, model: str | None = None) -> Answer:
    """The provider failed; the retrieved chunks still stand.

    Deliberately `abstained=False`. The corpus may well cover the question — we never got to ask.
    Reporting this as an abstention would inflate the abstention rate ADR-0004 measures with the
    free tier's quota errors, which on this VM is the *expected* failure rather than the rare one.
    """
    return Answer(
        degraded=True,
        degradation_reason=reason,
        support="not_applicable",
        provider=provider,
        model=model,
    )


class GeminiGenerator:
    """The hosted model behind the interface, over `google-genai`.

    The SDK is an *optional* extra (`pip install garage[gemini]`) and is imported here, in
    `__init__`, rather than at module import. That is what lets the whole test suite — and a
    retrieval-only deployment — run with neither the package nor an API key present, which is the
    state of the machine this was built on.

    Constructing one opens nothing over the network, mirroring `LexicalRetriever`: the client is
    built, no request is made. This class is honest about failure and raises; the endpoint decides
    what a visitor sees.
    """

    name = "gemini"

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL) -> None:
        # Late import, and `google-genai` specifically — not the retired `google-generativeai`.
        from google import genai
        from google.genai import errors, types

        self.model = model
        self._types = types
        self._errors = errors
        self._client = genai.Client(api_key=api_key)

    def generate(
        self, query: str, *, context: Sequence[Candidate], contract: Contract = Contract()
    ) -> Answer:
        types = self._types
        prompt, _ = build_prompt(query, context)
        config = types.GenerateContentConfig(
            system_instruction=contract.system_instruction,
            temperature=TEMPERATURE,
            max_output_tokens=contract.max_output_tokens,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            # Thinking tokens are billed as output and inflate both cost and latency without adding
            # a word of prose. The task here is quoting five paragraphs accurately, not reasoning.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            http_options=types.HttpOptions(
                timeout=int(REQUEST_TIMEOUT_SECONDS * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        try:
            # `models.generate_content` rather than the newer Interactions API (chosen 2026-08-01):
            # it is the better-documented path and its `usage_metadata` shape is stable, which is
            # what the cost column depends on. Worth revisiting when Interactions settles.
            response = self._client.models.generate_content(
                model=self.model, contents=prompt, config=config
            )
        except self._errors.APIError as failure:
            # 429 RESOURCE_EXHAUSTED is the free tier's ordinary state, not an exception to it.
            raise GenerationError(f"{type(failure).__name__}: {failure}") from failure
        except Exception as failure:  # timeouts and transport errors are not `APIError`
            raise GenerationError(f"{type(failure).__name__}: {failure}") from failure

        tokens_in, tokens_out = tokens_from_usage(getattr(response, "usage_metadata", None))
        validation = validate_payload(
            parse_payload(getattr(response, "text", "") or ""),
            context=context,
            contract=contract,
        )
        return answer_from_validation(
            validation,
            provider=self.name,
            model=self.model,
            contract=contract,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


def _as_sequence(value: Any) -> Iterable[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _as_index(value: Any) -> int | None:
    # `True` is an `int` in Python and `"1"` is not; both would be a model doing something strange,
    # and neither is quietly coerced into a citation.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
