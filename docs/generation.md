# Generation, citations and abstention

The service answers in prose, and it is only ever allowed to say what it retrieved:

```sh
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "qual o torque do parafuso do volante?", "k": 5}'
```

```jsonc
{
  "question": "qual o torque do parafuso do volante?",
  "corpus_hash": "21c4e571…",
  "strategy": "lexical",
  "k": 5,
  "tiers": ["A", "B"],
  "contract": "cited",
  "chunks": [ /* unchanged — see docs/retrieval.md */ ],
  "answer": {
    "text": "O torque do parafuso do volante do motor é de 63 N·m.",
    "claims": [
      {
        "text": "O torque do parafuso do volante do motor é de 63 N·m.",
        "citations": [{"index": 2, "chunk_id": "svc-kadett-1993#0006"}],
        "supported": true
      }
    ],
    "abstained": false, "abstention_reason": null,
    "degraded": false, "degradation_reason": null,
    "support": "supported",
    "provider": "gemini", "model": "gemini-2.5-flash", "contract": "cited",
    "tokens_in": 414, "tokens_out": 38,
    "cost_usd": 0.0002192, "cost_estimated": true, "pricing_as_of": "2026-08-01",
    "invalid_citations": 0, "unsupported_claims": 0, "contradictory": false
  },
  "trace": { /* below */ }
}
```

`answer` and `contract` were **added** to the response; nothing that was there before moved or
changed meaning. The deterministic gate ([ADR-0004](adr/0004-two-layer-evaluation.md)) scores this
exact object, and a deployment with no generator configured still produces the response it was
written against — with `"answer": null` and no `generate` span.

## The citation contract

The retrieved chunks go into the prompt numbered `[1]..[n]`, in retrieval order, each carrying its
tier, document title, section and page:

```
[1] (Tier A — documentação técnica do fabricante — Manual de Serviço — Section 3.2)
Section 3.2 — … — Fastener: Cylinder head bolt, stage 1; Thread: M11; Torque (N·m): 41
```

The tier is in the *prompt*, not only in the interface. A manual and a forum post must never look
alike (design §13), and a generator that cannot tell them apart cannot prefer the manufacturer's
figure over somebody's recollection of it.

**The model never sees a `chunk_id`.** `svc-kadett-1993#0001` is exactly the kind of opaque token a
language model produces plausibly and unverifiably; a small integer in a closed range is checkable.
The same function that numbers the chunks for the prompt builds the `int -> Candidate` map that
validation uses afterwards, so the numbering the model read and the numbering we check against
cannot drift apart. Numbering starts at 1 — an off-by-one here would resolve every citation in every
stored run record to the wrong chunk, consistently and invisibly.

The model answers in JSON, forced by `response_schema`:

```jsonc
{"abstained": false, "reason": null,
 "claims": [{"text": "…", "citations": [2]}]}
```

The prose is reassembled from the claims. Each claim carries its citations *structurally*, which
removes the parsing problem rather than handling it: recovering `[3]` from free prose with
`\[(\d+)\]` is brittle in the ways that matter, because a model writes `[1,2]`, `[1][2]` and `[1-3]`
for the same idea and every one of those is a different bug.

### Validation is what makes it true

The prompt asks for grounded citations. Only the check afterwards can tell whether it got them, and
that check is not optional — it is the property the whole feature claims.

| Case | What happens |
| ---- | ------------ |
| `n` outside `1..len(context)` | Discarded, counted in `invalid_citations`. Never repaired, never guessed at. |
| `n` valid | Resolved to a real `chunk_id`; both the number and the id travel on the wire. |
| A claim with no citation left | `supported: false`, counted in `unsupported_claims`. |
| Some claims unsupported | `support: "partially_supported"`. |
| `abstained: true` *and* claims | Abstention wins, the claims are dropped, `contradictory: true`. |
| Invalid JSON (a truncated answer) | Degradation, not a 500. |
| Valid JSON, wrong shape | Degradation — **never** an abstention. |

The whole check then runs **again, at the endpoint**, against the chunks in the response being
assembled. The duplication is deliberate: `Generator` is a runtime axis, so the implementation that
forgets to validate is the next one, not this one. An acceptance criterion that says "every citation
resolves" is a claim about the system, and the system only has that property if something outside
the replaceable part enforces it. It costs a set membership per citation.

Keeping an unsupported claim visible and marked is a deliberate choice over two alternatives.
Deleting it would hide the failure, and dropping the whole answer to abstention over a single bad
index is blunter than the situation deserves — a reader gets four grounded sentences and one flagged
one, and the ADR-0004 judge gets strictly more information than a blank page would give it.

Nothing here trusts the model twice: a citation on the wire is one that was checked against the
chunks in the same response.

## Abstention

Abstention is a first-class result, served with **HTTP 200**. Design §6 says plainly that a correct
refusal to answer when the corpus does not cover the question is routinely mistaken for an error;
encoding it as one would make that permanent.

