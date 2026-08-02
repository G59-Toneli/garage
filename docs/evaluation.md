# The deterministic evaluation gate

Retrieval quality is a number, the number is committed, and the build fails when it falls:

```sh
docker compose up -d postgres
python -m garage ingest
python -m garage eval gate
```

```
strategy:    lexical  k=10
corpus_hash: 21c4e571b96fefae82062b11d1cdd0f237b0b311d781d8a11f975d8b650b75d6
facts:       47 (sha256 864b1703fbb6…)
  mrr@10             0.914894
  ndcg@10            0.914894
  recall@1           0.904255
  recall@10          0.914894
  recall@5           0.914894
  value_match_rate   0.914894
gate: pass
```

No language model, no network, no API key. The questions, the expected answers, the baseline and the
thresholds are all files in `eval/`, so the gate is a pure function of the checkout and the artifact
`ingest` just built. That is the *deterministic* half of
[ADR-0004](adr/0004-two-layer-evaluation.md): it asks only whether the right chunk came back, which
is a question with a written-down answer. Whether a generated answer *reads* correctly is the judge
layer's question, and no build should ever block on a model's opinion.

## The fact set

`eval/facts.jsonl`, one JSON object per line, written by hand against real chunk identifiers:

```jsonc
{"fact_id": "torque-volante-motor", "question": "flywheel bolt torque",
 "expected_value": "63", "chunk_ids": ["svc-kadett-1993#0006"], "tolerance": 0.5}
{"fact_id": "coroa-curta-demais", "question": "coroa curta demais motor bravo",
 "expected_value": "curta demais", "chunk_ids": ["forum-swap-250s#0011"]}
```

| Field | Meaning |
| ----- | ------- |
| `fact_id` | Lowercase slug, unique. It is how a regression names the question that broke. |
| `question` | Typed the way a person would type it, including the accents they would leave off. |
| `expected_value` | What a correct answer must contain. Scored separately from the ranking. |
| `chunk_ids` | The chunks that answer it. A list, and at least one. |
| `tolerance` | Present exactly when `expected_value` is a number. Its absence selects text matching. |

`chunk_ids` is a list even though most facts name one chunk, and the reason is not hypothetical: with
exactly one relevant chunk per question, nDCG is a monotone function of the reciprocal rank and
reports nothing MRR did not already say. Facts with two relevant chunks — both cylinder head stages
are M11 — are what make the third metric earn its place.

Every line is validated against a Pydantic model that forbids unknown fields, blank lines are
rejected rather than skipped, and errors are reported with line numbers, all of them at once. A
tolerant reader would let a fact be deleted by a stray keystroke and the suite quietly shrink, and a
suite that shrinks is the cheapest way there is to make a quality gate pass.

Before a single query runs, every `chunk_id` in the file is checked against the `chunks` table — and
again, every missing one is listed together. `chunk_id` is `<doc_id>#<ordinal:04d>` and positional, so
a chunking change renumbers it; a fact left pointing at the old number scores zero forever, looks
exactly like a retrieval regression, and would be blamed on the retriever for a week.

The committed fact set covers all five fixture documents and both tiers, and deliberately includes
questions the lexical strategy **cannot** answer — Portuguese questions against English source text,
and forum spelling the trigram floor will not reach. Four of the forty-seven miss entirely. They stay
in: a suite everything passes measures nothing, and those four are precisely where a dense or hybrid
retriever will first show its worth.

## The metrics

Binary relevance throughout — a chunk either answers the question or it does not — macro-averaged, so
every question weighs the same whatever its document or tier. All of them come from a **single**
retrieval per question at `k = 10`; nothing is re-queried per depth and nothing is re-sorted in
Python, because the SQL already made the order total (`ORDER BY score DESC, chunk_id`).

- **`recall@1`, `recall@5`, `recall@10`** — `|top-k ∩ relevant| / |relevant|`. Three depths because
  they answer different questions: whether the top hit is usable on its own, and whether a reader
  scrolling the panel would find it at all.
