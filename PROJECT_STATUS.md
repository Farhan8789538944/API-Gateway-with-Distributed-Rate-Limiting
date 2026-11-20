# Project status

_Last updated: 2026-11-21. All 13 phases are implemented. Results below are from commands actually run on this machine._

## Implemented
- **Environment.**
  - Python 3.12 virtual environment (local 3.12.0; images use 3.12.16).
  - Hash-pinned universal lock files: `requirements.lock` (runtime) and `requirements-dev.lock` (runtime + pytest, ruff), generated with uv 0.13.7.
  - Container images pinned by digest.
- **Database.**
  - 4 versioned migrations.
  - One-shot `setup` service that holds an advisory lock and verifies checksums of applied migrations.
  - Idempotent seeds: 3 policies, 1000 products.
  - Least-privilege roles (`gateway_app` read-only, `catalog_app` read-only, `orders_app`).
  - Immutable policies (enforced by a trigger).
- **Backends.**
  - Product service: keyset pagination ≤ 100, 404s, GET+HEAD, instance header.
  - Order service: strict validation, catalog price snapshot written by one atomic INSERT…SELECT, ownership enforced in SQL.
  - Dev fault controls, off by default and unroutable through the gateway.
- **Gateway.**
  - Routing on segment boundaries; path-safety checks.
  - API-key auth: 256-bit secrets, SHA-256 digests, PostgreSQL lookup with no cache.
  - Token bucket and sliding-window Lua scripts on Redis TIME; fail closed; no blind retries; NOSCRIPT reload.
  - Immediate global and per-service admission (no queue).
  - Health monitor with off-request-path, single-flight DNS; round robin; one connect retry for idempotent methods only.
  - Chunk-wise bounded bodies (64 KiB request, 1 MiB response) and one total upstream deadline (5 s).
  - Hop-by-hop, Connection-named and identity header handling.
  - Prometheus metrics on an internal port; structured JSON logs.
- **Tooling and infrastructure.**
  - Admin CLI.
  - `compose.yaml` (only Nginx published, on 127.0.0.1) and `compose.test.yaml` (isolated, explicit extra ports).
  - Nginx OSS (passive failover, `proxy_next_upstream off`); Redis (noeviction, AOF everysec); Prometheus (`observability` profile).
  - Locust workloads, benchmark runner and summarizer.
- **Documentation.** README (PowerShell commands), architecture, design decisions, failure policy, benchmarks, interview notes.

## Tested successfully
| Check | Command | Actual result |
|---|---|---|
| Lint | `.\.venv\Scripts\python.exe -m ruff check .` | All checks passed |
| Unit tests (no containers) | `.\.venv\Scripts\python.exe -m pytest tests/unit -q` | 119 passed |
| Full suite against isolated stack | `.\scripts\test_stack.ps1 up` ; `.\.venv\Scripts\python.exe -m pytest -q` | **202 passed** (119 unit + 83 integration incl. 9 disruptive), 71 s; final run after all fixes |
| Compose config (normal, normal+test) | `docker compose config --quiet` ; `docker compose -f compose.yaml -f compose.test.yaml config --quiet` | both valid |
| Lock file reproducibility | `pip install --require-hashes --no-deps -r requirements-dev.lock` ; `pip check` | installed, no broken requirements |
| Fresh isolated deployment (normal `compose.yaml`, project `apigw-fresh`, new volumes, port 18090) | `docker compose -p apigw-fresh up -d --build --wait` + README flow | migrations 0001–0004 and seeds applied; only Nginx published; key issued; product read 200; page `11..15`; no key 401; order 201 (`total_cents=47714`) and retrieval; `/_dev` 404 via gateway and 404 on backend directly (not registered); 40 rapid requests: 25×200 + 15×429 (burst 20 + refill), served by both gateways, `Retry-After: 1`; after full `docker compose restart` the exhausted client was still 429 (Redis AOF) and orders persisted. Project and volumes then removed. |
| Prometheus scraping (observability profile, test stack) | `docker compose ... --profile observability up -d prometheus`; `/api/v1/targets` | `gateway-1:9100` and `gateway-2:9100` both `health=up`, scraped separately; 3 `gateway_backend_healthy` series each |
| Smoke benchmark | `.\scripts\run_benchmarks.ps1` | 6 workloads completed; raw results in `load_tests/results/20261121-010336/`; see `docs/benchmarks.md` |

