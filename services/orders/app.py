"""Order service (PostgreSQL schema `orders`, read-only lookup in `catalog`).

Identity: the gateway authenticates the caller and forwards the client id in
X-Client-Id (after stripping any client-supplied copy). This service trusts
that header ONLY because it is reachable solely on the private Compose
network; it is not an authentication mechanism on its own.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from shared.db import Database
from shared.devcontrols import install_dev_controls
from shared.logging import configure_logging
from shared.service import ServiceSettings, ServiceState, error_body, install_common

MAX_ID = 2**63 - 1
MAX_QUANTITY = 1000

ORDER_COLUMNS = (
    "id, client_id, product_id, quantity, unit_price_cents, currency, total_cents, created_at"
)

# One atomic statement: the price is read from the trusted catalog and the
# snapshot is written in the same statement (an implicit transaction). If the
# product does not exist, no row is inserted and nothing is returned.
INSERT_ORDER = f"""
INSERT INTO orders.orders (client_id, product_id, quantity, unit_price_cents, currency, total_cents)
SELECT $1::bigint, p.id, $2::integer, p.price_cents, p.currency, p.price_cents * $2::integer
FROM catalog.products AS p
WHERE p.id = $3::bigint
RETURNING {ORDER_COLUMNS}
"""


class OrderCreate(BaseModel):
    # extra="forbid": a client-supplied price (or anything else) is rejected.
    model_config = ConfigDict(extra="forbid", strict=True)
    product_id: int = Field(ge=1, le=MAX_ID)
    quantity: int = Field(ge=1, le=MAX_QUANTITY)


def _order(row) -> dict:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "product_id": row["product_id"],
        "quantity": row["quantity"],
        "unit_price_cents": row["unit_price_cents"],
        "currency": row["currency"],
        "total_cents": row["total_cents"],
        "created_at": row["created_at"].isoformat(),
    }


def _client_id(raw: str | None) -> int:
    if raw is None or not raw.isdigit() or len(raw) > 19:
        raise HTTPException(status_code=401, detail="missing_client_identity")
    value = int(raw)
    if not 1 <= value <= MAX_ID:
        raise HTTPException(status_code=401, detail="missing_client_identity")
    return value


def create_app(settings: ServiceSettings) -> FastAPI:
    state = ServiceState(settings=settings, db=Database(settings.database))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await state.db.open()
        try:
            yield
        finally:
            await state.db.close()

    app = FastAPI(title="orders", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = state
    # Dev controls first so the (outermost) common middleware also wraps
    # injected faults: they still get X-Request-ID / X-Instance-Id and a log line.
    if settings.dev_controls:
        install_dev_controls(app, state.faults)
    install_common(app, state)

    @app.post("/orders", status_code=201)
    async def create_order(
        body: OrderCreate,
        x_client_id: Annotated[str | None, Header()] = None,
    ):
        client_id = _client_id(x_client_id)
        async with state.db.connection() as conn:
            row = await conn.fetchrow(INSERT_ORDER, client_id, body.quantity, body.product_id)
        if row is None:
            return JSONResponse(error_body("unknown_product", "product does not exist"), 422)
        return JSONResponse(_order(row), 201, headers={"Location": f"/orders/{row['id']}"})

    @app.get("/orders/{order_id}")
    async def get_order(
        order_id: Annotated[int, Path(ge=1, le=MAX_ID)],
        x_client_id: Annotated[str | None, Header()] = None,
    ) -> dict:
        client_id = _client_id(x_client_id)
        async with state.db.connection() as conn:
            # Ownership is part of the query: another client's order is
            # indistinguishable from a missing one (no existence leak).
            row = await conn.fetchrow(
                f"SELECT {ORDER_COLUMNS} FROM orders.orders WHERE id = $1 AND client_id = $2",
                order_id,
                client_id,
            )
        if row is None:
            raise HTTPException(status_code=404, detail="order_not_found")
        return _order(row)

    return app


def create_app_from_env() -> FastAPI:
    settings = ServiceSettings.from_env("orders")
    configure_logging("orders", settings.instance_id, settings.log_level)
    return create_app(settings)
