# Failure policy

## Status codes produced by the gateway

| Status | `error.code` | Cause |
|---|---|---|
| 400 | `invalid_path`, `invalid_content_length`, `client_disconnected` | unsafe raw path, malformed length, client went away |
| 401 | `unauthorized` | missing/malformed/unknown/revoked/expired key, inactive client, multiple keys |
| 403 | `no_quota_policy` | authenticated client without a policy |
| 404 | `route_not_found` | path outside `/products`, `/orders` (also Nginx for `/health/`) |
| 408 | `request_body_timeout` | body not received within 5 s |
| 413 | `payload_too_large` | body > 64 KiB (declared or streamed) |
| 429 | `rate_limited` | quota exhausted; `Retry-After` (whole seconds, ≥ 1) |
| 503 | `capacity_exhausted` | global/service admission full, HTTPX pool wait exceeded; `Retry-After: 1` |
| 503 | `dependency_unavailable` | PostgreSQL or Redis could not answer in time (fail closed) |
| 503 | `no_healthy_backend` | no instance of the pool is healthy |
| 502 | `bad_gateway` | upstream connect failure, protocol error, reset, response > 1 MiB |
| 504 | `gateway_timeout` | total upstream deadline (5 s) or read timeout exceeded |

Every other status is the backend's own and is preserved, including backend 404, 422 and 5xx.

## Overload status precedence

Earlier layers reject before later ones see the request:

1. **Nginx.**
   - 413 (`client_max_body_size 64k`) and 408 (`client_header_timeout`/`client_body_timeout 5s`).
   - 502 when no gateway is reachable, and 504 if a gateway does not answer within 10 s. That is
     longer than the gateway's own 5 s deadline, so the gateway's JSON 504 normally wins.
   - These responses are plain Nginx bodies.
2. **Gateway Uvicorn `--limit-concurrency 150`.** A plain-text 503 before any application code runs.
3. **Gateway global admission (100).** JSON 503 `capacity_exhausted`. This happens before any
   database or Redis work, and no quota is consumed.
4. **Authentication.** 401/403, or 503 when PostgreSQL is unavailable. No quota is consumed.
5. **Quota.** 429, or 503 when Redis is unavailable. **From here on the allowance is consumed.**
6. **Body.** 413/408.
7. **Per-service admission and HTTPX pool.** JSON 503 `capacity_exhausted`.
8. **Backend.** The backend's own statuses, including its Uvicorn `--limit-concurrency` 503s and its
   database 503s, are relayed unchanged.

## Timeout ownership

| Concern | Enforced by | Value | Result |
|---|---|---|---|
| Slow request headers | Nginx `client_header_timeout` | 5 s | 408 (Nginx) |
| Slow request body | Nginx `client_body_timeout` (with request buffering) | 5 s between reads | 408 (Nginx) |
| Slow request body bypassing Nginx | gateway `BODY_READ_TIMEOUT_S` | 5 s total | 408 |
| Nginx → gateway connect | `proxy_connect_timeout` | 1 s | 502 (Nginx) |
| Nginx waiting for gateway | `proxy_read_timeout` | 10 s | 504 (Nginx) |
| Auth DB pool wait / statement | asyncpg acquire / `command_timeout` | 1 s / 2 s | 503 |
| Quota decision | Redis blocking-pool wait / socket timeout | 0.5 s / 0.5 s | 503 |
| Backend connect | HTTPX connect | 1 s | 502 (+1 retry for idempotent methods) |
| Whole upstream exchange (select, connect, send, wait, body) | gateway `UPSTREAM_DEADLINE_S` (`asyncio.timeout`) | 5 s | 504 |
| Individual reads/writes | HTTPX read / write | 5 s / 2 s | 504 |
| Health probe | gateway probe timeout | 0.5 s | instance failure count |
| Backend DB | asyncpg acquire / statement; PostgreSQL `statement_timeout` | 1 s / 2 s / 5 s | backend 503 (relayed) |

The total deadline matters because the HTTPX read timeout applies to *each* read. A backend that
trickles one byte every 4 s would never trip it, but the 5 s total deadline does (tested by
`test_slow_body_trickle_is_bounded_by_total_deadline`).

## Dependency failures

| Failure | Behaviour | Tested by |
|---|---|---|
| Redis down / timeout / OOM | 503 `dependency_unavailable`, `Retry-After: 1`; recovers automatically | `test_redis_outage_fails_closed_and_recovers`, `test_redis_oom_fails_closed_and_recovers` |
| Redis restart | AOF restores state (≤ ~1 s may be lost); NOSCRIPT handled by reload | `test_quota_survives_gateway_and_redis_restarts` |
| PostgreSQL down | 503 (gateway auth, or products not ready → no healthy backend); pools reconnect | `test_postgres_outage_fails_closed_and_recovers` |
| One product instance down | probes + request-time connect failures eject it; GETs retried once on the other instance: **no client-visible errors in the test** | `test_backend_container_loss_and_recovery` |
| Product readiness failing | ejected within ~2 probe intervals, re-added after 2 successes | `test_readiness_failure_ejects_and_recovers_backend` |
| Orders down | POST returns 502/503 and is **never replayed** | `test_orders_backend_down_gives_502_or_503_without_replay` |
| Slow backend | 504 at the 5 s deadline; excess concurrency shed immediately with 503 | `test_total_upstream_deadline_returns_504`, `test_slow_backend_overload_rejects_immediately` |
| One gateway down | Nginx `max_fails` stops routing to it after failures; some requests get 502 first (no replay by design) | `test_gateway_instance_loss_behind_nginx` |
| Gateway container recreated | Nginx may return 502 for ~5–10 s until it re-resolves the name and `fail_timeout` expires | observed; tests wait for the stack to settle |

## Non-idempotent requests

`POST /orders` is never retried by the gateway (connect retry is limited to GET/HEAD/OPTIONS) or by
Nginx (`proxy_next_upstream off`). After a 502/504 the order **may or may not have been committed**.
Clients must look the order up, or the API would need idempotency keys (not implemented).

## Single points of failure (local simplification)

- **Nginx.** One container, one host.
- **PostgreSQL.** One instance holds all three schemas. An outage stops authentication (all traffic)
  and orders; products report not ready.
- **Redis.** One instance holds all quota state, and an outage stops all traffic (fail closed). Data
  loss resets quotas.
- **The host.** Everything runs on one Docker host.
