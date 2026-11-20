"""Gateway-generated failures and their HTTP mapping.

``outcome`` is a small fixed vocabulary used for logs and metric labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OUTCOMES = frozenset({
    "success",            # upstream response relayed with status < 500
    "upstream_5xx",       # upstream response relayed with status >= 500 (preserved)
    "not_found",          # unknown gateway route
    "bad_request",        # unsafe/invalid path or framing
    "unauthorized",
    "forbidden",
    "payload_too_large",
    "request_timeout",    # client body not received in time
    "quota_rejected",
    "capacity_rejected",
    "dependency_unavailable",  # Postgres/Redis (fail closed)
    "no_healthy_backend",
    "upstream_error",     # connect/protocol failure or oversized response
    "upstream_timeout",
    "client_disconnected",
    "cancelled",
    "internal_error",
})


@dataclass
class GatewayError(Exception):
    status: int
    code: str
    message: str
    outcome: str
    headers: dict[str, str] = field(default_factory=dict)
    failure: str | None = None  # finer, still bounded, failure category for logs

    def __post_init__(self) -> None:
        assert self.outcome in OUTCOMES, self.outcome
        super().__init__(self.code)


def not_found() -> GatewayError:
    return GatewayError(404, "route_not_found", "no gateway route matches this path", "not_found")


def bad_path() -> GatewayError:
    return GatewayError(400, "invalid_path", "path is not allowed", "bad_request")


def bad_request(code: str, message: str) -> GatewayError:
    return GatewayError(400, code, message, "bad_request")


def unauthorized(reason: str) -> GatewayError:
    # One generic message for every reason (no account/key enumeration); the
    # specific reason is only logged.
    return GatewayError(401, "unauthorized", "missing or invalid API key", "unauthorized",
                        headers={"WWW-Authenticate": 'ApiKey header="X-API-Key"'},
                        failure=reason)


def no_policy() -> GatewayError:
    return GatewayError(403, "no_quota_policy", "client has no rate-limit policy assigned",
                        "forbidden", failure="no_policy")


def too_large(what: str) -> GatewayError:
    return GatewayError(413, "payload_too_large", f"{what} exceeds the size limit",
                        "payload_too_large")


def body_timeout() -> GatewayError:
    return GatewayError(408, "request_body_timeout", "request body not received in time",
                        "request_timeout")


def quota_exceeded(retry_after_s: int, limit: int) -> GatewayError:
    return GatewayError(429, "rate_limited", "quota exhausted", "quota_rejected",
                        headers={"Retry-After": str(retry_after_s),
                                 "X-RateLimit-Limit": str(limit),
                                 "X-RateLimit-Remaining": "0"})


def capacity_exhausted(scope: str) -> GatewayError:
    return GatewayError(503, "capacity_exhausted", "server is at capacity, retry later",
                        "capacity_rejected", headers={"Retry-After": "1"}, failure=scope)


def dependency_unavailable(dependency: str) -> GatewayError:
    return GatewayError(503, "dependency_unavailable", "a required dependency is unavailable",
                        "dependency_unavailable", headers={"Retry-After": "1"},
                        failure=dependency)


def no_healthy_backend(service: str) -> GatewayError:
    return GatewayError(503, "no_healthy_backend", "no healthy backend instance available",
                        "no_healthy_backend", headers={"Retry-After": "1"}, failure=service)


def upstream_failure(kind: str) -> GatewayError:
    return GatewayError(502, "bad_gateway", "upstream connection or protocol failure",
                        "upstream_error", failure=kind)


def upstream_timeout(kind: str) -> GatewayError:
    return GatewayError(504, "gateway_timeout", "upstream did not respond in time",
                        "upstream_timeout", failure=kind)
