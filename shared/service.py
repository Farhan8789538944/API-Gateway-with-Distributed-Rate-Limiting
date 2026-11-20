"""Common plumbing for the backend services (products, orders)."""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.db import Database, DatabaseUnavailable, PoolSettings
from shared.devcontrols import FaultState
from shared.env import env_bool, env_float, env_int, env_str

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
log = logging.getLogger("service.request")


@dataclass(frozen=True)
class ServiceSettings:
    service: str
    instance_id: str
    database: PoolSettings
    dev_controls: bool
    expose_instance_header: bool
    log_level: str

    @classmethod
    def from_env(cls, service: str) -> ServiceSettings:
        return cls(
            service=service,
            instance_id=env_str("INSTANCE_ID", service),
            database=PoolSettings(
                dsn=env_str("DATABASE_URL"),
                max_size=env_int("DB_POOL_MAX", 10, minimum=1, maximum=100),
                acquire_timeout_s=env_float("DB_ACQUIRE_TIMEOUT_S", 1.0, minimum=0.01),
                command_timeout_s=env_float("DB_COMMAND_TIMEOUT_S", 2.0, minimum=0.01),
            ),
            dev_controls=env_bool("DEV_CONTROLS_ENABLED", False),
            expose_instance_header=env_bool("EXPOSE_INSTANCE_HEADERS", False),
            log_level=env_str("LOG_LEVEL", "INFO"),
        )


@dataclass
class ServiceState:
    settings: ServiceSettings
    db: Database
    faults: FaultState = field(default_factory=FaultState)


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def install_common(app: FastAPI, state: ServiceState) -> None:
    """Request logging, instance header, uniform error bodies and health endpoints."""
    settings = state.settings

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        started = time.perf_counter()
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if settings.expose_instance_header:
            response.headers["X-Instance-Id"] = settings.instance_id
        route = request.scope.get("route")
        log.info(
            "request.completed",
            extra={"fields": {
                "request_id": request_id,
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }},
        )
        return response

    @app.exception_handler(DatabaseUnavailable)
    async def db_unavailable(_: Request, exc: DatabaseUnavailable):
        log.warning("database.unavailable", extra={"fields": {"reason": str(exc)}})
        return JSONResponse(error_body("database_unavailable", "database unavailable"), 503)

    @app.exception_handler(RequestValidationError)
    async def validation_failed(_: Request, exc: RequestValidationError):
        details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        body = error_body("validation_failed", "request validation failed")
        body["error"]["details"] = details
        return JSONResponse(body, 422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        code = exc.detail if isinstance(exc.detail, str) else "http_error"
        return JSONResponse(error_body(code, code.replace("_", " ")), exc.status_code)

    @app.get("/health/live")
    async def live() -> dict:
        # Liveness: the process and event loop respond. Dependencies are NOT
        # checked, so a database outage does not cause restarts.
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        # Readiness: able to serve real requests right now (database reachable).
        if state.faults.fail_ready:
            return JSONResponse({"status": "not_ready", "reason": "injected"}, 503)
        if not await state.db.ping(timeout_s=0.5):
            return JSONResponse({"status": "not_ready", "reason": "database"}, 503)
        return {"status": "ready"}