Two paths reach it, and only one of them costs anything:

- **Zero candidates — the model is never called.** The retriever's `WORD_SIMILARITY_FLOOR`
  (`docs/retrieval.md`) is what makes this reachable: a retriever that always returned its ten
  least-bad chunks would leave nothing to abstain on. No call, no tokens, no cost, and **no
  `generate` span** — a stage that did not run is absent from the trace, not present at zero
  milliseconds.
- **The model abstains over real chunks.** The chunks still come back in full. Abstaining is
  refusing to assert, not refusing to show the work.

## Degradation is not abstention

> Abstention is "the corpus does not cover this". Degradation is "I could not ask".

They are separate booleans on the answer and they must never be added together — the free tier's
quota errors would otherwise land in the abstention rate ADR-0004 measures, and on the target VM a
429 is the *expected* outcome rather than the rare one.

A provider failure produces 200, the complete `chunks`, and an answer with `degraded: true`,
`abstained: false`, empty `text` and a legible reason. Never a blank error page. These all degrade
identically, with different reasons: 429 `RESOURCE_EXHAUSTED`, 5xx, timeout, invalid JSON from a
truncated answer, and **valid JSON in the wrong shape** — a `claims` that arrived as an object, a
missing `abstained`, a claim whose text is whitespace. That last group is the one worth naming: it
looks like something a model chose to do, and it is not. It is a provider that changed behaviour,
and reading it as an abstention would file provider breakage under the single metric this whole
module exists to keep clean.

**A missing API key is not a degradation.** With no key there is no generator, so `answer` is `null`
and there is no `generate` span — nothing was attempted, and the project has already decided that a
stage which did not run is *absent* rather than present-and-zero. Manufacturing a degradation for a
call that never happened would be the mirror image of the zero-millisecond span, and just as untrue.

A third state exists and is neither of these: the endpoint re-checks every citation an answer
carries, and a `Generator` that publishes one resolving to nothing gets its answer refused with
`support: "rejected"` and `contract_violation` set, `degraded: false`, `abstained: false`. That is
this codebase failing to keep its own promise, and it is filed as such rather than blamed on the
model or the network. The chunks are still returned; they were never in question.

The `try/except` lives in the **endpoint**, not in the adapter, and wraps the provider call *only*.
The adapter is honest and raises; the service owns the policy. That keeps `Generator` testable as a
unit and the policy in one readable place. A bug in our own code around that call is a 500, not a
polite note blaming a provider that answered correctly. The visitor's `degradation_reason` names the
exception's type and stops there — the provider's raw message goes into the span, where an operator
reads it, and not onto the public wire.

## The interface, not the implementation

`Generator` is `generate(query, context, contract) -> Answer` and nothing else — a structural
`Protocol` with a `name`, shaped exactly like `Retriever` (design §7.1). The endpoint no more learns
that it is holding Gemini than it learns that retrieval is lexical. The tests prove it by running the
entire HTTP path against a generator that answers from memory.

Generation is optional at every level: no key means no generator, `"answer": null`, no `generate`
span, and a service that boots and serves normally — the same absence, in the response and in the
trace, for the same reason. The boot gate is the `corpus_hash` alone
([ADR-0002](adr/0002-database-as-derived-artifact.md)) — retrieval is the measurable layer and is not
held hostage to a hosted model's credentials.

```sh
pip install -e '.[gemini]'
export GEMINI_API_KEY=…        # GARAGE_GEMINI_API_KEY also works and wins if both are set
```

`google-genai` is an **optional extra**, imported inside `GeminiGenerator.__init__` rather than at
module import, which is what lets the whole suite run on a machine that has neither the package nor a
key. Constructing a generator opens nothing, exactly like constructing a retriever.

## The prompt contract is a runtime axis

`contract` is `cited` (default) or `free`, and the difference between them is **one system
instruction and nothing else** — which is what makes the comparison honest. `free` exists only so the
demo can show what the citation contract is buying; it is never what you get by leaving a field out,
and a test asserts that default, because a system whose central property is opt-in does not have that
property ([ADR-0005](adr/0005-build-time-vs-runtime-axes.md)). An unknown value is a 422, not a
fallback.

Under `free`, uncited claims are not marked unsupported — there is no contract to violate — and
`support` reads `unenforced`.

## The generation span

```jsonc
{
  "name": "generate", "parentSpanId": "3b71…", "durationMs": 942.6,
  "attributes": {
    "generation.provider": "gemini", "generation.model": "gemini-2.5-flash",
    "generation.contract": "cited", "generation.context.chunks": 5,
    "generation.tokens.input": 414, "generation.tokens.output": 38,
    "generation.tokens.total": 452,
    "generation.cost.usd_estimated": 0.0002192, "generation.cost.estimated": true,
    "generation.pricing.as_of": "2026-08-01",
    "generation.abstained": false, "generation.support": "supported",
    "generation.citations": 1, "generation.citations.invalid": 0,
    "generation.claims.unsupported": 0, "generation.contradictory": false,
    "generation.degraded": false
  },
  "children": []
}
```

