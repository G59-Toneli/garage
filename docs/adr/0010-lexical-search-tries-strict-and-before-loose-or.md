# Lexical search tries strict AND, and falls back to OR only when AND found nothing

Full text ran `plainto_tsquery('portuguese', question)` against a corpus roughly half of which is
written in English. `plainto_tsquery` is a strict conjunction, so every token that survives the
dictionary chain becomes a mandatory term — and under the `portuguese` configuration, `what`, `is`,
`the`, `of`, `for` and `how` are not stop words. They survive as ordinary lexemes and are ANDed into
every query.

The result was a retriever that worked on keyword phrasings and did not work on questions. Over the
76 questions in `eval/facts.jsonl`, **42 returned nothing at all**: `recall@10` 0.447 overall, 0.912
on the 34 keyword questions and **0.071** on the 42 natural-language ones. Spec tables were the worst
case and the most valuable one — a row reads `| Cylinder head bolt, stage 1 | M11 | 41 |` and
contains no `for` and no `the`, so any English sentence aimed at 21 of the 53 chunks was unsatisfiable
by construction (issue #12).

We change two things and deliberately do not change a third.

**A project text search configuration, `garage_bi`** (`database.CREATE_TEXT_SEARCH_CONFIG`): a copy
of `portuguese` whose word mapping is `unaccent, garage_en_stop, garage_pt_stop, portuguese_stem`,
where the two stop word dictionaries are `simple` templates declared `ACCEPT = false`.

**A two-reading query** (`retrieval._LEXICAL_SCORED`): the strict conjunction is tried against the
corpus, and if it matches **zero rows**, the same lexemes are re-joined with `|` and the question is
asked again as a disjunction. One statement, one round trip, no parsing in Python.

**`WORD_SIMILARITY_FLOOR` stays at 0.6**, unmeasured, on purpose. See the last section.

## The measurements

The two changes are independent, so they are measured as a grid rather than as a list of variants:
either text search configuration, crossed with either query shape and with the fallback. Same fact
set, same weights, same `RRF_K`, same floor, same tie-break; the configuration is swapped by
recomputing the `tsvector` inline over the same 53 chunks. `avgret` is the mean number of candidates
returned per question, `empty` the number of questions returning nothing, and the last three columns
slice the 42 natural questions by the language of the question and by whether the target is a spec
table.

| configuration | query | r@10 | r@1 | mrr | keyword | natural | nat-pt | nat-en | nat-spec | empty | avgret |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `portuguese` | AND | 0.447 | 0.434 | 0.441 | 0.912 | 0.071 | 0.095 | 0.048 | 0.063 | 42 | 0.70 |
| `portuguese` | OR always | 0.842 | 0.618 | 0.677 | 1.000 | 0.714 | 0.571 | 0.857 | 0.625 | 2 | 7.04 |
| `portuguese` | AND then OR | 0.842 | 0.632 | 0.686 | 1.000 | 0.714 | 0.571 | 0.857 | 0.625 | 2 | 4.87 |
| `garage_bi` | AND | 0.500 | 0.487 | 0.493 | 0.971 | 0.119 | 0.095 | 0.143 | 0.125 | 38 | 0.75 |
| `garage_bi` | OR always | 0.868 | 0.697 | 0.743 | 1.000 | 0.762 | 0.571 | 0.952 | 0.813 | 3 | 6.68 |
| **`garage_bi`** | **AND then OR** | **0.868** | **0.711** | **0.752** | **1.000** | **0.762** | 0.571 | 0.952 | **0.813** | 3 | **4.24** |

(The first row is what shipped; `r@1` there is 0.434 in this grid and 0.428 in the harness, because
the harness's `recall@1` credits a fraction for facts with two correct chunks and this table counts
a top-one hit. Every other column agrees to four decimals with `eval run`.)

Decomposed, the recall is almost entirely one thing: **the disjunctive fallback is worth +0.643 of
natural recall on its own**, before any configuration change. `garage_bi` adds **+0.048** overall,
which sounds marginal and is not — it is concentrated where the issue said the damage was, worth
**+0.188** on `nat-spec` and **+0.095** on `nat-en`, because English function words are what a spec
table row does not contain. `unaccent` is worth **0.000** on these metrics today, for the reason
given below.

## Why not the two rejected candidates from the issue

**`websearch_to_tsquery`** is identical to `plainto_tsquery` on every metric in the table. It is
still a strict AND, and — the part every blog post about it gets wrong — it does **not** remove stop
words itself; the dictionary chain does. Under the `portuguese` configuration it produces
`'what' & 'is' & 'the' & 'cylind' & 'head' & 'bolt' & 'torqu'`, the same seven mandatory lexemes.
What it adds is *user syntax*: quotes, `or`, `-negation`. Nobody types those into a question box. It
is the option that costs zero and delivers zero.

**Stacking `portuguese_stem, english_stem`** cannot work, and the reason is structural rather than a
tuning problem. A dictionary chain stops at the first dictionary that recognises a token, and a
Snowball stemmer recognises everything it is handed. The second stemmer is unreachable code:
`to_tsvector` under such a chain returns `'running' 'tightened' 'hous'` — the English words
untouched, the Portuguese one stemmed, `english_stem` never consulted. Bilingual *stemming* is not
available from a dictionary chain at all. Bilingual *stop word removal* is, and the measurements say
the stop words were carrying the failure.

`ACCEPT = false` is the piece that makes even that much work, and it is easy to miss in the
documentation. Without it the `simple` template recognises every token, so `garage_en_stop` would
consume the whole corpus and `portuguese_stem` would never run — measured, and worth no gain at all.
With it, `simple` returns NULL for anything that is not in its stop word list, which passes the token
down the chain instead of ending it. The two dictionaries become pure filters.

## `unaccent` measures zero today and is included anyway

Every question in `eval/facts.jsonl` is spelled with its accents, so no metric in the grid can move
because of `unaccent`, and none does. It is in the configuration on a direct measurement instead of
an aggregate one:

```
plainto_tsquery('portuguese', 'cabecote')  ->  0 chunks
plainto_tsquery('garage_bi',  'cabecote')  ->  6 chunks
```

Brazilian workshop writing drops accents constantly — it is why `jargon._fold` exists — and until now
the claim was that the trigram arm covered it. It covers it *unreliably*, which is worse than not
covering it, because the failures are invisible. Whether trigram clears its 0.6 floor depends on how
much of the rest of the sentence matches rather than on the misspelt word:

| query | max `word_similarity` over the 53 chunks | clears the floor |
| --- | --- | --- |
| `cabecote plainado` | 0.714 | yes |
| `cabecote` | 0.500 | no |
| `torque do parafuso do cabeçote` | 0.357 | no |

One word, three outcomes, decided by sentence length. With `unaccent` all three are ordinary
full-text hits carrying a `lexical_rank`, and nothing depends on an unmeasured threshold.

Including it now costs nothing beyond an `INGEST_VERSION` bump this change was spending anyway.
Deferring it would cost a second bump and a second invalidated baseline, for a feature already known
to be needed.

## Why the fallback rather than simply OR-ing always, stated honestly

This is the weakest of the three decisions here and the grid is the reason to say so out loud. Once
`garage_bi` is in place, **AND-then-OR and OR-always retrieve exactly the same recall** — 0.868 at
ten, 0.762 natural, 1.000 keyword, identical on every language slice — and they abstain the same
number of times, three. Comparing the fallback against OR under the *old* configuration would show a
0.026 recall gap that is really the configuration's work, and that comparison would be dishonest.

What the fallback actually buys, measured, is precision and the top of the list:

- **`avgret` 4.24 against 6.68** — 36% fewer candidates per question for the same recall. Every one
  of those extra candidates is a chunk that shares one stemmed word with the question.
- **`r@1` 0.711 against 0.697** and **`mrr@10` 0.752 against 0.743**, because a one-word coincidence
  can outrank a real match once every term is optional.

Those are small numbers on 53 chunks, and the honest statement is that on *this* corpus the fallback
is a modest win rather than a decisive one. It is chosen because the direction of the trade does not
reverse with scale and the magnitude does: the set of chunks sharing one stemmed word with a
seven-word question is a fixed fraction of the corpus, so `avgret` under OR-always grows roughly with
corpus size while the fallback keeps the precise reading whenever the precise reading works — which
is all 34 keyword questions today. One CTE, no round trip, no Python. If a later measurement on a
larger corpus says the gap stayed this small, deleting the two CTEs is a change this ADR would
support.

**Two `tsvector` columns**, one `portuguese` and one `english`, was measured by the researcher who
scoped this issue and is **not** reproduced in the grid above — it needs a schema change to measure
and was rejected before the schema was written. Their numbers: `recall@10` 0.882, better than
either row above, with `recall@1` *falling* to 0.638, `nat-spec` falling to 0.688 against 0.813, and
**zero empty results in 76 questions**.

That last one is the disqualifying figure. Zero-cost abstention is not an incidental property of
`lexical`; it is the difference between `lexical` and `dense` that `retrieval.py` spends four
paragraphs on, it is what makes the free path in `app._answer` reachable, and #8 is built on it.
Trading the project's own thesis for 0.013 of recall — inside the baseline's own 0.013 noise floor —
is a bad deal, and it would also cost the ADR-0007 bump twice, since it is a second schema change.

## What this does not fix, stated as a limit rather than as future work

**A Portuguese question against English source text is not solvable in the lexical arm.** `nat-pt`
sits at **0.5714 in every variant in the grid that returns anything at all, without moving a thousandth**. Split by the language
of the target document, the chosen variant finds 9 of 11 Portuguese-source facts and 3 of 10
English-source ones, and the three are cognates (`torque`, `motor`) rather than translation. What
fails is Portuguese vocabulary with no cognate: *folga das válvulas*, *correia dentada*, *bobina de
ignição*. No tsquery parser invents that `flywheel` and `volante do motor` name the same part. That
belongs to issue #13 and to the dense arm.

The complementary shape is worth recording, because `hybrid` is supposed to exploit it: corrected
lexical reaches **0.952** on natural English questions, which beats dense at position one, and loses
badly on natural Portuguese ones. The two arms fail on different questions.

**Abstention gets weaker.** Empty results fall from 42 to 3. What survives is genuine — `how do I
replace the turbocharger wastegate actuator` still returns nothing, because no content word of it is
anywhere in the corpus — but the bar moved from "not every term matched" to "not one term matched",
and that is much easier to clear. `test_dense_retrieval.py` now exercises the surviving case
deliberately rather than inheriting a query that would pass under any retriever.

**Precision is not gated and now needs watching.** `avgret` went from 0.7 to 4.2 and not one of the
seven gated metrics observes it. At 53 chunks it does not matter; at 50,000 the same change could
return fifty candidates per question while `recall@10` climbs and the gate stays green. `eval run`
now records `precision@10` and `candidates@10` for every arm, ungated — in the record, watched by a
person, and available to gate when somebody can say what a bad value is.

## `WORD_SIMILARITY_FLOOR` is left alone, and that is a decision

0.6 was picked by eye and has no measurement behind it. It is **not** inert, which an earlier draft of
this ADR claimed on a figure that had been read too broadly: 0.357 is the maximum for one particular
query, not for the corpus. Measured across the 76 fact questions, **31 have at least one chunk above
the floor**, so it is deciding real outcomes.

The distribution is what argues for leaving it alone. The questions that clear it clear it
enormously — five reach 1.000, because a keyword question can be a literal substring of a spec row —
while the ones that would most benefit from a lower floor sit between 0.36 and 0.50. There is no
value in that gap that is obviously right, any number picked now would be picked to make an aggregate
look good, the gate would then defend it, and #13 changes the distribution underneath it. It moves
when there is a measurement, with a re-promoted baseline behind it.

## Consequences

- `INGEST_VERSION` goes to **2**. Chunk text is unchanged, but `chunks.tsv` is a stored generated
  column and the configuration behind it changed, so a version-1 database holds a `tsvector` this
  code would never produce (ADR-0007).
- Ingestion's DDL order is now load bearing: extensions, `DROP_SCHEMA`, `CREATE_TEXT_SEARCH_CONFIG`,
  `CREATE_SCHEMA`. `chunks.tsv` references the configuration by OID, so no other order works.
  `test_ingest.py` builds twice and asserts the column survives.
- The baseline is re-measured and deliberately re-promoted. `measurement()` diverges on purpose:
  `text_search_config` becomes `public.garage_bi` and `text_search_dictionaries` becomes
  `unaccent, garage_en_stop, garage_pt_stop, portuguese_stem`. `facts_sha256` and `sample_count` do
  not change, which is what makes the comparison a comparison.
- **A new reproducibility dependency, and it is the only one this change adds.** `unaccent` loads its
  fold table from `unaccent.rules` in the server's `$SHAREDIR/tsearch_data`, and the two stop word
  dictionaries load `english.stop` and `portuguese.stop` from the same place. The stored `tsvector`
  is therefore a function of files that neither `corpus_hash` nor `INGEST_VERSION` covers.
  `measurement()` *detects* a divergence, through `postgres_version` and `text_search_dictionaries`;
  it does not prevent one. Shipping our own rules file is the fix available the day two servers
  disagree. Nothing measured today says they do.
- **The committed showcase record is now stale in its `lexical` arm, and nothing refuses to serve
  it.** `eval/showcase/20260802T051801Z-44a5db93da69.json` holds rankings measured under the old
  query and carries `ingest_version: 1`, `text_search_config: pg_catalog.portuguese` in its own
  provenance. `verify_showcase_records` compares only `corpus_hash`, which did not change, so the
  record boots and the screen shows a `lexical` column that this build would not produce. Rebuilding
  it costs provider calls, which this change was not authorised to spend, so it is left in place and
  named here rather than quietly shipped. Two candidate fixes, neither taken today: extend the boot
  check to `ingest_version` (refuses to serve, which breaks the demo rather than correcting it), or
  re-run `showcase build` with the budget for it. The second is the right one.
- `docs/retrieval.md`'s signal table said full text missed `cabecote` and trigram rescued it. Both
  halves were false and both are corrected: `plainto_tsquery('portuguese', 'cabecote')` matched 0
  chunks, trigram never cleared its floor, and `garage_bi` matches 6. The module docstring of
  `retrieval.py` made the same claim and is corrected too.
- The documented example is now produced by `python -m garage docs capture` and checked byte for byte
  by `tests/test_capture.py`, which needs no database (issue #12, criterion two).
