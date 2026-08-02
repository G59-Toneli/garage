# The precomputed showcase

A curated question, its answer, its citations, its chunks, its span tree and its cost — rendered in
a browser with **zero model calls** and no API key anywhere near the machine doing the rendering.

That is the artefact `#11` needs and the one the Run Record deliberately is not.

## Why this is a second record format and not a bigger Run Record

`evaluation.RunRecord` is deterministic, offline, needs no key, and is what `eval gate` re-measures
in CI on every push. A showcase is stochastic, costs money on every line, and cannot exist without a
provider. ADR-0007 already says that numbers which fail for different reasons are different numbers.

Fusing them would put the CI gate one dependency away from needing `GEMINI_API_KEY`, against
`pyproject.toml`, where `gemini` is an optional extra and `addopts = "-m 'not live'"` keeps the
default suite off the network. So there are two files, two commands and two lifecycles, and
`eval gate` never learns this format exists.

What the Run Record structurally cannot serve is spelled out in `ItemResult`: no answer, no claims,
no trace, no cost. This record adds those four. It adds no chunk text, and that is the next section.

## ADR-0003, resolved twice

### The record stores no chunk text. Ever.

The criterion says "no **model** call". It does not say "no database". The text already lives in
`chunks.text`, in the derived artifact that ADR-0002 makes the one legitimate home for third-party
material and that ADR-0003 keeps out of git. So:

- `chunk_id`, rank, score, tier, document and section go into the record;
- `GET /chunks?ids=...` hands back the paragraphs — local, free, deterministic, no model;
- a clone **without** the operator's material renders metrics, answer, cost and trace with the
  chunks shown as **absent and identified**, which is already this interface's vocabulary
  (`docs/ui.md`: an absence travels as an absence).

`ShowcaseChunk` is `RetrievedChunk` minus `text`, with `extra="forbid"`, so
`ShowcaseChunk(**vars(candidate))` fails loudly instead of quietly committing the corpus.
`tests/test_showcase.py` reads every committed record back and searches it for every non-trivial
line of the fixture Corpus.

Today the whole fixture is `rights: original-work`, so storing text would be legal *now* and illegal
the day a real manual is catalogued. A format that is correct only until the project gets serious
breaks exactly when it matters.

### The verbatim gate

The other way source material could reach git is the generated prose. A model answering over a
scanned manual can emit a sentence verbatim, and that sentence would be committed.

So for every claim, the build computes the longest run of tokens it shares with any **cited Tier A**
chunk. Over `VERBATIM_TOKEN_LIMIT` (25, configurable with `--verbatim-token-limit`) the build fails
and names the question.

It does **not** truncate and does **not** redact. Silent redaction would be the interface lying
about what the model produced, and it would hide the one signal an operator needs — "this
configuration copies". A human rewrites the question, drops it, or raises the threshold in a commit
where somebody can object. The threshold and the worst run actually observed both go into
`redistribution`, so the decision is auditable by whoever reads the file rather than by whoever ran
the command.

Two narrowings, both deliberate. **Tier A only**: Tier B is a forum post, a different problem with a
different answer, and folding them together makes the gate fire on the wrong thing. **Cited only**:
the model saw all *k* chunks in its prompt, so comparing against the uncited ones would flag a
coincidence as a leak.

**One deviation from issue #14's wording, stated rather than buried.** The issue says "longest common
token *subsequence*"; this implements the longest common *contiguous run*. A 25-token subsequence
scattered across a 400-token chunk is not redistribution — it is two Portuguese sentences about the
same torque figure sharing articles and prepositions, and a gate that fired on that would fire on
every correct answer the system produces. Contiguity is what makes a match a copy. A gate nobody can
leave switched on is worse than a looser gate that stays on. `test_showcase.py` asserts the
distinction so it cannot be undone by accident.

**On the fixture this gate will never fire on its own**, so it would otherwise be exercised for the
first time in production against real licensed material. There is therefore a mandatory test that
injects a long synthetic Tier A chunk, has the fake model copy it whole, and asserts the build fails,
names the question, and fails on the *first* offending sample rather than paying for the rest of the
run.

## The schema

