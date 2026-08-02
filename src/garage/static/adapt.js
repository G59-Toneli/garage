// The adaptation boundary. This file is the architectural item of issue #9.
//
// Nothing that renders is allowed to read a server payload. `toView` takes a *source* — a live pair
// of `QueryResponse` bodies today, a stored Run Record tomorrow (issue #11) — and returns a plain
// view object. Every component in `render.js` and `evalscreen.js` consumes only that object.
//
// Two things are bought by the indirection, and both are the reason it exists rather than a
// justification found afterwards:
//
//   1. **Issue #11** wants curated questions rendered with no model call. That is a second `source`
//      branch here and zero component changes. What the current Run Record cannot yet supply is
//      spelled out in `docs/ui.md`: it is retrieval-only, with no answer, no citations, no cost and
//      no trace, and by ADR-0003 it does not carry chunk text either. So today the record branch
//      produces the metrics view and nothing more, and it says so instead of faking the rest.
//   2. **React**, eventually. The components are functions from a view object to DOM. Replacing
//      them means writing functions from the same view object to JSX. The dumber this file is, the
//      cheaper that is — which is why it has no state, no fetching, no caching and no clever
//      memoisation. It is `payload in, plain object out`, synchronously.
//
// Formatting is deliberately *not* done here. This file decides what is present, absent, or
// unmeasured; `dom.js` decides how absent looks. Collapsing the two is how a `null` becomes a `0`.

// ---------------------------------------------------------------------------------------------
// Vocabulary. Three hand-written lists live in this interface and no more, and each is a name the
// pipeline owns rather than a number the interface invented (acceptance criterion six forbids the
// second, not the first).

// The stages this pipeline knows about. Written out because "which stages exist" is the pipeline's
// vocabulary, and it is the only way the interface can render `rerank` as **absent** rather than
// simply not knowing it was ever supposed to be there. `app._answer` documents the same set.
export const KNOWN_STAGES = ["retrieve", "rerank", "generate"];

// How the score panel names each signal. Iterated *generically* over whatever `components` actually
// contains, with the key itself as the fallback label — so the day `hybrid` arrives with three
// signals the panel shows three signals, unlabelled at worst, instead of showing two and silently
// dropping the one that mattered.
const COMPONENT_LABELS = {
  lexical: "full text · ts_rank_cd",
  trigram: "trigrama · word_similarity",
  lexical_rank: "posição no full text",
  trigram_rank: "posição no trigrama",
  cosine: "cosseno",
  dense_rank: "posição no cosseno",
};

// A component key ending in `_rank` is an ordering, not a magnitude; nulls in one mean "this signal
// did not fire", which is a different fact from a low score and is displayed as such.
const RANK_KEY = /_rank$/;

const TIER_LABELS = {
  A: { label: "TIER A · manual", short: "manual de serviço" },
  B: { label: "TIER B · comunidade", short: "relato de comunidade" },
};

// The fourth hand-written list, added by the showcase, and a name the pipeline owns exactly like the
// three above: these keys are `showcase.SPREAD_METRICS`, decided in Python. Iterated generically
// with the raw key as fallback, so a metric added there shows up here unlabelled rather than
// dropped — the same rule `COMPONENT_LABELS` follows and for the same reason.
const SPREAD_LABELS = {
  tokens_in: "tokens de entrada",
  tokens_out: "tokens de saída",
  cost_usd: "custo (US$)",
  claims: "afirmações",
  citations: "citações",
  invalid_citations: "citações inválidas",
  unsupported_claims: "afirmações sem suporte",
  generate_ms: "latência da geração (ms)",
};

// ---------------------------------------------------------------------------------------------

export function toView(source) {
  if (source.source === "live") return liveView(source);
  if (source.source === "record") return recordView(source);
  if (source.source === "showcase") return showcaseView(source);
  throw new Error(`fonte desconhecida: ${source.source}`);
}

// --- live ------------------------------------------------------------------------------------

