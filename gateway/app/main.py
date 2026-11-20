"""Gateway application factory and lifecycle.

Run (one Uvicorn process per container):
    uvicorn --factory gateway.app.main:create_app_from_env --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import start_http_server

from gateway.app.auth import Authenticator
from gateway.app.backends import BackendInstance, BackendPool, HealthMonitor, ProbeFn, http_probe
from gateway.app.config import ServiceConfig, Settings, load_settings
from gateway.app.metrics import Metrics
from gateway.app.ratelimit import RateLimiter
from gateway.app.ratelimit.limiter import create_redis
from gateway.app.service import Gateway
from shared.db import Database
from shared.logging import configure_logging

log = logging.getLogger("gateway")

PROXY_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Below the backends' Uvicorn keep-alive timeout (5 s) so the gateway never
# reuses a connection the backend is about to close (avoids reset races).
UPSTREAM_KEEPALIVE_EXPIRY_S = 2.0

TransportFactory = Callable[[ServiceConfig], httpx.AsyncBaseTransport]


def build_client(settings: Settings, service: ServiceConfig,
                 transport_factory: TransportFactory | None) -> httpx.AsyncClient:
    # Pool size == service admission limit, so an admitted request normally
    # finds a connection; pool_s bounds the wait if it does not.
    limits = httpx.Limits(
        max_connections=service.max_in_flight,
        max_keepalive_connections=service.max_in_flight,
        keepalive_expiry=UPSTREAM_KEEPALIVE_EXPIRY_S,
    )
    transport = (
        transport_factory(service) if transport_factory
        else httpx.AsyncHTTPTransport(limits=limits, retries=0)
    )
    t = settings.http
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=t.connect_s, read=t.read_s, write=t.write_s, pool=t.pool_s),
        follow_redirects=False,
        trust_env=False,
    )


def create_app(
    settings: Settings,
    *,
    transport_factory: TransportFactory | None = None,
    probe: ProbeFn | None = None,
    start_health_loop: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        metrics = Metrics()

        def on_backend_change(instance: BackendInstance, service: str) -> None:
            metrics.backend_healthy.labels(service, instance.name).set(int(instance.eligible))

        db = Database(settings.database)
        await db.open()
        redis = create_redis(settings.redis)
        clients = {s.name: build_client(settings, s, transport_factory) for s in settings.services}
        probe_client = httpx.AsyncClient(
            timeout=settings.health.timeout_s,
            limits=httpx.Limits(max_connections=8, keepalive_expiry=UPSTREAM_KEEPALIVE_EXPIRY_S),
            trust_env=False,
        )
        pools = {
            s.name: BackendPool(
                s, settings.health,
                on_change=lambda inst, name=s.name: on_backend_change(inst, name),
            )
            for s in settings.services
        }
        for pool in pools.values():
            for inst in pool.instances:
                on_backend_change(inst, pool.name)
        monitor = HealthMonitor(list(pools.values()), settings.health,
                                probe or http_probe(probe_client))
        # Establish health before serving: one bounded probe round.
        await monitor.probe_all()
        if start_health_loop:
            monitor.start()
        metrics_server = None
        if settings.metrics_port:
            metrics_server, _ = start_http_server(settings.metrics_port, registry=metrics.registry)

        app.state.gateway = Gateway(
            settings, Authenticator(db), RateLimiter(redis), pools, clients, metrics
        )
        app.state.db, app.state.redis, app.state.monitor = db, redis, monitor
        log.info("gateway.started", extra={"fields": {
            "backends": {p.name: {i.name: i.state for i in p.instances} for p in pools.values()}}})
        try:
            yield
        finally:
            await monitor.stop()
            for client in clients.values():
                await client.aclose()
            await probe_client.aclose()
            await redis.aclose()
            await db.close()
            if metrics_server is not None:
                metrics_server.shutdown()
            log.info("gateway.stopped")

    app = FastAPI(title="gateway", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        # Ready = able to authenticate and obtain quota decisions now.
        state = request.app.state
        db_ok = await state.db.ping(timeout_s=0.5)
        try:
            async with asyncio.timeout(0.5):
                redis_ok = bool(await state.redis.ping())
        except Exception:
            redis_ok = False
        body = {"status": "ready" if db_ok and redis_ok else "not_ready",
                "postgres": db_ok, "redis": redis_ok}
        return JSONResponse(body, 200 if db_ok and redis_ok else 503)

    @app.api_route("/{full_path:path}", methods=PROXY_METHODS, include_in_schema=False)
    async def proxy(request: Request):
        return await request.app.state.gateway.handle(request)

    return app


def create_app_from_env() -> FastAPI:
    settings = load_settings()
    configure_logging("gateway", settings.instance_id, settings.log_level)
    return create_app(settings)
