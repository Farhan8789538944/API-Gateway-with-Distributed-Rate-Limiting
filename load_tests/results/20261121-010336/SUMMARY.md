# Benchmark run 20261121-010336

- Docker: 12 CPUs, 8184336384 bytes memory, Docker Desktop, engine 29.8.0
- Host CPU: 12th Gen Intel(R) Core(TM) i5-1235U; host memory: 15.7 GB
- Warm-up 10 s (discarded), measured 30 s

| Scenario | Users / wait | Requests | Achieved req/s (all / success) | Success | 429 quota | 503 capacity | Other errors | Success P50 / P95 / P99 ms |
|---|---|---|---|---|---|---|---|---|
| 1-direct-baseline | 0 / none | 17390 | 587.7 / 587.7 | 17390 | 0 | 0 | 0 | 29.08 / 61.41 / 88.17 |
| 2-one-gateway | 0 / none | 5758 | 193.0 / 193.0 | 5758 | 0 | 0 | 0 | 87.99 / 203.25 / 304.49 |
| 3-two-gateways-reads | 0 / none | 7415 | 249.0 / 249.0 | 7415 | 0 | 0 | 0 | 64.17 / 226.13 / 353.14 |
| 4-product-order-mix | 0 / none | 7312 | 245.8 / 245.8 | 7312 | 0 | 0 | 0 | 67.33 / 218.94 / 340.02 |
| 5-quota-exhaustion | 0 / throughput:10 | 5963 | 199.8 / 51.5 | 1538 | 4425 | 0 | 0 | 28.59 / 42.45 / 53.8 |
| 6-slow-backend-overload | 0 / throughput:10 | 7495 | 251.8 / 108.0 | 3216 | 0 | 4279 | 0 | 411.71 / 1296.48 / 1888.12 |

| Scenario | CPU usage sampled mid-run (docker stats) |
|---|---|
| 1-direct-baseline | locust 70.11%, gateway-1 2.06%, gateway-2 1.64%, orders 1.15%, product-1 99.23%, product-2 1.08%, nginx 0.00%, redis 4.47%, postgres 27.08% |
| 2-one-gateway | locust 34.63%, gateway-1 98.18%, gateway-2 1.73%, orders 1.08%, product-1 26.87%, product-2 52.83%, nginx 3.67%, redis 7.71%, postgres 28.31% |
| 3-two-gateways-reads | locust 44.83%, gateway-1 76.13%, gateway-2 99.37%, orders 1.03%, product-1 32.89%, product-2 32.61%, nginx 8.37%, redis 12.46%, postgres 28.73% |
| 4-product-order-mix | locust 40.14%, gateway-1 93.68%, gateway-2 96.51%, orders 14.35%, product-1 25.67%, product-2 51.44%, nginx 8.13%, redis 10.82%, postgres 27.04% |
| 5-quota-exhaustion | locust 23.64%, gateway-1 20.23%, gateway-2 21.06%, orders 0.88%, product-1 6.75%, product-2 29.26%, nginx 5.42%, redis 5.96%, postgres 16.66% |
| 6-slow-backend-overload | locust 36.29%, gateway-1 98.56%, gateway-2 1.20%, orders 0.92%, product-1 13.89%, product-2 19.74%, nginx 0.00%, redis 7.59%, postgres 24.33% |