function liveView({ responses }) {
  const arms = responses.map((response) => (response.failed ? failedArm(response) : armView(response)));
  return { ...comparison(arms), source: "live" };
}

// Everything two columns mean *together*, computed from arm views and from nothing else.
//
// Extracted out of `liveView` when the showcase source arrived, and the extraction is the point
// rather than tidiness: a precomputed comparison and a live one are the same comparison, so the
// overlap arithmetic, the shared `corpus_hash` assertion, the floor note and the millisecond scale
// must be one implementation. Two would drift, and the divergence would appear as a demo that
// disagrees with the live page about which chunks the two strategies share.
function comparison(arms) {
  // A failed arm is excluded from every cross-column computation rather than counted as empty. It
  // did not retrieve nothing — it did not answer at all, and "0 em comum" against a column that
  // never ran would be a set operation over a set that does not exist.
  const answered = arms.filter((arm) => !arm.failed);

  // `question` and `corpus_hash` are properties of the *comparison*, not of an arm, so they are
  // lifted above the arms here — the same discipline Run Record v2 enforces structurally by putting
  // `provenance` above `arms`. And because two independent `POST /query` calls cannot be forced into
  // that shape by the server, the interface asserts it: two arms standing on different artifacts is
  // not a comparison, and rendering it anyway would publish a difference that is not there.
  const hashes = [...new Set(answered.map((arm) => arm.corpusHash))];
  // One answered arm agrees with itself, trivially. Zero answered arms cannot disagree either, and
  // the screen has a failure to show instead.
  const agrees = hashes.length <= 1;

  const sets = answered.map((arm) => new Set(arm.chunks.map((chunk) => chunk.chunkId)));
  const common = sets.length ? [...sets[0]].filter((id) => sets.every((set) => set.has(id))) : [];

  // Set membership across the two columns, computed here and nowhere else. It is the only thing
  // this interface calculates rather than displays, and it is deliberately arithmetic on served
  // identifiers — union and difference of `chunk_id`s — not a statistic about them.
  for (const [index, arm] of answered.entries()) {
    const others = answered.filter((_, other) => other !== index);
    for (const chunk of arm.chunks) {
      chunk.alsoIn = others
        .map((other) => {
          const match = other.chunks.find((candidate) => candidate.chunkId === chunk.chunkId);
          return match ? { strategy: other.strategy, position: match.position } : null;
        })
        .filter(Boolean);
      chunk.onlyHere = chunk.alsoIn.length === 0;
    }
  }

  // One shared millisecond scale across both waterfalls. Normalising each column against its own
  // root would draw two identical-looking traces for a 12 ms query and a 900 ms one, which is
  // exactly the difference the panel exists to show.
  const traceScaleMs = Math.max(
    1,
    ...arms.map((arm) => (arm.trace && arm.trace.totalMs) || 0)
  );

  // The lexical floor asymmetry, stated on screen the moment it happens rather than left for a
  // visitor to misread as a broken column. `LexicalRetriever` drops trigram matches below
  // `WORD_SIMILARITY_FLOOR` and can therefore return nothing at all; `DenseRetriever` has no floor
  // and always returns its k nearest vectors, however distant (`docs/retrieval.md`). One empty
  // column beside a full one is the designed behaviour of two different retrievers, not a fault.
  const empty = answered.filter((arm) => arm.chunks.length === 0);
  const full = answered.filter((arm) => arm.chunks.length > 0);
  const floorNote =
    empty.length > 0 && full.length > 0
      ? {
          empty: empty.map((arm) => arm.strategy),
          full: full.map((arm) => arm.strategy),
        }
      : null;

  return {
    shared: {
      question: answered.length ? answered[0].question : null,
      corpusHash: agrees ? hashes[0] ?? null : null,
      hashes,
      agrees,
    },
    arms,
    overlap: answered.length
      ? {
          perArm: answered.map((arm) => ({ strategy: arm.strategy, count: arm.chunks.length })),
          common: common.length,
          only: answered.map((arm, index) => ({
            strategy: arm.strategy,
            count: arm.chunks.length - common.length,
            total: sets[index].size,
          })),
        }
      : null,
    floorNote,
    traceScaleMs,
  };
}

