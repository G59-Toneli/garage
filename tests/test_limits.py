"""The three limiters, as arithmetic over a clock the test owns.

Not one of these sleeps. `limits` takes `now` as an argument precisely so that a token bucket, a UTC
day boundary and an eviction can be asserted exactly rather than approximately — a rate-limit test
that sleeps is slow, flaky under load, and cannot express "at 23:59:59.9" at all.
"""

from datetime import datetime, timedelta, timezone

import pytest

from garage.limits import Limiter, client_address, utc_day

NOON = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return NOON + timedelta(seconds=seconds)


# --- the anti-abuse bucket -----------------------------------------------------------------------


def test_the_bucket_admits_its_capacity_and_then_refuses():
    limiter = Limiter(requests_per_minute=3)
    assert [limiter.admit_request("1.2.3.4", at(0)).allowed for _ in range(3)] == [True] * 3

    refused = limiter.admit_request("1.2.3.4", at(0))
    assert refused.allowed is False
    # Never zero. `Retry-After: 0` invites the immediate retry that caused the refusal.
    assert refused.retry_after_seconds >= 1


def test_the_bucket_refills_continuously_rather_than_in_windows():
    """A token bucket and not a fixed window, and the difference is visible on this page.

    `main.js` fires two parallel `POST /query` calls per comparison, one per strategy. Under a fixed
    window a visitor who compared twice in the last second of a window would be refused for the
    whole of the next one; under a bucket they wait for one token.
    """
    limiter = Limiter(requests_per_minute=6)  # one token every ten seconds
    for _ in range(6):
        assert limiter.admit_request("ip", at(0)).allowed
    assert limiter.admit_request("ip", at(5)).allowed is False
    assert limiter.admit_request("ip", at(10)).allowed is True


def test_the_bucket_never_refills_above_its_capacity():
    limiter = Limiter(requests_per_minute=2)
    limiter.admit_request("ip", at(0))
    # An hour of idleness must not bank an hour of requests.
    assert [limiter.admit_request("ip", at(3600 + n)).allowed for n in range(4)] == [
        True,
        True,
        False,
        False,
    ]


def test_the_buckets_are_per_address():
    limiter = Limiter(requests_per_minute=1)
    assert limiter.admit_request("a", at(0)).allowed
    assert limiter.admit_request("a", at(0)).allowed is False
    assert limiter.admit_request("b", at(0)).allowed is True


def test_zero_disables_the_bucket_rather_than_refusing_everything():
    """A disabling value, not a trap. The test suite and a private deployment both want this."""
    limiter = Limiter(requests_per_minute=0)
    assert all(limiter.admit_request("ip", at(0)).allowed for _ in range(50))


# --- the two generation budgets ------------------------------------------------------------------


def test_the_client_daily_limit_is_reported_as_the_client_limit():
    limiter = Limiter(generations_per_day_per_client=2, generation_budget_per_day=100)
    assert limiter.admit_generation("ip", NOON).allowed
    assert limiter.admit_generation("ip", NOON).allowed
    refused = limiter.admit_generation("ip", NOON)
    # The distinction is on screen: "you have used your share" and "the site has used its share" are
    # different sentences and the second one is not the visitor's fault.
    assert (refused.allowed, refused.reason) == (False, "client_daily")
    # And it is per address, so one visitor cannot lock out the next.
    assert limiter.admit_generation("other", NOON).allowed


def test_the_global_budget_is_the_ceiling_no_matter_how_many_addresses():
    limiter = Limiter(generations_per_day_per_client=100, generation_budget_per_day=3)
    for index in range(3):
        assert limiter.admit_generation(f"ip-{index}", NOON).allowed
    refused = limiter.admit_generation("ip-99", NOON)
    assert (refused.allowed, refused.reason) == (False, "global_daily")


def test_a_refused_client_never_debits_the_global_budget():
    """Check and consume are one operation, or a refused request spends the site's quota."""
    limiter = Limiter(generations_per_day_per_client=1, generation_budget_per_day=10)
    limiter.admit_generation("ip", NOON)
    for _ in range(5):
        assert limiter.admit_generation("ip", NOON).reason == "client_daily"
    assert limiter.snapshot(NOON)["generations_used"] == 1


def test_both_budgets_roll_over_at_the_utc_day_boundary():
    limiter = Limiter(generations_per_day_per_client=1, generation_budget_per_day=1)
    last_moment = datetime(2026, 8, 1, 23, 59, 59, tzinfo=timezone.utc)
    assert limiter.admit_generation("ip", last_moment).allowed
    assert limiter.admit_generation("ip", last_moment).allowed is False

    first_moment = datetime(2026, 8, 2, 0, 0, 0, tzinfo=timezone.utc)
    assert limiter.admit_generation("ip", first_moment).allowed
    assert limiter.snapshot(first_moment)["generations_used"] == 1


def test_the_day_is_utc_and_not_the_machines_timezone():
    """The VM, CI and the author's laptop must roll over at the same instant."""
    # 21:00 in São Paulo is already the next day in UTC, which is the boundary the provider's own
    # quota is documented against.
    late = datetime(2026, 8, 1, 23, 30, tzinfo=timezone(timedelta(hours=-3)))
    assert utc_day(late) == "2026-08-02"


