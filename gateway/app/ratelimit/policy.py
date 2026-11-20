"""Rate-limit policy model and validation (mirrors the database CHECKs).

The bounds keep every intermediate value in the Lua scripts below 2^53, so
Lua's double-precision numbers represent them exactly:
  token bucket : tokens are stored in units of 1/period_us token, so
                 capacity * period_us <= 1e5 * 3.6e9 = 3.6e14 and, below the
                 full-refill time, elapsed_us * refill_tokens < the same bound
  sliding log  : capacity <= 10_000 members per key bounds memory and script time.
"""

from __future__ import annotations

from dataclasses import dataclass

TOKEN_BUCKET = "token_bucket"  # noqa: S105 - algorithm name, not a credential
SLIDING_WINDOW = "sliding_window"
MAX_CAPACITY = 100_000
MAX_WINDOW_CAPACITY = 10_000
MAX_PERIOD_MS = 3_600_000


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    id: int
    name: str
    algorithm: str
    capacity: int
    refill_tokens: int | None = None
    refill_period_ms: int | None = None
    window_ms: int | None = None

    def __post_init__(self) -> None:
        validate(self)

    @property
    def scope(self) -> str:
        return "global"


def _in_range(name: str, value: int | None, lo: int, hi: int) -> None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise PolicyError(f"{name} must be in [{lo}, {hi}], got {value}")


def validate(policy: Policy) -> None:
    _in_range("id", policy.id, 1, 2**63 - 1)
    if policy.algorithm == TOKEN_BUCKET:
        _in_range("capacity", policy.capacity, 1, MAX_CAPACITY)
        _in_range("refill_tokens", policy.refill_tokens, 1, MAX_CAPACITY)
        _in_range("refill_period_ms", policy.refill_period_ms, 1, MAX_PERIOD_MS)
        if policy.window_ms is not None:
            raise PolicyError("token bucket policies must not set window_ms")
    elif policy.algorithm == SLIDING_WINDOW:
        _in_range("capacity", policy.capacity, 1, MAX_WINDOW_CAPACITY)
        _in_range("window_ms", policy.window_ms, 1, MAX_PERIOD_MS)
        if policy.refill_tokens is not None or policy.refill_period_ms is not None:
            raise PolicyError("sliding window policies must not set refill parameters")
    else:
        raise PolicyError(f"unknown algorithm {policy.algorithm!r}")
