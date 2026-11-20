"""Authentication against real PostgreSQL, schema constraints and privileges."""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from database import manage
from gateway import admin
from gateway.app.auth import Authenticator
from gateway.app.errors import GatewayError
from gateway.app.keys import generate_key
from shared.db import Database, PoolSettings
from tests.integration.conftest import ADMIN_DSN, GATEWAY_DSN, unique

pytestmark = pytest.mark.anyio


@pytest.fixture
async def authenticator():
    db = Database(PoolSettings(GATEWAY_DSN, max_size=2))
    await db.open()
    try:
        yield Authenticator(db)
    finally:
        await db.close()


async def reason(auth, raw):
    with pytest.raises(GatewayError) as info:
        await auth.authenticate(raw)
    return info.value.status, info.value.failure


async def test_valid_key_authenticates_with_policy(authenticator, provision):
    client = await provision(capacity=7, refill_tokens=2, refill_period_ms=500)
    principal = await authenticator.authenticate(client.key)
    assert principal.client_id == client.id and principal.client_name == client.name
    assert principal.key_id == client.key_id
    assert (principal.policy.algorithm, principal.policy.capacity,
            principal.policy.refill_tokens, principal.policy.refill_period_ms) == (
        "token_bucket", 7, 2, 500)


async def test_missing_malformed_unknown_and_wrong_secret(authenticator, provision):
    client = await provision()
    assert await reason(authenticator, None) == (401, "missing_key")
    assert await reason(authenticator, "not-a-key") == (401, "malformed_key")
    assert await reason(authenticator, generate_key().raw) == (401, "unknown_key")
    # Correct key_id, wrong secret: must not authenticate.
    forged = client.key[:-4] + ("AAAA" if not client.key.endswith("AAAA") else "BBBB")
    assert await reason(authenticator, forged) == (401, "unknown_key")


async def test_revoked_key(authenticator, provision, admin_conn):
    client = await provision()
    await admin.revoke_key(admin_conn, key_id=client.key_id)
    assert await reason(authenticator, client.key) == (401, "revoked_key")
    with pytest.raises(admin.AdminError):
        await admin.revoke_key(admin_conn, key_id=client.key_id)  # already revoked


async def test_expired_key(authenticator, provision, admin_conn):
    client = await provision()
    soon = await admin.issue_key(admin_conn, client=client.name,
                                 expires_at=datetime.now(UTC) + timedelta(days=1))
    assert (await authenticator.authenticate(soon.raw)).client_id == client.id
    key = generate_key()
    # Insert an already-expired key (created 2 days ago, expired yesterday).
    await admin_conn.execute(
        "INSERT INTO gateway.api_keys (client_id, key_id, key_hash, created_at, expires_at) "
        "VALUES ($1, $2, $3, now() - interval '2 days', now() - interval '1 day')",
        client.id, key.key_id, key.key_hash)
    assert await reason(authenticator, key.raw) == (401, "expired_key")


async def test_inactive_client_and_reactivation(authenticator, provision, admin_conn):
    client = await provision()
    await admin.set_client_active(admin_conn, client=client.name, active=False)
    assert await reason(authenticator, client.key) == (401, "inactive_client")
    await admin.set_client_active(admin_conn, client=client.name, active=True)
    assert (await authenticator.authenticate(client.key)).client_id == client.id


async def test_client_without_policy_is_forbidden(authenticator, provision, admin_conn):
    client = await provision()
    await admin_conn.execute(
        "DELETE FROM gateway.client_policy_assignments WHERE client_id = $1", client.id)
    assert await reason(authenticator, client.key) == (403, "no_policy")


async def test_policy_reassignment_switches_policy(authenticator, provision, admin_conn):
    client = await provision()
    new_policy = unique("p")
    await admin.create_policy(admin_conn, name=new_policy, algorithm="sliding_window",
                              capacity=3, window_ms=1000)
    await admin.assign_policy(admin_conn, client=client.name, policy=new_policy)
    principal = await authenticator.authenticate(client.key)
    assert principal.policy.name == new_policy and principal.policy.algorithm == "sliding_window"


async def test_unavailable_database_maps_to_503():
    db = Database(PoolSettings("postgresql://x:y@127.0.0.1:1/db", max_size=1,
                               acquire_timeout_s=0.5))
    await db.open()  # min_size=0: opening does not need the database
    try:
        assert await reason(Authenticator(db), generate_key().raw) == (503, "postgres")
    finally:
        await db.close()


# -------------------------------------------------------------- database
async def test_all_migrations_applied_and_rerun_is_noop(admin_conn):
    applied = {r["version"] for r in await admin_conn.fetch(
        "SELECT version FROM public.schema_migrations")}
    assert applied == {m.version for m in manage.discover(manage.MIGRATIONS_DIR)}
    assert await manage.migrate(admin_conn) == []


