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

A *source* is a data origin. Three exist:

| source | input | produces |
| --- | --- | --- |
| `{source: "live", responses: [...]}` | the bodies of two `POST /query` calls | the comparison view |
| `{source: "record", baseline, record}` | `eval/baseline.json` and a run record | the metrics view |
| `{source: "showcase", record, item, chunks, missing}` | a showcase record and `GET /chunks` | the comparison view, precomputed |

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

### The hand-written lists

Acceptance criterion six says nothing displayed is hard-coded. Four lists in this interface are
written by hand anyway, and each one is a *name the pipeline owns* rather than a number (the fourth,
`SPREAD_LABELS`, arrived with the showcase and is described in that section):

No strategy name is written down either: the two selects start empty and disabled and are filled
from `GET /strategies`, so a lexical-only build never offers a visitor a `dense` option — not even
for the one round trip a hard-coded default used to leave it on screen.

1. `KNOWN_STAGES = ["retrieve", "rerank", "generate"]` — the pipeline's own vocabulary of stages.
   It is the only way the panel can draw `rerank` as **absent** instead of simply not knowing it was
   ever supposed to exist.
2. `COMPONENT_LABELS` — Portuguese labels for known scoring signals, applied while iterating
   *generically* over whatever `components` actually contains, with the raw key as fallback. The day
   `hybrid` arrives with three signals the panel shows three signals.
3. `TIER_LABELS` — the two tier letters and the words a reader sees for them.

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
- **One arm failing does not erase the other.** `Promise.allSettled`, not `Promise.all`: `all`
  rejects on the first failure, so a 429 on one column would take the other column's chunks, answer
  and trace with it. A failed arm becomes a view source like any other — empty `chunks`, null
  `trace`, a `failed` flag — and renders as a failure *column*, in place. It is excluded from the
  overlap band and from the `corpus_hash` check, because a column that never ran is not a column
  that retrieved nothing. It gets its own `state-unreachable` look, neutral and dashed, and
  deliberately not the red frame of state 4: "never completed" and "answered, billed, and refused by
  us" are semantic opposites and must not look alike side by side.
- **Latency is not a result.** One connection per query with no pool, and `dense` embeds the query
  on the CPU. Every waterfall is labelled *tempo de parede, uma amostra, conexão fria — não é uma
  medição de desempenho*. The two columns do share one absolute millisecond scale, normalised
  against the larger root, because otherwise the difference disappears.

### The strategy list

`GET /strategies` publishes what this build serves, in the order `available_retrievers` returns it,
with each strategy's `embedder` beside it (null under `lexical`, for the same reason `QueryResponse`
carries it). `main.refreshStrategies` reads that and fills the two selects.

The order is the pipeline's and is deliberately not sorted: it decides which arm the comparison opens
with, so alphabetising it would be a presentation decision taken in the wrong layer. The interface
does no re-ordering of its own.

**Every failure path here is visible.** The controls start disabled and are enabled only by a
successful read, so a silent failure would leave a permanently unusable page with a blank status
line — which is what the three quiet `return`s in this function produced once the HTML placeholders
were removed. All three now throw, and the page renders an "Interface indisponível" panel naming the
HTTP status, with a manual retry. Not an automatic poll: a page quietly retrying a service that is
down looks exactly like a page that is working, which is the failure the panel exists to prevent.

The invalid-strategy 422 also carries the list structurally now, in `detail[0].ctx.strategies`, again
in pipeline order. The human sentence in `msg` is unchanged — someone is already reading it in a
terminal — and the structured field was added beside it.

Both replace what was here before: the interface used to provoke a deliberate 422 at load and pull
the list out of `msg` with a regular expression. That parsed a human sentence *and* put a red error
in the console of every visitor loading a page that was working perfectly. An empty `<link
rel="icon" href="data:,">` removes the browser's automatic `/favicon.ico` 404 for the same reason:
the console of this demo is part of the demo, and a visitor who opens the developer tools to check
that nothing is being hidden should find it quiet.

## The five states

`answer` is not one component with a `variant`. It is five, tested **in this order**, which is the
only order that collapses nothing:

| # | test | meaning | look |
| --- | --- | --- | --- |
| 1 | `answer === null` | no generator configured — nothing ran, nothing failed | neutral, dashed frame, no alert colour |
| 2 | `answer.abstained` | the corpus does not cover this | quiet, labelled **correct behaviour**; adds "sem chamada ao modelo — custo zero" when `provider === null` |
| 3 | `answer.degraded` | the provider could not be asked | warning band; `exception.message` from the span folded behind `<details>` |
| 4 | `answer.contract_violation !== null` | it answered, it was billed, **we** refused it | error frame, the full cost shown anyway, clause list folded away |
| 5 | otherwise | prose by claims, with superscript citations | — |

Issue #8 spent its entire argument keeping 2, 3 and 4 apart. Abstention is *"the corpus does not
cover this"*; degradation is *"I could not ask"*; rejection is *"it answered, it was paid for, and we
refused it"*. Folding them into one banner would undo that work.

Two asymmetries the interface must **not** tidy, and they hold on the **wire** as well as on the
span:

- On a degradation there is no cost, no tokens and no pricing date, because the provider never
  answered. Filling zeros there would invent a charge.
- On a contract violation the full cost travels, along with the real count of invalid citations.
  Hiding it would let the configuration that reliably breaks the citation contract show up as the
  cheap one.

Building this screen is what found the defect that made those two identical. `reject_unverifiable`
filled five fields and let the billing fall to its defaults, so a rejection reached the browser as
`cost_usd: null`, `0 / 0` tokens and `invalid_citations: 0` — a cost panel byte for byte the same as a
degradation's, under a sentence saying the opposite, with a zero count of invalid citations in the
one state whose cause is an invalid citation. The span had been right the whole time; nothing read
the `Answer` in that state until an interface did.

Two things changed so it cannot recur. `app._answer` now builds the rejected `Answer` **first** and
writes the span *from it*, so the trace and the wire are one object read twice rather than two
assemblies of the same facts that can drift. And `verify_citations` gathers every violation instead
of raising on the first, because a count taken from a loop that exits on its first hit is always 1,
and that number is printed.

`answer.text` is `""` and `claims` is `[]` in states 2, 3 and 4 alike, so `if (answer.text)` is never
the test for anything.

State 4 splits its message the same way state 3 does: a short sentence in the page's language on the
wire, the raw clause-by-clause detail on the span and folded behind `<details>`. Two sentences are
possible and the count chooses between them, because one of them was a contradiction — a claim marked
supported with no citations at all used to be reported as "o gerador produziu citações que não
resolvem" above a clause saying there was no citation to resolve. At k=10 with every citation bad the
full list runs past 800 characters of near-identical English clauses, in front of the reader whose
useful number is already in the table below as `citações inválidas`.

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

The axis label says what the axis is and nothing more. It briefly read "absoluta, 0–1, sem piso",
which is not vocabulary but an assertion about `DenseRetriever`'s internals that this interface
cannot check and that nothing would invalidate if a floor were added. Whether there is a floor is
visible in the data — the lowest score in the column, on an absolute axis — and stated in prose in
the note between the columns, where it is an explanation rather than a label claiming to be a fact.

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
- **Rows are sorted back into `KNOWN_STAGES` order.** The absent ones used to be appended, which put
  `rerank` below `generate`: a reader takes vertical order in a waterfall as execution order, so a
  missing middle stage drawn last is a diagram of the wrong pipeline. Only depth 0 is reordered — a
  child belongs inside its parent's window — and a stage this list has never heard of keeps its
  arrival order after the ones it knows.
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

## The showcase screen

`showcase.html` renders curated questions with **zero model calls**. It is the third source, and the
one the adaptation boundary was built for. The format itself, the ADR-0003 argument behind it and
the verbatim gate are documented in `docs/showcase.md`; what follows is only what the interface does
with it.

Three reads, all of them free and local: `GET /showcase`, `GET /showcase/{id}`, and
`GET /chunks?ids=...`. There is no `POST /query` in `showcasescreen.js` and
`tests/test_showcase_contract.py` fails the build if one ever appears.

### The boundary paid off, and here is the receipt

