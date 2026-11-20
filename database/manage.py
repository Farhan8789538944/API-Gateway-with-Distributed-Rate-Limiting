"""Database setup command: versioned migrations, role passwords and seeds.

Usage (inside the Compose network; see README):
    python -m database.manage migrate     # apply pending migrations + set role passwords
    python -m database.manage seed        # apply idempotent seed files
    python -m database.manage setup       # migrate, then seed
    python -m database.manage status      # list applied / pending migrations

Concurrency: the whole run holds a PostgreSQL advisory lock, so two setup
processes can never apply migrations concurrently. Applications never migrate
on startup; Compose runs the one-shot `setup` service before they start.

Each migration file runs in its own transaction together with its bookkeeping
row, so a failed migration leaves no partial state. Applied files are verified
by SHA-256 checksum; editing an applied migration is an error.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SEEDS_DIR = Path(__file__).parent / "seeds"
ADVISORY_LOCK_ID = 727_001  # arbitrary constant shared by every setup process

# Role -> environment variable holding its password.
APP_ROLES = {
    "gateway_app": "GATEWAY_DB_PASSWORD",
    "catalog_app": "CATALOG_DB_PASSWORD",
    "orders_app": "ORDERS_DB_PASSWORD",
}


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def discover(directory: Path) -> list[Migration]:
    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    migrations = [Migration(p.stem, p) for p in files]
    versions = [m.version.split("_", 1)[0] for m in migrations]
    if len(set(versions)) != len(versions):
        raise SystemExit(f"duplicate migration numbers in {directory}")
    return migrations


async def _ensure_bookkeeping(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            version    text PRIMARY KEY,
            checksum   text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


async def _applied(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("SELECT version, checksum FROM public.schema_migrations")
    return {r["version"]: r["checksum"] for r in rows}


async def migrate(conn: asyncpg.Connection) -> list[str]:
    await _ensure_bookkeeping(conn)
    applied = await _applied(conn)
    known = {m.version for m in discover(MIGRATIONS_DIR)}
    unknown = sorted(set(applied) - known)
    if unknown:
        raise SystemExit(f"database has migrations unknown to this code: {unknown}")
    newly_applied = []
    for migration in discover(MIGRATIONS_DIR):
        if migration.version in applied:
            if applied[migration.version] != migration.checksum:
                raise SystemExit(f"checksum mismatch for applied migration {migration.version}")
            continue
        async with conn.transaction():
            await conn.execute(migration.sql)
            await conn.execute(
                "INSERT INTO public.schema_migrations (version, checksum) VALUES ($1, $2)",
                migration.version,
                migration.checksum,
            )
        newly_applied.append(migration.version)
    return newly_applied


async def configure_role_passwords(conn: asyncpg.Connection) -> list[str]:
    """Enable LOGIN for app roles whose password is provided in the environment."""
    configured = []
    for role, env_name in APP_ROLES.items():
        password = os.environ.get(env_name)
        if not password:
            continue
        # format() with %I/%L quotes the identifier and literal safely server-side.
        statement = await conn.fetchval(
            "SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', $1::text, $2::text)",
            role,
            password,
        )
        await conn.execute(statement)
        configured.append(role)
    return configured


async def seed(conn: asyncpg.Connection) -> list[str]:
    applied = []
    for seed_file in discover(SEEDS_DIR):
        async with conn.transaction():
            await conn.execute(seed_file.sql)
        applied.append(seed_file.version)
    return applied


async def status(conn: asyncpg.Connection) -> None:
    await _ensure_bookkeeping(conn)
    applied = await _applied(conn)
    for migration in discover(MIGRATIONS_DIR):
        state = "applied" if migration.version in applied else "pending"
        print(f"{state:8} {migration.version}")


async def run(command: str, dsn: str) -> None:
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_ID)
        try:
            if command == "status":
                await status(conn)
                return
            if command in ("migrate", "setup"):
                applied = await migrate(conn)
                print(f"migrations applied: {applied or 'none (up to date)'}")
                roles = await configure_role_passwords(conn)
                print(f"login enabled for roles: {roles or 'none'}")
            if command in ("seed", "setup"):
                seeds = await seed(conn)
                print(f"seed files applied: {seeds}")
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_ID)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m database.manage")
    parser.add_argument("command", choices=["migrate", "seed", "setup", "status"])
    parser.add_argument("--dsn", default=os.environ.get("ADMIN_DATABASE_URL"),
                        help="owner/admin DSN (default: $ADMIN_DATABASE_URL)")
    args = parser.parse_args(argv)
    if not args.dsn:
        print("ADMIN_DATABASE_URL (or --dsn) is required", file=sys.stderr)
        return 2
    asyncio.run(run(args.command, args.dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