### Cost

| Model | Input, USD / 1M tokens | Output, USD / 1M tokens |
| ----- | ---------------------- | ----------------------- |
| `gemini-2.5-flash` | 0.30 | 2.50 |
| `gemini-2.5-flash-lite` | 0.10 | 0.40 |

Read from the published price list on **2026-08-01**, and the date travels into every span as
`generation.pricing.as_of` so a stale figure is visible rather than silently believed. The number
exists to give an order of magnitude to a comparison *between configurations* — "the hybrid run costs
four times the lexical one" — not to reconcile with an invoice.

A **rejected** answer records its full cost — tokens, estimate, pricing date, and the count of
invalid citations — on the span **and on the wire**. The provider answered and charged for it; we
refused the result, and nobody refunded the tokens. Leaving it out would make a configuration that
reliably breaks the citation contract look like the cheap one. A **degraded** call records no cost at
all in either place, and the asymmetry between those two is the correct behaviour rather than an
inconsistency to tidy away: there, nothing came back and nothing was billed, so a zero would invent a
charge exactly as an omission above would hide one.

"And on the wire" is load-bearing and was missing for a while. `reject_unverifiable` set five fields
and let the billing fall to its defaults, so the span reported the tokens and the cost while the HTTP
response reported `cost_usd: null`, `0 / 0` and `invalid_citations: 0` for the same call — the
rejected state described as free, with zero invalid citations, in the one state whose cause is an
invalid citation. It survived because nothing read the `Answer` in that state until an interface did
(`docs/ui.md`), and because the test that guards the asymmetry only ever looked at the span.

Two changes make the drift structurally impossible rather than merely fixed. `app._answer` builds the
rejected `Answer` **first** and writes the span *from it*, so the two are one object read twice
instead of two assemblies of the same facts. And `verify_citations` collects every violation before
raising rather than raising on the first, because the served `invalid_citations` is a number a reader
sees: taken from a loop that exits on its first hit it would always be 1, whatever the truth. A
violation with no unresolvable citation in it — a claim marked supported with none at all — counts
zero, which is not a contradiction but the honest report of a different violation.

A model with no price on record yields `cost_usd: null` and `cost_estimated: false`, **never zero**.
A free-looking row in a cost comparison is a lie, and the trace is the product. The attribute name
says `usd_estimated` for the same reason.

The 2.5 family is on a retirement path announced for late 2026, so `DEFAULT_MODEL` is expected to
change; it is one constant, in one place, and the price table is beside it.

`thinking_config(thinking_budget=0)` is set: thinking tokens are billed as output and inflate cost
and latency without producing a word of prose, and the task here is quoting five paragraphs
accurately. Temperature is 0 so that two runs of the same Configuration over the same artifact
produce the same run record. The SDK's retry ladder is disabled and the timeout is short — four
backed-off attempts against an exhausted quota is a minute of a visitor staring at nothing, and a
demo that degrades in two seconds is better than one that hangs for sixty.

## What is not covered by tests

Stated plainly, because the gap is real:

- **`GeminiGenerator.generate` has no automated behavioural coverage in CI.** No test in `tests/`
  imports `google.genai` or touches the network; CI has no key and will not have one. What *is*
  covered without a network is everything the contract actually rests on — the numbering, the
  post-hoc validation, the cost arithmetic, the `usage_metadata → Answer` translation (extracted as a
  pure function taking a stand-in object for exactly this reason), and the endpoint's abstention and
  degradation policy against a fake generator. What is not covered is the SDK call itself: a change
  in the `generate_content` signature, in `response_schema` handling, or in the shape of
  `usage_metadata` would be caught by a human running the demo, not by `pytest`.
- **One live test exists and never runs unless you ask for it.**
  `test_generation.py::test_the_real_adapter_answers_with_citations_that_resolve` is marked `live`,
  and `addopts = "-m 'not live'"` excludes it from every default run. Skipping on the absence of a
  key would not have been a gate: this very document tells an operator to export `GEMINI_API_KEY`,
  so a developer following the instructions would make a paid API call by typing `pytest`. Spending
  money is an act — `pytest -m live` — and it additionally requires the key and the extra. It was run by hand
  against the real API on 2026-08-01 and passed: 414 input tokens, 38 output tokens, an estimated
  0.00022 USD, one claim with one citation that resolved, and an uncovered question produced a clean
  abstention.
- **Answer quality is not asserted anywhere**, and deliberately so. Whether an answer is *good* is
  what the two-layer evaluation exists to measure (ADR-0004); a unit test asserting a model's prose
  would be a change detector, not a test.
