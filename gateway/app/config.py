"""Gateway settings, loaded once from the environment and validated.

Routes are fixed in code (/products -> products pool, /orders -> orders pool);
the backend *destinations* of each pool come from configuration. No request
data can influence which host the gateway connects to.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from shared.db import PoolSettings
from shared.env import ConfigError, env_bool, env_float, env_int, env_str


@dataclass(frozen=True)
class UpstreamInstance:
    name: str
    base_url: str


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    prefix: str
    instances: tuple[UpstreamInstance, ...]
    max_in_flight: int


@dataclass(frozen=True)
class HttpTimeouts:
    connect_s: float = 1.0
    read_s: float = 5.0
    write_s: float = 2.0
    pool_s: float = 0.5


@dataclass(frozen=True)
class HealthSettings:
    interval_s: float = 2.0
    timeout_s: float = 0.5
    failure_threshold: int = 2
    recovery_threshold: int = 2


@dataclass(frozen=True)
class RedisSettings:
    url: str
    max_connections: int = 20
    socket_timeout_s: float = 0.5
    connect_timeout_s: float = 0.5
    pool_timeout_s: float = 0.5


@dataclass(frozen=True)
class Settings:
    instance_id: str
    database: PoolSettings
    redis: RedisSettings
    services: tuple[ServiceConfig, ...]
    global_max_in_flight: int = 100
    max_request_body_bytes: int = 64 * 1024
    max_response_body_bytes: int = 1024 * 1024
    body_read_timeout_s: float = 5.0
    upstream_deadline_s: float = 5.0
    http: HttpTimeouts = field(default_factory=HttpTimeouts)
    health: HealthSettings = field(default_factory=HealthSettings)
    expose_instance_headers: bool = False
    metrics_port: int | None = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.services:
            raise ConfigError("at least one service must be configured")
        for svc in self.services:
            if svc.max_in_flight > self.global_max_in_flight:
                raise ConfigError(
                    f"{svc.name} max_in_flight exceeds the gateway's global limit"
                )
        if self.http.read_s > self.upstream_deadline_s:
            # Not fatal in principle, but a read timeout longer than the total
            # deadline can never fire; reject the confusing configuration.
            raise ConfigError("UPSTREAM_READ_TIMEOUT_S must be <= UPSTREAM_DEADLINE_S")


def parse_upstreams(raw: str, setting: str) -> tuple[UpstreamInstance, ...]:
    """Parse ``name=http://host:port,name2=http://host2:port``."""
    instances = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        name, sep, url = item.partition("=")
        if not sep or not name.strip():
            raise ConfigError(f"{setting}: expected name=url, got {item!r}")
        instances.append(UpstreamInstance(name.strip(), validate_base_url(url.strip(), setting)))
    if not instances:
        raise ConfigError(f"{setting} must list at least one upstream")
    names = [i.name for i in instances]
    if len(set(names)) != len(names):
        raise ConfigError(f"{setting}: duplicate upstream names")
    return tuple(instances)


def validate_base_url(url: str, setting: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname or parts.port is None:
        raise ConfigError(f"{setting}: upstream must be http://host:port, got {url!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ConfigError(f"{setting}: upstream must not contain path/query/credentials")
    return f"http://{parts.hostname}:{parts.port}"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    def s(name, default=None):
        return env_str(name, default, env=env)

    def i(name, default, lo=None, hi=None):
        return env_int(name, default, minimum=lo, maximum=hi, env=env)

    def f(name, default, lo=0.001, hi=None):
        return env_float(name, default, minimum=lo, maximum=hi, env=env)

    services = (
        ServiceConfig(
            name="products",
            prefix="/products",
            instances=parse_upstreams(s("PRODUCTS_UPSTREAMS"), "PRODUCTS_UPSTREAMS"),
            max_in_flight=i("PRODUCTS_MAX_IN_FLIGHT", 50, 1, 10_000),
        ),
        ServiceConfig(
            name="orders",
            prefix="/orders",
            instances=parse_upstreams(s("ORDERS_UPSTREAMS"), "ORDERS_UPSTREAMS"),
            max_in_flight=i("ORDERS_MAX_IN_FLIGHT", 20, 1, 10_000),
        ),
    )
    metrics_port = i("METRICS_PORT", 0, 0, 65535)
    return Settings(
        instance_id=s("INSTANCE_ID", "gateway"),
        database=PoolSettings(
            dsn=s("DATABASE_URL"),
            max_size=i("DB_POOL_MAX", 10, 1, 100),
            acquire_timeout_s=f("DB_ACQUIRE_TIMEOUT_S", 1.0),
            command_timeout_s=f("DB_COMMAND_TIMEOUT_S", 2.0),
        ),
        redis=RedisSettings(
            url=s("REDIS_URL"),
            max_connections=i("REDIS_MAX_CONNECTIONS", 20, 1, 1000),
            socket_timeout_s=f("REDIS_SOCKET_TIMEOUT_S", 0.5),
            connect_timeout_s=f("REDIS_CONNECT_TIMEOUT_S", 0.5),
            pool_timeout_s=f("REDIS_POOL_TIMEOUT_S", 0.5),
        ),
        services=services,
        global_max_in_flight=i("GLOBAL_MAX_IN_FLIGHT", 100, 1, 100_000),
        max_request_body_bytes=i("MAX_REQUEST_BODY_BYTES", 64 * 1024, 0, 16 * 1024 * 1024),
        max_response_body_bytes=i("MAX_RESPONSE_BODY_BYTES", 1024 * 1024, 1, 64 * 1024 * 1024),
        body_read_timeout_s=f("BODY_READ_TIMEOUT_S", 5.0),
        upstream_deadline_s=f("UPSTREAM_DEADLINE_S", 5.0),
        http=HttpTimeouts(
            connect_s=f("UPSTREAM_CONNECT_TIMEOUT_S", 1.0),
            read_s=f("UPSTREAM_READ_TIMEOUT_S", 5.0),
            write_s=f("UPSTREAM_WRITE_TIMEOUT_S", 2.0),
            pool_s=f("UPSTREAM_POOL_TIMEOUT_S", 0.5),
        ),
        health=HealthSettings(
            interval_s=f("HEALTH_INTERVAL_S", 2.0, 0.05),
            timeout_s=f("HEALTH_TIMEOUT_S", 0.5, 0.01),
            failure_threshold=i("HEALTH_FAILURE_THRESHOLD", 2, 1, 100),
            recovery_threshold=i("HEALTH_RECOVERY_THRESHOLD", 2, 1, 100),
        ),
        expose_instance_headers=env_bool("EXPOSE_INSTANCE_HEADERS", False, env=env),
        metrics_port=metrics_port or None,
        log_level=s("LOG_LEVEL", "INFO"),
    )
