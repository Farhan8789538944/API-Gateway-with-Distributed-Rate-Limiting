"""Immediate (non-queuing) admission control, per process.

Uses anyio.CapacityLimiter with acquire_on_behalf_of_nowait: a full limiter
raises WouldBlock immediately, so there is no unbounded wait queue. Tokens are
explicit objects, so release does not depend on the acquiring task.

These limits are per gateway process. Two replicas with 20 slots each admit
up to 40 concurrent requests in total; there is no global in-flight limit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio

from gateway.app import errors


class Admission:
    def __init__(self, scope: str, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.scope = scope
        self.limit = limit
        self._limiter = anyio.CapacityLimiter(limit)

    @property
    def in_use(self) -> int:
        return self._limiter.borrowed_tokens

    def try_acquire(self) -> object | None:
        token = object()
        try:
            self._limiter.acquire_on_behalf_of_nowait(token)
        except anyio.WouldBlock:
            return None
        return token

    def release(self, token: object) -> None:
        self._limiter.release_on_behalf_of(token)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        token = self.try_acquire()
        if token is None:
            raise errors.capacity_exhausted(self.scope)
        try:
            yield
        finally:
            # Runs on success, exceptions and cancellation alike.
            self.release(token)