- **`mrr@10`** — `1 / rank` of the first relevant chunk, 1-based, and **zero when there is no hit in
  the top ten**. Named with the depth because it is truncated. A miss is never skipped: averaging
  over the questions that still work would let a change that stops answering the hard ones report a
  *higher* MRR than the version that answered them at rank 9.
- **`ndcg@10`** — `DCG / IDCG` with `DCG = Σ rel_i / log2(i+1)` and the ideal summed over
  `min(|relevant|, k)` positions. Under binary relevance the two textbook gain formulations agree —
  `rel` and `2^rel − 1` are both 0 for 0 and both 1 for 1 — so there is no choice being made. Zero by
  explicit convention if the ideal is zero, which `Fact` already forbids.
- **`value_match_rate`** — a *separate* measurement, never averaged into the three above. With a
  `tolerance` the numbers are extracted from the retrieved text and compared numerically, because
  `"63" in text` is true of `163`, of `0.63` and of `Section 6.3`. Without one the value is matched as
  text, accent- and case-insensitively, by the same folding ingestion used to detect Jargon.

`value_match_rate` is a necessary condition for a grounded answer, not a sufficient one: the value has
to be somewhere in the top-k context, but it may well sit in a chunk that does not answer the
question. Read it alongside recall, never instead of it. It is reported but **not gated**, for that
reason — gating a proxy invites tuning the proxy.

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
  "run_record_version": 1,
  "run_id": "20260802T004849Z-0823b0f37c94",
  "started_at": "2026-08-02T00:48:49Z",
  "duration_ms": 812,
  "layer": "deterministic",
  "suite": "facts",
  "provenance": {
    "git_sha": "0823b0f37c94…", "git_dirty": true,
    "corpus_id": "fixture", "corpus_hash": "21c4e571…", "ingest_version": 1,
    "python_version": "3.12.13", "platform": "Windows-11-…"
  },
  "configuration": {"strategy": "lexical", "k": 10, "tiers": ["A", "B"],
                    "reranker": null, "embedder": null},
  "sample_count": 47,
  "facts_sha256": "864b1703fbb6…",
  "metrics": {"recall@1": 0.904255, "mrr@10": 0.914894, "…": 0.0},
  "per_item": [
    {"fact_id": "torque-volante-motor", "question": "flywheel bolt torque",
     "expected_chunk_ids": ["svc-kadett-1993#0006"], "expected_value": "63",
     "hit_rank": 1, "reciprocal_rank": 1.0, "ndcg": 1.0, "value_matched": true,
     "retrieved_chunk_ids": ["svc-kadett-1993#0006"]}
  ]
}
```

`verify_artifact` runs before anything is measured and before anything is written, and the
`corpus_hash` and `ingest_version` in the record come from the `Artifact` the **database** returned,
not from the manifest in the checkout. The manifest is what we expected; the artifact is what we
measured ([ADR-0002](adr/0002-database-as-derived-artifact.md)). Both numbers are cited together or
not at all ([ADR-0007](adr/0007-corpus-hash-and-ingest-version-are-separate.md)): the first says which
material, the second says which rules turned it into the chunks the facts name, and the same Corpus
rechunked produces different `chunk_id`s.

`facts_sha256` is what makes a moved number attributable. Without it, "recall fell from 0.91 to 0.78"
is ambiguous between *retrieval got worse* and *someone added eight hard questions*, and those two
call for opposite responses.

`run_record_version` is `Literal[1]`. A record written by a future version fails to load rather than
loading partially, exactly like `manifest_version`.

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
  "baseline_version": 1,
  "run_id": "20260802T004849Z-0823b0f37c94",
  "configuration": { /* must match the run exactly */ },
  "sample_count": 47,
  "facts_sha256": "864b1703fbb6…",
  "metrics": { /* copied from the record */ },
  "gated_metrics": ["mrr@10", "ndcg@10", "recall@1", "recall@10", "recall@5"],
  "tolerance": 0.0,
  "noise_floor": 0.01
}
```

