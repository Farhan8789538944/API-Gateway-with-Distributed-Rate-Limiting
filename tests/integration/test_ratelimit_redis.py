"""Lua limiter tests against the real test Redis.

Deterministic tests use the explicit-clock build of the SAME script bodies
(only the time preamble differs); production-clock tests use Redis TIME.
Keys use random client/policy ids so they never collide with other tests.
"""

import asyncio
import random
import time

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from gateway.app.ratelimit import Policy, RateLimiter, RateLimitUnavailable
from gateway.app.ratelimit.limiter import bucket_key
from tests.integration.conftest import NO_REFILL_MS, REDIS_URL

pytestmark = pytest.mark.anyio
T0 = 1_800_000_000_000_000  # an arbitrary fixed "now" in microseconds


@pytest.fixture
async def redis():
    client = Redis.from_url(REDIS_URL)
    try:
        yield client
    finally:
        await client.aclose()


def ids():
    return random.randint(10**12, 10**13), random.randint(10**12, 10**13)


def tb(capacity, refill_tokens, period_ms):
    _, pid = ids()
    return Policy(pid, "tb", "token_bucket", capacity, refill_tokens=refill_tokens,
                  refill_period_ms=period_ms)


def sw(limit, window_ms):
    _, pid = ids()
    return Policy(pid, "sw", "sliding_window", limit, window_ms=window_ms)