function failedArm({ strategy, error }) {
  // A column that never answered, shaped like every other arm so that nothing downstream has to
  // branch on its absence. Empty `chunks` and a null `trace` are the honest values: this arm has no
  // ranking and no spans, and inventing either would be the interface reporting a pipeline run that
  // did not happen. `failed` is what tells the renderer to draw a failure rather than a result.
  return {
    failed: true,
    error,
    strategy,
    question: null,
    corpusHash: null,
    embedder: null,
    k: null,
    tiers: [],
    contract: null,
    scoring: { unit: null, axis: null, max: 1, ticks: [] },
    chunks: [],
    answer: { state: "disabled", cost: null },
    trace: null,
  };
}

function armView(body) {
  const scoring = scoringOf(body.chunks);
  const trace = traceView(body.trace);
  const chunks = body.chunks.map((chunk, index) => chunkView(chunk, index, scoring));
  return {
    question: body.question,
    corpusHash: body.corpus_hash,
    strategy: body.strategy,
    // Null under `lexical`, and rendered as "sem índice denso" rather than blank: the absence is
    // itself the identity of that arm (ADR-0005).
    embedder: body.embedder,
    k: body.k,
    tiers: body.tiers,
    contract: body.contract,
    scoring,
    chunks,
    answer: answerView(body.answer, trace),
    trace,
  };
}

function scoringOf(chunks) {
  // Derived from the *component keys the response actually carried*, never from the strategy name.
  // A strategy-name switch would need a new case for `hybrid`; this needs none, and more to the
  // point it cannot mislabel a unit, because the unit and the keys come from the same payload.
  const keys = new Set(chunks.flatMap((chunk) => Object.keys(chunk.components || {})));
  const scores = chunks.map((chunk) => chunk.score);
  const max = scores.length ? Math.max(...scores) : 1;

  if (keys.has("cosine") && !keys.has("lexical")) {
    // Cosine lives in 0..1, so the bar is drawn on that absolute axis with gridlines. This is what
    // makes "the nearest neighbour was still far away" visible — a dense arm that answers a question
    // the corpus does not cover returns its k nearest anyway, and only an absolute axis shows that
    // the best of them sits at 0.31. Normalising to the column maximum would draw that arm's top
    // result as a full bar and hide the entire finding. No floor is drawn, because there is none.
    // The axis label states the axis and nothing else. It used to add "sem piso", which is not
    // vocabulary but an assertion about `DenseRetriever`'s internals that this file cannot check and
    // that nothing would invalidate if a floor were added tomorrow. The floor — or its absence — is
    // visible in the data instead: the lowest score in the column, on an absolute axis.
    return { unit: "cosseno", axis: "absoluta, 0–1", max: 1, ticks: [0.25, 0.5, 0.75] };
  }
  if (keys.has("lexical")) {
    // Reciprocal rank fusion, so the score is around 0.016 at the top and carries no absolute
    // meaning at all: it is a fused *ordering*. Drawn relative to this column's own maximum and
    // labelled as such — and never on the same axis as the column beside it, which is a cosine.
    return { unit: "RRF", axis: "relativa ao topo desta coluna", max: max || 1, ticks: [] };
  }
  return { unit: null, axis: "relativa ao topo desta coluna", max: max || 1, ticks: [] };
}

