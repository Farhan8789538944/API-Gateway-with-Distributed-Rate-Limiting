# API Gateway with Distributed Rate Limiting

**Scalable request routing and backend load management.**

An asynchronous API gateway (FastAPI/Uvicorn + HTTPX) in front of PostgreSQL-backed product and
order services. The gateway:

- authenticates API keys;
- enforces quotas shared by **two gateway instances** through atomic Redis Lua scripts (token
  bucket and sliding-window log);
- round-robins product traffic over healthy instances;
- sheds overload immediately;
- bounds bodies and deadlines;
- emits JSON logs and Prometheus metrics.

Nginx Open Source is the single entry point.

Everything runs locally with Docker Desktop (Linux containers). No paid services or API keys are
needed.

| Document | Content |
|---|---|
| [PROJECT_REPORT.md](PROJECT_REPORT.md) | full report of what was built, how it was verified, results and limitations |
| [docs/architecture.md](docs/architecture.md) | topology, request lifecycle, health/balancing, data ownership |
| [docs/design-decisions.md](docs/design-decisions.md) | API keys, both limiter algorithms, admission and bounds, trade-offs |
| [docs/failure-policy.md](docs/failure-policy.md) | status codes, overload precedence, timeout ownership, outages |
| [docs/benchmarks.md](docs/benchmarks.md) | reproducible benchmark procedure and measured smoke results |
| [docs/interview-notes.md](docs/interview-notes.md) | talking points and limitations |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | what is implemented, tested (with results), unverified |

## Versions used

| Component | Version |
|---|---|
| Python | 3.12 (image `python:3.12-slim-bookworm`, 3.12.16) |
| FastAPI / Starlette / Uvicorn | 0.165.0 / 1.10.0 / 0.54.0 |
| HTTPX / AnyIO | 0.28.1 / 4.16.0 |
| asyncpg / redis-py | 0.32.0 / 8.1.0 |
| prometheus-client | 0.26.0 |
| pytest / ruff | 9.2.0 / 0.17.6 |
| Locust (image) | 2.47.0 |
| PostgreSQL | 17.7 (alpine) |
| Redis | 8.2.11 (alpine) |
| Nginx (OSS) | 1.28.3 (alpine) |
| Prometheus | 3.7.3 |

All Python dependencies, including transitive ones, are pinned with hashes in `requirements.lock`
(runtime) and `requirements-dev.lock` (runtime + test tools). Container images are pinned by digest
in `compose.yaml`.

## Quick start (Windows PowerShell)

Run all commands from the repository root.

### 1. Prepare the environment

```powershell
docker version          # "Server: ... OS/Arch: linux/amd64" => Linux containers
docker compose version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --no-deps -r requirements-dev.lock
```

The virtual environment is only needed for tests and the benchmark summarizer. The services
themselves run in containers.

### 2. Configure

```powershell
Copy-Item .env.example .env
notepad .env            # change the passwords (letters/digits/-/_ only: they are used inside URLs)
```

### 3. Start the application

```powershell
docker compose up -d --build --wait
docker compose ps
```

`up` runs the one-shot **`setup`** service before the applications start. It applies migrations
under a PostgreSQL advisory lock, sets the application role passwords, and applies idempotent
seeds (3 policies, 1000 products). Applications never migrate themselves.

### 4. Database setup and seeding (explicit)

```powershell
docker compose run --rm setup                                       # re-run migrate + seed (idempotent)
docker compose run --rm setup python -m database.manage status      # applied/pending migrations
docker compose run --rm setup python -m database.manage seed        # seeds only
```

### 5. Create a client and an API key

```powershell
docker compose run --rm admin list-policies
docker compose run --rm admin create-client --name acme --policy default-token-bucket
docker compose run --rm admin issue-key --client acme        # prints the raw key ONCE
```

Copy the printed `api_key:` value; it cannot be shown again. Other commands:

```powershell
docker compose run --rm admin create-policy --name strict --algorithm token_bucket --capacity 5 --refill-tokens 1 --refill-period-ms 1000
docker compose run --rm admin create-policy --name window --algorithm sliding_window --capacity 100 --window-ms 60000
docker compose run --rm admin assign-policy --client acme --policy strict
docker compose run --rm admin issue-key --client acme --expires-in-days 30 --label ci
docker compose run --rm admin list-keys --client acme
docker compose run --rm admin revoke-key --key-id <key_id>
docker compose run --rm admin deactivate-client --client acme
docker compose run --rm admin activate-client --client acme
docker compose run --rm admin list-clients
```

### 6. Authenticated requests

