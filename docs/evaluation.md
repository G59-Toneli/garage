# The deterministic evaluation gate

Retrieval quality is a number, the number is committed, and the build fails when it falls:

```sh
docker compose up -d postgres
python -m garage ingest
python -m garage eval gate
```

```
corpus_hash: 21c4e571b96fefae82062b11d1cdd0f237b0b311d781d8a11f975d8b650b75d6
postgres:    16.14 (Debian 16.14-1.pgdg12+1) (pg_trgm 1.6)
facts:       76 (sha256 3c7dc8b7e9dd…)
lexical  k=10
  candidates@10      4.236842
  mrr@10             0.751974
  ndcg@10            0.779283
  precision@10       0.088158
  recall@1           0.703947
  recall@10          0.868421
  recall@10:keyword  1.000000
  recall@10:natural  0.761905
  recall@5           0.815789
  value_match@1      0.710526
  hit_rank           1:54  2:2  3:3  4:1  5:2  6:1  9:3  miss:10
gate: pass
```

No language model, no network, no API key. The questions, the expected answers, the baseline and the
thresholds are all files in `eval/`, so the gate is a pure function of the checkout and the artifact
`ingest` just built. That is the *deterministic* half of
[ADR-0004](adr/0004-two-layer-evaluation.md): it asks only whether the right chunk came back, which
is a question with a written-down answer. Whether a generated answer *reads* correctly is the judge
layer's question, and no build should ever block on a model's opinion.

Read the two numbers in the middle before anything else. `recall@10:keyword` is 0.91 and
`recall@10:natural` is 0.07. The lexical strategy answers a bag of words almost perfectly and
answers a sentence almost never, and the headline 0.45 is the average of two populations that behave
nothing alike. That gap is the most useful thing this gate currently reports.

## The fact set

`eval/facts.jsonl`, one JSON object per line, written by hand against real chunk identifiers:

```jsonc
{"fact_id": "kw-torque-volante-motor", "question": "flywheel bolt torque", "phrasing": "keyword",
 "expected_value": "63", "chunk_ids": ["svc-kadett-1993#0006"], "tolerance": 0.5}
{"fact_id": "torque-volante-motor", "question": "What torque do the flywheel bolts need?",
 "phrasing": "natural", "expected_value": "63", "chunk_ids": ["svc-kadett-1993#0006"],
 "tolerance": 0.5}
```

| Field | Meaning |
| ----- | ------- |
| `fact_id` | Lowercase slug, unique. It is how a regression names the question that broke. |
| `question` | Typed the way a person would type it, including the accents they would leave off. |
| `phrasing` | `keyword` or `natural`. Required — see below. |
| `expected_value` | What a correct answer must contain. Scored separately from the ranking. |
| `chunk_ids` | The chunks that answer it. A list, and at least one. |
| `tolerance` | Present exactly when `expected_value` is a number. Its absence selects text matching. |

`chunk_ids` is a list even though most facts name one chunk, and the reason is not hypothetical: with
exactly one relevant chunk per question *and* that chunk always at rank 1, nDCG is a constant and
reports nothing MRR did not already say. Facts with two relevant chunks — both cylinder head stages
are M11 — are what make the third metric earn its place.

### The two phrasings, and the suite that was thrown away

The suite is a sample of an input distribution, and `phrasing` is what makes the sample auditable.
Real users type both: three keywords into a search box, and a whole sentence with a question mark at
the end. Against a conjunctive `plainto_tsquery` those behave completely differently.

**The first version of this file was 100% keyword and scored 0.914894 — and it was worthless.** It
was discarded, and the reasoning is recorded here because a number that high is exactly the kind of
result nobody re-examines:

- Not one of its questions was an interrogative sentence. No auxiliary verb, no question word, no
  question mark. It measured whether an inverted index exists.
- Five of its questions were literal substrings of the sentence that answered them.
- Its `hit_rank` distribution was `{1: 43, miss: 4}` — no question anywhere in ranks 2–10, so five of
  its six metrics collapsed onto the identical value. Six names, two bits of signal.
