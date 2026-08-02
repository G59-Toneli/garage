# The interface

The Glass Box, in its first form: a question answered twice, side by side, with everything that
produced each answer visible beside it — the retrieved chunks and their scores, the span tree with
its timings, the tier of every citation, and the cost.

It is hand-written HTML, CSS and ES modules in `src/garage/static/`, served by `StaticFiles` from
the same process and the same image that answers `/query` (ADR-0006). There is no Node, no bundler
and no `package.json`, and that is a decision rather than an omission: the requirement today is a
working demo, and a build step would be a second toolchain in a project whose whole claim is that it
reproduces from one checkout. The styling is deliberately plain — the visual language of the project
has not been decided, and committing to one here would only have to be undone.

React is expected eventually. The section below is what makes that cheap.

## The adaptation boundary

`static/adapt.js` exports exactly one entry point:

```js
toView(source) -> view
```

A *source* is a data origin. Two exist:

| source | input | produces |
| --- | --- | --- |
| `{source: "live", responses: [...]}` | the bodies of two `POST /query` calls | the comparison view |
| `{source: "record", baseline, record}` | `eval/baseline.json` and a run record | the metrics view |

Everything that renders — `static/render.js`, `static/evalscreen.js` — consumes only the returned
view object. **No component ever reads a server payload.** A component that did would be the thing
that has to be rewritten twice: once for issue #11, and again for React.

That is the whole architecture. It is deliberately thin: no state, no caching, no reactivity, no
fetching inside the adapter. Payload in, plain object out, synchronously. Components are functions
from that object to DOM, which is exactly the surface JSX replaces.

The adapter is also where "what is missing" is decided, and where it is *not* repaired. A null page,
a null cost, a `_rank` that never fired and an absent span all travel through as absences.
Formatting — how an absence looks — lives in `static/dom.js`. Keeping the two apart is what stops a
`null` from being helpfully turned into a `0` somewhere upstream of the screen.

### The three hand-written lists

Acceptance criterion six says nothing displayed is hard-coded. Three lists in this interface are
written by hand anyway, and each one is a *name the pipeline owns* rather than a number:

1. `KNOWN_STAGES = ["retrieve", "rerank", "generate"]` — the pipeline's own vocabulary of stages.
   It is the only way the panel can draw `rerank` as **absent** instead of simply not knowing it was
   ever supposed to exist.
2. `COMPONENT_LABELS` — Portuguese labels for known scoring signals, applied while iterating
   *generically* over whatever `components` actually contains, with the raw key as fallback. The day
   `hybrid` arrives with three signals the panel shows three signals.
3. `DEFAULT_COLUMNS = ["lexical", "dense"]` in `main.js` — which arm reads on the left, and a
   fallback when the build cannot be probed.

Every metric, score, duration, token count and cost on screen comes from a response body or from a
file read over HTTP.

## Two requests, no `/compare`

The interface issues two ordinary `POST /query` calls in parallel, one per strategy. There is no
`/compare` endpoint, and there should not be: "two columns" is a decision the interface makes, and
an endpoint shaped around it would be that decision leaking into the API. The acceptance criteria
never asked for comparable timings.

Three consequences, all handled rather than hidden:

- **Two unrelated `traceId`s.** Two `Tracer`s, no parent/child relation. Normal; both are shown.
- **`corpus_hash` is asserted in JavaScript.** `question` and `corpus_hash` are properties of the
  comparison, not of an arm, so they render once above both columns — the same discipline Run Record
  v2 enforces structurally by holding `provenance` above `arms`. Two independent HTTP calls cannot be
  forced into that shape by the server, so `renderComparison` refuses to draw at all when the two
  hashes differ. A side-by-side spanning two artifacts would show a difference in retrieval that is
  really a difference in the database.
- **Latency is not a result.** One connection per query with no pool, and `dense` embeds the query
  on the CPU. Every waterfall is labelled *tempo de parede, uma amostra, conexão fria — não é uma
  medição de desempenho*. The two columns do share one absolute millisecond scale, normalised
  against the larger root, because otherwise the difference disappears.

### The strategy list, and the one place prose is parsed

`app.query` rejects an unknown strategy with FastAPI's standard 422 envelope and puts the list of
served strategies in `msg` as `"this build serves lexical, dense"` — a human sentence, not a
structured field. `main.refreshStrategies` sends a deliberate invalid strategy at load and parses
that sentence with one regex, so a lexical-only build does not render a permanently failing second
column. It fails safe: any surprise leaves `DEFAULT_COLUMNS` standing.

This is a wart, named as one. The clean fix is a structured field on that error; it is a contract
change and was not made unilaterally. The visible cost today is one 422 in the browser console on
every page load.

