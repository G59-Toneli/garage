"""The answer cache, and an argument about what "the same question" means.

A cache in front of a paid generator is ordinary. What is not ordinary — and is the whole reason this
file is long — is the key. Get the key wrong and the cache does not merely serve a stale answer: it
erases the axis the demo exists to demonstrate.

## The key is the thesis

ADR-0005 splits the system into build-time axes and runtime axes, and `contract` is the runtime axis
that carries the project's central claim. `free` exists *only* so a visitor can see what the citation
contract is buying. A cache keyed on the question alone would serve the `cited` answer to a `free`
request, the two columns would agree, and the one comparison the site was built to show would
disappear into a cache hit. The same is true of `strategy`: `lexical` and `dense` answering
identically is the failure mode of a benchmark, not a saving.

So every axis that changes what the system produces is in the key:

* `question` — normalised, see below
* `strategy`, `k`, `tiers`, `contract` — the four runtime axes of `QueryRequest`
* `corpus_hash` — a reingest of different material invalidates every answer over it (ADR-0002)
* `embedder` — two arms both labelled `dense` are two different retrievals (ADR-0005)
* `model` — a different provider model is a different answer at the same price of being wrong
* `git_sha` — the one that is easy to leave out and the one that bites

`git_sha` deserves its sentence. Without it, a deploy that changes the prompt, the citation
validator, or the way claims are split keeps serving answers produced by the previous build — and the
interface would show them beside a trace and a version stamp from the *new* build. That is a glass
box lying about its own contents. With it, every deploy starts with a cold cache, which costs a few
generations and buys the property that what is on screen was produced by the code that is running.

Nothing here is a performance optimisation dressed up as correctness. The cache exists to protect a
free tier's daily quota; every field above exists to stop it protecting the quota by answering a
different question.

## Normalisation, and where it stops

NFC, strip, collapse internal whitespace, casefold. Four operations, all of them about typing rather
than about meaning.

**Accents are deliberately not folded**, which is the opposite of what `jargon.fold` does, and the
asymmetry is intentional. That function decides whether two words are the same *term* for retrieval;
this one decides whether two visitors asked the same *question*. The corpus is Portuguese, the
baseline embedder is multilingual, and the request's `question` travels back out on the response and
is printed on screen. Serving the cached answer for "cabeçote" to somebody who typed "cabecote" would
echo back a question they did not ask, under a stamp saying it came from cache — a small dishonesty
in exactly the place this project claims not to have any. The raw string is preserved and returned;
only the normalised form reaches the hash.

No stemming, no stopword removal, no synonyms. Every one of those would be the cache deciding two
different questions are one, which is a retrieval judgement made in the wrong module.

## Size, eviction and staleness

512 entries, 24 hours. An entry holds an `Answer` and a trace — a few tens of kilobytes at worst — so
the ceiling is around 25 MB against the VM's 24 GB. Least-recently-used, checked lazily on read: a
sweeping thread would be a thread to supervise for a saving of nothing.

The TTL is not about correctness — the key already covers every input that can change the output — it
is about a visitor's reasonable expectation that a demo is not showing them yesterday. It is stated
on screen with the time the answer was generated, so nobody has to guess.
"""

from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

DEFAULT_MAX_ENTRIES = 512
DEFAULT_TTL_SECONDS = 24 * 60 * 60


def normalize_question(question: str) -> str:
    """The form that goes into the hash. Never the form that goes onto the screen.

    NFC first, so that a `ç` typed as one codepoint and a `ç` typed as `c` plus a combining cedilla
    are the same bytes — that is a keyboard difference, not a question difference, and it is invisible
    to a reader. Then whitespace, then case. See the module docstring for why accents survive.
    """
    return " ".join(unicodedata.normalize("NFC", question).casefold().split())


def cache_key(
    *,
    question: str,
    strategy: str,
    k: int,
    tiers: Sequence[str],
    contract: str,
    corpus_hash: str,
    embedder: str | None,
    model: str | None,
    git_sha: str,
) -> str:
    """sha256 over a canonical JSON object of every axis that changes the answer.

    Canonical: sorted keys, no whitespace, UTF-8, `ensure_ascii=False`. The same discipline the run
    and showcase records use for their bytes, and for the same reason — a key that depends on
    dictionary ordering is a key that changes when someone reorders a literal.

    `tiers` is sorted here rather than trusted. `["A","B"]` and `["B","A"]` select the same rows, so
    two visitors who ticked the same boxes in a different order must not pay twice.
    """
    payload = {
        "question": normalize_question(question),
        "strategy": strategy,
        "k": k,
        "tiers": sorted(tiers),
        "contract": contract,
        "corpus_hash": corpus_hash,
        "embedder": embedder,
        "model": model,
        "git_sha": git_sha,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedAnswer:
    """One stored response, whole, with the moment it was produced.

    `payload` is the *entire* set of `QueryResponse` fields the previous request produced — chunks,
    answer and trace together — and not just the generated answer. That is a deliberate choice over
    the obvious alternative, which was to cache the answer alone and re-run retrieval on a hit.

    Re-running retrieval sounds better because retrieval is free, and it is worse for one reason: the
    trace. A response assembled from a `retrieve` span measured now and a `generate` span measured
    four hours ago is a single trace describing two different events, and it would have to be
    stitched together by rewriting span parentage to look like one. This project renders the trace as
    evidence. Fabricating a coherent-looking one is exactly the thing it must not do. A cache hit is
    honestly a *copy of a previous response*, it is labelled as one on the wire and on screen, and it
    is internally consistent because it is unmodified.

    The type is `Any` and not a `QueryResponse`, because `app` imports this module and the arrow must
    not point back. The endpoint owns the shape; this owns the eviction.

    `stored_at` is not bookkeeping — it is displayed. "resposta em cache, gerada às 14:32" is the
    difference between a cache a visitor can see and a cache that quietly makes the site look faster
    than it is.
    """

    payload: Any
    stored_at: datetime


class AnswerCache:
    """An LRU with a TTL, guarded by a lock, and nothing else.

    The lock is for the same reason `limits.Limiter` has one: a `def` endpoint runs in a worker
    thread and `OrderedDict.move_to_end` during another thread's `popitem` is not defined behaviour.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.max_entries = max_entries
        self.ttl = timedelta(seconds=ttl_seconds)
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, CachedAnswer] = OrderedDict()
        # Counted rather than logged. They go into `origin_detail` so an operator reading a single
        # response can see whether the cache is doing anything, without shelling into the VM.
        self.hits = 0
        self.misses = 0

    def get(self, key: str, now: datetime) -> CachedAnswer | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if now - entry.stored_at >= self.ttl:
                # Expired entries are dropped on the read that found them. A hit is the only event
                # that proves anybody cares about this key, so it is the cheapest possible moment to
                # do the work, and it needs no thread.
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry

    def put(self, key: str, entry: CachedAnswer) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
            }
