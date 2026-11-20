import asyncio
from collections import Counter

import anyio
import pytest

from gateway.app.admission import Admission
from gateway.app.backends import BackendPool, HealthMonitor
from gateway.app.config import HealthSettings, ServiceConfig, UpstreamInstance
from gateway.app.errors import GatewayError
from gateway.app.proxy import (
    declared_length,
    downstream_response_headers,
    read_bounded,
    upstream_request_headers,
)

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------- headers
def names(headers):
    return [n for n, _ in headers]


def test_upstream_headers_strip_credentials_identity_and_hop_by_hop():
    raw = [
        (b"Host", b"evil.example"),
        (b"X-API-Key", b"gwk_secret"),
        (b"Authorization", b"Bearer x"),
        (b"X-Client-Id", b"999"),
        (b"X-Client-Name", b"admin"),
        (b"X-Gateway-Instance", b"spoof"),
        (b"X-Request-ID", b"client-chosen"),
        (b"X-Forwarded-For", b"1.2.3.4"),
        (b"Connection", b"keep-alive, X-Custom-Hop"),
        (b"X-Custom-Hop", b"drop-me"),
        (b"Keep-Alive", b"timeout=5"),
        (b"Transfer-Encoding", b"chunked"),
        (b"TE", b"trailers"),
        (b"Upgrade", b"websocket"),
        (b"Content-Length", b"5"),
        (b"Content-Type", b"application/json"),
        (b"X-Trace", b"a"),
        (b"X-Trace", b"b"),
    ]
    out = upstream_request_headers(raw, request_id="rid-1", client_id=42)
    assert (b"content-type", b"application/json") in out
    # repeated headers keep order
    assert [v for n, v in out if n == b"x-trace"] == [b"a", b"b"]
    forbidden = {b"host", b"x-api-key", b"authorization", b"x-gateway-instance",
                 b"x-client-name", b"x-forwarded-for", b"connection", b"x-custom-hop",
                 b"keep-alive", b"transfer-encoding", b"te", b"upgrade", b"content-length"}
    assert not forbidden & set(names(out))
    # identity is exactly the authenticated one; request id is the gateway's
    assert [v for n, v in out if n == b"x-client-id"] == [b"42"]
    assert [v for n, v in out if n == b"x-request-id"] == [b"rid-1"]
    assert [v for n, v in out if n == b"accept-encoding"] == [b"identity"]


def test_client_accept_encoding_is_preserved():
    out = upstream_request_headers([(b"accept-encoding", b"gzip")], request_id="r",
                                   client_id=1)
    assert [v for n, v in out if n == b"accept-encoding"] == [b"gzip"]


def test_downstream_headers_filter_hop_by_hop_and_keep_repeats():
    raw = [
        (b"content-type", b"application/json"),
        (b"content-encoding", b"gzip"),
        (b"content-length", b"999"),
        (b"connection", b"close, x-internal"),
        (b"x-internal", b"secret"),
        (b"transfer-encoding", b"chunked"),
        (b"set-cookie", b"a=1"),
        (b"set-cookie", b"b=2"),
        (b"date", b"x"),
        (b"server", b"uvicorn"),
        # Regression: gateway-owned headers from the backend were duplicated.
        (b"x-request-id", b"backend-copy"),
        (b"x-gateway-instance", b"spoofed"),
        (b"x-ratelimit-remaining", b"999"),
    ]
    out = downstream_response_headers(raw)
    assert out == [(b"content-type", b"application/json"), (b"content-encoding", b"gzip"),
                   (b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]


@pytest.mark.parametrize("headers,expected", [
    ([], None), ([(b"content-length", b"10")], 10), ([(b"Content-Length", b" 0 ")], 0),
    ([(b"content-length", b"5"), (b"content-length", b"5")], 5),
])
def test_declared_length(headers, expected):
    assert declared_length(headers) == expected


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"-1")], [(b"content-length", b"abc")],
    [(b"content-length", b"5"), (b"content-length", b"6")],
])
def test_declared_length_rejects_invalid(headers):
    with pytest.raises(GatewayError) as info:
        declared_length(headers)
    assert info.value.status == 400


async def test_read_bounded_stops_at_limit_without_consuming_everything():
    consumed = 0

    async def chunks():
        nonlocal consumed
        for _ in range(1000):
            consumed += 1
            yield b"x" * 100

    with pytest.raises(GatewayError) as info:
        await read_bounded(chunks(), 1000, "request body")
    assert info.value.status == 413
    assert consumed == 11  # stopped at the first chunk that crossed the limit


async def test_read_bounded_accepts_exact_limit():
    async def chunks():
        yield b"a" * 6
        yield b"b" * 4

    assert await read_bounded(chunks(), 10, "x") == b"aaaaaabbbb"