## The five states

`answer` is not one component with a `variant`. It is five, tested **in this order**, which is the
only order that collapses nothing:

| # | test | meaning | look |
| --- | --- | --- | --- |
| 1 | `answer === null` | no generator configured — nothing ran, nothing failed | neutral, dashed frame, no alert colour |
| 2 | `answer.abstained` | the corpus does not cover this | quiet, labelled **correct behaviour**; adds "sem chamada ao modelo — custo zero" when `provider === null` |
| 3 | `answer.degraded` | the provider could not be asked | warning band; `exception.message` from the span folded behind `<details>` |
| 4 | `answer.contract_violation !== null` | it answered, it was billed, **we** refused it | error frame, and the full cost shown anyway |
| 5 | otherwise | prose by claims, with superscript citations | — |

Issue #8 spent its entire argument keeping 2, 3 and 4 apart. Abstention is *"the corpus does not
cover this"*; degradation is *"I could not ask"*; rejection is *"it answered, it was paid for, and we
refused it"*. Folding them into one banner would undo that work.

Two asymmetries the interface must **not** tidy:

- On a degradation the span carries no cost and no tokens, because the provider never answered.
  Filling zeros there would erase the distinction.
- On a contract violation the span carries the full cost. Hiding it would let the configuration that
  reliably breaks the citation contract show up as the cheap one.

`answer.text` is `""` and `claims` is `[]` in states 2, 3 and 4 alike, so `if (answer.text)` is never
the test for anything.

## Tier: a product requirement, four channels

Colour alone fails WCAG 1.4.1, and the information here is the difference between a factory manual
and someone's recollection of one on a forum (design §13). So the tier is carried four times over:

1. **Text**, always spelled out — `TIER A · manual`, `TIER B · comunidade`. Never a bare letter.
2. **Frame shape** — Tier A a solid 4px left border, Tier B a dashed one. Survives monochrome.
3. **Typography** — A semibold, B normal italic (and the chunk body italic too).
4. **Colour**, last, as reinforcement.

Inline citations are `<button>`s, not styled spans: focusable, announced with
`aria-label="citação 1, Tier A, manual de serviço, <título>, página 47"`, and they scroll the cited
chunk into view and flash it. The glyph itself carries the tier — `¹ᴬ`, `²ᴮ` — because mid-sentence
is where a reader actually looks. A claim with `supported: false` stays on screen, framed with a
dotted border and a `SEM SUPORTE` badge: hidden, or merely faded, it would hide the failure.

**The QA check this must keep passing:** under `filter: grayscale(1)` the two tiers are still
distinguishable. Channels 1–3 survive that. If colour is ever the only difference, the treatment has
regressed.

## Scores are not on one scale

`score` means different things per strategy and the panel says so rather than drawing them alike.
The unit is derived from the *component keys the response carried*, never from the strategy name —
a name switch would need a new case for `hybrid`, and could mislabel a unit.

- `cosine` present → unit **cosseno**, drawn on an absolute 0–1 axis with gridlines at 0.25/0.5/0.75.
  This is what makes "the nearest neighbour was still 0.31 away" legible. No floor is drawn, because
  `DenseRetriever` has none.
- `lexical` present → unit **RRF**, around 0.016 at the top and carrying no absolute meaning at all.
  Drawn relative to the column's own maximum and labelled as such.

`components` is iterated generically. A `_rank` of `null` renders **"não disparou"**, never `0` — the
"matched on trigram alone" case is the single most informative row in the panel. The array position
(`#3`) and the component rank are shown separately, because `rank()` ties: two adjacent rows may
legitimately report the same rank, which is a tie and not a bug.

## The waterfall

`tracer.tree()` gives `{traceId, spanId, parentSpanId, name, startTimeUnixNano, endTimeUnixNano,
durationMs, attributes, children}`. Depth is 2 at most and there are 1–3 spans, so there is no ruler,
no minimap, no zoom and no collapse — those would spend width on nothing.

Two rules are load-bearing:

- **A stage that did not run is absent, not zero.** `rerank` does not exist in this build at all;
  `generate` does not exist without a generator, nor on the zero-cost abstention. Those rows are
  drawn in secondary text with a dashed track and the label `não executado`, duration `—`. A zero
  millisecond bar would be the trace describing a pipeline it does not have.
- **Offsets come from a running sum of sibling `durationMs`, in `children` order — never from
  `startTimeUnixNano`.** Those are two different clocks: `time.time_ns` stamps the timestamps and
  `perf_counter_ns` measures durations (`tracing.Span`), so subtracting timestamps produces a number
  that does not reconcile with `durationMs`. The premise that makes the running sum correct is that
  stages are sequential by construction, and it must be revisited the first time two run
  concurrently.

