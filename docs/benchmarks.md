# Benchmarks

> **Scope.** These are **smoke** load tests on one developer machine. They are short runs, the load
> generator shares the CPU with the system under test, and there is no tuning. They show that the
> workloads run and how the system behaves relative to itself across scenarios. They are **not**
> sustained-capacity results. Figures such as "500 req/s, 100 clients, P95 100 ms / P99 200 ms"
> that appeared in early project descriptions were illustrative targets and were **never
> established** here.

## How to reproduce

```powershell
.\scripts\test_stack.ps1 up
.\scripts\run_benchmarks.ps1 -Users 20 -Duration 30 -Warmup 10
.\.venv\Scripts\python.exe scripts\summarize_benchmarks.py load_tests\results\<run-id>
```

- **Deployment.** The isolated test stack (`compose.yaml` + `compose.test.yaml`, project
  `apigw-test`) is used, because the direct-backend baseline and the slow-backend scenario need the
  dev fault controls and direct addressing.
- **Load generator.** Locust 2.47.0 runs as a container on the same Compose network (profile
  `loadtest`), limited to 2 CPUs and 512 MiB. It is a single Locust process with no workers.
- **Clients.** Created by the script: one client on `benchmark-unlimited` (token bucket, 100 000
  burst with a 100 000/ms refill, so effectively never limits but still executes the Redis script on
  every request), and one client on a 50 req/s token bucket (capacity 50, refill 50 per 1000 ms) for
  the quota scenario.
- **Warm-up.** Each scenario first runs a warm-up of `-Warmup` seconds (discarded), then a measured
  run of `-Duration` seconds.
- **Raw output.** Locust CSVs, Locust logs, the outcome summary JSON, a mid-run `docker stats`
  snapshot and `meta.json` are saved in `load_tests/results/<run-id>/`.
- **Outcome classification** (`load_tests/locustfile.py`):
  - `success` (2xx)
  - `quota_rejected` (429)
  - `capacity_rejected` (503 with `capacity_exhausted`)
  - `error_*` (everything else)
  
  Successful-response percentiles are computed only from successes. Locust's own CSV percentiles
  include failures and are kept only as raw data.

## Workloads

| # | Scenario | Target | Users / wait | Workload |
|---|---|---|---|---|
| 1 | Direct backend baseline | `product-1:8000` (no gateway) | 20, closed loop, no think time | 90 % `GET /products/{random 1..1000}`, 10 % `GET /products?limit=20` |
| 2 | One gateway | `gateway-1:8000` | 20, closed loop | same as 1 |
| 3 | Two gateways sharing Redis, product-heavy reads | `nginx:80` → both gateways | 20, closed loop | same as 1 |
| 4 | Product/order mix | `nginx:80` | 20, closed loop | 80 % product reads, 15 % `POST /orders`, 5 % `GET /orders/{own id}` |
| 5 | Quota exhaustion | `nginx:80`, 50 req/s quota | 20 × 10 req/s = **200 req/s offered** | same as 1 |
| 6 | Slow-backend overload | `gateway-1:8000`, products delayed 200 ms | 80 × 10 req/s = **800 req/s offered** | same as 1 |

- **Closed loop.** Each user sends its next request as soon as the previous one returns, so the
  offered load equals the achieved load and is not known independently.
- **Payloads.** Single-product responses are about 140 B of JSON. Page responses (20 items) are about
  2.9 KB. Order bodies are about 30 B. The dataset is 1000 seeded products.

## Results: smoke run `20261121-010336`

- **Hardware.** Laptop with a 12th Gen Intel Core i5-1235U (10 cores / 12 threads, mobile CPU) and
  15.7 GB RAM.
- **Docker Desktop.** Engine 29.8.0, 12 CPUs and 7.6 GiB visible to Docker.
- **Container limits.** Each Python service and Nginx: 1 CPU, 256 MiB (Nginx 64 MiB). Locust: 2 CPUs.
  PostgreSQL 512 MiB, Redis 256 MiB.
- **Software.** Versions as listed in the README.
- **Run settings.** 10 s warm-up (discarded), then 30 s measured per scenario. Users: 20, except 80
  in scenario 6. The `users` field in this run's JSON files reads `0` because of a reporting bug in
  the locustfile (since fixed); the configured counts are recorded in `runner-output.txt`.

