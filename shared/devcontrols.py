"""Development-only fault injection for backend services.

Disabled by default (DEV_CONTROLS_ENABLED=false): the /_dev routes are then not
registered at all. Even when enabled they live outside the /products and
/orders prefixes, so the gateway can never route public traffic to them.

Faults are per process (per container instance), which is what the
balancing/failure tests need.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


@dataclass
class FaultState:
    delay_ms: int = 0
    error_status: int | None = None
    fail_ready: bool = False

    def reset(self) -> None:
        self.delay_ms = 0
        self.error_status = None
        self.fail_ready = False


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delay_ms: int = Field(default=0, ge=0, le=60_000)
    error_status: int | None = Field(default=None, ge=500, le=599)
    fail_ready: bool = False


def install_dev_controls(app: FastAPI, state: FaultState) -> None:
    """Register /_dev routes and the fault-applying middleware."""
    router = APIRouter(prefix="/_dev")

    @router.get("/faults")
    async def get_faults() -> dict:
        return {"delay_ms": state.delay_ms, "error_status": state.error_status,
                "fail_ready": state.fail_ready}

    @router.put("/faults")
    async def put_faults(body: FaultRequest) -> dict:
        state.delay_ms = body.delay_ms
        state.error_status = body.error_status
        state.fail_ready = body.fail_ready
        return await get_faults()

    @router.delete("/faults")
    async def delete_faults() -> dict:
        state.reset()
        return await get_faults()

    app.include_router(router)

    @app.middleware("http")
    async def apply_faults(request: Request, call_next):
        path = request.url.path
        if path.startswith("/_dev") or path.startswith("/health"):
            return await call_next(request)
        if state.delay_ms:
            await asyncio.sleep(state.delay_ms / 1000)
        if state.error_status is not None:
            return JSONResponse(
                {"error": {"code": "injected_fault", "message": "development fault injection"}},
                status_code=state.error_status,
            )
        return await call_next(request)
