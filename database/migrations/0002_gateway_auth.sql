-- Gateway authentication and quota policy tables.

CREATE TABLE gateway.clients (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text        NOT NULL UNIQUE
                CHECK (name ~ '^[a-z0-9][a-z0-9_-]{2,63}$'),
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Only a SHA-256 digest of the key is stored. key_id is a public, random
-- identifier embedded in the key that is used for lookup and in logs.
CREATE TABLE gateway.api_keys (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id   bigint      NOT NULL REFERENCES gateway.clients (id) ON DELETE RESTRICT,
    key_id      text        NOT NULL UNIQUE CHECK (key_id ~ '^[0-9a-f]{16}$'),
    key_hash    bytea       NOT NULL CHECK (octet_length(key_hash) = 32),
    label       text        CHECK (char_length(label) <= 100),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz,
    revoked_at  timestamptz,
    CONSTRAINT api_keys_expiry_after_creation CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE INDEX api_keys_client_id_idx ON gateway.api_keys (client_id);

CREATE TABLE gateway.rate_limit_policies (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             text        NOT NULL UNIQUE
                     CHECK (name ~ '^[a-z0-9][a-z0-9_-]{2,63}$'),
    algorithm        text        NOT NULL CHECK (algorithm IN ('token_bucket', 'sliding_window')),
    -- token bucket: bucket capacity (burst); sliding window: max accepted events per window
    capacity         integer     NOT NULL CHECK (capacity BETWEEN 1 AND 100000),
    refill_tokens    integer     CHECK (refill_tokens BETWEEN 1 AND 100000),
    refill_period_ms integer     CHECK (refill_period_ms BETWEEN 1 AND 3600000),
    window_ms        integer     CHECK (window_ms BETWEEN 1 AND 3600000),
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT policy_parameters_match_algorithm CHECK (
        (algorithm = 'token_bucket'
            AND refill_tokens IS NOT NULL AND refill_period_ms IS NOT NULL AND window_ms IS NULL)
        OR
        (algorithm = 'sliding_window'
            AND window_ms IS NOT NULL AND refill_tokens IS NULL AND refill_period_ms IS NULL
            AND capacity <= 10000)
    )
);

-- Policies are immutable: limiter state in Redis is keyed by policy id, so
-- editing parameters in place would silently reinterpret existing state.
-- A policy change is "create a new policy, then reassign the client".
CREATE FUNCTION gateway.reject_policy_parameter_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.algorithm, NEW.capacity, NEW.refill_tokens, NEW.refill_period_ms, NEW.window_ms)
       IS DISTINCT FROM
       (OLD.algorithm, OLD.capacity, OLD.refill_tokens, OLD.refill_period_ms, OLD.window_ms) THEN
        RAISE EXCEPTION 'rate limit policies are immutable; create a new policy and reassign';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER rate_limit_policies_immutable
    BEFORE UPDATE ON gateway.rate_limit_policies
    FOR EACH ROW EXECUTE FUNCTION gateway.reject_policy_parameter_change();

-- One active global policy per client (primary key on client_id + scope).
CREATE TABLE gateway.client_policy_assignments (
    client_id   bigint      NOT NULL REFERENCES gateway.clients (id) ON DELETE CASCADE,
    scope       text        NOT NULL DEFAULT 'global' CHECK (scope = 'global'),
    policy_id   bigint      NOT NULL REFERENCES gateway.rate_limit_policies (id) ON DELETE RESTRICT,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, scope)
);
CREATE INDEX client_policy_assignments_policy_idx ON gateway.client_policy_assignments (policy_id);

-- The gateway only reads; administration uses the owner role via the CLI.
GRANT SELECT ON gateway.clients, gateway.api_keys, gateway.rate_limit_policies,
                gateway.client_policy_assignments TO gateway_app;
