import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from task4_model_router.limiter import RateLimited, RequestTooLarge, TokenRateLimiter


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def limiter(tmp_path, clock):
    return TokenRateLimiter(tmp_path / "limits.db", limit=50_000, window=60, clock=clock)


def test_allows_up_to_the_limit_then_refuses(limiter):
    limiter.reserve("key-a", 30_000)
    limiter.reserve("key-a", 20_000)

    with pytest.raises(RateLimited):
        limiter.reserve("key-a", 1)
    assert limiter.used("key-a") == 50_000


def test_window_slides_and_old_usage_is_evicted(limiter, clock, tmp_path):
    limiter.reserve("key-a", 30_000)             # t=0
    clock.now += 30
    limiter.reserve("key-a", 20_000)             # t=30
    clock.now += 15
    with pytest.raises(RateLimited) as exc:      # t=45, window is full
        limiter.reserve("key-a", 10_000)
    assert exc.value.retry_after == pytest.approx(15)  # the t=0 row leaves at t=60

    clock.now += 15.01                           # t=60.01
    limiter.reserve("key-a", 10_000)
    assert limiter.used("key-a") == 30_000

    rows = sqlite3.connect(tmp_path / "limits.db").execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    assert rows == 2  # expired row deleted


def test_retry_after_counts_only_the_rows_that_need_to_expire(limiter, clock):
    limiter.reserve("key-a", 10_000)             # t=0
    clock.now += 10
    limiter.reserve("key-a", 10_000)             # t=10
    clock.now += 10
    limiter.reserve("key-a", 30_000)             # t=20, window now full

    with pytest.raises(RateLimited) as exc:
        limiter.reserve("key-a", 15_000)         # t=0 and t=10 both have to expire
    assert exc.value.retry_after == pytest.approx(50)


def test_settle_replaces_the_estimate_with_real_usage(limiter):
    reservation = limiter.reserve("key-a", 40_000)
    limiter.settle(reservation, 8_000)

    limiter.reserve("key-a", 40_000)
    assert limiter.used("key-a") == 48_000


def test_release_gives_the_tokens_back(limiter):
    reservation = limiter.reserve("key-a", 50_000)
    limiter.release(reservation)

    assert limiter.used("key-a") == 0


def test_tenants_are_limited_separately(limiter):
    limiter.reserve("key-a", 50_000)
    limiter.reserve("key-b", 50_000)

    with pytest.raises(RateLimited):
        limiter.reserve("key-b", 1)


def test_request_larger_than_the_limit_can_never_fit(limiter):
    with pytest.raises(RequestTooLarge):
        limiter.reserve("key-a", 50_001)


def test_concurrent_reservations_never_overshoot(tmp_path):
    # separate limiters on one file, like several workers
    path = tmp_path / "shared.db"
    workers = [TokenRateLimiter(path, limit=50_000) for _ in range(4)]

    def attempt(i):
        try:
            workers[i % 4].reserve("key-a", 5_000)
            return True
        except RateLimited:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(40)))

    assert sum(results) == 10
    assert workers[0].used("key-a") == 50_000


def test_usage_survives_a_restart(tmp_path, clock):
    TokenRateLimiter(tmp_path / "limits.db", clock=clock).reserve("key-a", 45_000)

    restarted = TokenRateLimiter(tmp_path / "limits.db", clock=clock)
    with pytest.raises(RateLimited):
        restarted.reserve("key-a", 10_000)


def test_raw_api_keys_are_not_written_to_disk(limiter, tmp_path):
    limiter.reserve("sk-live-very-secret", 10)

    assert b"sk-live-very-secret" not in (tmp_path / "limits.db").read_bytes()