# --------------------------------------------------------------- backends
def make_pool(n=3, fall=2, rise=2):
    svc = ServiceConfig("products", "/products",
                        tuple(UpstreamInstance(f"p{i}", f"http://p{i}:8000") for i in range(n)),
                        10)
    return BackendPool(svc, HealthSettings(failure_threshold=fall, recovery_threshold=rise))


def test_nothing_is_selected_before_health_is_established():
    pool = make_pool()
    assert pool.select() is None


def test_round_robin_over_healthy_instances():
    pool = make_pool()
    for inst in pool.instances:
        inst.record_success()
    picks = [pool.select().name for _ in range(6)]
    assert picks == ["p0", "p1", "p2", "p0", "p1", "p2"]
    assert pool.select(exclude=frozenset({"p0", "p1"})).name == "p2"
    assert pool.select(exclude=frozenset({"p0", "p1", "p2"})) is None


def test_failure_and_recovery_thresholds():
    pool = make_pool(n=1, fall=2, rise=3)
    inst = pool.instances[0]
    inst.record_success()
    assert inst.eligible
    inst.record_failure("x")
    assert inst.eligible  # one failure is below the threshold
    inst.record_success()
    inst.record_failure("x")
    assert inst.eligible  # success reset the consecutive count
    inst.record_failure("x")
    assert not inst.eligible
    inst.record_success()
    inst.record_success()
    assert not inst.eligible
    inst.record_failure("x")  # interrupts recovery
    for _ in range(3):
        inst.record_success()
    assert inst.eligible


async def test_monitor_probes_marks_and_stops_cleanly():
    pool = make_pool(n=2, fall=1, rise=1)
    healthy = {"p0": True, "p1": False}

    async def probe(inst):
        if inst.name == "p1":
            await asyncio.sleep(10)  # hangs: must be cut by the probe timeout
        return healthy[inst.name]

    monitor = HealthMonitor([pool], HealthSettings(interval_s=0.01, timeout_s=0.05,
                                                   failure_threshold=1, recovery_threshold=1),
                            probe, resolve=None)
    await monitor.probe_all()
    assert [i.state for i in pool.instances] == ["healthy", "unhealthy"]
    monitor.start()
    await asyncio.sleep(0.1)
    task = monitor._task
    await monitor.stop()
    assert task.done() and monitor._task is None


async def test_dns_lookups_are_single_flight_and_off_the_request_path():
    """Regression: hanging lookups for a stopped container starved healthy ones."""
    pool = make_pool(n=2, fall=1, rise=1)
    calls = Counter()
    addresses = {"p0": "10.0.0.5"}

    async def resolve(host, port):
        calls[host] += 1
        if host == "p1":
            await asyncio.sleep(3600)  # behaves like a lookup stuck in a resolver thread
        return addresses[host]

    async def probe(inst):
        return True

    monitor = HealthMonitor([pool], HealthSettings(timeout_s=0.05, failure_threshold=1,
                                                   recovery_threshold=1), probe, resolve)
    for _ in range(5):
        await monitor.probe_all()
    assert calls["p1"] == 1  # never more than one lookup in flight per instance
    assert calls["p0"] == 5  # healthy names are re-resolved every round
    p0, p1 = pool.instances
    assert p0.eligible and p0.connect_url == "http://10.0.0.5:8000"
    assert p0.host_header == b"p0:8000"
    assert not p1.eligible
    addresses["p0"] = "10.0.0.9"  # container recreated with a new IP
    await monitor.probe_all()
    assert p0.connect_url == "http://10.0.0.9:8000"
    await monitor.stop()
    assert p1._lookup.cancelled() or p1._lookup.done()


# -------------------------------------------------------------- admission
async def test_admission_rejects_immediately_when_full():
    admission = Admission("global", 2)
    a, b = admission.try_acquire(), admission.try_acquire()
    assert a and b and admission.in_use == 2
    with anyio.fail_after(0.1):  # must not wait
        assert admission.try_acquire() is None
        with pytest.raises(GatewayError) as info:
            async with admission.slot():
                pass
    assert info.value.status == 503
    admission.release(a)
    admission.release(b)
    assert admission.in_use == 0


async def test_admission_slot_released_on_exception_and_cancellation():
    admission = Admission("svc", 1)
    with pytest.raises(RuntimeError):
        async with admission.slot():
            raise RuntimeError("boom")
    assert admission.in_use == 0

    entered = asyncio.Event()

    async def hold():
        async with admission.slot():
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(hold())
    await entered.wait()
    assert admission.in_use == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.in_use == 0


async def test_admission_release_from_other_task():
    admission = Admission("svc", 1)
    token = admission.try_acquire()

    async def release_elsewhere():
        admission.release(token)

    await asyncio.create_task(release_elsewhere())
    assert admission.in_use == 0
    assert admission.try_acquire() is not None