`showcaseView` builds each arm by handing `armView` — the *same* function a live `POST /query` body
goes through — an object of the shape it already reads, then runs the result through the *same*
`comparison` a live pair goes through. `renderComparison` cannot tell a stored record from two live
HTTP calls and does not have to.

`liveView`'s cross-column half was extracted into `comparison(arms)` to make that literal rather than
approximate. Two implementations of the overlap arithmetic, the shared `corpus_hash` assertion, the
floor note and the millisecond scale would drift, and the drift would show up as a demo that
disagrees with the live page about which chunks the two strategies share.

### The absence branch in `chunkCard`, and why it is not a breach of the boundary

`render.chunkCard` gained a branch for a chunk whose paragraph this artifact does not hold. An
earlier draft of this document described that as "the one component that had to change, against the
boundary's promise". **That was wrong, and the wording mattered enough to correct rather than
soften: as written it would license a real breach later.**

The boundary's rule is that no component reads a *server payload*. `adapt.js` emits `textAbsent` as
a boolean — a datum, decided by the adapter, in the view object — and `render.js` consumes it. That
is precisely the contract, and it has abundant precedent in this very interface: `failed`, `ran`,
`fired`, `zeroCost` and the five-state enum are all the same shape. What the component added is copy
and structure, which is formatting, which is the component's job.

The division still holds and is worth restating: the adapter decides *what is missing*, `dom.js`
decides *how an absence is spelled*, and the component decides *what an absence looks like as
markup*. Rendering `null` through `el({text})` would produce an empty `<p>`, and a blank card is an
absence pretending to be a short chunk.

Dashed, quiet, and never red: nothing failed, and everything around it is still the product.

The same hydration path carries `section`, and for a reason that is not cosmetic. A `section` is set
by `chunking` from the source document's own heading, so it is the operator's prose and it does not
go into a committed record (ADR-0003) — it comes back from `GET /chunks` exactly as `text` does.
`doc_title` is the exception that stays in the record, because it is the manifest's catalogue entry
and is in git already.

### Saying "precomputed" on four channels

A visitor who reads these as live numbers has been misled, so the claim is carried the same way the
tier is (see above), and for the same reason — colour alone fails WCAG 1.4.1 and fails in print:

1. **Text** — a banner naming the `showcase_id`, the scope, the date the samples were drawn, the
   provider, the model, the temperature, n, and both verbatim-gate readings. When the record was
   built from a dirty tree the banner says so *inline beside the id*: `showcase_id` has `run_id`'s
   format and therefore appears to make `run_id`'s promise, and a sha that does not identify the
   code which produced the numbers has to be marked where the sha is read, not in a footnote.
2. **A stamp on every panel of numbers**, each carrying the `showcase_id` **and which draw it is** —
   `PRÉ-COMPUTADO · amostra 2 de 3 · 20260802T…`. Beside every *group* of numbers rather than every
   numeral, which would be unreadable, and a screenshot of one panel out of context still says both
   where it came from and that it is one sample of several.

   The draw qualifier used to live only in the spread panel at the bottom of the column, several
   hundred pixels below the cost figures it qualified. That does not break ADR-0004's letter — the
   file holds no scalar and the spread is right there — but a reader who never scrolls has read a
   point value off the screen, which is the thing ADR-0004 exists to prevent. The spread panel's own
   stamp reads `todas as N amostras`, because it is the one panel whose numbers are not a draw.
3. **Texture, not tone** — a repeating diagonal hatch behind the whole region, plus a 2px border so
   the edge of "what is precomputed" is a line and not a gradient. A lighter shade of grey says
   nothing in monochrome.
4. **`aria-label`** on the region and on each stamp, spelling it out for a reader who gets none of
   the first three.

The stamps are applied by walking the rendered DOM for `.panel`, `.state` and `[data-strategy]` —
class names those components already publish — rather than by threading a flag through `render.js`.
That is the trade: the alternative is every component learning about a screen it has nothing to do
with.

### The strip plot

n draws are drawn as n marks on a line, and that is all. **No error bar, no mean, no standard
deviation, no box.** Ten samples do not support a sigma, and the record deliberately holds no scalar
for any stochastic quantity precisely so that this file *cannot* render one (ADR-0004) — the same
argument the metrics screen makes about the tolerance corridor, one layer further.

