"""Administrative CLI for clients, API keys and rate-limit policies.

    python -m gateway.admin create-policy --name strict --algorithm token_bucket \
        --capacity 5 --refill-tokens 1 --refill-period-ms 1000
    python -m gateway.admin create-client --name acme --policy default-token-bucket
    python -m gateway.admin issue-key --client acme [--expires-in-days 30] [--label ci]
    python -m gateway.admin revoke-key --key-id 0123456789abcdef
    python -m gateway.admin assign-policy --client acme --policy strict
    python -m gateway.admin deactivate-client --client acme | activate-client --client acme
    python -m gateway.admin list-clients | list-keys --client acme | list-policies

Connects with $ADMIN_DATABASE_URL (the schema owner); the gateway itself only
has SELECT on these tables. A newly issued raw key is printed exactly once and
is never stored or logged; only its SHA-256 digest is persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

import asyncpg

from gateway.app.keys import GeneratedKey, generate_key
from gateway.app.ratelimit.policy import SLIDING_WINDOW, TOKEN_BUCKET, Policy, PolicyError


class AdminError(Exception):
    pass


async def create_policy(conn: asyncpg.Connection, *, name: str, algorithm: str, capacity: int,
                        refill_tokens: int | None = None, refill_period_ms: int | None = None,
                        window_ms: int | None = None) -> int:
    try:
        Policy(id=1, name=name, algorithm=algorithm, capacity=capacity,
               refill_tokens=refill_tokens, refill_period_ms=refill_period_ms,
               window_ms=window_ms)
    except PolicyError as exc:
        raise AdminError(f"invalid policy: {exc}") from exc
    try:
        return await conn.fetchval(
            "INSERT INTO gateway.rate_limit_policies "
            "(name, algorithm, capacity, refill_tokens, refill_period_ms, window_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            name, algorithm, capacity, refill_tokens, refill_period_ms, window_ms,
        )
    except asyncpg.UniqueViolationError as exc:
        raise AdminError(f"policy {name!r} already exists") from exc
    except asyncpg.CheckViolationError as exc:
        raise AdminError(f"invalid policy: {exc.constraint_name}") from exc


async def _policy_id(conn: asyncpg.Connection, name: str) -> int:
    policy_id = await conn.fetchval(
        "SELECT id FROM gateway.rate_limit_policies WHERE name = $1", name)
    if policy_id is None:
        raise AdminError(f"unknown policy {name!r}")
    return policy_id


async def _client_id(conn: asyncpg.Connection, name: str) -> int:
    client_id = await conn.fetchval("SELECT id FROM gateway.clients WHERE name = $1", name)
    if client_id is None:
        raise AdminError(f"unknown client {name!r}")
    return client_id


async def create_client(conn: asyncpg.Connection, *, name: str, policy: str) -> int:
    async with conn.transaction():
        policy_id = await _policy_id(conn, policy)
        try:
            client_id = await conn.fetchval(
                "INSERT INTO gateway.clients (name) VALUES ($1) RETURNING id", name)
        except asyncpg.UniqueViolationError as exc:
            raise AdminError(f"client {name!r} already exists") from exc
        except asyncpg.CheckViolationError as exc:
            raise AdminError("client name must match ^[a-z0-9][a-z0-9_-]{2,63}$") from exc
        await conn.execute(
            "INSERT INTO gateway.client_policy_assignments (client_id, scope, policy_id) "
            "VALUES ($1, 'global', $2)", client_id, policy_id)
    return client_id


async def assign_policy(conn: asyncpg.Connection, *, client: str, policy: str) -> None:
    """Replace the client's global policy.

    Behaviour: quota state is keyed by (client, policy), so the client starts
    with a fresh allowance under the new policy; the old state expires by TTL.
    Requests in flight during the switch may be decided under either policy.
    """
    async with conn.transaction():
        client_id = await _client_id(conn, client)
        policy_id = await _policy_id(conn, policy)
        await conn.execute(
            "INSERT INTO gateway.client_policy_assignments (client_id, scope, policy_id) "
            "VALUES ($1, 'global', $2) ON CONFLICT (client_id, scope) "
            "DO UPDATE SET policy_id = EXCLUDED.policy_id, assigned_at = now()",
            client_id, policy_id)


async def issue_key(conn: asyncpg.Connection, *, client: str, label: str | None = None,
                    expires_at: datetime | None = None) -> GeneratedKey:
    client_id = await _client_id(conn, client)
    key = generate_key()
    await conn.execute(
        "INSERT INTO gateway.api_keys (client_id, key_id, key_hash, label, expires_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        client_id, key.key_id, key.key_hash, label, expires_at)
    return key


async def revoke_key(conn: asyncpg.Connection, *, key_id: str) -> None:
    status = await conn.execute(
        "UPDATE gateway.api_keys SET revoked_at = now() "
        "WHERE key_id = $1 AND revoked_at IS NULL", key_id)
    if status != "UPDATE 1":
        raise AdminError(f"no active key with key_id {key_id!r}")


async def set_client_active(conn: asyncpg.Connection, *, client: str, active: bool) -> None:
    status = await conn.execute(
        "UPDATE gateway.clients SET is_active = $2 WHERE name = $1", client, active)
    if status != "UPDATE 1":
        raise AdminError(f"unknown client {client!r}")


async def _print_rows(conn: asyncpg.Connection, query: str, *args) -> None:
    rows = await conn.fetch(query, *args)
    if not rows:
        print("(none)")
        return
    columns = list(rows[0].keys())
    print("\t".join(columns))
    for row in rows:
        print("\t".join("" if row[c] is None else str(row[c]) for c in columns))


async def run(args: argparse.Namespace, dsn: str) -> int:
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        cmd = args.command
        if cmd == "create-policy":
            policy_id = await create_policy(
                conn, name=args.name, algorithm=args.algorithm, capacity=args.capacity,
                refill_tokens=args.refill_tokens, refill_period_ms=args.refill_period_ms,
                window_ms=args.window_ms)
            print(f"created policy {args.name!r} (id {policy_id})")
        elif cmd == "create-client":
            client_id = await create_client(conn, name=args.name, policy=args.policy)
            print(f"created client {args.name!r} (id {client_id}) with policy {args.policy!r}")
        elif cmd == "issue-key":
            expires_at = None
            if args.expires_in_days is not None:
                expires_at = datetime.now(UTC) + timedelta(days=args.expires_in_days)
            key = await issue_key(conn, client=args.client, label=args.label,
                                  expires_at=expires_at)
            print(f"key_id:  {key.key_id}")
            print(f"api_key: {key.raw}")
            print("Store this key now; it cannot be shown again.", file=sys.stderr)
        elif cmd == "revoke-key":
            await revoke_key(conn, key_id=args.key_id)
            print(f"revoked key {args.key_id}")
        elif cmd == "assign-policy":
            await assign_policy(conn, client=args.client, policy=args.policy)
            print(f"client {args.client!r} now uses policy {args.policy!r}")
        elif cmd in ("activate-client", "deactivate-client"):
            await set_client_active(conn, client=args.client, active=cmd == "activate-client")
            print(f"{cmd.split('-')[0]}d client {args.client!r}")
        elif cmd == "list-clients":
            await _print_rows(conn, """
                SELECT c.id, c.name, c.is_active, p.name AS policy, c.created_at
                FROM gateway.clients c
                LEFT JOIN gateway.client_policy_assignments a ON a.client_id = c.id
                LEFT JOIN gateway.rate_limit_policies p ON p.id = a.policy_id
                ORDER BY c.id""")
        elif cmd == "list-keys":
            client_id = await _client_id(conn, args.client)
            await _print_rows(conn, """
                SELECT key_id, label, created_at, expires_at, revoked_at
                FROM gateway.api_keys WHERE client_id = $1 ORDER BY id""", client_id)
        elif cmd == "list-policies":
            await _print_rows(conn, """
                SELECT id, name, algorithm, capacity, refill_tokens, refill_period_ms, window_ms
                FROM gateway.rate_limit_policies ORDER BY id""")
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gateway.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-policy")
    p.add_argument("--name", required=True)
    p.add_argument("--algorithm", required=True, choices=[TOKEN_BUCKET, SLIDING_WINDOW])
    p.add_argument("--capacity", required=True, type=int)
    p.add_argument("--refill-tokens", type=int)
    p.add_argument("--refill-period-ms", type=int)
    p.add_argument("--window-ms", type=int)

    p = sub.add_parser("create-client")
    p.add_argument("--name", required=True)
    p.add_argument("--policy", default="default-token-bucket")

    p = sub.add_parser("issue-key")
    p.add_argument("--client", required=True)
    p.add_argument("--label")
    p.add_argument("--expires-in-days", type=int)

    p = sub.add_parser("revoke-key")
    p.add_argument("--key-id", required=True)

    p = sub.add_parser("assign-policy")
    p.add_argument("--client", required=True)
    p.add_argument("--policy", required=True)

    for name in ("activate-client", "deactivate-client"):
        sub.add_parser(name).add_argument("--client", required=True)

    sub.add_parser("list-clients")
    sub.add_parser("list-keys").add_argument("--client", required=True)
    sub.add_parser("list-policies")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dsn = os.environ.get("ADMIN_DATABASE_URL")
    if not dsn:
        print("ADMIN_DATABASE_URL is required", file=sys.stderr)
        return 2
    return asyncio.run(run(args, dsn))


if __name__ == "__main__":
    raise SystemExit(main())
