"""Route matching on path-segment boundaries and path safety checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from gateway.app.config import ServiceConfig

_NUMERIC = re.compile(r"^[0-9]+$")
# Encoded slash/backslash/dot or a literal backslash could make the backend
# interpret the path differently from the gateway's prefix match.
_AMBIGUOUS = re.compile(rb"%2f|%5c|%2e|%00|\\", re.IGNORECASE)
MAX_PATH_BYTES = 2048


@dataclass(frozen=True)
class Route:
    service: ServiceConfig

    @property
    def name(self) -> str:
        return self.service.name


def match_route(path: str, services: tuple[ServiceConfig, ...]) -> Route | None:
    """``/products`` and ``/products/...`` match; ``/products-other`` does not."""
    for service in services:
        prefix = service.prefix
        if path == prefix or path.startswith(prefix + "/"):
            return Route(service)
    return None


def is_safe_raw_path(raw_path: bytes) -> bool:
    if not raw_path.startswith(b"/") or len(raw_path) > MAX_PATH_BYTES:
        return False
    if _AMBIGUOUS.search(raw_path):
        return False
    segments = unquote(raw_path.decode("latin-1")).split("/")
    return not any(seg in (".", "..") for seg in segments)


def route_template(path: str, route: Route | None) -> str:
    """Bounded label for logs/metrics: never the raw path."""
    if route is None:
        return "unmatched"
    prefix = route.service.prefix
    rest = path[len(prefix):].strip("/")
    if not rest:
        return prefix
    if "/" not in rest and _NUMERIC.match(rest):
        return f"{prefix}/{{id}}"
    return f"{prefix}/{{path}}"