`gated_metrics` is explicit rather than "everything present", so a new measurement can be reported and
watched for a while before it is allowed to fail anyone's build.

**`tolerance` is a policy number, not a statistical one.** Nothing in this pipeline is random: the
same commit against the same artifact produces bit-identical metrics, so there is no variance to
estimate and no confidence interval to derive. The tolerance says how much quality a human is willing
to wave through on a change that buys something elsewhere. It is `0.0` today — any real loss is a real
loss — and it lives in a committed file rather than an environment variable precisely so that raising
it is a reviewable act rather than a CI setting somebody changed.

`noise_floor` is the other side: an improvement smaller than this is not worth a promotion commit. At
`0.01` it sits just under the `1/47 ≈ 0.021` that a single question is worth, so every real
improvement is reported and nothing else is.

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
anyway. A baseline measured at `k=10` says nothing about a run at `k=5`; a baseline measured on thirty
questions says nothing about a run on forty-seven. Answering "did it get worse?" across either is not
a conservative approximation, it is a wrong answer.

| Failure | Why it is fatal |
| ------- | --------------- |
| `facts_sha256` differs from the baseline | The questions changed. Promote deliberately. |
| `configuration` differs from the baseline | Different measurement, not a worse one. |
| `sample_count` below the baseline | Losing questions raises every macro-average it touches. |
| A gated metric regressed by more than `tolerance` | The thing this gate exists for. |
| A gated metric is missing from the run | A metric cannot be retired by deleting it. |
| A `chunk_id` in the fact set is not in the database | Stale facts read as a retrieval regression. |
| `verify_artifact` failed | Measuring a database that is not this commit's artifact. |
| The baseline's `run_id` is not in `eval/runs/` | A baseline must point at something readable. |
| The newest run record does not match this build | Below. |

An improvement never fails a build. It prints the positive delta and a reminder to promote, because a
baseline nobody promotes stops being a floor and becomes a memory.

A regression also prints the questions that moved, read off `per_item` in both records:

```
questions that moved: 2
  torque-volante-motor: rank 1 -> none
  coroa-curta-demais: rank 2 -> 7
```

That line is what makes this a debugging tool rather than a red light. "recall@1 fell by 0.04" is
unactionable; a named `fact_id` is a query you can paste into `/query` and watch.

## Why CI re-measures the committed record

Run records accumulate in the repository because a developer generates one and commits it alongside
the change that moved it. CI then re-runs the evaluation and asserts the newest record in the tree
still describes this build. That inverts what a record is: not a by-product of whichever machine
happened to run CI, but a **reproducible assertion** checked into the repository, in the same spirit
as the database being a derived artifact ([ADR-0002](adr/0002-database-as-derived-artifact.md)).

```
the newest run record in the tree (20260802T004849Z-0823b0f37c94.json) does not match what this
build measures. It was committed against a different corpus, Configuration or retrieval behaviour.
```

The comparison is over the *measurement*, and three groups of fields are excluded:

- `run_id`, `started_at`, `duration_ms` — a clock and a stopwatch, different by definition.
- `python_version`, `platform` — the gate runs on a developer's laptop and on CI's Ubuntu. Requiring
  these to match would assert the two are the same machine, which is the opposite of what
  reproducibility means. They stay in the record because they are exactly what you want to read when
  a number *does* differ.
- `git_sha`, `git_dirty` — a record cannot name the commit that contains it. It is generated, then
  committed, so its sha is always its parent's. Requiring a match would make the check unsatisfiable
  by construction rather than strict.

What is left — corpus, chunking rules, Configuration, questions, and what came back for each one —
must be identical. If it is not, the record in the tree describes a build that no longer exists, and
the fix is one command:

```sh
python -m garage eval run && git add eval/runs
```
