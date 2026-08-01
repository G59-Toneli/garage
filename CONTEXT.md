# Garage

Garage answers questions about a single car — a 1993 Chevrolet Kadett GSi — and shows its own
work: the sources it retrieved, the scores they got, and how the answer changes as the retrieval
pipeline changes. The system exists to make the quality of an AI feature measurable and auditable,
not to be a chatbot.

Domain vocabulary is Brazilian Portuguese where the vocabulary *is* the subject matter (workshop
jargon, part names). Everything else is in English.

## Language

### Sources

**Corpus**:
The versioned, immutable set of source material a given answer was derived from, identified by a
hash. There is exactly one corpus per version; it is never edited in place.
_Avoid_: knowledge base, database, dataset

**Tier A Source**:
A technical source whose claims are checkable against a named publication — service manual, owner's
manual, parts catalogue, published specification sheet.
_Avoid_: official source, trusted source

**Tier B Source**:
A community source — forum thread, blog post, group discussion. Carries real knowledge that exists
nowhere else, with lower authority than Tier A.
_Avoid_: unreliable source, unofficial source

**Jargon**:
Workshop or enthusiast vocabulary absent from formal text ("swap 250-S", "projetinho de rua"). The
gap between how a person asks and how a manual writes is the problem the system exists to close.
_Avoid_: slang, colloquialism

### Questions and answers

**Fact**:
A claim with one exact, checkable value — a torque figure, a clearance, a part number. Either the
answer matches the source or it does not.
_Avoid_: data point, spec

**Recipe**:
A procedure or recommendation with no single correct answer ("how do I build a street setup for a
Kadett?"). Judged on grounding and attribution, never on matching an expected value.
_Avoid_: opinion, advice

**Abstention**:
Correctly declining to answer because the corpus does not cover the question. A first-class success,
not a failure.
_Avoid_: refusal, no-answer, miss

### Measurement

**Configuration**:
One concrete combination of pipeline choices — retrieval strategy, embedder, reranker, tier filter,
prompt contract — used to produce an answer.
_Avoid_: model, mode, setup

**Preset**:
A named Configuration curated for the demo, so a visitor can compare meaningful alternatives without
understanding the axes.
_Avoid_: profile, option

**Run Record**:
The artifact produced by executing an evaluation: metrics plus the provenance needed to reproduce
them (corpus hash, commit, model identity, sample count). Always generated, never written by hand.
_Avoid_: result, report, score

**Judge**:
A language model that grades answers a Fact cannot grade — grounding, citation accuracy, abstention.
Its verdicts count only once its agreement with human labels has been measured and published.
_Avoid_: evaluator, grader

**Glass Box**:
The product stance that every claim the system makes about itself is inspectable in the interface —
retrieved chunks, scores, timings, cost, and the run record behind every published number.
_Avoid_: transparency, explainability