- Worst, with `tolerance: 0.0` it would have frozen 0.91 as a floor. Any future work that taught the
  retriever to read a sentence would reorder those 43 docile queries and **fail the build**. It was a
  ratchet protecting the bug instead of exposing it.

The replacement is stratified and the honest number is much lower. That is the point: a gate
calibrated at 0.91 against docile questions measures nothing, and one calibrated at 0.45 against
questions people would really ask measures everything that matters. Every point issue #7 wins on
`recall@10:natural` will show up as a real improvement.

### What the numbers look like today

| Stratum | Facts | `recall@10` |
| ------- | ----- | ----------- |
| `keyword` | 34 | 0.911765 |
| `natural` | 42 | 0.071429 |
| all | 76 | 0.447368 |

The `hit_rank` distribution is `1: 33`, `2: 1`, `miss: 42`. It is **bimodal with a thin tail at 2**,
and that shape is a property of the retriever rather than of the suite.

`plainto_tsquery` ANDs its terms, so a query containing one word the chunk does not have matches it
not at all — and the `word_similarity` floor of 0.6 is too high for the trigram half to rescue it.
That is the `miss` mode, and it accounts for the whole left half of the table. The other mode is not
quite "rank 1 or nothing", though: when the conjunction matches *several* chunks they frequently tie
on `ts_rank_cd`, and `ORDER BY score DESC, chunk_id` breaks the tie by identifier — deterministically,
which is the point, but the gold chunk lands wherever its `chunk_id` sorts.

```
'bearing cap nuts order'  ->  #0014, #0012, #0003        gold #0012 at rank 2
'injector part'           ->  #0008, #0009               gold #0009 at rank 2
'capacity litres'         ->  #0015, #0016, #0017, #0018 all four tied, ordered by chunk_id
```

So ranks 2 and 3 are reachable. What is *not* reachable is a smooth curve out to 10: every gold chunk
found beyond rank 2 in probing turned out to belong to a question with more than one defensible
answer, and an ambiguous question is not a Fact. A fact set cannot manufacture the middle of the
distribution here, and writing questions that try would be writing worse questions.

The right response is to fix the retriever, not to write questions that flatter it. The rank
histogram is printed on every run so that the day it changes shape, everyone sees it.

Every line is validated against a Pydantic model that forbids unknown fields, blank lines are
rejected rather than skipped, and errors are reported with line numbers, all of them at once. A
tolerant reader would let a fact be deleted by a stray keystroke and the suite quietly shrink, and a
suite that shrinks is the cheapest way there is to make a quality gate pass.

Before a single query runs, every `chunk_id` in the file is checked against the `chunks` table — and
again, every missing one is listed together. `chunk_id` is `<doc_id>#<ordinal:04d>` and positional, so
a chunking change renumbers it; a fact left pointing at the old number scores zero forever, looks
exactly like a retrieval regression, and would be blamed on the retriever for a week.

## The metrics

Binary relevance throughout — a chunk either answers the question or it does not — macro-averaged, so
every question weighs the same whatever its document or tier. All of them come from a **single**
retrieval per question at `k = 10`; nothing is re-queried per depth and nothing is re-sorted in
Python, because the SQL already made the order total (`ORDER BY score DESC, chunk_id`).

- **`recall@1`, `recall@5`, `recall@10`** — `|top-k ∩ relevant| / |relevant|`. Three depths because
  they answer different questions: whether the top hit is usable on its own, and whether a reader
  scrolling the panel would find it at all.
- **`recall@10:keyword`, `recall@10:natural`** — the same measurement over each stratum. Reported and
  **gated**, both of them, because gating only the average is what lets a change buy keyword recall
  with sentence recall and pass.
- **`mrr@10`** — `1 / rank` of the first relevant chunk, 1-based, and **zero when there is no hit in
  the top ten**. Named with the depth because it is truncated. A miss is never skipped: averaging
  over the questions that still work would let a change that stops answering the hard ones report a
  *higher* MRR than the version that answered them at rank 9.