```
showcase_record_version: 1
showcase_id              <timestamp>-<git_sha[:12]>, the same format as run_id
started_at, duration_ms
layer: "showcase"        neither of RunRecord.layer's values; nothing compares across them
scope                    required, free text: what this file is FOR
provenance               evaluation.Provenance — imported, never redeclared
sampling                 {n, generator, model, temperature, measured_on} — once, at the top
redistribution           {chunk_text_stored: false, verbatim_token_limit, worst_verbatim}
displayed_sample_rule    the rule, in the record
items[]
  question_id, question, why
  arms[]
    strategy, embedder, k, tiers, contract
    retrieval  { chunks[] }         one measurement, no spread
    samples[]  { index, answer, trace }   answer is app.GeneratedAnswer exactly
    spread     { metric: {values[], n, minimum, maximum, distinct} }
    displayed_sample
```

Things that are not negotiable and why:

- **`provenance` is `evaluation.Provenance`**, imported. A second declaration is a place to drift.
- **`samples[].answer` is `app.GeneratedAnswer`**, imported, for the same reason and one more: the
  interface reads those keys by name, so a redeclaration would let a renamed field leave the suite
  green and break the demo in a browser. That import points at the HTTP layer from a record module,
  which is the wrong direction for a dependency arrow and is taken anyway — the alternative is a
  second description of the same object. `tests/test_showcase_contract.py` asserts it on the
  serialized bytes regardless, because "we imported the right class" is a claim about today's
  source and not about the file on disk.
- **`sampling` sits at the top, once**, exactly where `RunRecord` puts `sample_count`. Every sample
  in the file was drawn the same way, or the file compares different things and no reader can tell.
  A validator checks the promise against every arm underneath it.
- **`retrieval` is one measurement with no spread**, and that is honest rather than lazy: the same
  query against the same artifact returns the same order (`ORDER BY score DESC, chunk_id`). The
  build *asserts* it across all n samples and stops if it ever fails, rather than assuming it.
- **No scalar field for a stochastic metric anywhere.** `Spread` carries `values`, `minimum`,
  `maximum` and `distinct` — order statistics and a count, all four observed — and deliberately no
  mean and no standard deviation. At n=10 a sigma is a number invented from too little evidence and
  drawn with the authority of a measured one (ADR-0004). The interface *cannot* render a point
  estimate because the file does not contain one, and a test walks the whole document rejecting any
  key named `mean`, `median`, `stddev`, `p95` and friends.
- **`displayed_sample` is the median of `tokens_out`, ties to the lowest index**, and the rule is
  written into the record. Never "the best one" — picking the nicest of ten answers for a demo is
  cherry-picking dressed as curation, and storing all ten is what lets a reader check the choice.
- **`why` is required on every question.** A showcase is a set of questions somebody chose, so the
  reason for each choice is the difference between a benchmark and a highlight reel. It is shown on
  screen beside the result, and a test asserts it still matches `eval/showcase/questions.jsonl`.

## Building one

```
python -m garage showcase build --scope "…" [--limit N] [-n N] [--throttle SECONDS] --yes
```

Without `--yes` it prints the plan — how many calls, at what spacing, for how long — and stops.
This is the only command in the CLI that spends money, so it is the only one that refuses to act on
its own: 8 questions × 2 arms × n=10 is 160 calls, and a mistyped `-n` is a day of free-tier quota
on the wrong thing.

**Throttling.** The default is `THROTTLE_SECONDS = 6`, sized for the documented ~10 RPM free tier.
Only draws that actually reached the provider count: a question the retriever comes back empty on is
abstained *without asking anybody* (`app._answer`), consumes no quota, and therefore consumes no
pause either — otherwise the arm that behaves best would be the slowest part of the build. The plan
prints an upper bound for the same reason.

**Six seconds was not enough on a shared key.** The proving run below hit
`429 RESOURCE_EXHAUSTED … limit: 20` twice. If the key is used by anything else, raise `--throttle`.
A 429 is recorded honestly as a degradation, with the provider's raw message on the span — it is not
silently retried and it is not confused with a result. That is the correct behaviour and it is also
the reason to throttle harder: a record full of degradations measures the rate limiter, not the
model.

## Serving and rendering

Three read-only endpoints, all free:

| endpoint | returns |
| --- | --- |
| `GET /showcase` | the record ids, newest first |
| `GET /showcase/{id}` | the bytes on disk, unmodelled |
| `GET /chunks?ids=…&ids=…` | `{corpus_hash, chunks[], missing[]}`, capped at `MAX_CHUNK_IDS` |

