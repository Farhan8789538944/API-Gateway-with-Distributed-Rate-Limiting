"""HTTP forwarding primitives: header filtering and bounded body I/O.

Scope: bounded JSON HTTP APIs. Request and response bodies are buffered up to
fixed limits while being read chunk by chunk (never unbounded); streaming,
WebSockets and large uploads are intentionally unsupported.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

import httpx

from gateway.app import errors

# RFC 9110 section 7.6.1 connection-specific headers plus common legacy ones.
HOP_BY_HOP = frozenset({
    b"connection", b"keep-alive", b"proxy-connection", b"proxy-authenticate",
    b"proxy-authorization", b"te", b"trailer", b"transfer-encoding", b"upgrade",
})

# Request headers the gateway never forwards as received.
_DROP_REQUEST = HOP_BY_HOP | {
    b"host",            # rebuilt for the upstream by httpx
    b"content-length",  # recomputed from the buffered body
    b"expect",
    b"x-api-key",       # credentials stay at the gateway
    b"authorization",
    b"x-request-id",    # replaced by the validated/generated id
    b"x-client-id",     # identity headers: only the gateway may set them
    b"x-client-name",
    b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto",
    b"x-real-ip",
    b"accept-encoding",  # set explicitly below
}
_DROP_REQUEST_PREFIXES = (b"x-gateway-",)

# Response headers the gateway never relays as received. Headers the gateway
# sets itself are dropped so a backend cannot duplicate or spoof them.
_DROP_RESPONSE = HOP_BY_HOP | {
    b"content-length", b"date", b"server",
    b"x-request-id", b"x-gateway-instance", b"x-upstream-instance",
    b"x-ratelimit-limit", b"x-ratelimit-remaining",
}

Headers = list[tuple[bytes, bytes]]


def connection_tokens(headers: Iterable[tuple[bytes, bytes]]) -> set[bytes]:
    """Header names listed in Connection are hop-by-hop for this message."""
    tokens: set[bytes] = set()
    for name, value in headers:
        if name.lower() == b"connection":
            tokens.update(t.strip().lower() for t in value.split(b",") if t.strip())
    return tokens


def upstream_request_headers(
    raw: Iterable[tuple[bytes, bytes]],
    *,
    request_id: str,
    client_id: int,
) -> Headers:
    raw = list(raw)
    named = connection_tokens(raw)
    out: Headers = []
    accept_encoding = b"identity"
    for name, value in raw:
        lname = name.lower()
        if lname == b"accept-encoding":
            accept_encoding = value  # client's own preference (raw bytes are relayed)
            continue
        if lname in _DROP_REQUEST or lname in named or lname.startswith(_DROP_REQUEST_PREFIXES):
            continue
        out.append((lname, value))  # repeated headers are preserved in order
    out.append((b"accept-encoding", accept_encoding))
    out.append((b"x-request-id", request_id.encode("ascii")))
    out.append((b"x-client-id", str(client_id).encode("ascii")))
    return out


def downstream_response_headers(raw: Iterable[tuple[bytes, bytes]]) -> Headers:
    raw = list(raw)
    named = connection_tokens(raw)
    return [
        (name.lower(), value)
        for name, value in raw
        if name.lower() not in _DROP_RESPONSE and name.lower() not in named
    ]


def declared_length(headers: Iterable[tuple[bytes, bytes]]) -> int | None:
    """Content-Length if present; raises 400 for malformed/conflicting values."""
    values = {v.strip() for n, v in headers if n.lower() == b"content-length"}
    if not values:
        return None
    if len(values) > 1 or not next(iter(values)).isdigit():
        raise errors.bad_request("invalid_content_length", "invalid Content-Length")
    return int(next(iter(values)))


async def read_bounded(chunks: AsyncIterator[bytes], limit: int, what: str) -> bytes:
    """Accumulate chunks, failing as soon as the total exceeds `limit`."""
    buffer = bytearray()
    async for chunk in chunks:
        if len(buffer) + len(chunk) > limit:
            raise errors.too_large(what)
        buffer.extend(chunk)
    return bytes(buffer)


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: Headers
    body: bytes


class ResponseTooLarge(Exception):
    pass


METHODS_WITHOUT_BODY = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


async def send_upstream(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: httpx.URL,
    headers: Headers,
    body: bytes,
    max_response_bytes: int,
) -> UpstreamResponse:
    """Send one request (no retries, no redirects) and read a bounded response.

    The raw (still content-encoded) bytes are relayed, so Content-Encoding stays
    valid and Content-Length is recomputed from what is actually sent.
    """
    content = None if (not body and method in METHODS_WITHOUT_BODY) else body
    request = client.build_request(method, url, headers=headers, content=content)
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        try:
            declared = declared_length(response.headers.raw) if method != "HEAD" else None
        except errors.GatewayError as exc:
            # A malformed upstream Content-Length is an upstream protocol error.
            raise httpx.RemoteProtocolError("invalid upstream Content-Length") from exc
        if declared is not None and declared > max_response_bytes:
            raise ResponseTooLarge()
        try:
            data = await read_bounded(response.aiter_raw(), max_response_bytes, "response")
        except errors.GatewayError as exc:
            raise ResponseTooLarge() from exc
        return UpstreamResponse(response.status_code, list(response.headers.raw), data)
    finally:
        # Always return/close the connection: success, errors, cancellation.
        await response.aclose()
