"""Backend pools: health state, round-robin selection and active probing.

State machine per instance (independently in every gateway process):
    unknown   --probe ok--------------------------------> healthy
    unknown   --`failure_threshold` consecutive failures--> unhealthy
    healthy   --`failure_threshold` consecutive failures--> unhealthy
    unhealthy --`recovery_threshold` consecutive probe successes--> healthy
Only `healthy` instances are selected. Startup is safe: nothing is routed
until a probe succeeds (the gateway runs one probe round before serving).

Failures come from probes AND from real requests (connection/protocol errors),
so a backend that dies between probes is ejected quickly. Request successes do
not count towards recovery; recovery is decided by readiness probes only.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from gateway.app.config import HealthSettings, ServiceConfig, UpstreamInstance

log = logging.getLogger("gateway.health")

UNKNOWN, HEALTHY, UNHEALTHY = "unknown", "healthy", "unhealthy"


@dataclass
class BackendInstance:
    config: UpstreamInstance
    failure_threshold: int
    recovery_threshold: int
    state: str = UNKNOWN
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    on_change: Callable[[BackendInstance], None] | None = field(default=None, repr=False)
    # Last successfully resolved address ("http://ip:port"), set by the
    # health monitor. Requests connect here so they never block on DNS.
    resolved_url: str | None = None
    _lookup: asyncio.Task | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def base_url(self) -> str:
        return self.config.base_url

    @property
    def host_header(self) -> bytes:
        """Host header for the configured name (``host:port``)."""
        return self.base_url.removeprefix("http://").encode("ascii")

    @property
    def connect_url(self) -> str:
        return self.resolved_url or self.base_url

    @property
    def eligible(self) -> bool:
        return self.state == HEALTHY

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.consecutive_successes += 1
        if self.state == UNKNOWN or (
            self.state == UNHEALTHY and self.consecutive_successes >= self.recovery_threshold
        ):
            self._set(HEALTHY)

    def record_failure(self, reason: str) -> None:
        self.consecutive_successes = 0
        self.consecutive_failures += 1
        if self.state != UNHEALTHY and self.consecutive_failures >= self.failure_threshold:
            self._set(UNHEALTHY, reason)

    def _set(self, state: str, reason: str | None = None) -> None:
        previous, self.state = self.state, state
        log.info("backend.state_changed", extra={"fields": {
            "backend": self.name, "from": previous, "to": state, "reason": reason}})
        if self.on_change:
            self.on_change(self)


class BackendPool:
    def __init__(self, service: ServiceConfig, health: HealthSettings,
                 on_change: Callable[[BackendInstance], None] | None = None) -> None:
        self.service = service
        self.instances = [
            BackendInstance(cfg, health.failure_threshold, health.recovery_threshold,
                            on_change=on_change)
            for cfg in service.instances
        ]
        self._counter = itertools.count()

    @property
    def name(self) -> str:
        return self.service.name

    def select(self, exclude: frozenset[str] = frozenset()) -> BackendInstance | None:
        """Round-robin over currently healthy instances (minus `exclude`)."""
        candidates = [i for i in self.instances if i.eligible and i.name not in exclude]
        if not candidates:
            return None
        return candidates[next(self._counter) % len(candidates)]


ProbeFn = Callable[[BackendInstance], Awaitable[bool]]
ResolveFn = Callable[[str, int], Awaitable[str]]


async def dns_resolve(host: str, port: int) -> str:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return infos[0][4][0]


def _consume_result(task: asyncio.Task) -> None:
    # A lookup we stopped waiting for may fail later; retrieve its exception
    # so asyncio does not log "Task exception was never retrieved".
    if not task.cancelled():
        task.exception()


class HealthMonitor:
    """Periodically resolves and probes every instance's readiness endpoint.

    DNS is resolved here, off the request path. getaddrinfo runs in a small,
    uncancellable thread pool (libuv: 4 threads); in Docker a lookup for a
    stopped container takes ~4 s, so per-request lookups would pile up and
    starve lookups for HEALTHY instances. Each instance therefore has at most
    one lookup in flight, and requests connect to the last resolved address.
    """

    def __init__(self, pools: list[BackendPool], settings: HealthSettings,
                 probe: ProbeFn, resolve: ResolveFn | None = dns_resolve) -> None:
        self._pools = pools
        self._settings = settings
        self._probe = probe
        self._resolve = resolve
        self._task: asyncio.Task | None = None

    @property
    def instances(self) -> list[BackendInstance]:
        return [i for pool in self._pools for i in pool.instances]

    async def probe_all(self) -> None:
        instances = self.instances
        results = await asyncio.gather(*(self._check(i) for i in instances))
        for instance, ok in zip(instances, results, strict=True):
            if ok:
                instance.record_success()
            else:
                instance.record_failure("probe_failed")

    async def _check(self, instance: BackendInstance) -> bool:
        if self._resolve is not None and not await self._refresh_address(instance):
            return False
        return await self._safe_probe(instance)

    async def _refresh_address(self, instance: BackendInstance) -> bool:
        parts = urlsplit(instance.base_url)
        task = instance._lookup
        if task is None or task.done():
            task = asyncio.create_task(self._resolve(parts.hostname, parts.port))
            task.add_done_callback(_consume_result)
            instance._lookup = task
        try:
            async with asyncio.timeout(self._settings.timeout_s):
                ip = await asyncio.shield(task)  # a slow lookup keeps running, single-flight
        except Exception:
            return False
        instance.resolved_url = f"http://{ip}:{parts.port}"
        return True

    async def _safe_probe(self, instance: BackendInstance) -> bool:
        try:
            async with asyncio.timeout(self._settings.timeout_s):
                return await self._probe(instance)
        except Exception:
            return False

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.interval_s)
            await self.probe_all()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="health-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        pending = [i._lookup for i in self.instances if i._lookup and not i._lookup.done()]
        for task in pending:
            task.cancel()
        # Returns promptly: cancelling only detaches from the resolver thread.
        await asyncio.gather(*pending, return_exceptions=True)


def http_probe(client: httpx.AsyncClient) -> ProbeFn:
    async def probe(instance: BackendInstance) -> bool:
        response = await client.get(f"{instance.connect_url}/health/ready",
                                    headers={"Host": instance.host_header.decode()})
        return response.status_code == 200

    return probe
