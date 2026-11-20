import pytest

from gateway.app.config import ConfigError, load_settings, parse_upstreams
from gateway.app.keys import generate_key, hash_key, parse_key_id, verify_key
from gateway.app.ratelimit.limiter import Decision, bucket_key
from gateway.app.ratelimit.policy import Policy, PolicyError
from gateway.app.routing import is_safe_raw_path, match_route, route_template

BASE_ENV = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "PRODUCTS_UPSTREAMS": "p1=http://product-1:8000,p2=http://product-2:8000",
    "ORDERS_UPSTREAMS": "orders=http://orders:8000",
}


# ------------------------------------------------------------------ keys
def test_generated_key_format_and_verification():
    key = generate_key()
    assert key.raw.startswith(f"gwk_{key.key_id}_")
    assert parse_key_id(key.raw) == key.key_id
    assert verify_key(key.raw, key.key_hash)
    assert key.key_hash == hash_key(key.raw) and len(key.key_hash) == 32
    assert key.raw not in key.key_hash.hex()


def test_keys_are_unique_and_high_entropy():
    keys = {generate_key().raw for _ in range(2000)}
    assert len(keys) == 2000
    secret = generate_key().raw.split("_", 2)[2]
    assert len(secret) == 43  # 32 random bytes, url-safe base64


@pytest.mark.parametrize("raw", [
    None, "", "gwk_", "gwk_0123456789abcdef_short",
    "xyz_0123456789abcdef_" + "a" * 43,
    "gwk_0123456789ABCDEF_" + "a" * 43,         # key_id must be lowercase hex
    "gwk_0123456789abcdef_" + "a" * 43 + "x",   # too long secret
    "gwk_0123456789abcdef_" + "a" * 42 + "!",
    "a" * 500,
])
def test_malformed_keys_are_rejected(raw):
    assert parse_key_id(raw) is None


def test_verification_fails_for_modified_key():
    key = generate_key()
    tampered = key.raw[:-1] + ("A" if key.raw[-1] != "A" else "B")
    assert not verify_key(tampered, key.key_hash)


# --------------------------------------------------------------- routing
def services():
    return load_settings(BASE_ENV).services


@pytest.mark.parametrize("path,expected", [
    ("/products", "products"),
    ("/products/", "products"),
    ("/products/1", "products"),
    ("/products/1/reviews", "products"),
    ("/orders", "orders"),
    ("/orders/9", "orders"),
    ("/products-other", None),
    ("/productsX/1", None),
    ("/product", None),
    ("/orders2", None),
    ("/", None),
    ("/_dev/faults", None),
    ("/health/ready", None),
    ("/PRODUCTS", None),
])
def test_route_matching_respects_segment_boundaries(path, expected):
    route = match_route(path, services())
    assert (route.name if route else None) == expected


@pytest.mark.parametrize("raw,safe", [
    (b"/products/1", True),
    (b"/products/a%20b", True),
    (b"/products/../_dev/faults", False),
    (b"/products/./1", False),
    (b"/products/%2e%2e/_dev", False),
    (b"/products/%2E%2E/_dev", False),
    (b"/products%2f..%2f_dev", False),
    (b"/products/..%5c_dev", False),
    (b"/products/\\..", False),
    (b"/products/%00", False),
    (b"products/1", False),
    (b"/" + b"a" * 5000, False),
])
def test_path_safety(raw, safe):
    assert is_safe_raw_path(raw) is safe


def test_route_templates_are_bounded():
    svc = services()
    assert route_template("/products", match_route("/products", svc)) == "/products"
    assert route_template("/products/123", match_route("/products/123", svc)) == "/products/{id}"
    assert route_template("/products/a/b", match_route("/products/a/b", svc)) == "/products/{path}"
    assert route_template("/orders/x", match_route("/orders/x", svc)) == "/orders/{path}"
    assert route_template("/whatever/123", None) == "unmatched"


# ---------------------------------------------------------------- config
def test_settings_load_with_defaults():
    settings = load_settings(BASE_ENV)
    assert [s.name for s in settings.services] == ["products", "orders"]
    assert [i.name for i in settings.services[0].instances] == ["p1", "p2"]
    assert settings.global_max_in_flight == 100
    assert settings.metrics_port is None


@pytest.mark.parametrize("raw", [
    "", "noequals", "p1=https://x:1", "p1=http://x", "p1=http://x:1/path",
    "p1=http://u:p@x:1", "p1=http://x:1,p1=http://y:2", "p1=http://x:1?q=1",
])
def test_invalid_upstreams_rejected(raw):
    with pytest.raises(ConfigError):
        parse_upstreams(raw, "X")


@pytest.mark.parametrize("override", [
    {"PRODUCTS_MAX_IN_FLIGHT": "500"},   # exceeds global limit
    {"UPSTREAM_READ_TIMEOUT_S": "10"},   # longer than total deadline
    {"GLOBAL_MAX_IN_FLIGHT": "0"},
    {"UPSTREAM_DEADLINE_S": "abc"},
    {"EXPOSE_INSTANCE_HEADERS": "maybe"},
])
def test_invalid_settings_rejected(override):
    with pytest.raises(ConfigError):
        load_settings({**BASE_ENV, **override})


def test_missing_required_setting_rejected():
    env = dict(BASE_ENV)
    del env["REDIS_URL"]
    with pytest.raises(ConfigError):
        load_settings(env)


# ---------------------------------------------------------------- policy
def test_valid_policies():
    Policy(1, "tb", "token_bucket", 10, refill_tokens=1, refill_period_ms=1000)
    Policy(2, "sw", "sliding_window", 10, window_ms=1000)


@pytest.mark.parametrize("kwargs", [
    dict(algorithm="token_bucket", capacity=0, refill_tokens=1, refill_period_ms=1),
    dict(algorithm="token_bucket", capacity=10, refill_tokens=None, refill_period_ms=1),
    dict(algorithm="token_bucket", capacity=10, refill_tokens=1, refill_period_ms=3_600_001),
    dict(algorithm="token_bucket", capacity=100_001, refill_tokens=1, refill_period_ms=1),
    dict(algorithm="token_bucket", capacity=10, refill_tokens=1, refill_period_ms=1, window_ms=5),
    dict(algorithm="sliding_window", capacity=10_001, window_ms=1000),
    dict(algorithm="sliding_window", capacity=10, window_ms=0),
    dict(algorithm="sliding_window", capacity=10, window_ms=10, refill_tokens=1),
    dict(algorithm="sliding_window", capacity=True, window_ms=10),
    dict(algorithm="leaky", capacity=10),
])
def test_invalid_policies(kwargs):
    with pytest.raises(PolicyError):
        Policy(id=1, name="x", **kwargs)


def test_bucket_keys_scope_client_and_policy():
    assert bucket_key("token_bucket", 7, 3) == "rl:tb:c7:p3"
    assert bucket_key("sliding_window", 7, 3) == "rl:sw:c7:p3"
    assert bucket_key("token_bucket", 7, 4) != bucket_key("token_bucket", 7, 3)


@pytest.mark.parametrize("retry_us,expected", [
    (0, 1), (1, 1), (999_999, 1), (1_000_000, 1), (1_000_001, 2), (59_000_000, 59),
])
def test_retry_after_rounds_up_to_whole_seconds(retry_us, expected):
    decision = Decision(False, 10, 0, retry_us, "token_bucket")
    assert decision.retry_after_s == expected