- **`ndcg@10`** — `DCG / IDCG` with `DCG = Σ rel_i / log2(i+1)` and the ideal summed over
  `min(|relevant|, k)` positions. Under binary relevance the two textbook gain formulations agree —
  `rel` and `2^rel − 1` are both 0 for 0 and both 1 for 1 — so there is no choice being made. Zero by
  explicit convention if the ideal is zero, which `Fact` already forbids.
- **`value_match@1`** — the fraction of questions whose **first** chunk states the expected value.
  With a `tolerance` the numbers are extracted and compared numerically, because `"63" in text` is
  true of `163`, of `0.63` and of `Section 6.3`. Without one the value is matched as text, accent-
  and case-insensitively, by the same folding ingestion used to detect Jargon.

- **`precision@10`, `candidates@10`** — `|top-k ∩ relevant| / k`, and the mean number of candidates
  a question got back at all. Both **ungated**, and both added by
  [ADR-0010](adr/0010-lexical-search-tries-strict-and-before-loose-or.md) for one reason: every other
  metric here rewards finding the right chunk and none of them notices what came back with it. That
  was harmless while `lexical` returned 0.7 candidates per question and is not now that it returns
  4.2 — the same change on a corpus of fifty thousand chunks could return fifty per question while
  `recall@10` climbs and the gate stays green the whole way. `candidates@10` is the one carrying the
  news; `precision@10` is capped near 0.1 on a fact set of mostly single-chunk answers and moves
  almost exactly like recall. They stay out of `gated_metrics` because gating means committing to a
  direction, and the honest direction for `candidates@10` is not "lower" — a retriever that abstains
  on everything scores a perfect zero. In the record, watched by a person, gated when somebody can
  say what a bad value is.

`value_match@1` used to scan all ten retrieved chunks, and at that width it agreed with `mrr@10` to
six decimals on every question — a metric that never disagrees with another is noise in the report.
Asked of the single chunk a reader is shown first it disagrees with both: a hit at rank 3 states the
value and scores zero here, and a wrong top-1 that happens to carry the number scores one. It is
reported but **not gated** — it is a proxy for groundedness, and gating a proxy invites tuning the
proxy.

Everything is rounded to six decimals before it is written and before it is compared, so the file and
the check agree by construction.

Fifteen lines of `math` and no scientific dependency. A gate that has to run on an ARM VM
([ADR-0001](adr/0001-architecture-characteristics.md)) does not import numpy to avoid writing
`1 / log2(i + 1)`.

## Run Records

`python -m garage eval run` measures and writes `eval/runs/<started_at>-<git_sha>.json`. Records are
**generated, never hand-written**, and they accumulate: one file per run, because two branches each
adding a file merge cleanly while two branches each appending a line to a JSONL conflict on that line
every single time. The history is the directory listing.

```jsonc
{
  "run_record_version": 2,
  "run_id": "20260802T013309Z-fba1dad4ff09",
  "started_at": "2026-08-02T01:33:09Z",
  "duration_ms": 1204,
  "layer": "deterministic",
  "suite": "facts",
  "provenance": {
    "git_sha": "fba1dad4ff09…", "git_dirty": false,
    "corpus_id": "fixture", "corpus_hash": "21c4e571…", "ingest_version": 2,
    "python_version": "3.12.13", "platform": "Windows-11-…",
    "postgres_version": "16.14 (Debian 16.14-1.pgdg12+1)", "pg_trgm_version": "1.6",
    "text_search_config": "public.garage_bi",
    "text_search_dictionaries": "garage_en_stop, garage_pt_stop, portuguese_stem, simple, unaccent"
  },
  "sample_count": 76,
  "facts_sha256": "3c7dc8b7e9dd…",
  "arms": [
    {
      "configuration": {"strategy": "lexical", "k": 10, "tiers": ["A", "B"],
                        "reranker": null, "embedder": null},
      "metrics": {"recall@1": 0.703947, "mrr@10": 0.751974, "…": 0.0},
      "per_item": [
        {"fact_id": "kw-torque-volante-motor", "question": "flywheel bolt torque",
         "expected_chunk_ids": ["svc-kadett-1993#0006"], "expected_value": "63",
         "hit_rank": 1, "reciprocal_rank": 1.0, "ndcg": 1.0, "value_matched": true,
         "retrieved_chunk_ids": ["svc-kadett-1993#0006"]}
      ]
    }
  ]
}
```