| # | Scenario | Requests | Achieved req/s (all / success) | Success | 429 quota | 503 capacity | Other errors | Success P50 / P95 / P99 (ms) |
|---|---|---|---|---|---|---|---|---|
| 1 | Direct backend baseline | 17 390 | 587.7 / 587.7 | 17 390 | 0 | 0 | 0 | 29 / 61 / 88 |
| 2 | One gateway | 5 758 | 193.0 / 193.0 | 5 758 | 0 | 0 | 0 | 88 / 203 / 304 |
| 3 | Two gateways (Nginx), reads | 7 415 | 249.0 / 249.0 | 7 415 | 0 | 0 | 0 | 64 / 226 / 353 |
| 4 | Product/order mix (Nginx) | 7 312 | 245.8 / 245.8 | 7 312 | 0 | 0 | 0 | 67 / 219 / 340 |
| 5 | Quota exhaustion (offered 200 req/s, quota 50 req/s) | 5 963 | 199.8 / 51.5 | 1 538 | 4 425 | 0 | 0 | 29 / 42 / 54 |
| 6 | Slow backend (+200 ms), 80 users at one gateway | 7 495 | 251.8 / 108.0 | 3 216 | 0 | 4 279 | 0 | 412 / 1 296 / 1 888 |

CPU sampled once mid-run with `docker stats` (100 % = one core; Python services are capped at
100 %):

| # | Notable CPU usage |
|---|---|
| 1 | product-1 99 %, Locust 70 %, PostgreSQL 27 % |
| 2 | **gateway-1 98 %**, product-2 53 %, product-1 27 %, PostgreSQL 28 %, Locust 35 % |
| 3 | **gateway-1 76 %, gateway-2 99 %**, products ~33 % each, Redis 12 %, Nginx 8 % |
| 4 | **gateway-1 94 %, gateway-2 97 %**, orders 14 % |
| 5 | gateways ~20 % each (most requests are cheap 429s) |
| 6 | **gateway-1 99 %**, products 14–20 % |

### Observations

- **The gateway process is the bottleneck.** One gateway saturates its 1-CPU limit at about
  190 req/s, which is roughly 5 ms of CPU per proxied request. Per request it does one PostgreSQL
  auth query, one Redis script call, the HTTPX upstream exchange, FastAPI/Starlette routing and
  JSON logging. No profiler was run, so the split between these costs is **not measured**. Profiling
  is the next step before optimizing anything, and an auth cache is not justified until a profile
  shows authentication dominates.
- **Two gateways scale sub-linearly here** (193 → 249 req/s). Both gateways are near their CPU
  limit, but they share the laptop's cores with Locust, Nginx, both products and PostgreSQL. This
  run cannot separate gateway scaling from host contention.
- **Quota accuracy under load is exact to within rounding.** The expected success rate is
  (50 burst + 50 req/s × 30 s) / 30 s ≈ 51.7 req/s; the measured rate is 51.5 req/s. All excess
  requests got 429, with no errors and no 503s.
- **Overload is shed, not queued.** With products delayed 200 ms and 80 users at one gateway
  (offered about 800 req/s), the gateway admitted about 108 req/s and rejected 4 279 requests
  immediately with 503 `capacity_exhausted`. There were zero timeouts and zero other errors.
  Success latency (P50 412 ms vs. 200 ms injected) also reflects gateway-1 running at 99 % CPU.
- **Mixed workload** with order writes performs about the same as reads alone, because the gateway,
  not PostgreSQL, is the limit at this load.
- **No 5xx other than intended capacity 503s** occurred in any scenario.

### Limitations of these numbers

- **Short runs.** 30 s measured windows show no long-term effects (GC, connection churn, AOF
  growth).
- **Shared host.** Locust uses up to 70 % of a core on the same host, and the laptop CPU may
  turbo-boost or throttle, so the numbers vary between runs and machines.
- **Closed-loop clients** (scenarios 1–4) hide latency under queueing ("coordinated omission"), so
  the percentiles are not open-loop latencies.
- **Single sample.** The `docker stats` figures are one sample per scenario, not an average.