What is printed beside the marks is the minimum and the maximum, both observed, and a count of how
many distinct values appeared. That count is the most useful thing on the panel: at temperature 0 it
is frequently 1, and "this number did not move across ten calls" is what a reader actually wants to
know. When it is **zero** the row says "sem valor — não houve chamada", which is a different fact
from ten identical measurements and used to render as one.

Marks are translucent so overlapping draws read darker, focusable, and individually announced —
a plot a screen reader cannot enumerate would put the draws out of reach of exactly the reader who
needs the numbers rather than the picture. The draw actually on screen in the column beside it is
marked taller, solid and accented: three channels, colour last.

`displayed_sample` is never "the best one" and the rule that chose it is printed beside the plot,
out of the record.

### The fourth hand-written list

`SPREAD_LABELS` joins the three above. Same rule: the keys are `showcase.SPREAD_METRICS`, decided in
Python, iterated generically with the raw key as fallback, so a metric added there shows up
unlabelled rather than dropped.

## Where an answer came from, and why it is a body field

The public deployment (issue #11) made `POST /query` a cascade: an answer is measured now, copied out
of a cache, or read off a file somebody committed in April, and those are three different claims
about how much a number is worth. `QueryResponse` carries `origin` and `origin_detail`; `adapt.js`
turns them into one flat `arm.origin` shape with nulls where an origin has nothing to say; and
`render.originBand` draws one chip per arm above the columns.

**Per arm and not per comparison**, because the two columns are two independent `POST /query` calls
and can genuinely differ — the left one a cache hit, the right one freshly generated. A single band
claiming one origin for both would be wrong half the time.

**A body field and not an HTTP header.** A header would be cheaper and is the wrong shape: `toView`
receives payloads, not responses, so a fact the screen must render cannot live somewhere this file
cannot see. Putting it in a header would punch through the boundary this whole document is about, on
its first day. The cost is one key on `QueryResponse` and a deliberate failure in
`tests/test_ui_contract.py`, which is what that test is for.

Each chip differs in **border style** as well as tone — solid, dashed, dotted — so a screenshot in
monochrome and a colour-blind reader both keep the distinction. The sentences are as specific as the
payload allows: a cached answer prints the time it was generated, because "resposta em cache" alone
invites the reader to assume it is minutes old, and a precomputed one prints the `showcase_id` and
"amostra k de n", because `answer.tokens_out` below it is one draw of n and ADR-0004 forbids a point
estimate without its distribution.

### Still five states, not six

A spent generation budget reaches the screen as `degraded`, the state issue #8 argued into existence,
with a different `degradation_reason`. Adding a sixth would undo that work to express a *cause*
rather than a kind: "I could not ask" already covers "I could not ask because the day's budget is
gone".

What differs is the heading, and `render.degradation` chooses it from `answer.cause`, which
`adapt.answerView` derives from `origin` — never from matching the Portuguese in `reason`, which
would make translating the site a way to break the interface. "O provedor não respondeu" is *false*
when the provider was never called, and a heading that is false is worse than one that is vague.

## The fixture banner

While `provenance.corpus_id === "fixture"`, every screen carries a permanent, non-dismissible banner
saying the documents were invented for this project. `banner.js` mounts it from `GET /provenance`,
and all three entry modules call it.

It is keyed on the **field**, not on a constant, so it disappears by itself the day issue #10
catalogues real material — impossible to forget to remove, and impossible to remove early. There is
no dismiss button and there must never be one: it would make the statement true for the first thirty
seconds of a session, and the reader most likely to dismiss it is the reader most likely to quote a
number off the page.

A page that *cannot* read `/provenance` says so, in a distinct and more alarming frame. The
conservative direction: not knowing whether the material is invented must not look like having found
out that it is not.

## The re-run button

On the showcase screen, one panel per arm, and it is the only thing there that is **not** stamped
`PRÉ-COMPUTADO` — it is the one measurement taken for the visitor. It sends `rerun: true`, which the
endpoint honours by skipping both the record and the cache: a re-run answered out of either is a
re-run of nothing, and the point is that a published number is a claim to falsify (ADR-0004).

`rerun.js` is a set of refusals more than a feature, and a reviewer should treat the disappearance of
any of them as a defect:

| level | claim it may make | why not more |
| --- | --- | --- |
| retrieval order | "identical to the recorded ranking", or the finding, in full | deterministic over a fixed artifact, so it *must* match; ADR-0008 measured zero top-10 differences between x86-64 and aarch64 with a margin of only 1.13× against the analytical bound, which is thin enough to watch |
| generated quantities | "inside" / "outside the observed range", with n | one draw on aarch64 against n draws on x86-64; provider variance dwarfs any architecture effect, and n=1 against a distribution supports neither "better", "worse", nor a percentage |
| latency | nothing at all | ARM against x86, cold connection, one sample — and the waterfall on the same page is already labelled "não é uma medição de desempenho" |

`generate_ms` is excluded from `adapt.COMPARABLE_METRICS` by name, and the exclusion is *stated* on
screen rather than done quietly: leaving latency out silently would let a reader assume it was
compared and found equal, which is a stronger claim than refusing to compare it.

The plot is the same strip plot, widened to contain the live value so an outlying result is drawn
outside the recorded range rather than clamped to the edge — clamping would render the single most
interesting outcome as the least interesting one. No error bar, no mean, no standard deviation. A
permanent footnote under every re-run names the whole comparison for what it is.

## Geometry goes through the CSSOM, never the `style` attribute

`dom.el` takes `style: {left: 42}` — property names to **numbers**, written with
`element.style.setProperty` and a percent suffix. The inline `style` attribute is banned, and
`tests/test_ui_contract.py` fails the build on one, exactly as it does for `innerHTML`.

This is a deployment fact rather than taste. `deploy/Caddyfile` serves `style-src 'self'`, which
blocks the `style` attribute outright while leaving the CSSOM path alone — a value assigned by this
code is not a stylesheet the page received from anywhere. The first version of the origin band
shipped with the attribute, and in production every bar in the waterfall, every tick on a score axis
and every mark on a strip plot silently failed to position: a column of empty tracks, eighty-five CSP
violations in a console this project promises will be quiet, and nothing a visitor could see saying
anything was wrong. It was found in a browser against the real overlay, and it is invisible in
development because there is no Caddy there and therefore no policy.

Numbers only, enforced in the helper, is the second half. Nothing here ever interpolates a served
string into CSS, and a non-finite value is dropped rather than written — so a missing measurement
leaves an element unpositioned instead of parked at the left edge pretending to be zero.

## Two things a later reader should not have to discover alone

**`REPO_ROOT` is `Path(__file__).parents[2]`.** `evaluation.py` resolves `eval/` relative to the
package, which is correct for `pip install -e .` and for the image (`/app/src/garage` → `/app`), and
wrong for a non-editable wheel, where `eval/` would sit outside it and the three `GET /eval/*`
endpoints would 404. Nothing in this project installs that way today, and the failure is visible
rather than silent — the metrics screen shows the HTTP error — so it is recorded here rather than
engineered around. Whoever first builds a real wheel will need to decide whether the evaluation data
is package data or deployment data.

**`adapt.js` has real logic and no automated test.** The five-state ordering, the overlap set
arithmetic, the cumulative waterfall offsets and the scoring-unit inference all live there and are
verified by hand in a browser. Adding Vitest would mean Node, which the stack decision excludes. The
honest path when this stops being acceptable is a headless-browser test driven from `pytest`: the
service already serves the modules, so a test can load a page, `import` the module and assert on the
view object without any JavaScript toolchain entering the repository.

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

Issue #11 is the worked example. Adding `origin` and `origin_detail` broke this test, which is the
test doing its job — the failure is the moment somebody has to decide whether the new field will be
rendered. `origin_detail` is a bare dict on the Pydantic model, so its shape is asserted here too,
as three per-origin key sets rather than one union: the fields genuinely do not overlap, and a flat
model would publish a null `showcase_id` on every live answer, which is how a screen ends up drawing
"showcase: —" beside a number that was measured a second ago.