### Why arms, and why the shared fields sit above them

A record holds **one arm per strategy**, measured in the same pass. `provenance`, `sample_count` and
`facts_sha256` are held once, at the top, *outside* the arms — and that placement is the argument.
Every arm in a record is by construction the same database, the same `corpus_hash`, the same chunking
rules and the same questions, which is exactly what makes the arms comparable **to each other**. That
comparison is what the whole demo rests on. With the fields held once, "these two strategies were
measured against different corpora" stops being a mistake anyone can make and becomes a sentence this
format cannot express.

Metrics are per-arm rather than one flat dictionary with prefixed keys, because prefixed keys are a
namespace pretending not to be one. An arm is the namespace.

`run_record_version` is a `Literal`, so a record written by a future version fails to load rather than
loading partially — exactly like `manifest_version`. It went to **2 before anything shipped**,
deliberately: the single-arm shape could not express the comparison the benchmark exists to make, and
migrating later would have meant every committed record and the promoted baseline failing to load on
the same afternoon.

`verify_artifact` runs before anything is measured and before anything is written, and the
`corpus_hash` and `ingest_version` in the record come from the `Artifact` the **database** returned,
not from the manifest in the checkout. The manifest is what we expected; the artifact is what we
measured ([ADR-0002](adr/0002-database-as-derived-artifact.md)). Both numbers are cited together or
not at all ([ADR-0007](adr/0007-corpus-hash-and-ingest-version-are-separate.md)).

The four database fields are in provenance because the ranking is not in Python. It is `ts_rank_cd`,
a snowball stemmer and `word_similarity`, all of them inside the server, and a record that noted the
laptop's OS but not the engine that did the ranking would be describing the wrong machine.

`text_search_config` is the configuration the **search actually runs under** — `TEXT_SEARCH_CONFIG`
from `database.py`, resolved through `::regconfig` exactly as `to_tsvector` and `plainto_tsquery`
resolve it. It is deliberately *not* the server's `default_text_search_config`: `chunks.tsv` and the
query both name `portuguese` explicitly, so the default is a setting this pipeline never reads. An
earlier version of this field recorded the default, which meant it could not detect a change to the
configuration that matters and quietly misdescribed the run to anyone reading it.

`text_search_dictionaries` is there because the configuration name is only a pointer. `portuguese` is
a label for a snowball stemmer plus a stop word list, and a server upgrade that reissues either moves
every `ts_rank_cd` in the file while the configuration is still called `portuguese`. That is the field
which would actually catch it.

The single constant is what keeps all of this honest: `database.TEXT_SEARCH_CONFIG` is interpolated
into the stored `tsvector`, into `plainto_tsquery`, and into what the record cites, so the record
cannot drift from the SQL it claims to describe.

`git_dirty` excludes `eval/runs/` from its answer, which is a fix rather than a convenience: writing a
record dirties the tree, so without the exclusion every run after the first would report a dirty tree
because the previous run existed. The flag is about the inputs; records are outputs.

`facts_sha256` is what makes a moved number attributable. Without it, "recall fell from 0.45 to 0.31"
is ambiguous between *retrieval got worse* and *someone added eight hard questions*, and those two
call for opposite responses.

**No chunk text is ever written to a record.** `chunk_id` is enough to reproduce any line of it
against the artifact, and records are committed — against a real Corpus of scanned manuals, a record
carrying text would be a slow redistribution of the material this repository promises not to hold
([ADR-0003](adr/0003-no-redistribution-of-source-material.md)).

