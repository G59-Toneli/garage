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

### The record stores no source prose. Ever.

The criterion says "no **model** call". It does not say "no database". The text already lives in
`chunks.text`, in the derived artifact that ADR-0002 makes the one legitimate home for third-party
material and that ADR-0003 keeps out of git. So:

- `chunk_id`, rank, score, tier, page, kind and `doc_title` go into the record;
- `GET /chunks?ids=...` hands back the paragraphs **and the section headings** — local, free,
  deterministic, no model;
- a clone **without** the operator's material renders metrics, answer, cost and trace with the
  chunks shown as **absent and identified**, which is already this interface's vocabulary
  (`docs/ui.md`: an absence travels as an absence).

`ShowcaseChunk` is `RetrievedChunk` minus `_SOURCE_TEXT_FIELDS`.

**`section` is on that list, and it was not at first.** This is the leak QA found, and the shape of
it is worth keeping written down. `chunking` sets `section` from `heading.group(2)` — it *is* the
source document's own heading — and the first version of this module wrote it to disk. Sixteen
fields of the committed record carried twelve-token runs of the fixture through it. Harmless there:
the fixture is `rights: original-work` and the leaked headings are short Tier B thread titles. Not
harmless the day a scanned manual is catalogued, when `Section 3.2 — Cylinder head, tightening
specifications` is fifty-four characters of a publisher's table of contents going into git without
passing any gate — the verbatim gate below reads `claim.text` against cited Tier A chunks, and a
`section` is neither.

`doc_title` deliberately stays. It is the manifest's own `title`, in git already, by hand, as the
catalogue entry ADR-0002 says a Corpus *is*. Committing it a second time redistributes nothing.

**What holds this line is not the field list**, because the field list is what got `section` wrong.
`extra="forbid"` catches a *new* text-bearing field on `Candidate`; it cannot catch one that already
existed and was enumerated incorrectly, and an earlier version of the comment beside it claimed
otherwise. The real guard is
`test_no_ngram_of_any_source_document_reaches_a_committed_record`: it reads the bytes on disk,
tokenises every string in them, and rejects any **seven**-token run of any source document that is
not already in the manifest. It knows nothing about fields.

Subtracting the manifest's own n-grams is the only exemption, and it is what distinguishes "this
text is in git on purpose" from "this text escaped" — `Catálogo de Peças — Kadett / Ipanema, grupo
12 e 18` is both a manifest `title` and its document's first heading.

**Seven was swept, not guessed**, because the guard it replaces was chosen by eye and had a blind
spot:

| n | exempt by manifest | false positives on the record | catches `Section 3.2 — Cylinder head, tightening specifications` |
| --- | --- | --- | --- |
| 5 | 16 | **15** | yes |
| 6 | 11 | 0 | yes |
| **7** | **8** | **0** | **yes** |
| 8 | 5 | 0 | no |
| 12 | 0 | 0 | no |

Twelve was the first choice and was loose in the direction that matters: it cannot see a short Tier
A heading, which is precisely the string a real service manual's `section` will hold — that heading
is seven tokens folded. Five is too strict, flagging `doc_title` against its own document's H1
beyond what the manifest exempts. Note the exempt column too: at twelve the manifest subtraction was
inert and `doc_title` was passing on length alone.

**A limit worth knowing:** an n-gram guard cannot see a heading shorter than n tokens. It is a
backstop. The structural protection is that `section` is not stored at all.

The test it replaced matched whole markdown **lines**, so it never fired: the record stores a
heading without its `## ` prefix, and `line in written` was false every time. That is why the suite
was green with sixteen leaking fields on disk. There is a second test that reconstructs exactly that
case and asserts the new check rejects it, because a guard that has only ever passed is a guard
nobody can trust.

Today the whole fixture is `rights: original-work`, so storing any of it would be legal *now* and
illegal the day a real manual is catalogued. A format that is correct only until the project gets
serious breaks exactly when it matters.

### The verbatim gate, in two measures

The other way source material could reach git is the generated prose. A model answering over a
scanned manual can emit a sentence verbatim, and that sentence would be committed.

So the build measures every claim against every **cited Tier A** chunk, twice, because either
measure alone is escapable:

| measure | limit | catches |
| --- | --- | --- |
| longest contiguous run | `VERBATIM_TOKEN_LIMIT` = 25 | a quotation: N tokens in a row, no gaps |
| longest common subsequence | `VERBATIM_SUBSEQUENCE_LIMIT` = 25 | a copy in order with words dropped in |

Over either limit the build fails, names the question, and says which measure fired — a long run is
a quotation, a long subsequence with a short run is a paraphrase-shaped copy, and the two have
different fixes.

