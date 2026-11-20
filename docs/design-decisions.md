# Design decisions

## API keys

- **Format.** `gwk_<key_id>_<secret>`. `key_id` is 16 hex characters (64 random bits) and acts as the
  public lookup identifier, which is safe to log. `secret` is 43 URL-safe characters (256 random bits
  from `secrets`, the OS CSPRNG).
- **Storage.** Only `SHA-256(full key)` is stored, plus metadata (label, created/expires/revoked
  timestamps). A slow password hash such as bcrypt or argon2 protects *low-entropy human* passwords
  against offline guessing. A uniformly random 256-bit secret cannot be guessed offline, so a fast
  hash is sufficient and keeps per-request verification cheap. Comparison uses
  `hmac.compare_digest`.
- **Display.** The raw key is printed once by `issue-key`, and is never stored or logged.
- **Failures.** Every authentication failure (missing, malformed, unknown, wrong secret, revoked,
  expired, inactive client) returns the same 401 body, so callers cannot enumerate keys or accounts.
  The specific reason is logged. A client without a policy gets 403 `no_quota_policy`.
- **Identity forwarding.** The gateway strips client-supplied `X-Client-Id`, `X-Client-Name`,
  `X-Gateway-*`, `Authorization`, `X-API-Key` and forwarding headers, and sets `X-Client-Id` to the
  authenticated id. Backends trust that header **only because they are reachable solely on the
  private Compose network**. Anyone who can reach a backend directly can impersonate any client.
  Production would need mTLS or signed identity tokens between gateway and backends.

## Rate limiting

### Shared properties

- **One atomic script per decision.** Each decision is one Lua script (`EVALSHA`) that reads state,
  decides, mutates and sets expiry. Redis runs scripts atomically, so concurrent requests on any
  gateway cannot interleave a check and an update: there is no check-then-act race.
- **Shared clock.** The script reads `redis.call('TIME')`, so all gateways share one clock. This is
  one wall clock, *not* a monotonic one: an NTP step on the Redis host is still visible. Both
  algorithms handle backward steps conservatively (no allowance is minted).
- **Keys.** `rl:tb:c<client_id>:p<policy_id>` and `rl:sw:c<client_id>:p<policy_id>`, scoped by
  authenticated client and policy. Initially one `global` policy per client is supported (primary key
  `(client_id, scope)`).
- **Policy changes.** Policies are immutable: a database trigger rejects parameter updates, because
  stored state is interpreted with the policy's parameters. To change a client's limits, create a new
  policy and run `assign-policy`. The client then starts with a fresh allowance under the new policy,
  and the old key expires by TTL. Requests in flight during the switch may be decided under either
  policy. Live policy changes are excluded from the baseline correctness tests.
- **Consumption.** Once a request is allowed, its allowance is consumed, even if later admission, body
  limits or the backend fail. This is tested by `test_quota_consumed_even_when_backend_rejects`.
- **Fail closed.** Any Redis error (connection, timeout, `OOM`, `LOADING`, script error) returns 503.
- **No retries.** redis-py 8 defaults to 10 retries with backoff, and a retried `EVALSHA` after a
  timeout could consume quota twice. Retries are disabled (`Retry(NoBackoff(), 0)`). The one safe
  retry is after `NOSCRIPT`, which guarantees the script did not run: the wrapper loads the script and
  executes it once more.
- **OOM behaviour.** Scripts start with `#!lua`, so Redis refuses to *start* them while out of memory,
  instead of failing between writes. With `noeviction`, quota keys are never silently evicted. Writes
  fail and the gateway fails closed.
- **Persistence.** AOF with `appendfsync everysec` and no RDB snapshots. A Redis crash can lose about
  1 s of mutations, which partially resets quotas. Deleting the volume resets all quotas. A single
  Redis instance is not highly available.

### Token bucket (`token_bucket.lua`)

- **State.** `t` (tokens) and `ts` (last refill time, µs).
- **Exact integer arithmetic.** One token = `period_us` units, so each elapsed microsecond adds
  exactly `refill_tokens` units, with no rounding and no drift. Validated bounds keep
  `capacity × period_us ≤ 3.6e14 < 2^53`, so Lua doubles are exact. An earlier milli-token design
  lost up to 1 µs per milli-token; the regression test caught it.
- **Refill and consumption.** Tokens refill up to capacity, and each allowed request consumes one
  token.
