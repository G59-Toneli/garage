"""Generation, tested without a network and without an API key.

Nothing here imports `google.genai`, and that is a requirement rather than a convenience: the package
is an optional extra and the machine this was built on has no key at all. Everything worth asserting
about the citation contract is pure — the numbering, the post-hoc validation, the cost arithmetic and
the usage translation — because those are the parts that must hold no matter which provider answers.

What is deliberately *not* covered here is `GeminiGenerator.generate` end to end; see the last test
in this file for the part of it that can be checked, and `docs/generation.md` for what cannot.
"""

import os

import pytest

from garage.generation import (
    CITED_SYSTEM_INSTRUCTION,
    FREE_SYSTEM_INSTRUCTION,
    PRICING_AS_OF,
    Contract,
    GenerationError,
    answer_from_validation,
    build_prompt,
    degrade,
    estimate_cost_usd,
    index_map,
    numbered_context,
    parse_payload,
    tokens_from_usage,
    validate_payload,
)
from garage.retrieval import Candidate


def candidate(chunk_id="svc-kadett-1993#0001", tier="A", title="Manual de Serviço") -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("#")[0],
        doc_title=title,
        tier=tier,
        page=12,
        section="Section 3.2",
        kind="spec",
        text="Torque (N·m): 41",
        score=0.9,
        components={"lexical": 0.8, "trigram": 0.5},
    )


CONTEXT = (
    candidate("svc-kadett-1993#0001"),
    candidate("svc-kadett-1993#0002"),
    candidate("forum-swap-250s#0003", tier="B", title="Fórum Opala & Cia"),
)


def claim(text="41 N·m.", citations=(1,)) -> dict:
    return {"text": text, "citations": list(citations)}


# --- the prompt ------------------------------------------------------------------------------


def test_chunks_are_numbered_from_one_and_carry_their_tier_and_title():
    block, mapping = numbered_context(CONTEXT)

    assert "[1]" in block and "[2]" in block and "[3]" in block
    assert "[0]" not in block and "[4]" not in block
    assert "Tier A" in block and "Tier B" in block
    assert "Manual de Serviço" in block and "Fórum Opala & Cia" in block
    # The map is what makes validation possible at all, and it must follow retrieval order exactly.
    assert mapping == {1: CONTEXT[0], 2: CONTEXT[1], 3: CONTEXT[2]}
    assert index_map(CONTEXT) == mapping


def test_the_prompt_never_shows_the_model_a_chunk_id():
    # A `chunk_id` is exactly the kind of opaque token a model invents plausibly. Small integers in
    # a closed range are checkable afterwards; `svc-kadett-1993#0001` is not.
    prompt, _ = build_prompt("torque do cabeçote", CONTEXT)

    assert "svc-kadett-1993#0001" not in prompt
    assert "torque do cabeçote" in prompt


def test_the_contract_lives_in_the_system_instruction_and_nowhere_else():
    # The whole `cited`/`free` comparison is only honest if that is the single difference between
    # the two runs (ADR-0005).
    cited = Contract()
    assert cited.mode == "cited" and cited.enforced
    assert cited.system_instruction == CITED_SYSTEM_INSTRUCTION
    assert Contract(mode="free").system_instruction == FREE_SYSTEM_INSTRUCTION
    assert not Contract(mode="free").enforced


@pytest.mark.parametrize("bad", [{"mode": "livre-demais"}, {"max_output_tokens": 0}])
def test_a_contract_validates_itself(bad):
    with pytest.raises(ValueError):
        Contract(**bad)


# --- post-hoc validation ---------------------------------------------------------------------


def test_every_citation_resolves_to_a_chunk_that_was_actually_retrieved():
    result = validate_payload(
        {"abstained": False, "claims": [claim(citations=[1, 3])]}, context=CONTEXT
    )

    assert result.support == "supported"
    cited = result.claims[0].citations
    assert [c.index for c in cited] == [1, 3]
    assert [c.chunk_id for c in cited] == ["svc-kadett-1993#0001", "forum-swap-250s#0003"]
    assert result.invalid_citations == 0


def test_a_citation_outside_the_range_is_discarded_and_counted():
    # Three chunks in context, so `[7]` and `[0]` are both hallucinations. Nothing in the prompt can
    # prevent this; only this function can catch it, which is why it is not optional.
    result = validate_payload(
        {"abstained": False, "claims": [claim(citations=[7, 0])]}, context=CONTEXT
    )

    assert result.claims[0].citations == ()
    assert result.claims[0].supported is False
    assert result.invalid_citations == 2
    assert result.unsupported_claims == 1
    assert result.support == "unsupported"


def test_one_bad_claim_downgrades_the_answer_rather_than_destroying_it():
    result = validate_payload(
        {"abstained": False, "claims": [claim(citations=[2]), claim("63 N·m.", citations=[9])]},
        context=CONTEXT,
    )

    # Both claims survive; the second is marked. Collapsing the whole answer to abstention over one
    # bad index would be blunter and less informative than saying which sentence is unfounded.
    assert [c.supported for c in result.claims] == [True, False]
    assert result.support == "partially_supported"
    assert result.text == "41 N·m. 63 N·m."


@pytest.mark.parametrize("bogus", ["3", 3.0, True, None, [3]])
def test_a_citation_that_is_not_an_integer_is_not_coerced_into_one(bogus):
    result = validate_payload({"abstained": False, "claims": [claim(citations=[bogus])]},
                              context=CONTEXT)

    assert result.claims[0].citations == ()
    assert result.invalid_citations == 1