**Why both, with the numbers.** The first version of this module implemented only the contiguous run
and argued that a scattered subsequence is just two Portuguese sentences sharing articles. Measured,
that argument does not survive:

- over unrelated Portuguese paragraph pairs in this corpus, the longest common subsequence is **9**
  tokens — so a limit of 25 has sixteen tokens of margin over the worst false positive;
- correct, original, cited answers score **14–21**;
- and the cost of leaving it out: a 44-token Tier A paragraph copied in order with a linking word
  dropped in every twenty tokens — "ou seja", "segundo o manual", which is *ordinary LLM behaviour
  over a manual*, not an attack — scores a **contiguous run of 20** and sails through, while
  redistributing the paragraph word for word. Its subsequence is 44.

The exact ceiling of the run gate is also worth stating plainly: 25 contiguous tokens pass and 26
fail, so under the run gate alone a model could copy 25 contiguous tokens per claim, without limit,
by design. Four tokens of margin over the 21 a legitimate answer reached is thin, is written down
rather than smoothed over, and must be re-measured before a real corpus is catalogued — the measure
is absolute, so a longer generated answer mechanically scores higher against the same chunk.

It does **not** truncate and does **not** redact. Silent redaction would be the interface lying
about what the model produced, and it would hide the one signal an operator needs — "this
configuration copies". A human rewrites the question, drops it, or raises a threshold in a commit
where somebody can object. Both thresholds and both worst observed values go into `redistribution`,
so the decision is auditable by whoever reads the file rather than by whoever ran the command. The
two worsts are tracked **per measure**, not as one answer's pair: the answer with the longest run is
frequently not the one with the longest subsequence.

Two narrowings, both deliberate. **Tier A only**: Tier B is a forum post, a different problem with a
different answer, and folding them together makes the gate fire on the wrong thing. **Cited only**:
the model saw all *k* chunks in its prompt, so comparing against the uncited ones would flag a
coincidence as a leak.

**On the fixture neither gate will fire on its own**, so they would otherwise be exercised for the
first time in production against real licensed material. There are therefore two mandatory tests: a
long synthetic Tier A chunk copied whole (the run gate), and the same paragraph copied with a
linking word every twenty tokens (the subsequence gate, which asserts *both* that the run measure
does not fire and that the build fails anyway). Both check that the build names the question and
stops on the **first** offending sample rather than paying for the rest of the run.

## The schema

