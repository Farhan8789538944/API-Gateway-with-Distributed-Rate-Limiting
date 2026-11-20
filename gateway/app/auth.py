"""API-key authentication directly against PostgreSQL (no cache).

One indexed query (by key_id) returns the key, its client and the client's
global policy. Expiry is evaluated with the database clock in the same query.
"""

from __future__ import annotations

from dataclasses import dataclass

from gateway.app import errors
from gateway.app.keys import parse_key_id, verify_key
from gateway.app.ratelimit.policy import Policy, PolicyError
from shared.db import Database, DatabaseUnavailable

AUTH_QUERY = """
SELECT k.key_hash,
       k.revoked_at IS NOT NULL                              AS revoked,
       (k.expires_at IS NOT NULL AND k.expires_at <= now())  AS expired,
       c.id        AS client_id,
       c.name      AS client_name,
       c.is_active AS client_active,
       p.id        AS policy_id,
       p.name      AS policy_name,
       p.algorithm, p.capacity, p.refill_tokens, p.refill_period_ms, p.window_ms
FROM gateway.api_keys AS k
JOIN gateway.clients  AS c ON c.id = k.client_id
LEFT JOIN gateway.client_policy_assignments AS a
       ON a.client_id = c.id AND a.scope = 'global'
LEFT JOIN gateway.rate_limit_policies AS p ON p.id = a.policy_id
WHERE k.key_id = $1
"""


@dataclass(frozen=True)
class Principal:
    client_id: int
    client_name: str
    key_id: str
    policy: Policy


class Authenticator:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def authenticate(self, raw_key: str | None) -> Principal:
        if raw_key is None:
            raise errors.unauthorized("missing_key")
        key_id = parse_key_id(raw_key)
        if key_id is None:
            raise errors.unauthorized("malformed_key")
        try:
            async with self._db.connection() as conn:
                row = await conn.fetchrow(AUTH_QUERY, key_id)
        except DatabaseUnavailable as exc:
            raise errors.dependency_unavailable("postgres") from exc
        if row is None or not verify_key(raw_key, bytes(row["key_hash"])):
            raise errors.unauthorized("unknown_key")
        if row["revoked"]:
            raise errors.unauthorized("revoked_key")
        if row["expired"]:
            raise errors.unauthorized("expired_key")
        if not row["client_active"]:
            raise errors.unauthorized("inactive_client")
        if row["policy_id"] is None:
            raise errors.no_policy()
        try:
            policy = Policy(
                id=row["policy_id"],
                name=row["policy_name"],
                algorithm=row["algorithm"],
                capacity=row["capacity"],
                refill_tokens=row["refill_tokens"],
                refill_period_ms=row["refill_period_ms"],
                window_ms=row["window_ms"],
            )
        except PolicyError as exc:
            # Should be impossible given the DB constraints; fail closed.
            raise errors.dependency_unavailable("invalid_policy") from exc
        return Principal(row["client_id"], row["client_name"], key_id, policy)