def test_a_refund_returns_a_generation_that_was_never_spent():
    """The zero-cost abstention is the caller: admitted, then nobody was asked."""
    limiter = Limiter(generations_per_day_per_client=1, generation_budget_per_day=1)
    assert limiter.admit_generation("ip", NOON).allowed
    limiter.refund_generation("ip", NOON)
    assert limiter.snapshot(NOON)["generations_used"] == 0
    assert limiter.admit_generation("ip", NOON).allowed


def test_a_refund_can_never_manufacture_budget():
    """A refund arriving twice is a bug that must not become free quota."""
    limiter = Limiter(generations_per_day_per_client=5, generation_budget_per_day=5)
    limiter.admit_generation("ip", NOON)
    for _ in range(4):
        limiter.refund_generation("ip", NOON)
    assert limiter.snapshot(NOON)["generations_used"] == 0
    assert [limiter.admit_generation("ip", NOON).allowed for _ in range(6)] == [True] * 5 + [False]


def test_the_snapshot_is_published_before_the_budget_runs_out():
    limiter = Limiter(generation_budget_per_day=10)
    limiter.admit_generation("ip", NOON)
    assert limiter.snapshot(NOON) == {
        "utc_day": "2026-08-01",
        "generation_budget": 10,
        "generations_used": 1,
        "generations_remaining": 9,
    }


# --- eviction ------------------------------------------------------------------------------------


def test_tracked_addresses_are_capped_and_evicted_least_recently_seen():
    """An unbounded dict keyed on a remote value is a memory leak with a remote trigger."""
    limiter = Limiter(requests_per_minute=5, max_tracked_clients=3)
    for name in ("a", "b", "c"):
        limiter.admit_request(name, at(0))
    # `a` is touched again, so `b` is now the least recently seen and is the one that goes.
    limiter.admit_request("a", at(1))
    limiter.admit_request("d", at(1))
    assert set(limiter._buckets) == {"a", "c", "d"}


def test_eviction_hands_a_client_its_daily_allowance_back_and_that_is_written_down():
    """The cost of eviction, asserted rather than left as a comment.

    An evicted address gets its per-client count back. That is tolerable — and only tolerable —
    because the *global* budget is keyed on nothing and cannot be evicted, so the ceiling the
    provider actually bills against still holds. The per-client limit is a fairness measure, not a
    cost control, and this test is here so nobody later mistakes it for one.
    """
    limiter = Limiter(
        generations_per_day_per_client=1, generation_budget_per_day=100, max_tracked_clients=2
    )
    assert limiter.admit_generation("victim", NOON).allowed
    assert limiter.admit_generation("victim", NOON).allowed is False

    limiter.admit_generation("other-1", NOON)
    limiter.admit_generation("other-2", NOON)
    # Forgotten, and therefore allowed again.
    assert limiter.admit_generation("victim", NOON).allowed
    # The global counter remembers all four, which is the number that matters.
    assert limiter.snapshot(NOON)["generations_used"] == 4


# --- who the client is -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forwarded, peer, trust, expected",
    [
        # Not behind a proxy: the header is a string the client chose and is ignored outright.
        ("9.9.9.9", "10.0.0.5", False, "10.0.0.5"),
        # Behind Caddy: the **last** element, because Caddy appends the peer it accepted the
        # connection from and everything to its left is client-supplied text.
        ("9.9.9.9, 203.0.113.7", "10.0.0.5", True, "203.0.113.7"),
        ("  203.0.113.7  ", "10.0.0.5", True, "203.0.113.7"),
        # Trusted but absent, and trusted but empty: fall back to the peer rather than to a blank key.
        (None, "10.0.0.5", True, "10.0.0.5"),
        (" , ", "10.0.0.5", True, "10.0.0.5"),
        # No peer at all — what `TestClient` produces. One shared bucket, the conservative direction.
        (None, None, False, "unknown"),
    ],
)
def test_the_client_key_is_spoofable_only_when_the_deployment_says_so(forwarded, peer, trust, expected):
    assert client_address(forwarded, peer, trust_forwarded_for=trust) == expected


def test_taking_the_first_forwarded_element_would_be_the_spoofable_choice():
    """The mistake this function exists to not make, asserted as a difference.

    A client that sends `X-Forwarded-For: <random>` gets a fresh identity on every request if the
    leftmost entry is honoured. The rightmost is the one Caddy wrote.
    """
    header = "attacker-chosen, 203.0.113.7"
    assert client_address(header, "10.0.0.5", trust_forwarded_for=True) == "203.0.113.7"


def test_zero_means_the_opposite_for_a_budget_and_that_is_deliberate():
    """The asymmetry, asserted rather than left to read like an inconsistency.

    `requests_per_minute=0` disables the anti-abuse bucket; either generation budget at zero refuses
    everything. Both readings are wanted: a bucket of zero requests a minute is a policy nobody
    wants, and a budget of zero generations is exactly how this site runs with retrieval and the
    precomputed showcase and no provider spend at all. Making the second mean "unlimited" would turn
    the safest configuration into the most expensive one.
    """
    open_bucket = Limiter(requests_per_minute=0, generation_budget_per_day=5)
    assert all(open_bucket.admit_request("ip", at(0)).allowed for _ in range(50))

    closed_global = Limiter(requests_per_minute=0, generation_budget_per_day=0)
    refused = closed_global.admit_generation("ip", NOON)
    assert (refused.allowed, refused.reason) == (False, "global_daily")

    closed_client = Limiter(generations_per_day_per_client=0, generation_budget_per_day=100)
    refused = closed_client.admit_generation("ip", NOON)
    assert (refused.allowed, refused.reason) == (False, "client_daily")
