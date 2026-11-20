"""Redis-backed limiter: runs one atomic Lua script per decision.

Time: production scripts read Redis TIME inside the script, so every gateway
uses the same clock and the read-decide-write sequence is atomic. Tests can
build a limiter whose scripts take `now_us` as the last argument instead; the
algorithm body is byte-for-byte the same, only the time preamble differs.

Failure policy: any Redis error (connection, timeout, OOM, loading, ...)
raises RateLimitUnavailable and the gateway fails closed (503). Commands are
never retried after an ambiguous failure because the script may already have
consumed allowance. The single exception is NOSCRIPT: Redis guarantees the
script did not run, so loading it and executing once more is not a replay.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import time
from dataclasses import dataclass
from importlib import resources

from redis.asyncio import BlockingConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.exceptions import NoScriptError, RedisError
from redis.retry import Retry

from gateway.app.config import RedisSettings
from gateway.app.ratelimit.policy import SLIDING_WINDOW, TOKEN_BUCKET, Policy

REDIS_TIME_PREAMBLE = (
    "local _t = redis.call('TIME')\n"
    "local now_us = tonumber(_t[1]) * 1000000 + tonumber(_t[2])\n"
)
EXPLICIT_TIME_PREAMBLE = "local now_us = tonumber(ARGV[#ARGV])\n"


class RateLimitUnavailable(Exception):
    """A quota decision could not be obtained (fail closed)."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_us: int
    algorithm: str
    duration_s: float = 0.0

    @property
    def retry_after_s(self) -> int:
        """Whole seconds for the Retry-After header (rounded up, at least 1)."""
        return max(1, math.ceil(self.retry_after_us / 1_000_000))


class LuaScript:
    def __init__(self, source: str) -> None:
        self.source = source
        self.sha = hashlib.sha1(source.encode("utf-8"), usedforsecurity=False).hexdigest()

    async def run(self, redis: Redis, keys: list[str], args: list[str | int]):
        try:
            return await redis.evalsha(self.sha, len(keys), *keys, *args)
        except NoScriptError:
            # Script cache miss (Redis restart, SCRIPT FLUSH, failover). The
            # script was NOT executed, so this is the only safe retry.
            await redis.script_load(self.source)
            return await redis.evalsha(self.sha, len(keys), *keys, *args)


def _script(name: str, preamble: str) -> LuaScript:
    body = resources.files("gateway.app.ratelimit").joinpath(name).read_text("utf-8")
    # "#!lua" (no flags) => Redis refuses to start the script while OOM instead
    # of failing halfway through, and treats it as a writing script.
    return LuaScript("#!lua\n" + preamble + body)


def create_redis(settings: RedisSettings) -> Redis:
    pool = BlockingConnectionPool.from_url(
        settings.url,
        max_connections=settings.max_connections,
        timeout=settings.pool_timeout_s,
        socket_timeout=settings.socket_timeout_s,
        socket_connect_timeout=settings.connect_timeout_s,
        # redis-py 8 defaults to 10 retries with backoff; a retried EVALSHA
        # after a timeout could consume quota twice, so retries are disabled.
        retry=Retry(NoBackoff(), 0),
        retry_on_error=[],
    )
    return Redis(connection_pool=pool)


def bucket_key(algorithm: str, client_id: int, policy_id: int) -> str:
    short = "tb" if algorithm == TOKEN_BUCKET else "sw"
    # Keyed by authenticated client AND policy: reassigning a client to a new
    # policy starts fresh state; the old key expires on its own TTL.
    return f"rl:{short}:c{client_id}:p{policy_id}"


class RateLimiter:
    def __init__(self, redis: Redis, *, explicit_clock: bool = False) -> None:
        self._redis = redis
        self._explicit_clock = explicit_clock
        preamble = EXPLICIT_TIME_PREAMBLE if explicit_clock else REDIS_TIME_PREAMBLE
        self._scripts = {
            TOKEN_BUCKET: _script("token_bucket.lua", preamble),
            SLIDING_WINDOW: _script("sliding_window.lua", preamble),
        }

    async def check(self, client_id: int, policy: Policy, *, now_us: int | None = None) -> Decision:
        if (now_us is not None) != self._explicit_clock:
            raise ValueError("now_us must be given iff the limiter uses an explicit clock")
        args = self._args(policy)
        if now_us is not None:
            args.append(str(now_us))
        key = bucket_key(policy.algorithm, client_id, policy.id)
        started = time.perf_counter()
        try:
            result = await self._scripts[policy.algorithm].run(self._redis, [key], args)
        except (RedisError, OSError, TimeoutError) as exc:
            raise RateLimitUnavailable(type(exc).__name__) from exc
        duration = time.perf_counter() - started
        return self._decision(result, policy, duration)

    @staticmethod
    def _args(policy: Policy) -> list[str | int]:
        if policy.algorithm == TOKEN_BUCKET:
            return [policy.capacity, policy.refill_tokens, policy.refill_period_ms]
        return [policy.capacity, policy.window_ms, secrets.token_hex(16)]

    @staticmethod
    def _decision(result, policy: Policy, duration: float) -> Decision:
        if not isinstance(result, list) or len(result) != 3:
            raise RateLimitUnavailable("unexpected script result")
        allowed, remaining, retry_us = (int(v) for v in result)
        return Decision(
            allowed=allowed == 1,
            limit=policy.capacity,
            remaining=max(0, remaining),
            retry_after_us=max(0, retry_us),
            algorithm=policy.algorithm,
            duration_s=duration,
        )