async def test_seed_data_is_deterministic(admin_conn):
    assert await admin_conn.fetchval("SELECT count(*) FROM catalog.products") == 1000
    row = await admin_conn.fetchrow("SELECT sku, price_cents FROM catalog.products WHERE id = 1")
    assert (row["sku"], row["price_cents"]) == ("SKU-00001", 100 + (7919 % 99900))
    await manage.seed(admin_conn)  # idempotent
    assert await admin_conn.fetchval("SELECT count(*) FROM catalog.products") == 1000


async def test_policies_are_immutable(admin_conn):
    name = unique("p")
    await admin.create_policy(admin_conn, name=name, algorithm="token_bucket", capacity=5,
                              refill_tokens=1, refill_period_ms=1000)
    with pytest.raises(asyncpg.RaiseError, match="immutable"):
        await admin_conn.execute(
            "UPDATE gateway.rate_limit_policies SET capacity = 50 WHERE name = $1", name)


@pytest.mark.parametrize("sql", [
    "INSERT INTO gateway.rate_limit_policies (name, algorithm, capacity, window_ms) "
    "VALUES ('bad-tb-1', 'token_bucket', 5, 1000)",
    "INSERT INTO gateway.rate_limit_policies (name, algorithm, capacity, refill_tokens, "
    "refill_period_ms) VALUES ('bad-sw-1', 'sliding_window', 5, 1, 1000)",
    "INSERT INTO gateway.rate_limit_policies (name, algorithm, capacity, window_ms) "
    "VALUES ('bad-sw-2', 'sliding_window', 20000, 1000)",
    "INSERT INTO gateway.rate_limit_policies (name, algorithm, capacity, refill_tokens, "
    "refill_period_ms) VALUES ('bad-tb-2', 'token_bucket', 0, 1, 1000)",
    "INSERT INTO gateway.clients (name) VALUES ('Bad Name!')",
])
async def test_constraints_reject_invalid_rows(admin_conn, sql):
    with pytest.raises(asyncpg.CheckViolationError):
        await admin_conn.execute(sql)


async def test_admin_validation_errors(admin_conn, provision):
    client = await provision()
    with pytest.raises(admin.AdminError):
        await admin.create_client(admin_conn, name=client.name, policy=client.policy)
    with pytest.raises(admin.AdminError):
        await admin.create_client(admin_conn, name=unique("c"), policy="no-such-policy")
    with pytest.raises(admin.AdminError):
        await admin.create_policy(admin_conn, name=unique("p"), algorithm="token_bucket",
                                  capacity=5)
    with pytest.raises(admin.AdminError):
        await admin.issue_key(admin_conn, client="no-such-client")


async def test_only_digests_are_stored(admin_conn, provision):
    client = await provision()
    row = await admin_conn.fetchrow(
        "SELECT * FROM gateway.api_keys WHERE key_id = $1", client.key_id)
    assert client.key not in repr(dict(row))
    assert len(row["key_hash"]) == 32


@pytest.mark.parametrize("role_env,sql", [
    ("GATEWAY_DB_PASSWORD", "INSERT INTO gateway.clients (name) VALUES ('sneaky-client')"),
    ("GATEWAY_DB_PASSWORD", "SELECT * FROM orders.orders"),
    ("CATALOG_DB_PASSWORD", "UPDATE catalog.products SET price_cents = 1"),
    ("CATALOG_DB_PASSWORD", "SELECT * FROM gateway.api_keys"),
    ("ORDERS_DB_PASSWORD", "UPDATE catalog.products SET price_cents = 1"),
    ("ORDERS_DB_PASSWORD", "DELETE FROM orders.orders"),
    ("ORDERS_DB_PASSWORD", "SELECT * FROM gateway.api_keys"),
])
async def test_application_roles_are_least_privilege(role_env, sql):
    from tests.integration.conftest import DB, ENV
    role = {"GATEWAY_DB_PASSWORD": "gateway_app", "CATALOG_DB_PASSWORD": "catalog_app",
            "ORDERS_DB_PASSWORD": "orders_app"}[role_env]
    conn = await asyncpg.connect(
        f"postgresql://{role}:{ENV[role_env]}@127.0.0.1:15432/{DB}", timeout=5)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(sql)
    finally:
        await conn.close()


async def test_advisory_lock_serializes_setup_runs():
    # Two concurrent setup runs must both succeed (one waits for the lock).
    import asyncio
    await asyncio.gather(manage.run("migrate", ADMIN_DSN), manage.run("migrate", ADMIN_DSN))