# ------------------------------------------------------------ token bucket
async def test_token_bucket_burst_then_exhaustion(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = tb(5, 1, 1000)
    decisions = [await limiter.check(client, policy, now_us=T0) for _ in range(6)]
    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert [d.remaining for d in decisions[:5]] == [4, 3, 2, 1, 0]
    assert decisions[5].retry_after_us == 1_000_000  # one token per second


async def test_token_bucket_refill_is_proportional_and_capped(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = tb(5, 1, 1000)
    for _ in range(5):
        await limiter.check(client, policy, now_us=T0)
    half = await limiter.check(client, policy, now_us=T0 + 500_000)
    assert not half.allowed and half.retry_after_us == 500_000
    assert (await limiter.check(client, policy, now_us=T0 + 1_000_000)).allowed
    assert not (await limiter.check(client, policy, now_us=T0 + 1_000_000)).allowed
    # After a long idle period the bucket is full but never above capacity.
    later = T0 + 3600 * 1_000_000
    results = [(await limiter.check(client, policy, now_us=later)).allowed for _ in range(6)]
    assert results == [True] * 5 + [False]


async def test_token_bucket_frequent_calls_do_not_starve_refill(redis):
    """Regression: fractional refill must accumulate across rejected calls."""
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = tb(1, 3, 1000)  # one token every 333.334 ms
    assert (await limiter.check(client, policy, now_us=T0)).allowed
    step = 250  # each call adds floor(250 * 3 / 1000) = 0 milli-tokens on its own
    t = T0
    while True:
        t += step
        if (await limiter.check(client, policy, now_us=t)).allowed:
            break
        assert t - T0 < 400_000, "refill starved"
    # Exact arithmetic: ceil(1e6 / 3) = 333_334 us per token, no rounding drift.
    assert 333_334 <= t - T0 < 333_334 + step


async def test_token_bucket_backward_clock_does_not_mint_tokens(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = tb(2, 1, 1000)
    for _ in range(2):
        await limiter.check(client, policy, now_us=T0)
    back = await limiter.check(client, policy, now_us=T0 - 10_000_000)
    assert not back.allowed
    assert back.retry_after_us == 1_000_000 + 10_000_000  # includes the backward gap
    assert not (await limiter.check(client, policy, now_us=T0 + 999_999)).allowed
    assert (await limiter.check(client, policy, now_us=T0 + 1_000_000)).allowed


async def test_token_bucket_expiry_is_equivalent_to_full_bucket(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = tb(4, 2, 1000)  # full refill from empty takes 2 s
    for _ in range(4):
        await limiter.check(client, policy, now_us=T0)
    key = bucket_key(policy.algorithm, client, policy.id)
    ttl = await redis.pttl(key)
    assert 2000 < ttl <= 3000  # time-to-full (2 s) + 1 s margin
    # At time-to-full the stored state is a full bucket, exactly like no key.
    at_full = [(await limiter.check(client, policy, now_us=T0 + 2_000_000)).allowed
               for _ in range(5)]
    await redis.delete(key)
    fresh = [(await limiter.check(client, policy, now_us=T0 + 2_000_000)).allowed
             for _ in range(5)]
    assert at_full == fresh == [True] * 4 + [False]


async def test_invalid_parameters_are_rejected_by_script(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    script = limiter._scripts["token_bucket"]
    with pytest.raises(ResponseError, match="invalid token bucket"):
        await script.run(redis, ["rl:test:invalid"], [0, 1, 1000, T0])
    script = limiter._scripts["sliding_window"]
    with pytest.raises(ResponseError, match="invalid sliding window"):
        await script.run(redis, ["rl:test:invalid2"], [5, 1000, "short", T0])


# ------------------------------------------------------- sliding window
async def test_sliding_window_exact_boundary(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = sw(3, 1000)
    for dt in (0, 1, 2):
        assert (await limiter.check(client, policy, now_us=T0 + dt)).allowed
    rejected = await limiter.check(client, policy, now_us=T0 + 3)
    assert not rejected.allowed
    # Next relevant expiration: the oldest event (T0) leaves at T0 + window.
    assert rejected.retry_after_us == 1_000_000 - 3
    # Window is (now - W, now]: at T0 + W - 1 the T0 event still counts ...
    assert not (await limiter.check(client, policy, now_us=T0 + 999_999)).allowed
    # ... and at exactly T0 + W it no longer does.
    assert (await limiter.check(client, policy, now_us=T0 + 1_000_000)).allowed
    assert not (await limiter.check(client, policy, now_us=T0 + 1_000_000)).allowed


async def test_sliding_window_counts_only_accepted_events(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = sw(2, 1000)
    key = bucket_key(policy.algorithm, client, policy.id)
    await limiter.check(client, policy, now_us=T0)
    await limiter.check(client, policy, now_us=T0 + 100)
    for i in range(50):
        assert not (await limiter.check(client, policy, now_us=T0 + 200 + i)).allowed
    assert await redis.zcard(key) == 2
    # Rejections did not extend the window: exactly one slot frees at T0 + W.
    assert (await limiter.check(client, policy, now_us=T0 + 1_000_000)).allowed
    assert not (await limiter.check(client, policy, now_us=T0 + 1_000_001)).allowed


async def test_sliding_window_simultaneous_events_are_unique(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = sw(100, 1000)
    key = bucket_key(policy.algorithm, client, policy.id)
    results = await asyncio.gather(*(limiter.check(client, policy, now_us=T0)
                                     for _ in range(60)))
    assert all(r.allowed for r in results)
    assert await redis.zcard(key) == 60  # no member collided at the same timestamp
    assert sorted(r.remaining for r in results) == list(range(40, 100))


async def test_sliding_window_ttl_and_backward_clock(redis):
    limiter = RateLimiter(redis, explicit_clock=True)
    client, _ = ids()
    policy = sw(1, 2000)
    key = bucket_key(policy.algorithm, client, policy.id)
    assert (await limiter.check(client, policy, now_us=T0)).allowed
    assert 2000 < await redis.pttl(key) <= 3000
    back = await limiter.check(client, policy, now_us=T0 - 5_000_000)
    assert not back.allowed  # a "future" event still counts: conservative
    assert back.retry_after_us == 2_000_000 + 5_000_000
    # TTL is computed from the newest event, so it still covers T0 + window.
    assert 7000 < await redis.pttl(key) <= 8000


# ------------------------------------------------- production clock (TIME)
@pytest.mark.parametrize("make_policy", [lambda: tb(10, 1, NO_REFILL_MS),
                                         lambda: sw(10, NO_REFILL_MS)])
async def test_concurrent_decisions_with_redis_time_never_overshoot(redis, make_policy):
    limiter = RateLimiter(redis)
    client, _ = ids()
    policy = make_policy()
    started = time.monotonic()
    results = await asyncio.gather(*(limiter.check(client, policy) for _ in range(100)))
    elapsed_h = (time.monotonic() - started) / 3600
    allowed = sum(r.allowed for r in results)
    # Refill is 1 token/hour; allow for the (tiny) refill during the test.
    assert 10 <= allowed <= 10 + int(elapsed_h) + 1
    assert all(r.retry_after_s >= 1 for r in results if not r.allowed)


async def test_script_cache_miss_reloads_and_consumes_once(redis):
    limiter = RateLimiter(redis)
    client, _ = ids()
    policy = tb(5, 1, NO_REFILL_MS)
    first = await limiter.check(client, policy)
    await redis.script_flush()
    second = await limiter.check(client, policy)  # NOSCRIPT -> load -> run once
    assert first.remaining == 4 and second.remaining == 3


async def test_unreachable_redis_raises_unavailable():
    from gateway.app.config import RedisSettings
    from gateway.app.ratelimit.limiter import create_redis

    client = create_redis(RedisSettings(url="redis://127.0.0.1:1/0", connect_timeout_s=0.3,
                                        socket_timeout_s=0.3))
    try:
        with pytest.raises(RateLimitUnavailable):
            await RateLimiter(client).check(1, tb(1, 1, 1000))
    finally:
        await client.aclose()


@pytest.mark.disruptive
async def test_redis_oom_fails_closed_and_recovers(redis):
    limiter = RateLimiter(redis)
    client, _ = ids()
    policy = tb(5, 1, 1000)
    original = (await redis.config_get("maxmemory"))["maxmemory"]
    try:
        await redis.config_set("maxmemory", "1")  # force OOM on the TEST Redis only
        with pytest.raises(RateLimitUnavailable):
            await limiter.check(client, policy)
    finally:
        await redis.config_set("maxmemory", original)
    assert (await limiter.check(client, policy)).allowed