- **Retry hint.** `retry_after_us = ceil(deficit / refill_tokens)`.
- **Backward clock.** If `now < last`, there is no refill and `last` is not moved back, so no tokens
  are minted. The retry hint includes the gap.
- **Expiry.** An absent key is treated as a full bucket. The TTL is set to the time until the bucket
  would be full, plus 1 s. At that point the stored state equals "absent", so expiry never changes
  behaviour (tested).

### Sliding-window log (`sliding_window.lua`)

- **State.** A sorted set of *accepted* events, scored by acceptance time in µs.
- **Window boundary.** For a decision at `now`, an event counts iff `now − W < score ≤ now`. An event
  stops counting exactly `W` after acceptance. Expired entries (`score ≤ now − W`) are removed first.
- **Admission.** The request is allowed iff the count is below the limit, and only then is the event
  added. Rejections are not recorded, so they do not extend the window.
- **Member uniqueness.** Members are `<now_us>:<128-bit random nonce>`, which are unique even when
  requests share a microsecond. `ZADD NX` failing (a duplicate) is reported as an error, never
  silently ignored.
- **Retry hint.** When rejected, the request may proceed when the event at index `count − limit`
  leaves the window: `retry_after = score + W − now`.
- **Expiry.** The TTL is `newest_score + W − now + 1 s`. Using the newest score (not `now`) stays
  correct if the clock moved backwards.
- **Bounds.** Limit ≤ 10 000 bounds memory per key and script run time.

### Deterministic testing

`RateLimiter(explicit_clock=True)` builds the *same* script bodies with a preamble that reads `now_us`
from the last argument instead of `TIME`. Production code never enables it. Algorithm tests are
deterministic, and separate tests exercise the production clock (concurrency, NOSCRIPT, OOM).

## Admission control and bounds

- **Global admission.** An `anyio.CapacityLimiter` with `acquire_on_behalf_of_nowait`: when full, the
  request gets an immediate 503 and there is no queue. Explicit tokens make release independent of
  the acquiring task. Release happens in `finally` on success, error and cancellation (tested).
- **Per-service admission.** `/products` 50, `/orders` 20, global 100.
- **Per-process limits.** These limits are per process: two gateways admit up to 200 in total. There
  is **no global in-flight limit**.
- **Coordination with other layers:**

| Layer | Limit | Relationship |
|---|---|---|
| Nginx | `worker_connections 1024`, keepalive 32 to gateways | ample above gateway limits |
| Gateway Uvicorn | `--limit-concurrency 150` | > global admission 100 (+ keep-alive connections), so the JSON 503 from the app normally acts first |
| Gateway admission | global 100, products 50, orders 20 | service ≤ global (validated at startup) |
| Gateway HTTPX pool | max connections = service limit; pool wait 0.5 s | an admitted request normally finds a connection |
| Gateway PostgreSQL pool | max 10, acquire 1 s, statement 2 s | auth waits are bounded, then 503 |
| Gateway Redis pool | max 20 (blocking pool), wait 0.5 s, socket 0.5 s | decision waits are bounded, then 503 |
| Backend Uvicorn | `--limit-concurrency 120` | above 2 gateways × 50 = 100 product requests per instance worst case |
| Backend PostgreSQL pool | max 10, acquire 1 s, statement 2 s | 503 from backend, relayed unchanged |
| PostgreSQL | `max_connections=100`, `statement_timeout=5s` | 2×10 + 2×10 + 10 + setup/admin ≪ 100 |

- **Keep-alive ordering.** HTTPX `keepalive_expiry` (2 s) < backend Uvicorn keep-alive (5 s), and
  Nginx upstream `keepalive_timeout` (4 s) < gateway Uvicorn keep-alive (5 s). Clients therefore never
  reuse a connection the server is closing.

## Bounded JSON scope

- **Bounded buffering.** Request bodies (≤ 64 KiB) and responses (≤ 1 MiB) are buffered, but only
  chunk by chunk with a running total, and both `Content-Length` and chunked bodies are bounded.
  Oversized requests get 413. Oversized responses get 502 and the upstream connection is closed.
- **Not supported:** WebSockets, `Upgrade`, large uploads and streaming responses.

## Why no auth cache

Every request runs one indexed PostgreSQL query. A cache would delay revocation and add
invalidation logic, and no measurement showed authentication to be the bottleneck. Revisit only if
benchmarks show it.