## The baseline

`eval/baseline.json` holds the numbers to beat and the policy for beating them. It is **not** a file
of typed numbers: `run_id` points at a real record in `eval/runs/`, and the gate fails if that record
is not there. A baseline of hand-typed numbers is a wish; a baseline that names a run is a claim
someone can go and check.

```jsonc
{
  "baseline_version": 2,
  "run_id": "20260802T013309Z-fba1dad4ff09",
  "sample_count": 76,
  "facts_sha256": "3c7dc8b7e9dd…",
  "arms": [
    {
      "configuration": { /* must match the run arm exactly */ },
      "metrics": { /* copied from the record */ },
      "gated_metrics": ["mrr@10", "ndcg@10", "recall@1", "recall@10",
                        "recall@10:keyword", "recall@10:natural", "recall@5"]
    }
  ],
  "tolerance": 0.014,
  "noise_floor": 0.013
}
```

`gated_metrics` is per arm, explicit, and **may be empty**. A newly promoted arm gates nothing until
someone lists its metrics by hand: a measurement should be watched before it is allowed to fail
everyone's build, and auto-gating whatever a new arm happened to report turns an unreviewed number
into a build dependency. Both `promote` and `gate` say out loud when an arm gates nothing.

**`tolerance` is a policy number, not a statistical one.** Nothing in this pipeline is random: the
same commit against the same artifact produces bit-identical metrics, so there is no variance to
estimate and no confidence interval to derive. The tolerance says how much quality a human is willing
to wave through.

It is compared against each metric's own delta, so what `0.014` actually buys depends on how many
questions that metric averages over — and the two cases are deliberately different:

| Metric | Population | One question is worth | Effect of `tolerance: 0.014` |
| ------ | ---------- | --------------------- | ---------------------------- |
| `recall@10`, `mrr@10`, `ndcg@10`, … | 76 | 0.0132 | one question may regress; two may not |
| `recall@10:keyword` | 34 | 0.0294 | **strict** — one question fails the build |
| `recall@10:natural` | 42 | 0.0238 | **strict** — one question fails the build |

The aggregate metrics get exactly one question of slack, so a change that is a clear win overall is
not blocked by a single unlucky query. The strata get none, and that is the intent rather than an
oversight: the strata exist to stop a change trading one phrasing against the other, and a stratum
with slack in it could not do that job. If you want to lose a keyword question to win several natural
ones, the gate makes you say so — run `eval run`, look at the record, and promote deliberately.

`noise_floor` is `0.013`, just under the `1/76 ≈ 0.0132` that one question is worth on an aggregate,
so every real improvement is reported and nothing else is.

Both live in a committed file rather than an environment variable precisely so that changing either
is a reviewable act rather than a CI setting somebody adjusted.

## Promotion

```sh
python -m garage eval run                     # writes eval/runs/<run_id>.json
python -m garage eval promote <run_id>        # copies its measurement into the baseline
git add eval/ && git commit
```

Deliberate, local, and **never run in CI**. The commit that promotes a baseline is the only durable
record that a human looked at a change in retrieval quality and decided to keep it; a CI job that
promoted on green would erase that record and turn the gate into a ratchet that agrees with whatever
landed last. Policy — `gated_metrics`, `tolerance`, `noise_floor` — is carried over rather than reset,
because promoting a measurement is not the same decision as changing what the build may fail on.

## What fails the build

The gate collects **every** reason at once and prints them to stderr with the next action on the last
line. It writes nothing, ever.

Comparability is checked before quality, and a mismatch is a refusal rather than a comparison made
anyway. A baseline arm measured at `k=10` says nothing about a run arm at `k=5`; a baseline measured
on thirty questions says nothing about a run on seventy-six. Answering "did it get worse?" across
either is not a conservative approximation, it is a wrong answer.