Integration coverage (real PostgreSQL 17.7, Redis 8.2.11, Nginx 1.28.3):

- **Credentials:** valid, invalid, wrong secret, expired, revoked, inactive client, no policy.
- **Database:** constraints, privileges, policy immutability, migration idempotence, concurrent setup.
- **Token bucket:** burst, refill, exhaustion, precision regression, backward clock, TTL equivalence.
- **Sliding window:** exact boundary, unique same-timestamp members, rejected events not counted, retry from next expiry, TTL with backward clock.
- **Limiter on the real clock:** Redis TIME concurrency, NOSCRIPT reload, Redis OOM.
- **Shared quota across both gateways:** token bucket via the gateways directly, sliding window via Nginx.
- **Forwarding:** query, status, HEAD, request ids, spoofed identity overwritten, ownership.
- **Body limits:** Nginx and gateway, both declared and chunked.
- **Exposure:** dev controls unreachable; metrics internal with no client labels.
- **Timeouts and overload:** 504 deadline; slow-backend overload gives exactly 50×200 + 30×503 immediately.
- **Outages and recovery:** readiness ejection and recovery; product container loss with zero client errors; orders down without replay; Redis and PostgreSQL outages; gateway loss behind Nginx; quota persistence across gateway and Redis restarts.

## Bugs found by the checks and fixed (with regression tests)
1. **`redis.conf` inline comments are invalid**, which put Redis in a crash loop. Comments moved to their own lines.
2. **Order INSERT parameter `$2` was inferred as both bigint and integer.** Fixed with explicit casts.
3. **Token bucket rounding** lost up to 1 µs per milli-token (0.2 % slower refill). Redesigned to exact integer units. Test: `test_token_bucket_frequent_calls_do_not_starve_refill`.
4. **Backend `X-Request-ID` duplicated the gateway's own header.** Gateway-owned response headers are now stripped from upstream responses.
5. **Product routes lacked HEAD** (FastAPI does not add it to GET routes). Now explicit GET+HEAD.
6. **DNS starvation.** Lookups of a stopped container take ~4 s in Docker and saturated the resolver thread pool, so the *healthy* product failed (29/30 requests returned 502).
   - Fix: DNS is resolved by the health monitor, single-flight per instance, and requests connect to the cached address.
   - Tests: `test_dns_lookups_are_single_flight_and_off_the_request_path`, `test_backend_container_loss_and_recovery`.
7. **Nginx returns 502 for ~5–10 s after gateway containers are recreated.**
   - Resolver `valid` reduced to 5 s and the behaviour documented.
   - Test setup and disruptive tests wait for the stack to settle.
8. **Test-isolation issues.** Disruptive tests left Nginx mid-failover, and teardown reused a stale DB connection. Fixed with an autouse restore fixture that uses a fresh connection.
9. **Locust summary recorded `users: 0`** (live count at stop). Now uses the configured count. The committed run `20261121-010336` predates the fix; configured users are in its `runner-output.txt`.

## Observations to be aware of
- **Fresh-deployment anomaly caused by my own procedure.** In one attempt I ran `docker compose restart` + `up` *without* the port override used for the first `up`. Compose then recreated Nginx with a different port mapping, and the container stayed in `Created` while dependencies were restarting. Re-running with a consistent configuration worked, and a full restart with a consistent configuration verified correctly. The exact reason the container stayed in `Created` was not investigated further.
- **The gateway is CPU-bound at about 190 req/s per process** (1-CPU limit) in the smoke benchmark. It has not been profiled.

## Implemented but unverified
- **Sustained or large-scale performance.** Only 30 s smoke runs were performed.
- **The `--timeout-graceful-shutdown` / in-flight drain behaviour** of Uvicorn during container stop was not specifically tested.

## Blocked
- Nothing.

## Remaining work (optional, not required for the current scope)
- Profile the gateway's per-request CPU cost before optimizing anything.
- Idempotency keys for `POST /orders`; HA Redis/PostgreSQL; mTLS between gateway and backends.
