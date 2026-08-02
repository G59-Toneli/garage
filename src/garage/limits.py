"""Three rate limiters, in memory, over an injected clock — and the reason there is no Redis.

This module exists because the public deployment (issue #11) puts a paid provider's free tier behind
a URL anyone can open. Everything here is about *one* scarce resource: a Gemini generation. Retrieval
is deliberately not limited at all — it is local, it costs nothing, it runs in about eleven
milliseconds, and it is the thing this project actually wants a visitor to do. Limiting the free part
would punish the visitor for using the product.

## Why three and not one

| limiter                | key                     | protects                                          |
|------------------------|-------------------------|---------------------------------------------------|
| per IP, per minute     | client address          | trivial abuse — a loop, a crawler, an open tab     |
| per IP, per UTC day    | client address          | one visitor quietly consuming the whole day's quota |
| global, per UTC day    | nothing                 | the free tier's own daily ceiling                 |

A single limiter cannot express those. A global one alone lets the first visitor of the day spend
everything; a per-IP one alone lets fifty visitors spend more than the provider will sell.

## Why in memory, and what that costs

One instance, one process, one artifact (ADR-0006). A Redis would be a second service to run, back
up and reason about, on a VM whose entire budget for this is a few hundred megabytes — and ADR-0001
refuses "hardened production" infrastructure by name. Persisting to Postgres would be worse in a
different way: nothing writes to that database at runtime today (ADR-0002, and the schema enforces
it), so a counter table would change the character of the serving process from read-only to
read-write in order to hold a number that is already backstopped.

**The counters reset on restart, and that is accepted rather than overlooked.** A deploy or a crash
hands the day's budget back. The real backstop is the provider's own 429, which
`app._answer` already converts into a degradation with the chunks intact — so the worst case of a
lost counter is that a few requests discover the ceiling from Google instead of from us, which is
exactly the behaviour the cascade was built to survive.

**Tracked addresses are capped and evicted least-recently-seen.** An unbounded dictionary keyed on
client address is a memory leak with a remote trigger. Eviction has a real consequence, stated here
rather than discovered later: an evicted address gets its daily generation count back. That is
tolerable because the *global* budget is not keyed on anything and cannot be evicted, so the ceiling
the provider actually bills against still holds; the per-IP daily limit is a fairness measure, not a
cost control.

## Why a class with a lock rather than free functions

FastAPI runs a `def` endpoint in a worker thread, so two requests genuinely execute
`admit_generation` at the same time. Check-then-increment without a mutex is the textbook way to
serve 201 generations against a budget of 200. The lock is held for a few microseconds around
arithmetic and never across I/O.

Everything below takes `now` as an argument. There is no `time.time()` call in this module, which is
what makes a day boundary, a bucket refill and an eviction testable as pure arithmetic rather than
as a test that sleeps.

## Zero means two different things, on purpose

`requests_per_minute=0` **disables** the anti-abuse bucket. `generations_per_day_per_client=0` and
`generation_budget_per_day=0` **refuse everything**.

The asymmetry is deliberate and is written here because it is the kind of thing that reads like an
inconsistency until you need both. A bucket of zero requests a minute is not a policy anybody wants —
it would make the service answer nothing at all — so the value is free to mean "no bucket", which is
what the test suite and a private deployment want. A generation budget of zero *is* a policy somebody
wants: it is exactly how an operator runs this site with retrieval and the precomputed showcase and
no provider spend, and it is how `tests/test_cascade.py` reaches the degraded branch. Making that
mean "unlimited" would turn the safest possible configuration into the most expensive one.

`.env.example` and `docs/deploy.md` say the same thing, because whoever sets these values is reading
those and not this.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

# The anti-abuse bucket. Ten requests a minute is generous for a human reading a two-column
# comparison and immediately uncomfortable for a script. It is a *token bucket* rather than a fixed
# window so that a visitor who opens the page and fires two parallel `POST /query` calls — which is
# exactly what `main.js` does, one per strategy — is not spending a window's worth of allowance on
# one comparison.
DEFAULT_REQUESTS_PER_MINUTE = 10

# Generations, not requests. Sixty a day is roughly six comparisons an hour for eight hours from one
# address, which is more than any honest visitor will do and far less than the global ceiling.
DEFAULT_GENERATIONS_PER_DAY_PER_CLIENT = 60

# The free tier publishes something near 250 requests per day for `gemini-2.5-flash`. Two hundred
# leaves headroom for the operator's own `showcase build`, for retries, and for the fact that the
# provider's day boundary is not necessarily ours. Configurable, because the tier's number is not
# ours to promise and has changed before.
DEFAULT_GENERATION_BUDGET_PER_DAY = 200

# How many client addresses the per-IP limiters remember. Each entry is a float, an int and a date
# string; 4096 of them is tens of kilobytes. See the module docstring for what eviction costs.
MAX_TRACKED_CLIENTS = 4096

RefusalReason = Literal["client_daily", "global_daily", "no_generator"]


@dataclass(frozen=True)
class RequestDecision:
    """The anti-abuse verdict, and the only place in this system a 429 comes from.

    A refusal here is genuinely "you are asking too fast", which is the one situation where the
    correct answer is an HTTP error with a `Retry-After` — nothing was going to be produced anyway.
    Every *other* refusal in this module degrades instead, because a visitor who is over the
    generation budget can still be served retrieval, which is free and is the product.
    """

    allowed: bool
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class GenerationDecision:
    """Whether this request may spend a generation, and — when not — why, in machine-readable form.

    `reason` is a `Literal` and not a sentence. The sentence a visitor reads is assembled by the
    endpoint in the page's language; a limiter that owned the prose would be a limiter that has to be
    edited to translate the site.

    `remaining_global` travels on the allowed decision as well as the refused one, so the endpoint can
    put "3 gerações restantes hoje" on screen before the budget runs out rather than only after.
    """

    allowed: bool
    reason: RefusalReason | None = None
    remaining_client: int = 0
    remaining_global: int = 0


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


def utc_day(now: datetime) -> str:
    """The day a moment belongs to, in UTC, as a sortable string.

    UTC and never local time. The VM's timezone is one more thing that can differ between the
    author's machine, CI and Oracle, and a budget that rolls over at a different instant in each
    place is a budget nobody can reason about. It is also the boundary the provider's own quota is
    documented against.
    """
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


class Limiter:
    """The three limiters, sharing one lock and one eviction policy.

    Constructed once per process, in `create_app`, and read through a FastAPI dependency on the
    endpoint. Deliberately **not** an ASGI middleware: the decision depends on `contract` and
    `strategy` out of an already-validated body and on whether this particular request is going to
    reach a generator at all. A middleware sees bytes, and would have to either parse the body a
    second time or limit requests that were never going to cost anything.
    """

    def __init__(
        self,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        generations_per_day_per_client: int = DEFAULT_GENERATIONS_PER_DAY_PER_CLIENT,
        generation_budget_per_day: int = DEFAULT_GENERATION_BUDGET_PER_DAY,
        max_tracked_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self.requests_per_minute = requests_per_minute
        self.generations_per_day_per_client = generations_per_day_per_client
        self.generation_budget_per_day = generation_budget_per_day
        self.max_tracked_clients = max_tracked_clients
        self._lock = threading.Lock()
        # `OrderedDict` rather than `dict` because the eviction policy needs `move_to_end` and
        # `popitem(last=False)`. Insertion order alone would evict the oldest *first seen* address,
        # which is the wrong one: a visitor who has been reading the site for an hour would be
        # forgotten before a bot that arrived a minute ago.
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._client_days: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._global_day: str = ""
        self._global_used: int = 0

    # --- the anti-abuse bucket -------------------------------------------------------------------

    def admit_request(self, client: str, now: datetime) -> RequestDecision:
        """One token per request, refilled continuously. Refusal is a 429 and nothing else."""
        capacity = float(self.requests_per_minute)
        if capacity <= 0:
            # Zero means "no anti-abuse bucket", which is what the test suite and a private
            # deployment want. Spelled as a disabling value rather than as a separate flag, so there
            # is one number to read instead of a number and a boolean that can disagree.
            #
            # **This is the opposite of what zero means for the two generation budgets**, where it
            # refuses everything. See the module docstring: a bucket of zero requests a minute is a
            # policy nobody wants, while a budget of zero generations is a policy several people
            # want — it is how you run this site with no provider spend at all.
            return RequestDecision(allowed=True)
        seconds = now.timestamp()
        refill_per_second = capacity / 60.0
        with self._lock:
            bucket = self._buckets.get(client)
            if bucket is None:
                bucket = _Bucket(tokens=capacity, updated_at=seconds)
                self._buckets[client] = bucket
            else:
                elapsed = max(0.0, seconds - bucket.updated_at)
                bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_second)
                bucket.updated_at = seconds
            self._buckets.move_to_end(client)
            self._evict(self._buckets)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return RequestDecision(allowed=True)
            # Rounded up and floored at one: `Retry-After: 0` invites an immediate retry, which is
            # the behaviour that got the client here.
            wait = (1.0 - bucket.tokens) / refill_per_second
            return RequestDecision(allowed=False, retry_after_seconds=max(1, int(wait) + 1))

    # --- the two generation budgets --------------------------------------------------------------

    def admit_generation(self, client: str, now: datetime) -> GenerationDecision:
        """Check *and* consume, atomically, or consume nothing at all.

        Two counters move together or neither moves. Checking both and then incrementing both would
        leave a window where the global budget was spent on a client that turned out to be over its
        own limit — a generation debited against a request that was refused.

        Either limit set to **zero refuses every generation**, which is the opposite of what zero
        does to the anti-abuse bucket and is argued in the module docstring. It is a supported
        configuration, not a footgun: it is how this site runs with retrieval and the precomputed
        showcase and no provider spend.

        The client counter is checked first, so a single hammering address is reported as the client
        limit rather than as the global one. The distinction matters on screen: "you have used your
        share for today" and "the site has used its share for today" are different messages and the
        second one is not the visitor's fault.
        """
        day = utc_day(now)
        with self._lock:
            if self._global_day != day:
                self._global_day = day
                self._global_used = 0

            stored_day, used = self._client_days.get(client, (day, 0))
            if stored_day != day:
                used = 0

            client_limit = self.generations_per_day_per_client
            global_limit = self.generation_budget_per_day
            remaining_client = max(0, client_limit - used)
            remaining_global = max(0, global_limit - self._global_used)

            if remaining_client <= 0:
                return GenerationDecision(
                    allowed=False,
                    reason="client_daily",
                    remaining_client=0,
                    remaining_global=remaining_global,
                )
            if remaining_global <= 0:
                return GenerationDecision(
                    allowed=False,
                    reason="global_daily",
                    remaining_client=remaining_client,
                    remaining_global=0,
                )

            self._client_days[client] = (day, used + 1)
            self._client_days.move_to_end(client)
            self._evict(self._client_days)
            self._global_used += 1
            return GenerationDecision(
                allowed=True,
                remaining_client=remaining_client - 1,
                remaining_global=remaining_global - 1,
            )

    def refund_generation(self, client: str, now: datetime) -> None:
        """Give back a generation that was admitted and then never attempted.

        There is exactly one caller and it is narrow: the endpoint admits before it knows whether the
        retriever will return any candidates, and the zero-cost abstention asks nobody. A budget that
        counted those would spend the day's quota on questions the corpus does not cover — which are
        precisely the questions that are supposed to be free.

        Refunds are floored at zero rather than trusted, because a refund that arrives twice is a
        bug that must not manufacture budget.
        """
        day = utc_day(now)
        with self._lock:
            if self._global_day == day and self._global_used > 0:
                self._global_used -= 1
            stored_day, used = self._client_days.get(client, (day, 0))
            if stored_day == day and used > 0:
                self._client_days[client] = (day, used - 1)

    # --- what the endpoint reports -----------------------------------------------------------------

    def snapshot(self, now: datetime) -> dict[str, int | str]:
        """The budget as a fact, for `origin_detail` and for an operator reading a response.

        Published on every response rather than only on a refusal. A visitor who can watch the number
        fall is a visitor who is not surprised when it reaches zero, and the whole design of this
        deployment is that the failure modes are visible before they happen.
        """
        day = utc_day(now)
        with self._lock:
            used = self._global_used if self._global_day == day else 0
            return {
                "utc_day": day,
                "generation_budget": self.generation_budget_per_day,
                "generations_used": used,
                "generations_remaining": max(0, self.generation_budget_per_day - used),
            }

    def _evict(self, tracked: OrderedDict[str, object]) -> None:
        # Called with the lock held, always. Bounded by a `while` rather than an `if` because the cap
        # is configurable and can be lowered by a restart with a smaller value.
        while len(tracked) > self.max_tracked_clients:
            tracked.popitem(last=False)


def client_address(forwarded_for: str | None, peer: str | None, *, trust_forwarded_for: bool) -> str:
    """Who to key the per-IP limiters on, and the one place the proxy question is answered.

    Behind Caddy every request arrives from the container network, so the peer address is the same
    for everybody and per-IP limiting would silently become a second global limit. `X-Forwarded-For`
    is the answer — but only when something trustworthy is setting it. Direct on the public internet,
    that header is a string the client chose, so honouring it would let one address present itself as
    a new visitor on every request and walk straight through both per-IP limiters.

    So it is a deployment setting, defaulting to *off*, and the production overlay is where it is
    turned on — beside the Caddy that makes it true.

    The **last** element is taken, not the first. Caddy appends the immediate peer to whatever the
    client sent, so with exactly one trusted hop the rightmost entry is the address Caddy actually
    accepted the connection from and every entry to its left is client-supplied text. Taking the
    first is the common mistake and is precisely the spoofable one.

    A missing peer — which `TestClient` produces — becomes `"unknown"` rather than an exception. Every
    such request shares one bucket, which is the conservative direction.
    """
    if trust_forwarded_for and forwarded_for:
        parts = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if parts:
            return parts[-1]
    return peer or "unknown"