function chunkView(chunk, index, scoring) {
  const tier = TIER_LABELS[chunk.tier] || { label: `TIER ${chunk.tier}`, short: chunk.tier };
  return {
    // The position in the array *is* the ranking. Kept separate from any `_rank` component below,
    // because `rank()` ties: two adjacent rows may legitimately report the same component rank at
    // different positions, and showing one number for both would turn a tie into a bug report.
    position: index + 1,
    chunkId: chunk.chunk_id,
    docId: chunk.doc_id,
    docTitle: chunk.doc_title,
    kind: chunk.kind,
    tier: chunk.tier,
    tierLabel: tier.label,
    tierShort: tier.short,
    // Null is legitimate — plenty of documents genuinely have no page — and must never render as
    // `p. null`.
    page: chunk.page,
    section: chunk.section,
    text: chunk.text ?? null,
    // Null text is a state only the showcase can reach, and it is a *designed* one. A showcase
    // record commits `chunk_id`s and never a word of the material (ADR-0003), so a clone without
    // the operator's Corpus hydrates nothing and this chunk has an identity, a rank, a score and a
    // tier — everything except the paragraph. Flagged rather than left as an empty string, because
    // an empty string renders as a blank card and a blank card is an absence pretending not to be
    // one. The adapter says what is missing; `render.chunkCard` says how missing looks.
    textAbsent: chunk.text === null || chunk.text === undefined,
    score: chunk.score,
    scoreFraction: Math.max(0, Math.min(1, chunk.score / (scoring.max || 1))),
    components: Object.entries(chunk.components || {}).map(([key, value]) => ({
      key,
      label: COMPONENT_LABELS[key] || key,
      value,
      isRank: RANK_KEY.test(key),
      // Null in a `_rank` means the signal never fired for this chunk — the "matched on trigram
      // alone" case, which is the single most informative row in the panel. It is not a zero.
      fired: value !== null && value !== undefined,
    })),
    alsoIn: [],
    onlyHere: false,
  };
}

// --- the five states ---------------------------------------------------------------------------
//
// Tested in this order, which is the only order that collapses nothing. They are five distinct
// things, not one component with a `variant`: abstention is "the corpus does not cover this",
// degradation is "I could not ask", rejection is "it answered, it was billed, and we refused it".
// Issue #8 spent its whole argument keeping those three apart; flattening them here would undo it.

function answerView(answer, trace) {
  if (answer === null || answer === undefined) {
    // Nothing failed. No generator is configured, so no stage ran and no span exists — the same
    // absence the trace already expresses. Neutral, never an error.
    return { state: "disabled", cost: null };
  }

  const cost = costView(answer);
  const base = { cost, support: answer.support, contract: answer.contract };

  if (answer.abstained) {
    return {
      ...base,
      state: "abstained",
      reason: answer.abstention_reason,
      // `provider === null` means the model was never called at all: the retriever came back empty
      // and `app._answer` abstained without asking. Worth saying out loud, because it is the
      // cheapest correct behaviour the system has.
      zeroCost: answer.provider === null,
    };
  }
  if (answer.degraded) {
    return {
      ...base,
      state: "degraded",
      // The exception *type* is all the HTTP response carries, on purpose. The raw provider message
      // lives on the span and is shown folded away.
      reason: answer.degradation_reason,
      detail: spanAttribute(trace, "generate", "exception.message"),
    };
  }
  if (answer.contract_violation !== null && answer.contract_violation !== undefined) {
    return {
      ...base,
      state: "rejected",
      violation: answer.contract_violation,
      // The clause-by-clause list, folded away exactly as the provider's raw message is on a
      // degradation. At k=10 with every citation bad it runs to ten near-identical English clauses
      // and 800-odd characters, in front of a reader whose useful number — how many were invalid —
      // is already in the table below. It is evidence, not prose, and it reads like evidence.
      detail: spanAttribute(trace, "generate", "exception.message"),
    };
  }
  return {
    ...base,
    state: "answered",
    text: answer.text,
    claims: (answer.claims || []).map((claim) => ({
      text: claim.text,
      supported: claim.supported,
      citations: claim.citations.map((citation) => ({
        index: citation.index,
        chunkId: citation.chunk_id,
      })),
    })),
  };
}

