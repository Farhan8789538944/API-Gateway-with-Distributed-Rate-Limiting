"""Typed, validated environment-variable readers.

Configuration errors are raised at startup (fail fast) instead of surfacing as
surprising runtime behaviour.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


class ConfigError(ValueError):
    """Raised when a configuration value is missing or invalid."""


def _raw(env: Mapping[str, str], name: str, default: str | None) -> str:
    value = env.get(name, default)
    if value is None or value == "":
        raise ConfigError(f"missing required setting {name}")
    return value


def env_str(name: str, default: str | None = None, env: Mapping[str, str] | None = None) -> str:
    return _raw(os.environ if env is None else env, name, default)


def env_int(
    name: str,
    default: int | None = None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    raw = _raw(os.environ if env is None else env, name, None if default is None else str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    _check_range(name, value, minimum, maximum)
    return value


def env_float(
    name: str,
    default: float | None = None,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    env: Mapping[str, str] | None = None,
) -> float:
    raw = _raw(os.environ if env is None else env, name, None if default is None else str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value != value:  # NaN
        raise ConfigError(f"{name} must not be NaN")
    _check_range(name, value, minimum, maximum)
    return value


def env_bool(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _check_range(name: str, value: float, minimum: float | None, maximum: float | None) -> None:
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
