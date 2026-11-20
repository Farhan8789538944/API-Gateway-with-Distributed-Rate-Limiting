"""Bounded asyncpg pool helpers.

Every pool is bounded (max_size), every acquisition has a timeout and every
statement has a timeout (command_timeout). Any failure to obtain a result in
time is reported as ``DatabaseUnavailable`` so callers can map it to 503.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg


class DatabaseUnavailable(Exception):
    """PostgreSQL could not serve the request within the configured bounds."""


# Errors that mean "the dependency is unavailable" rather than "bad query".
_TRANSIENT = (
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.TooManyConnectionsError,
    asyncpg.QueryCanceledError,
    asyncpg.AdminShutdownError,
    asyncpg.InvalidAuthorizationSpecificationError,
    asyncpg.InvalidPasswordError,
)


@dataclass(frozen=True)
class PoolSettings:
    dsn: str
    max_size: int = 10
    acquire_timeout_s: float = 1.0
    command_timeout_s: float = 2.0
    connect_timeout_s: float = 2.0


class Database:
    """Lazily-connecting bounded pool (min_size=0: startup does not need PostgreSQL)."""

    def __init__(self, settings: PoolSettings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._settings.dsn,
            min_size=0,
            max_size=self._settings.max_size,
            command_timeout=self._settings.command_timeout_s,
            timeout=self._settings.connect_timeout_s,
            max_inactive_connection_lifetime=60.0,
        )

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await asyncio.wait_for(self._pool.close(), timeout=5)
            except TimeoutError:
                self._pool.terminate()
            self._pool = None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        if self._pool is None:
            raise DatabaseUnavailable("pool is not open")
        try:
            conn = await self._pool.acquire(timeout=self._settings.acquire_timeout_s)
        except _TRANSIENT as exc:
            raise DatabaseUnavailable(type(exc).__name__) from exc
        try:
            yield conn
        except _TRANSIENT as exc:
            raise DatabaseUnavailable(type(exc).__name__) from exc
        finally:
            await self._pool.release(conn)

    async def ping(self, timeout_s: float = 1.0) -> bool:
        try:
            async with asyncio.timeout(timeout_s), self.connection() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except (DatabaseUnavailable, TimeoutError):
            return False

    @property
    def size(self) -> int:
        return 0 if self._pool is None else self._pool.get_size()