function costView(answer) {
  return {
    // Both null on the zero-cost abstention. Rendering a provider name here when the payload says
    // null would be the interface inventing a call that never happened.
    provider: answer.provider,
    model: answer.model,
    tokensIn: answer.tokens_in,
    tokensOut: answer.tokens_out,
    // Null is not zero: it is "no published price" or "no call". The formatter renders a dash.
    costUsd: answer.cost_usd,
    estimated: answer.cost_estimated,
    // Shown next to the figure, always. A cost from a stale price table that does not say when it
    // was priced is a number asking to be believed.
    pricingAsOf: answer.pricing_as_of,
    // The contract health panel, displayed **even when every number is zero** — zeros here are the
    // result, not the absence of one.
    invalidCitations: answer.invalid_citations,
    unsupportedClaims: answer.unsupported_claims,
    contradictory: answer.contradictory,
  };
}

// --- the trace -----------------------------------------------------------------------------------

function traceView(trace) {
  if (!trace) return null;
  const rows = [];
  // Offsets come from a running sum of sibling `durationMs`, in the order of the `children` array,
  // and deliberately **not** from `startTimeUnixNano`. Two different clocks produce those two
  // fields: `time.time_ns` stamps the timestamps and `perf_counter_ns` measures the durations
  // (`tracing.Span`), so subtracting timestamps yields a number that does not reconcile with
  // `durationMs`. The premise that makes the running sum correct is that stages are sequential by
  // construction — `app.query` opens each one after the previous has closed — and it must be
  // revisited the first time a stage runs concurrently with another.
  walk(trace.children || [], 0, 0, rows);

  const seen = new Set(rows.map((row) => row.name));
  for (const stage of KNOWN_STAGES) {
    if (seen.has(stage)) continue;
    // Absent, not zero. `rerank` does not exist in this build at all; `generate` does not exist when
    // no generator is configured, nor on the zero-cost abstention. A bar of length zero would be the
    // trace claiming an instantaneous stage instead of an unexecuted one.
    rows.push({
      name: stage,
      ran: false,
      depth: 0,
      durationMs: null,
      offsetMs: null,
      error: false,
      attributes: [],
    });
  }

  // Sorted back into pipeline order, because appending the absent stages left `rerank` *below*
  // `generate` and drew a pipeline this system does not have. A reader takes vertical order in a
  // waterfall as execution order — that is what a waterfall is — so a missing middle stage shown
  // last is a diagram of the wrong pipeline. It looked right in the zero-cost abstention only by
  // accident, because there the two absent stages happen to be the last two.
  //
  // Stable, and only among top-level rows: a stage the pipeline has never heard of keeps its arrival
  // order after the ones it knows, and nested children stay attached under their parent.
  sortByPipelineOrder(rows);

  return {
    traceId: trace.traceId,
    name: trace.name,
    totalMs: trace.durationMs,
    error: trace.attributes && trace.attributes.error === true,
    attributes: attributeList(trace.attributes),
    rows,
  };
}

function sortByPipelineOrder(rows) {
  const position = (row) => {
    const known = KNOWN_STAGES.indexOf(row.name);
    return known === -1 ? KNOWN_STAGES.length : known;
  };
  // Only depth 0 is reordered. Depth is 2 at most today and a child belongs to its parent's window,
  // so hoisting one into the top-level ordering would move it out of the span that contains it.
  const top = rows.filter((row) => row.depth === 0);
  if (top.length !== rows.length) return;
  rows.sort((left, right) => position(left) - position(right));
}

function walk(children, depth, start, rows) {
  let offset = start;
  for (const child of children) {
    const duration = child.durationMs === null || child.durationMs === undefined ? 0 : child.durationMs;
    rows.push({
      name: child.name,
      ran: true,
      depth,
      durationMs: child.durationMs,
      offsetMs: offset,
      error: Boolean(child.attributes && child.attributes.error),
      attributes: attributeList(child.attributes),
    });
    // Depth is 2 at most today. Children of a child are laid out inside their parent's window, which
    // is the same running-sum premise one level down.
    walk(child.children || [], depth + 1, offset, rows);
    offset += duration;
  }
}

function attributeList(attributes) {
  return Object.entries(attributes || {}).map(([key, value]) => ({ key, value }));
}

function spanAttribute(trace, name, key) {
  if (!trace) return null;
  const row = trace.rows.find((candidate) => candidate.name === name && candidate.ran);
  if (!row) return null;
  const found = row.attributes.find((attribute) => attribute.key === key);
  return found ? found.value : null;
}

