# Interview notes

Every claim below is backed by code and tests in this repository. Numbers are local smoke
measurements (see [benchmarks.md](benchmarks.md)), not capacity guarantees.

## 60-second summary

- **What it is.** Nginx in front of two stateless FastAPI gateways that share quota state in Redis
  and proxy to product and order services on PostgreSQL.
- **Per request.** The gateway authenticates the API key with one indexed query and makes the quota
  decision with one atomic Lua script that uses Redis's clock. It forwards to a healthy instance
  under one total deadline and relays the backend's status unchanged.
- **Overload.** Overload is shed immediately with 503 instead of queueing.
- **Failures.** Every dependency failure has a defined status. Redis and PostgreSQL failures fail
  closed.

## Talking points

1. **Why Lua and not `INCR` + `EXPIRE` from Python?** Read-decide-write must be atomic across both
   gateways. A script runs atomically in Redis, so no check-then-act race is possible.
   `test_shared_token_bucket_across_both_gateways` fires 60 concurrent requests at both gateways
   against capacity 20 and gets exactly 20 (+≤1 refill) successes.
2. **Why Redis `TIME`?** Gateways' clocks may disagree, and one shared clock removes skew between
   them. It is still a wall clock, so both scripts treat backward steps conservatively (tested), and
   I would not claim immunity to clock adjustments.
3. **Token bucket vs sliding-window log.**
   - Token bucket: O(1) state (two fields), smooth refill, allows bursts.
   - Sliding log: an exact count over a rolling window, but O(limit) memory per client, hence the
     10 000 cap.
   - Both are implemented. The policy chooses the algorithm per client.
4. **A precision bug the tests found.** The first token-bucket version stored milli-tokens and
   rounded the refill time up. A test that polls every 250 µs showed refill was ~0.2 % slow. I
   switched to units of 1/period_us of a token, which makes refill exact integer arithmetic, and
   proved the bounds stay below 2^53.
5. **Fail closed and don't retry quota mutations.** redis-py 8 retries 10 times by default, and a
   retried script after a timeout could double-charge, so retries are off. The exception is
   `NOSCRIPT`, which guarantees the script did not run. Redis OOM (`noeviction`) returns 503 instead
   of silently resetting someone's quota.
6. **Allowance is consumed once allowed.** Even if the backend then fails, the allowance is spent.
   This is the simple, predictable semantic and it is tested.
7. **Admission control without queues.** `acquire_on_behalf_of_nowait` gives an immediate 503. In
   the slow-backend test, 80 concurrent requests produce exactly 50 successes and 30 immediate 503s
   (< 1.5 s), instead of piling up. Limits are per process: two gateways admit twice as much, so
   there is no global in-flight limit.
8. **One total deadline.** HTTPX timeouts are per read, so a backend trickling bytes never trips them.
   `asyncio.timeout` covers select, connect, send and body. Tested with a trickling upstream.
9. **No silent replays.**
   - Nginx uses `proxy_next_upstream off`.
   - The gateway retries only *connect* failures, and only for GET/HEAD/OPTIONS.
   - `POST /orders` after a timeout may have committed, so it is never retried.
10. **The DNS starvation bug.** Stopping one product container made the *healthy* one fail
    (29/30 requests 502).
    - Root cause: inside Docker, resolving a stopped container's name takes ~4 s. `getaddrinfo` runs
      in a small, uncancellable thread pool, so lookups piled up and starved lookups for the healthy
      name.
    - Fix: resolve in the health monitor, single-flight per instance, and connect to the cached
      address with the original `Host` header.
    - Result: 30/30 requests succeed during the outage.
11. **Identity propagation.** The gateway strips client-supplied identity headers and sets
    `X-Client-Id`. Order ownership is enforced in the SQL `WHERE` clause, and another client's order
    is indistinguishable from a missing one. Backends trust the header only because the network is
    private. In production I'd use mTLS or signed tokens.
12. **Keys.** 256-bit random secrets stored as SHA-256 digests. A slow KDF adds nothing for
    high-entropy random secrets, and auth failures share one generic response.
13. **Bounded everything.**
    - Body ≤ 64 KiB, counted while streaming, including chunked bodies.
    - Response ≤ 1 MiB.
    - DB and Redis pool waits bounded.
    - HTTPX pool = admission size.
    - Keep-alive timeouts ordered so a client never reuses a connection the server is closing.
14. **Observability with bounded cardinality.** Routes are templated (`/products/{id}`) and outcomes
    come from a fixed vocabulary. No client ids or raw paths appear as labels (tested). The success
    latency histogram excludes rejections, and the Locust summary keeps successful percentiles
    separate as well.

## Known limitations (say these before being asked)

- **Single points of failure:**
  - Single PostgreSQL (all three schemas), single Redis (all quota state), single Nginx, one host.
  - Redis AOF `everysec` can lose about 1 s of quota updates on a crash.
- **Limits are per process.** There is no global concurrency limit across gateways.
- **Nginx OSS** has only passive health checks and no replay. A dead gateway causes a few 502s
  before `max_fails` trips, and recreated gateways need ~5–10 s to be picked up again.
- **Auth** queries PostgreSQL on every request (no cache, by design until measured otherwise). A
  PostgreSQL outage therefore stops all traffic.
- **Backend trust.** Identity trust relies on network isolation.
- **Unsupported:** large uploads, streaming responses, WebSockets.
- **Policies.** One global policy per client. Policy changes start a fresh allowance, and live
  policy changes are not covered by correctness tests.
- **Orders.** Order creation is not idempotent (no idempotency keys).
- **Benchmarks** are local smoke tests: the load generator shares the machine with the system under
  test, and the runs are short. They are not sustained-capacity results.