```
showcase_record_version: 1
showcase_id              <timestamp>-<git_sha[:12]>, the same format as run_id
started_at, duration_ms
layer: "showcase"        neither of RunRecord.layer's values; nothing compares across them
scope                    required, free text: what this file is FOR
provenance               evaluation.Provenance — imported, never redeclared
sampling                 {n, generator, model, temperature, measured_on} — once, at the top
redistribution           {chunk_text_stored: false,
                          verbatim_token_limit, worst_verbatim,
                          verbatim_subsequence_limit, worst_verbatim_subsequence}
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

**It also refuses a dirty working tree** unless `--allow-dirty` is passed. `showcase_id` is
`<timestamp>-<git_sha[:12]>`, deliberately `run_id`'s format, and that format is a *promise*: the
sha identifies the code that produced the numbers. Built from an uncommitted tree the sha names the
last commit and the code that produced the record exists nowhere.

`eval run` only warns about the same condition, and the asymmetry is the point rather than an
inconsistency. A run record is regenerated by one free local command, so a dirty one costs a minute
to replace; this one costs 160 provider calls, gets committed, and is then what a demo cites for
months — an unidentifiable build is not recoverable at that price. `--allow-dirty` makes the
exception deliberate and visible, and `provenance.git_dirty` carries it into the record either way,
where the screen prints it inline beside the id.

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

**There is no committed record in this checkout, and that is a state rather than an omission.** The
screen says so in as many words, because a build with no showcase is legitimate: producing one calls
a paid provider and is a deliberate act.

The proving run described below was deleted by
[ADR-0010](adr/0010-lexical-search-tries-strict-and-before-loose-or.md). That change altered the
lexical ranking without altering a document, so the record's measurements stopped describing this
build while its `corpus_hash` still matched — and the extended boot gate now refuses to serve it,
naming `ingest_version`, `text_search_config` and `text_search_dictionaries`. Rebuilding costs
provider calls that were not authorised, and the refusal message offers deletion as the other way
out, so deletion is what happened. The paragraphs below are kept as the record of what was measured
and of the four caveats the next build must not repeat.

`eval/showcase/20260802T051801Z-44a5db93da69.json` **was** a **proving run**: 2 questions × 2 arms × n=3,
built against the real Gemini API to show end to end that the command works, that the record
renders with no calls, and that a 429 is recorded as a degradation. Its `scope` says so.

**It is not the curated set.** The full showcase — 8 questions × 2 arms × n=10 = 160 calls — is the
owner's to authorise and pay for. Four things about the proving run should be read before citing it:

- **Its `provenance.git_dirty` is `true`**, so the sha in its `showcase_id` does not identify the
  code that produced it. It predates the refusal described above and could not be rebuilt without
  spending money that was not authorised. The screen says so inline beside the id, and the first
  curated build will not have the problem.
- **Its editorial text and its `section` fields were corrected by hand**, offline, after the record
  was measured: `section` was stripped from every stored chunk (the ADR-0003 leak), the second
  verbatim gate's fields were added — the worst subsequence is 0 because every sample abstained with
  zero claims, so the gate compared nothing — and the `why` on each item was refreshed from the
  corrected question set. **No measurement was touched**: the samples, spreads, `displayed_sample`,
  traces, token counts, costs and `showcase_id` are exactly as measured. A rebuild would have been
  the clean fix and costs 12 calls.

- **Two of its twelve draws are real 429 degradations**, left in deliberately. Deleting them would
  be curating the evidence, and they are the most useful thing in the file: they show that a
  provider failure is recorded as a provider failure, with the raw message on the span, rather than
  as a result. They are also why `--throttle` exists as a flag.
- **Nothing in it is a cited answer.** Every draw abstained — correctly. That is a real finding
  about this corpus, not a defect in the machinery, and it is the next section.

## A finding: this fixture does not support the demo's headline claim

An earlier draft of this section overstated it, in exactly the way the owner's own correction on
issue #7 warns about, and the corrected numbers are the ones below. **Do not write "every natural
Portuguese question retrieves zero under lexical" or "dense returns ten Tier B chunks" as statements
about the corpus** — both are true of the one question the record holds and false as generalisations.

Measured over the **21 natural Portuguese questions** in `eval/facts.jsonl`:

| claim | reality |
| --- | --- |
| lexical retrieves nothing | false as a generalisation — it retrieves something for **2 of 21** |
| dense returns only Tier B | false as a generalisation — it brings some Tier A for **4 of 21** |

The number that actually names the debt, from the owner's re-analysis of the run record behind
`baseline.json` (issue #7):

- **recall Portuguese → Tier A under dense = 2/10 = 0.20.**
- Dense scores `recall@10:natural` 0.809524, but **21 of 21** of that is the natural questions in
  *English*; genuine cross-language retrieval is **6.5% of the gain — two questions out of
  forty-two**. Another 29% is Portuguese question → Portuguese Tier B, same language.
- Of the 8 Portuguese naturals dense misses, **all 8** have Tier A gold.

The reason is structural: the Tier A documents in `corpus/fixture/` are written in English and the
Tier B ones in Portuguese, so a Portuguese question's nearest neighbours are frequently the forum
posts. The dense improvement in `eval/baseline.json` is real and correctly measured — it just does
not mean "ask in Portuguese and dense will find the manual". The Portuguese debt is open.

For the one question the committed record holds, `Com que aperto eu fecho o cabeçote do meu Kadett
GSi?`, lexical does return nothing and dense does return ten Tier B chunks at cosine 0.85–0.87, and
the model abstains correctly. That is what the record shows, and its `why` says so in those terms
with the population numbers beside them.

The curated question set was rewritten in light of this rather than around it. It opens with
`cylinder head bolt stage 2 torque`, where both arms retrieve Tier A and answer with citations, and
keeps the Portuguese phrasing as a separate item whose `why` states what happens for *that question*,
gives the population numbers above, and says plainly that a showcase which only showed the wins
would be advertising.

## Tests

| what | where |
| --- | --- |
| a curated question renders answer, citations, chunks, trace and cost with a generator that **raises if called** | `test_showcase.py` |
| no 7-token n-gram of any source document in any committed record, minus the manifest's own | `test_showcase.py` |
| that guard rejects the exact `section` leak the line-based one missed | `test_showcase.py` |
| the run gate fails the build on a synthetic long Tier A chunk, and names the question | `test_showcase.py` |
| the subsequence gate fails on the paragraph the run gate lets through | `test_showcase.py` |
| a dirty tree is refused before a single call, and `--allow-dirty` is the deliberate exception | `test_showcase.py` |
| `samples[].answer` keys are **exactly** `app.GeneratedAnswer`'s, on the serialized bytes | `test_showcase_contract.py` |
| no scalar for a stochastic metric anywhere in the document | `test_showcase_contract.py` |
| the build throttles between calls, not before the first, and not around a free abstention | `test_showcase.py` |
| a stale `corpus_hash` invalidates the record loudly, at boot | `test_showcase.py` |
| the stored spread recomputes from the samples | `test_showcase.py` |
| the screen's source contains no `POST` and no `/query` | `test_showcase_contract.py` |