// --- the record source ---------------------------------------------------------------------------

function recordView({ baseline, record }) {
  // Everything below is read out of `eval/baseline.json` and `eval/runs/*.json`. Not one figure on
  // that screen is written in this repository's JavaScript, which is the whole of acceptance
  // criterion six and the reason the numbers arrive over HTTP rather than in a constant.
  const measuredByArm = new Map(
    (record ? record.arms : []).map((arm) => [armKey(arm.configuration), arm])
  );

  return {
    source: "record",
    promotedRunId: baseline.run_id,
    measuredRunId: record ? record.run_id : null,
    sampleCount: baseline.sample_count,
    factsSha256: baseline.facts_sha256,
    // A *policy* number, explicitly not a statistical one (`evaluation.Baseline`), and labelled as
    // policy wherever it is drawn. Nothing on this screen gets an error bar: these metrics are
    // deterministic and have no variance, so a whisker on one would be an invented number — which is
    // precisely what the acceptance criterion forbids.
    tolerance: baseline.tolerance,
    noiseFloor: baseline.noise_floor,
    provenance: record ? attributeList(record.provenance) : [],
    startedAt: record ? record.started_at : null,
    arms: baseline.arms.map((arm) => {
      const measured = measuredByArm.get(armKey(arm.configuration));
      const gated = new Set(arm.gated_metrics || []);
      const names = [...new Set([...Object.keys(arm.metrics), ...Object.keys(measured ? measured.metrics : {})])].sort();
      return {
        strategy: arm.configuration.strategy,
        embedder: arm.configuration.embedder,
        k: arm.configuration.k,
        reranker: arm.configuration.reranker,
        tiers: arm.configuration.tiers,
        metrics: names.map((name) => {
          const promoted = arm.metrics[name] ?? null;
          const current = measured ? measured.metrics[name] ?? null : null;
          return {
            name,
            gated: gated.has(name),
            promoted,
            measured: current,
            delta: promoted === null || current === null ? null : current - promoted,
            // The corridor a gated metric is allowed to fall into before the build fails. Drawn as
            // a band, labelled as policy.
            floor: promoted === null || !gated.has(name) ? null : promoted - baseline.tolerance,
          };
        }),
      };
    }),
  };
}

function armKey(configuration) {
  return `${configuration.strategy}::${configuration.embedder ?? ""}::${configuration.k}`;
}

// --- the showcase source ---------------------------------------------------------------------------
//
// This is the branch issue #11's criterion two rests on, and the reason `toView` was built with a
// `source` discriminator in the first place. Not one component below changes for it: a showcase item
// is turned into arm views by the *same* `armView` a live `POST /query` goes through, and then
// through the *same* `comparison` a live pair goes through, so `renderComparison` cannot tell the
// two apart and does not have to.
//
// The one thing it adds is `showcase`, and everything in it exists to stop a visitor believing these
// numbers were measured for them just now: the `showcase_id`, when it was sampled, at what
// temperature, how many draws, and the rule that picked the draw on screen. The screen stamps that
// beside every panel of numbers (`showcasescreen.js`), on a distinct texture rather than a distinct
// tone, so it survives monochrome — the same argument the tier treatment already makes.