```powershell
$key = "<paste api_key here>"
$h = @{ "X-API-Key" = $key }
Invoke-RestMethod http://127.0.0.1:8080/products/1 -Headers $h
Invoke-RestMethod "http://127.0.0.1:8080/products?limit=5&after_id=10" -Headers $h
Invoke-WebRequest http://127.0.0.1:8080/products/1 -SkipHttpErrorCheck | Select-Object StatusCode   # 401 without a key

# Quota: default-token-bucket = burst 20, refill 10/s. Expect 429s with Retry-After:
1..40 | ForEach-Object { (Invoke-WebRequest http://127.0.0.1:8080/products/1 -Headers $h -SkipHttpErrorCheck).StatusCode } | Group-Object | Select-Object Name, Count
```

### 7. Create and retrieve an order

```powershell
$order = Invoke-RestMethod -Method Post http://127.0.0.1:8080/orders -Headers $h -ContentType "application/json" -Body '{"product_id": 3, "quantity": 2}'
$order
Invoke-RestMethod "http://127.0.0.1:8080/orders/$($order.id)" -Headers $h
```

The price snapshot comes from the catalog. A client-supplied price is rejected (422), and another
client's order returns 404.

### 8. Metrics (optional observability profile)

```powershell
docker compose --profile observability up -d prometheus
Start-Process http://127.0.0.1:9090     # e.g. query: sum by (outcome) (rate(gateway_requests_total[1m]))
```

Gateway metrics are served on port 9100 inside the network only. Prometheus scrapes each gateway
separately.

### 9. Run the tests

```powershell
# Unit tests (no containers needed)
.\.venv\Scripts\python.exe -m pytest tests/unit -q

# Integration tests run against an ISOLATED stack: project "apigw-test", its own volumes,
# internal ports published on 127.0.0.1 (15432, 16379, 18001-18002, 18080-18083, 19101-19102).
.\scripts\test_stack.ps1 up
.\.venv\Scripts\python.exe -m pytest -q                       # everything (~70 s)
.\.venv\Scripts\python.exe -m pytest -q -m "not disruptive"   # skip container stop/start tests
.\.venv\Scripts\python.exe -m ruff check .
```

Disruptive tests stop and start containers of `apigw-test` only, and restore them afterwards. If
the test stack is not running, integration tests are reported as **skipped**, never as passed.

### 10. Load tests

```powershell
.\scripts\test_stack.ps1 up
.\scripts\run_benchmarks.ps1                       # 6 workloads, 20 users, 10 s warm-up + 30 s each
.\.venv\Scripts\python.exe scripts\summarize_benchmarks.py load_tests\results\<run-id>
```

See [docs/benchmarks.md](docs/benchmarks.md) for workloads, methodology and measured results.

### 11. Stop

```powershell
docker compose down                 # stop the normal deployment (data volumes kept)
.\scripts\test_stack.ps1 down       # stop the test stack (its volumes kept)
```

### 12. Optional destructive reset

> **Destructive:** the following commands permanently delete the PostgreSQL and Redis data of the
> named project only. All clients, keys, orders and quota state are lost.

```powershell
docker compose down --volumes        # normal project "apigw"
.\scripts\test_stack.ps1 reset       # test project "apigw-test"
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `set POSTGRES_PASSWORD in .env` | `Copy-Item .env.example .env` and fill in the passwords |
| Port 8080 already in use | set `GATEWAY_HTTP_PORT=8081` in `.env`, then `docker compose up -d` |
| `setup` exited non-zero | `docker compose logs setup`; check passwords contain only URL-safe characters |
| 502 from Nginx for a few seconds after `up --build` | gateways were recreated; Nginx re-resolves names every 5 s and skips failed peers for 5 s. Wait ~10 s |
| A config file edit (nginx.conf, redis.conf) has no effect | single-file bind mounts keep the old file when an editor replaces it: `docker compose up -d --force-recreate nginx` (or `redis`) |
| Every request returns 503 `dependency_unavailable` | PostgreSQL or Redis is down: `docker compose ps`, `docker compose logs redis postgres` |
| 503 `no_healthy_backend` | products not ready: `docker compose logs product-1 product-2` |
| Integration tests all skipped | start the test stack: `.\scripts\test_stack.ps1 up` |
| `docker compose` cannot connect | start Docker Desktop and ensure it uses Linux containers |

## Development controls

Backends can inject delay, 5xx responses or readiness failure (`PUT /_dev/faults`). They are
enabled only by `DEV_CONTROLS_ENABLED=true`, which only `compose.test.yaml` sets. The routes are
outside `/products` and `/orders`, so the gateway can never forward to them, and encoded or
dot-segment paths are rejected (tested). In the normal deployment they are not registered at all.