`GET /showcase/*` hands back bytes for the same reason `GET /eval/*` does: re-serialising through a
Pydantic model here would be a second description of the format and therefore a place for it to
drift with no test noticing.

`GET /chunks` reports `missing` as a **field**, not a 404. A partial answer is the designed state,
not an error, and a 404 would turn a legitimate partial artifact into an error page. Over the cap it
is a 422 with the real number rather than a truncation, because a silently shortened hydration is
indistinguishable from chunks the artifact genuinely lacks.

**The boot gate.** `showcase.verify_showcase_records` refuses to start the service when any record's
`corpus_hash` does not match the artifact, naming every mismatch at once — exactly as ADR-0002
refuses a mismatched database. A precomputed answer over material this artifact does not hold cites
chunks nobody can check, and `GET /chunks` would happily hydrate it with the wrong paragraph.

`GARAGE_SHOWCASE_DIR` decides which records a container serves, exactly as `GARAGE_CORPUS_DIR`
decides which manifest it checks against. Null means the repository's own `eval/showcase/`.

**The screen** is `showcase.html` + `showcasescreen.js`, and it renders through
`toView({source: "showcase"})` into `renderComparison` — the same components the live page uses, no
changes. `docs/ui.md` covers the four channels the page says "precomputed" on, the strip plot, and
the one component that did change.

## What is committed here, and what it is not

`eval/showcase/20260802T051801Z-44a5db93da69.json` is a **proving run**: 2 questions × 2 arms × n=3,
built against the real Gemini API to show end to end that the command works, that the record
renders with no calls, and that a 429 is recorded as a degradation. Its `scope` says so.

**It is not the curated set.** The full showcase — 8 questions × 2 arms × n=10 = 160 calls — is the
owner's to authorise and pay for. Two things about the proving run should be read before citing it:

- **Two of its twelve draws are real 429 degradations**, left in deliberately. Deleting them would
  be curating the evidence, and they are the most useful thing in the file: they show that a
  provider failure is recorded as a provider failure, with the raw message on the span, rather than
  as a result. They are also why `--throttle` exists as a flag.
- **Nothing in it is a cited answer.** Every draw abstained — correctly. That is a real finding
  about this corpus, not a defect in the machinery, and it is the next section.

## A finding: this fixture does not support the demo's headline claim

Measured retrieval-only, for free, over all eight curated questions on both arms:

- **Every natural Portuguese question retrieves zero chunks under `lexical`** — as expected, and the
  0.07 the baseline records.
- **Dense does not close it on this corpus.** For `Com que aperto eu fecho o cabeçote do meu Kadett
  GSi?` the dense arm returns ten chunks, **all Tier B**, from the forum and the blog, at cosine
  0.85–0.87. The model then abstains, correctly, because no Tier A chunk is in front of it.

The reason is structural: the Tier A documents in `corpus/fixture/` are written in English and the
Tier B ones in Portuguese, so a Portuguese question's nearest neighbours are the forum posts.
`eval/baseline.json`'s dense improvement is measured over a fact suite that is half English
phrasings, and it is real — it just does not mean "ask in Portuguese and dense will find the
manual".

The curated question set was rewritten in light of this rather than around it. It now opens with
`cylinder head bolt stage 2 torque`, where both arms retrieve Tier A and answer with citations, and
keeps the Portuguese phrasing as a separate item whose `why` states plainly that neither column
answers it and that a showcase which only showed the wins would be advertising.

## Tests

| what | where |
| --- | --- |
| a curated question renders answer, citations, chunks, trace and cost with a generator that **raises if called** | `test_showcase.py` |
| no chunk text in any committed file, checked against the corpus line by line | `test_showcase.py` |
| the verbatim gate fails the build on a synthetic long Tier A chunk, and names the question | `test_showcase.py` |
| `samples[].answer` keys are **exactly** `app.GeneratedAnswer`'s, on the serialized bytes | `test_showcase_contract.py` |
| no scalar for a stochastic metric anywhere in the document | `test_showcase_contract.py` |
| the build throttles between calls, not before the first, and not around a free abstention | `test_showcase.py` |
| a stale `corpus_hash` invalidates the record loudly, at boot | `test_showcase.py` |
| the stored spread recomputes from the samples | `test_showcase.py` |
| the screen's source contains no `POST` and no `/query` | `test_showcase_contract.py` |