def test_abstention_cancels_any_claims_that_came_with_it_and_records_the_contradiction():
    result = validate_payload(
        {"abstained": True, "reason": "sem cobertura", "claims": [claim()]}, context=CONTEXT
    )

    assert result.abstained is True
    assert result.claims == ()
    assert result.abstention_reason == "sem cobertura"
    # Recorded rather than smoothed over: a model that does this is a fact about the configuration.
    assert result.contradictory is True


def test_an_empty_answer_is_reported_as_an_abstention_rather_than_as_empty_prose():
    result = validate_payload({"abstained": False, "claims": []}, context=CONTEXT)

    assert result.abstained is True and result.text == ""


def test_the_free_contract_does_not_call_an_uncited_claim_unsupported():
    result = validate_payload(
        {"abstained": False, "claims": [claim(citations=[])]},
        context=CONTEXT,
        contract=Contract(mode="free"),
    )

    assert result.claims[0].supported is True
    assert result.support == "unenforced"


def test_a_truncated_answer_is_a_generation_error_rather_than_a_crash():
    # `max_output_tokens` cuts the model off mid-object; that has to degrade, not 500.
    with pytest.raises(GenerationError):
        parse_payload('{"abstained": false, "claims": [{"text": "41 N')
    with pytest.raises(GenerationError):
        parse_payload("[]")


# --- cost and usage --------------------------------------------------------------------------


def test_cost_is_the_published_rate_applied_to_the_tokens_actually_spent():
    cost = estimate_cost_usd("gemini-2.5-flash", tokens_in=1_000, tokens_out=500)

    expected = (1_000 / 1_000_000) * 0.30 + (500 / 1_000_000) * 2.50
    assert cost == pytest.approx(expected)
    assert estimate_cost_usd("gemini-2.5-flash-lite", tokens_in=2_000, tokens_out=0) == pytest.approx(
        (2_000 / 1_000_000) * 0.10
    )


def test_a_model_with_no_published_price_costs_none_rather_than_zero():
    # A silent zero in a cost comparison is a lie, and the trace is the product.
    assert estimate_cost_usd("gemini-9.9-imaginary", tokens_in=1_000, tokens_out=1_000) is None


def test_an_unpriced_model_says_so_on_the_answer():
    answer = answer_from_validation(
        validate_payload({"abstained": False, "claims": [claim()]}, context=CONTEXT),
        provider="gemini",
        model="gemini-9.9-imaginary",
        contract=Contract(),
        tokens_in=10,
        tokens_out=10,
    )

    assert answer.cost_usd is None and answer.cost_estimated is False
    assert answer.pricing_as_of == PRICING_AS_OF


class FakeUsage:
    prompt_token_count = 812
    candidates_token_count = 96
    total_token_count = 908


def test_usage_metadata_translates_to_the_two_counts_the_answer_carries():
    # A stand-in object rather than the SDK's: this is the one piece of adapter behaviour that can
    # be asserted without a network, and it is asserted here for exactly that reason.
    assert tokens_from_usage(FakeUsage()) == (812, 96)
    assert tokens_from_usage(None) == (0, 0)


def test_an_answer_totals_its_own_tokens():
    answer = answer_from_validation(
        validate_payload({"abstained": False, "claims": [claim()]}, context=CONTEXT),
        provider="gemini",
        model="gemini-2.5-flash",
        contract=Contract(),
        tokens_in=812,
        tokens_out=96,
    )

    assert answer.tokens_total == 908
    assert answer.contract == "cited" and answer.provider == "gemini"


def test_a_degradation_is_not_an_abstention():
    # The two must never be collapsed: one says the corpus does not cover the question, the other
    # says the model could not be asked, and the ADR-0004 abstention rate depends on the difference.
    answer = degrade("quota", provider="gemini", model="gemini-2.5-flash")

    assert answer.degraded is True and answer.abstained is False
    assert answer.text == "" and answer.claims == ()


LIVE_KEY = os.environ.get("GARAGE_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")


@pytest.mark.skipif(not LIVE_KEY, reason="no Gemini key in the environment; this never runs in CI")
def test_the_real_adapter_answers_with_citations_that_resolve():
    """The one test that leaves the machine, and it is opt-in on purpose.

    CI has no key and never will (ADR-0001: no managed services in the loop that has to stay
    reproducible). This exists so that the adapter is known to have worked at least once against the
    real API rather than only against the author's reading of the documentation. Everything it
    proves is about the adapter; nothing about the system depends on it passing.
    """
    pytest.importorskip("google.genai", reason="the `gemini` extra is not installed")
    from garage.generation import GeminiGenerator

    context = (
        candidate("svc-kadett-1993#0001"),
        candidate("svc-kadett-1993#0006", title="Manual de Serviço"),
    )
    answer = GeminiGenerator(api_key=LIVE_KEY).generate("Qual o torque do cabeçote?",
                                                        context=context)

    assert answer.tokens_in > 0 and answer.tokens_out > 0
    assert answer.cost_usd is not None
    if not answer.abstained:
        cited = {c.index for claim in answer.claims for c in claim.citations}
        assert cited <= {1, 2}
        assert answer.invalid_citations == 0


def test_constructing_the_gemini_adapter_is_the_only_thing_the_suite_asks_of_it():
    # Mirrors `LexicalRetriever`: constructing one opens nothing. Skipped rather than failed where
    # the optional extra is absent, which is the normal state of this repository's CI.
    genai = pytest.importorskip("google.genai", reason="the `gemini` extra is not installed")
    assert genai is not None

    from garage.generation import GeminiGenerator

    generator = GeminiGenerator(api_key="not-a-real-key", model="gemini-2.5-flash")
    assert generator.name == "gemini" and generator.model == "gemini-2.5-flash"
