"""The cache key, one test per axis, because the key is the thesis and not a detail.

`docs/deploy.md` and `garage/cache.py` both argue it at length: a cache keyed on the question alone
would serve the `cited` answer to a `free` request and delete the one comparison ADR-0005 exists to
show. So every axis gets its own test, and each one asserts the same thing in a different dimension —
change it, get a different key.

The tests are deliberately written as "changing X is a miss" rather than as a golden digest. A golden
digest would pin the *hash function*, which nobody promised, and would have to be rewritten for a
change that is purely cosmetic. What is promised is that these nine inputs are distinguishing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from garage.cache import AnswerCache, CachedAnswer, cache_key, normalize_question

NOON = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

BASE = dict(
    question="torque do cabeçote",
    strategy="lexical",
    k=10,
    tiers=["A", "B"],
    contract="cited",
    corpus_hash="a" * 64,
    embedder=None,
    model="gemini-2.5-flash",
    git_sha="c0ffee",
)


@pytest.mark.parametrize(
    "field, other",
    [
        ("question", "torque do virabrequim"),
        # The two that carry the project's argument. `strategy` is the retrieval axis and `contract`
        # is the citation axis; a cache that collapsed either would make the demo agree with itself.
        ("strategy", "dense"),
        ("contract", "free"),
        ("k", 5),
        ("tiers", ["A"]),
        # The build-time identity. A reingest of different material invalidates every answer over it
        # (ADR-0002), and two arms both labelled `dense` are two different retrievals (ADR-0005).
        ("corpus_hash", "b" * 64),
        ("embedder", "baseline@abc123"),
        ("model", "gemini-2.5-pro"),
        # The one that is easy to leave out. Without it a deploy that changes the prompt keeps
        # serving the previous build's answers under this build's version stamp.
        ("git_sha", "deadbeef"),
    ],
)
def test_every_axis_of_the_key_changes_the_key(field, other):
    assert cache_key(**BASE) != cache_key(**{**BASE, field: other})


def test_the_same_request_twice_is_the_same_key():
    assert cache_key(**BASE) == cache_key(**dict(BASE))


def test_tier_order_is_not_an_axis():
    """`["A","B"]` and `["B","A"]` select the same rows; two visitors must not pay twice."""
    assert cache_key(**BASE) == cache_key(**{**BASE, "tiers": ["B", "A"]})


# --- normalisation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    [
        "torque do cabeçote",
        "  torque do cabeçote  ",
        "torque   do\tcabeçote",
        "TORQUE DO CABEÇOTE",
        "Torque Do Cabeçote",
        # NFD: the `ç` below is genuinely `c` + U+0327 COMBINING CEDILLA, two codepoints, and
        # renders identically to the line above. That is the whole point — it is a keyboard
        # difference, invisible to a reader, and it must not be a cache miss. Check the bytes,
        # not the glyph, if this line ever looks like a duplicate.
        "torque do cabeçote",
    ],
)
def test_typing_differences_are_the_same_question(typed):
    assert cache_key(**{**BASE, "question": typed}) == cache_key(**BASE)


def test_accents_are_not_folded_and_that_is_the_point():
    """The deliberate asymmetry with `jargon.fold`, asserted so nobody "fixes" it.

    That function decides whether two words are the same *term* for retrieval. This one decides
    whether two people asked the same *question* — and the question travels back out on the response
    and is printed on screen. Serving the "cabeçote" answer to somebody who typed "cabecote" would
    echo back words they did not write, under a stamp saying it came from cache.
    """
    assert cache_key(**{**BASE, "question": "torque do cabecote"}) != cache_key(**BASE)


def test_normalisation_does_no_stemming_and_no_stopword_removal():
    """Deciding two different questions are one is a retrieval judgement in the wrong module."""
    assert normalize_question("o torque dos cabeçotes") == "o torque dos cabeçotes"
    assert normalize_question("torque cabeçote") != normalize_question("torques cabeçotes")


# --- the store ---------------------------------------------------------------------------------------


def test_a_hit_returns_what_was_stored_and_counts_itself():
    cache = AnswerCache()
    cache.put("k", CachedAnswer(payload="body", stored_at=NOON))
    assert cache.get("k", NOON).payload == "body"
    assert cache.get("missing", NOON) is None
    assert (cache.hits, cache.misses) == (1, 1)


def test_an_expired_entry_is_a_miss_and_is_dropped_on_the_read_that_found_it():
    cache = AnswerCache(ttl_seconds=60)
    cache.put("k", CachedAnswer(payload="body", stored_at=NOON))
    assert cache.get("k", NOON + timedelta(seconds=59)) is not None
    assert cache.get("k", NOON + timedelta(seconds=60)) is None
    # Dropped, not merely hidden: no sweeping thread exists and none is wanted.
    assert len(cache) == 0


def test_eviction_is_least_recently_used_and_not_least_recently_written():
    cache = AnswerCache(max_entries=2)
    cache.put("a", CachedAnswer(payload=1, stored_at=NOON))
    cache.put("b", CachedAnswer(payload=2, stored_at=NOON))
    cache.get("a", NOON)  # `a` is now the most recently used
    cache.put("c", CachedAnswer(payload=3, stored_at=NOON))
    assert cache.get("b", NOON) is None
    assert cache.get("a", NOON) is not None
    assert cache.get("c", NOON) is not None


def test_the_ceiling_holds_under_more_writes_than_it_can_hold():
    cache = AnswerCache(max_entries=8)
    for index in range(200):
        cache.put(str(index), CachedAnswer(payload=index, stored_at=NOON))
    assert len(cache) == 8
