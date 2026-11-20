"""Product catalog service (read-only, PostgreSQL schema `catalog`)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query

from shared.db import Database
from shared.devcontrols import install_dev_controls
from shared.logging import configure_logging
from shared.service import ServiceSettings, ServiceState, install_common

MAX_PAGE_SIZE = 100
MAX_ID = 2**63 - 1

PRODUCT_COLUMNS = "id, sku, name, description, price_cents, currency"


def _product(row) -> dict:
    return {
        "id": row["id"],
        "sku": row["sku"],
        "name": row["name"],
        "description": row["description"],
        "price_cents": row["price_cents"],
        "currency": row["currency"],
    }


def create_app(settings: ServiceSettings) -> FastAPI:
    state = ServiceState(settings=settings, db=Database(settings.database))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await state.db.open()
        try:
            yield
        finally:
            await state.db.close()

    app = FastAPI(title="products", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = state
    # Dev controls first so the (outermost) common middleware also wraps
    # injected faults: they still get X-Request-ID / X-Instance-Id and a log line.
    if settings.dev_controls:
        install_dev_controls(app, state.faults)
    install_common(app, state)

    # HEAD is listed explicitly: FastAPI does not add it to GET routes.
    @app.api_route("/products", methods=["GET", "HEAD"])
    async def list_products(
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
        after_id: Annotated[int, Query(ge=0, le=MAX_ID)] = 0,
    ) -> dict:
        # Keyset pagination: stable and O(limit) regardless of depth.
        async with state.db.connection() as conn:
            rows = await conn.fetch(
                f"SELECT {PRODUCT_COLUMNS} FROM catalog.products WHERE id > $1 "
                "ORDER BY id LIMIT $2",
                after_id,
                limit,
            )
        items = [_product(r) for r in rows]
        next_after = items[-1]["id"] if len(items) == limit else None
        return {"items": items, "next_after_id": next_after}

    @app.api_route("/products/{product_id}", methods=["GET", "HEAD"])
    async def get_product(product_id: Annotated[int, Path(ge=1, le=MAX_ID)]) -> dict:
        async with state.db.connection() as conn:
            row = await conn.fetchrow(
                f"SELECT {PRODUCT_COLUMNS} FROM catalog.products WHERE id = $1", product_id
            )
        if row is None:
            raise HTTPException(status_code=404, detail="product_not_found")
        return _product(row)

    return app


def create_app_from_env() -> FastAPI:
    settings = ServiceSettings.from_env("products")
    configure_logging("products", settings.instance_id, settings.log_level)
    return create_app(settings)