| Failure | Why it is fatal |
| ------- | --------------- |
| `facts_sha256` differs from the baseline | The questions changed. Promote deliberately. |
| `sample_count` below the baseline | Losing questions raises every macro-average it touches. |
| An arm's `configuration` differs from its baseline arm | Different measurement, not a worse one. |
| A gated metric regressed by more than `tolerance` | The thing this gate exists for. |
| A gated metric is missing from the run | A metric cannot be retired by deleting it. |
| A baselined arm the run no longer measures | Nor can a strategy be retired by not running it. |
| A `chunk_id` in the fact set is not in the database | Stale facts read as a retrieval regression. |
| `verify_artifact` failed | Measuring a database that is not this commit's artifact. |
| The baseline's `run_id` is not in `eval/runs/` | A baseline must point at something readable. |
| The newest run record is not an ancestor of HEAD | It came from another branch. Below. |
| The newest run record does not match this build | Below. |

Comparability is per arm, so a `dense` arm arriving at a different `k` does not make the `lexical`
comparison unavailable. A **brand-new** arm never fails a build — it is reported, with its numbers, so
a human can promote it deliberately.

An improvement never fails a build either. It prints the positive delta and a reminder to promote,
because a baseline nobody promotes stops being a floor and becomes a memory.

A regression also prints the questions that moved, read off `per_item` in both records:

```
lexical: 2 question(s) moved
  torque-volante-motor: rank 1 -> none
  coroa-curta: rank 2 -> 7
```

That is what makes this a debugging tool rather than a red light. "recall@1 fell by 0.04" is
unactionable; a named `fact_id` is a query you can paste into `/query` and watch.

## Why CI re-measures the committed record

Run records accumulate in the repository because a developer generates one and commits it alongside
the change that moved it. CI then re-runs the evaluation and asserts the newest record in the tree
still describes this build. That inverts what a record is: not a by-product of whichever machine
happened to run CI, but a **reproducible assertion** checked into the repository, in the same spirit
as the database being a derived artifact ([ADR-0002](adr/0002-database-as-derived-artifact.md)).

Two checks, not one:

1. **Ancestry.** `latest_run_record` picks the newest filename, and a record committed on an unmerged
   branch with a later timestamp would otherwise silently become the thing this build is validated
   against. Its `git_sha` must be an ancestor of `HEAD` — satisfiable, since a record is always
   committed after the sha it names, and it catches exactly the orphan.
2. **The measurement.** Everything the run must reproduce, compared field by field.

```
the newest run record in the tree (20260802T013309Z-fba1dad4ff09.json) does not match what this
build measures. It was committed against a different corpus, engine, Configuration or retrieval
behaviour.
```

Three groups of fields are excluded from that second comparison:

- `run_id`, `started_at`, `duration_ms` — a clock and a stopwatch, different by definition.
- `python_version`, `platform` — the gate runs on a developer's laptop and on CI's Ubuntu. Requiring
  these to match would assert the two are the same machine. They stay in the record because they are
  what you want to read when a number *does* differ. This is the weakest of the three exclusions,
  which is why the database that actually did the ranking — version, `pg_trgm`, text search config —
  **is** compared.
- `git_sha`, `git_dirty` — a record cannot name the commit that contains it. Requiring a match would
  make the check unsatisfiable by construction rather than strict; ancestry is the satisfiable form
  of the same question, and it is checked.

What is left — corpus, chunking rules, engine, Configurations, questions, and what came back for each
one — must be identical. If it is not, the record in the tree describes a build that no longer exists,
and the fix is one command:

```sh
python -m garage eval run && git add eval/runs
```

## Adding a strategy

Issue #7 adds `dense`. The path is one line in `retrieval.available_retrievers`, which is the tuple
the gate iterates over — `evaluation.py` is not touched at all. The new arm appears in the next run
record beside `lexical`, measured against the same database and the same questions in the same pass,
is reported as ungated, and becomes a floor only when someone promotes it and names its metrics.