function showcaseView({ record, item, chunks, missing }) {
  // `chunks` is what `GET /chunks?ids=...` handed back: local, free, deterministic, no model. The
  // record itself holds no text at all, and a clone whose database does not hold the operator's
  // material simply gets fewer entries here and renders those chunks as identified absences.
  const byId = new Map((chunks || []).map((chunk) => [chunk.chunk_id, chunk]));
  const absent = new Set(missing || []);

  const arms = item.arms.map((arm) => {
    const sample = arm.samples[arm.displayed_sample];
    // Hand-assembled into exactly the body shape `armView` reads, and deliberately not a second
    // adapter written beside it. If the record ever stops being convertible into this shape the
    // record is wrong, not the renderer — that is the boundary doing its job.
    const view = armView({
      question: item.question,
      corpus_hash: record.provenance.corpus_hash,
      strategy: arm.strategy,
      embedder: arm.embedder,
      k: arm.k,
      tiers: arm.tiers,
      contract: arm.contract,
      chunks: arm.retrieval.chunks.map((chunk) => hydrate(chunk, byId, absent)),
      answer: sample.answer,
      trace: sample.trace,
    });
    return {
      ...view,
      // Which of the n is on screen, and — always beside it — the rule that chose it. Shown together
      // because "sample 1 of 3" on its own invites the reader to assume it is the first or the best,
      // and it is neither.
      displayedSample: arm.displayed_sample,
      sampleCount: arm.samples.length,
      displayRule: record.displayed_sample_rule,
      spread: spreadView(arm.spread),
    };
  });

  return {
    ...comparison(arms),
    source: "showcase",
    showcase: {
      showcaseId: record.showcase_id,
      scope: record.scope,
      startedAt: record.started_at,
      sampling: {
        n: record.sampling.n,
        generator: record.sampling.generator,
        model: record.sampling.model,
        temperature: record.sampling.temperature,
        measuredOn: record.sampling.measured_on,
      },
      redistribution: {
        limit: record.redistribution.verbatim_token_limit,
        worst: record.redistribution.worst_verbatim.tokens,
        chunkTextStored: record.redistribution.chunk_text_stored,
      },
      displayRule: record.displayed_sample_rule,
      why: item.why,
      // Counted here so the screen can say it in one sentence instead of every card repeating it.
      // Zero is the ordinary case on the machine that built the record and is *not* silence: the
      // line is drawn either way, because "every chunk was hydrated" is a fact worth stating on a
      // page whose whole claim is that it stores no text.
      hydrated: item.arms.reduce(
        (total, arm) => total + arm.retrieval.chunks.filter((chunk) => byId.has(chunk.chunk_id)).length,
        0
      ),
      absent: item.arms.reduce(
        (total, arm) => total + arm.retrieval.chunks.filter((chunk) => !byId.has(chunk.chunk_id)).length,
        0
      ),
    },
  };
}

function hydrate(chunk, byId, absent) {
  const held = byId.get(chunk.chunk_id);
  return {
    ...chunk,
    // Null, never `""`. The record carries no text by design and the database may not hold it
    // either; both arrive here as the same absence, and `absent` distinguishes "the artifact says it
    // does not have this" from "nobody asked for it".
    text: held ? held.text : null,
    unaskedFor: !held && !absent.has(chunk.chunk_id),
  };
}

function spreadView(spread) {
  // The strip plot's geometry, computed once here rather than in the renderer, because it is
  // arithmetic on served values — the same thing the overlap band is. What is *not* computed is a
  // mean, a standard deviation or an error bar: n is ten at most and the record deliberately holds
  // no scalar for any of these, so there is nothing to draw one from and nothing that should be
  // (ADR-0004).
  return Object.entries(spread || {}).map(([key, values]) => {
    const low = values.minimum;
    const high = values.maximum;
    // A constant metric — every draw identical, which at temperature 0 is the good outcome — has a
    // zero-width range. Its marks sit at the centre rather than at 0/0, which would draw ten
    // identical values as a hard left edge and read as "the lowest possible".
    const span = low === null || high === null || high === low ? 0 : high - low;
    return {
      key,
      label: SPREAD_LABELS[key] || key,
      n: values.n,
      minimum: low,
      maximum: high,
      distinct: values.distinct,
      // True when the quantity did not move across n draws. The single most useful thing on the
      // panel, and it is a count that was observed, not an estimate.
      constant: values.distinct <= 1,
      marks: values.values.map((value, index) => ({
        index,
        value,
        // Null is not a position. `cost_usd` is null for a model with no published price and for a
        // call that never happened; the mark is omitted rather than drawn at zero.
        fraction: value === null || span === 0 ? (value === null ? null : 0.5) : (value - low) / span,
      })),
    };
  });
}