`error: true` turns the bar into a diagonal hatch with a 2px border and adds a textual `ERRO` badge.
Red is layered on top of those; it is never the signal by itself. The timestamps arrive as **strings**
because a nanosecond value does not survive a JavaScript number, and the interface never does
arithmetic on them.

## The comparison, without forcing alignment

- **Scroll is independent, headers are sticky.** Synchronising the scrollbars was considered and
  rejected: one column routinely abstains with zero chunks while the other returns ten, and a synced
  scroll would pair a paragraph with several screens of nothing.
- **The highlight is synchronised instead.** Hovering or focusing a chunk highlights the same
  `chunk_id` in the other column, and when the twin is off screen a small indicator says where it is
  (`também na outra coluna, posição 7 ↓`).
- **Neither column is ever reordered to match the other.** The order *is* the information each column
  exists to show. Correspondence is stated in text on each card — `#3 · também em dense (#1)` or
  `#3 · só nesta coluna` — computed as set intersection and difference over served `chunk_id`s. That
  is the only thing this interface calculates, and it is arithmetic on identifiers, not statistics.
- Above the panels, an overlap band: `2 recuperados em lexical · 10 recuperados em dense · 2 em comum
  · 0 só em lexical · 8 só em dense`.

### The lexical floor, explained on screen

`LexicalRetriever` drops trigram matches below `WORD_SIMILARITY_FLOOR` and can therefore return
nothing at all, which is what makes the zero-cost abstention reachable. `DenseRetriever` has no floor
and always returns its k nearest vectors, however distant (`docs/retrieval.md`). So the screen
routinely shows an empty left column beside a right column answering at cosine 0.31.

That is not a bug and is not smoothed over. When one column has zero chunks and the other does not, a
persistent note between them explains the floor and tells the reader to check the cosines before
reading the full column as "the one that worked". A visitor will ask; answering it on screen is the
product.

## The metrics screen

`eval.html` reads the promoted baseline and the newest committed run record over three read-only
endpoints — `GET /eval/baseline`, `GET /eval/runs`, `GET /eval/runs/{run_id}` — which hand back the
bytes on disk unmodelled. Re-serialising them through Pydantic would be a second description of the
record format and therefore a place for it to drift from `evaluation.py`. `0.911765` and `0.071429`
reach the screen from `eval/baseline.json` or not at all.

**No error bars.** These metrics are deterministic: same questions, same artifact, same result. A
whisker on a number with no variance is an invented number, which is exactly what the acceptance
criterion forbids. What is drawn is the **tolerance corridor**, labelled as policy — `Baseline.tolerance`
is explicitly a policy number and not a statistical one.

## What issue #11 has to add

Issue #11 wants curated questions rendering with no model call. **The current Run Record does not
support that**, and the gap should be closed in the record rather than papered over in the interface:

- it is retrieval-only — no `answer`, no claims, no citations;
- no cost, no tokens, no provider;
- no trace, so no waterfall;
- no chunk text, by ADR-0003 — only `chunk_id`s and `expected_chunk_ids`.

What a record can serve statically today is exactly what `eval.html` already shows: the per-arm
metrics table and the ranked `chunk_id`s.

So the work in #11 is a **record source that produces the same shape `liveView` produces** — an arm
with `chunks`, an `answer` in one of the five states, and a `trace` — which means the record format
grows the fields above. When it does, the change lands in `adapt.recordView` and **no component
changes**. That is what the boundary was built for.

## Security

`chunk.text`, `answer.text`, `claim.text`, `doc_title`, `section` and `exception.message` are all
untrusted strings — from the corpus and from a language model — rendered on a page. Every node is
built with `createElement` and every string set with `textContent`. `innerHTML`, `outerHTML`,
`insertAdjacentHTML` and `document.write` appear nowhere in `static/*.js`, and
`tests/test_ui_contract.py` fails the build if one ever does. It strips comments before checking, so
the reason for the ban can still be written down beside it.

## The test that buys the type checking

`tests/test_ui_contract.py` asserts the **exact** key set of `QueryResponse`, `RetrievedChunk`,
`GeneratedAnswer`, `AnsweredClaim`, `CitedChunk` and every span node, over `httpx` against the test
app. Without it, renaming a field in Python leaves the whole suite green and breaks the demo
silently, in a browser, at runtime.

Exact rather than "contains", on purpose: a field *added* to the response and not shown on screen is
also a regression, because the claim of this interface is that it displays what the system produced.
