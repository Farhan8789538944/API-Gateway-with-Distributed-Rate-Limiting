"""API key format, generation and verification.

Format:  gwk_<key_id>_<secret>
  key_id  16 hex chars (64 random bits)  - public identifier, used for lookup/logs
  secret  43 url-safe chars (256 random bits from the OS CSPRNG via `secrets`)

Storage: SHA-256(full key) only. A slow password hash (bcrypt/argon2) is not
needed because the secret is uniformly random with 256 bits of entropy:
offline brute force of a leaked digest is infeasible, and a fast hash keeps
per-request verification cheap. Comparison uses hmac.compare_digest.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

KEY_PREFIX = "gwk"
_KEY_RE = re.compile(r"^gwk_([0-9a-f]{16})_([A-Za-z0-9_-]{43})$")
MAX_HEADER_LENGTH = 128


@dataclass(frozen=True)
class GeneratedKey:
    raw: str  # shown once to the operator, never stored or logged
    key_id: str
    key_hash: bytes


def hash_key(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("ascii")).digest()


def generate_key() -> GeneratedKey:
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    raw = f"{KEY_PREFIX}_{key_id}_{secret}"
    return GeneratedKey(raw=raw, key_id=key_id, key_hash=hash_key(raw))


def parse_key_id(raw: str | None) -> str | None:
    """Return the key_id if ``raw`` is syntactically a key, else None."""
    if raw is None or len(raw) > MAX_HEADER_LENGTH:
        return None
    match = _KEY_RE.match(raw)
    return match.group(1) if match else None


def verify_key(raw: str, stored_hash: bytes) -> bool:
    return hmac.compare_digest(hash_key(raw), stored_hash)
