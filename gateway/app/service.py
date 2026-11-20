"""The gateway request pipeline.

Order of checks (each step either continues or produces exactly one status):

  1. route match on segment boundaries ............ 404 unknown route
  2. path safety / declared Content-Length ........ 400 / 413
  3. global admission (immediate, per process) .... 503 capacity
  4. API-key authentication (PostgreSQL) .......... 401 / 403 no policy / 503 db
  5. quota decision (Redis Lua, atomic) ........... 429 + Retry-After / 503 redis
  6. bounded, time-limited request body read ...... 413 / 408
  7. per-service admission (immediate) ............ 503 capacity
  8. healthy backend selection (round robin) ...... 503 no healthy backend
  9. forward under ONE total upstream deadline .... 502 connect/protocol / 504 deadline
 10. relay the upstream status unchanged (incl. 4xx/5xx from the backend)

Quota semantics: once step 5 allows a request its allowance is consumed, even
if a later step rejects it or the backend fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from gateway.app import errors
from gateway.app.admission import Admission
from gateway.app.auth import Authenticator, Principal
from gateway.app.backends import BackendPool
from gateway.app.config import Settings
from gateway.app.errors import GatewayError
from gateway.app.metrics import Metrics, status_class
from gateway.app.proxy import (
    Headers,
    ResponseTooLarge,
    UpstreamResponse,
    declared_length,
    downstream_response_headers,
    read_bounded,
    send_upstream,
    upstream_request_headers,
)
from gateway.app.ratelimit import Decision, RateLimiter, RateLimitUnavailable
from gateway.app.routing import Route, is_safe_raw_path, match_route, route_template

log = logging.getLogger("gateway.request")

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Only requests that never reached a server (connect failure) are retried, and
# only for idempotent methods. Nothing is retried after bytes were sent.
CONNECT_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
NO_BODY_STATUSES = frozenset({204, 304})


@dataclass
class RequestContext:
    request_id: str
    method: str
    started: float
    route: str = "unmatched"
    client_id: int | None = None
    key_id: str | None = None
    backend: str | None = None
    status: int = 500
    outcome: str = "internal_error"
    failure: str | None = None
    rate_limit: str = "not_checked"
    upstream_s: float | None = None


def request_id_from(request: Request) -> str:
    values = request.headers.getlist("x-request-id")
    if len(values) == 1 and REQUEST_ID_RE.match(values[0]):
        return values[0]
    return uuid.uuid4().hex


class Gateway:
    def __init__(
        self,
        settings: Settings,
        authenticator: Authenticator,
        limiter: RateLimiter,
        pools: dict[str, BackendPool],
        clients: dict[str, httpx.AsyncClient],
        metrics: Metrics,
    ) -> None:
        self.settings = settings
        self.auth = authenticator
        self.limiter = limiter
        self.pools = pools
        self.clients = clients
        self.metrics = metrics
        self.global_admission = Admission("global", settings.global_max_in_flight)
        self.service_admission = {
            svc.name: Admission(svc.name, svc.max_in_flight) for svc in settings.services
        }

    # ------------------------------------------------------------------ entry
    async def handle(self, request: Request) -> Response:
        ctx = RequestContext(request_id_from(request), request.method, time.perf_counter())
        try:
            response = await self._process(request, ctx)
        except GatewayError as exc:
            response = self._error_response(exc, ctx)
        except asyncio.CancelledError:
            ctx.outcome, ctx.status = "cancelled", 499
            raise
        except Exception:
            log.exception("gateway.unhandled_error", extra={"fields": {
                "request_id": ctx.request_id}})
            exc = GatewayError(500, "internal_error", "internal gateway error", "internal_error")
            response = self._error_response(exc, ctx)
        finally:
            self._finish(ctx)
        return response

    # --------------------------------------------------------------- pipeline
    async def _process(self, request: Request, ctx: RequestContext) -> Response:
        path = request.url.path
        route = match_route(path, self.settings.services)
        ctx.route = route_template(path, route)
        if route is None:
            raise errors.not_found()
        raw_path = request.scope.get("raw_path") or path.encode("utf-8")
        if not is_safe_raw_path(raw_path):
            raise errors.bad_path()
        declared = declared_length(request.headers.raw)
        if declared is not None and declared > self.settings.max_request_body_bytes:
            raise errors.too_large("request body")

        async with self.global_admission.slot():
            self.metrics.in_flight.inc()
            try:
                principal = await self._authenticate(request, ctx)
                decision = await self._check_quota(principal, ctx)
                body = await self._read_body(request)
                admission = self.service_admission[route.name]
                async with admission.slot():
                    gauge = self.metrics.upstream_in_flight.labels(route.name)
                    gauge.inc()
                    try:
                        upstream = await self._forward(request, route, principal, body,
                                                       raw_path, ctx)
                    finally:
                        gauge.dec()
            finally:
                self.metrics.in_flight.dec()
        return self._relay(request.method, upstream, ctx, decision)

    async def _authenticate(self, request: Request, ctx: RequestContext) -> Principal:
        keys = request.headers.getlist("x-api-key")
        if len(keys) > 1:
            raise errors.unauthorized("multiple_keys")
        principal = await self.auth.authenticate(keys[0] if keys else None)
        ctx.client_id, ctx.key_id = principal.client_id, principal.key_id
        return principal

    async def _check_quota(self, principal: Principal, ctx: RequestContext) -> Decision:
        algorithm = principal.policy.algorithm
        try:
            decision = await self.limiter.check(principal.client_id, principal.policy)
        except RateLimitUnavailable as exc:
            ctx.rate_limit = "error"
            self.metrics.rate_limit_decisions.labels(algorithm, "error").inc()
            raise errors.dependency_unavailable("redis") from exc
        self.metrics.redis_decision_latency.labels(algorithm).observe(decision.duration_s)
        result = "allowed" if decision.allowed else "rejected"
        self.metrics.rate_limit_decisions.labels(algorithm, result).inc()
        ctx.rate_limit = result
        if not decision.allowed:
            raise errors.quota_exceeded(decision.retry_after_s, decision.limit)
        return decision

    async def _read_body(self, request: Request) -> bytes:
        try:
            async with asyncio.timeout(self.settings.body_read_timeout_s):
                return await read_bounded(
                    request.stream(), self.settings.max_request_body_bytes, "request body"
                )
        except TimeoutError as exc:
            raise errors.body_timeout() from exc
        except ClientDisconnect as exc:
            raise GatewayError(400, "client_disconnected", "client disconnected",
                               "client_disconnected") from exc

    async def _forward(self, request: Request, route: Route, principal: Principal,
                       body: bytes, raw_path: bytes, ctx: RequestContext) -> UpstreamResponse:
        service = route.name
        pool, client = self.pools[service], self.clients[service]
        headers = upstream_request_headers(
            request.headers.raw, request_id=ctx.request_id, client_id=principal.client_id
        )
        query = request.scope.get("query_string", b"")
        target = raw_path + (b"?" + query if query else b"")
        tried: set[str] = set()
        started = time.perf_counter()
        try:
            # ONE deadline for selection, connect, send, wait and body read,
            # including the single connect-failure retry.
            async with asyncio.timeout(self.settings.upstream_deadline_s):
                while True:
                    instance = pool.select(exclude=frozenset(tried))
                    if instance is None:
                        if tried:
                            raise errors.upstream_failure("connect")
                        raise errors.no_healthy_backend(service)
                    tried.add(instance.name)
                    ctx.backend = instance.name
                    # Connect to the address resolved by the health monitor
                    # (no DNS on the request path); Host keeps the configured name.
                    url = httpx.URL(instance.connect_url).copy_with(raw_path=target)
                    try:
                        response = await send_upstream(
                            client, method=request.method, url=url,
                            headers=[(b"host", instance.host_header), *headers],
                            body=body, max_response_bytes=self.settings.max_response_body_bytes,
                        )
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        instance.record_failure("connect_error")
                        self.metrics.dependency_failures.labels("upstream_connect").inc()
                        if request.method in CONNECT_RETRY_METHODS and len(tried) < 2:
                            continue
                        raise errors.upstream_failure("connect") from None
                    ctx.upstream_s = time.perf_counter() - started
                    self.metrics.upstream_latency.labels(service).observe(ctx.upstream_s)
                    return response
        except TimeoutError as exc:
            self.metrics.upstream_timeouts.labels(service).inc()
            raise errors.upstream_timeout("deadline") from exc
        except httpx.PoolTimeout as exc:
            raise errors.capacity_exhausted(f"{service}_connection_pool") from exc
        except httpx.TimeoutException as exc:  # read/write timeout
            self.metrics.upstream_timeouts.labels(service).inc()
            raise errors.upstream_timeout(type(exc).__name__) from exc
        except ResponseTooLarge as exc:
            raise errors.upstream_failure("response_too_large") from exc
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as exc:
            if ctx.backend:
                pool_instance = next(i for i in pool.instances if i.name == ctx.backend)
                pool_instance.record_failure("protocol_error")
            self.metrics.dependency_failures.labels("upstream_protocol").inc()
            raise errors.upstream_failure("protocol") from exc
        except httpx.HTTPError as exc:
            raise errors.upstream_failure(type(exc).__name__) from exc

    # -------------------------------------------------------------- responses
    def _relay(self, method: str, upstream: UpstreamResponse, ctx: RequestContext,
               decision: Decision) -> Response:
        status = upstream.status
        headers = downstream_response_headers(upstream.headers)
        if method == "HEAD":
            body = b""
            # HEAD: relay the upstream's Content-Length describing the GET body.
            headers += [(n, v) for n, v in upstream.headers if n.lower() == b"content-length"][:1]
        elif status in NO_BODY_STATUSES or status < 200:
            body = b""
        else:
            body = upstream.body
            headers.append((b"content-length", str(len(body)).encode()))
        headers.append((b"x-ratelimit-limit", str(decision.limit).encode()))
        headers.append((b"x-ratelimit-remaining", str(decision.remaining).encode()))
        self._common_headers(headers, ctx)
        ctx.status = status
        ctx.outcome = "success" if status < 500 else "upstream_5xx"
        response = Response(content=body, status_code=status)
        response.raw_headers = headers
        return response

    def _error_response(self, exc: GatewayError, ctx: RequestContext) -> Response:
        ctx.status, ctx.outcome, ctx.failure = exc.status, exc.outcome, exc.failure
        payload = {"error": {"code": exc.code, "message": exc.message,
                             "request_id": ctx.request_id}}
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers: Headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        headers += [(k.lower().encode(), v.encode()) for k, v in exc.headers.items()]
        self._common_headers(headers, ctx)
        response = Response(content=body, status_code=exc.status)
        response.raw_headers = headers
        return response

    def _common_headers(self, headers: Headers, ctx: RequestContext) -> None:
        headers.append((b"x-request-id", ctx.request_id.encode()))
        if self.settings.expose_instance_headers:
            headers.append((b"x-gateway-instance", self.settings.instance_id.encode()))
            if ctx.backend:
                headers.append((b"x-upstream-instance", ctx.backend.encode()))

    # -------------------------------------------------------------- telemetry
    def _finish(self, ctx: RequestContext) -> None:
        total = time.perf_counter() - ctx.started
        m = self.metrics
        m.requests.labels(ctx.route, status_class(ctx.status), ctx.outcome).inc()
        if ctx.outcome == "success":
            m.success_latency.labels(ctx.route).observe(total)
        elif ctx.outcome == "capacity_rejected":
            m.capacity_rejections.labels(ctx.failure or "unknown").inc()
        elif ctx.outcome == "dependency_unavailable":
            m.dependency_failures.labels(ctx.failure or "unknown").inc()
        elif ctx.outcome == "no_healthy_backend":
            m.dependency_failures.labels("no_healthy_backend").inc()
        level = logging.WARNING if ctx.status >= 500 else logging.INFO
        log.log(level, "request.completed", extra={"fields": {
            "request_id": ctx.request_id,
            "gateway_instance": self.settings.instance_id,
            "client_id": ctx.client_id,
            "key_id": ctx.key_id,           # public identifier, never the secret
            "method": ctx.method,
            "route": ctx.route,             # bounded template, never the raw path/query
            "backend": ctx.backend,
            "status": ctx.status,
            "outcome": ctx.outcome,
            "failure": ctx.failure,
            "rate_limit": ctx.rate_limit,
            "duration_ms": round(total * 1000, 2),
            "upstream_ms": None if ctx.upstream_s is None else round(ctx.upstream_s * 1000, 2),
        }})
