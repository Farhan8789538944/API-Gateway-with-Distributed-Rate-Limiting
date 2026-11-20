-- Schemas and least-privilege application roles.
-- Roles are created NOLOGIN here; the setup command enables LOGIN and sets
-- passwords from the environment (passwords never live in migration files).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_app') THEN
        CREATE ROLE gateway_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catalog_app') THEN
        CREATE ROLE catalog_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orders_app') THEN
        CREATE ROLE orders_app NOLOGIN;
    END IF;
END
$$;

CREATE SCHEMA gateway;
CREATE SCHEMA catalog;
CREATE SCHEMA orders;

-- Nobody but the owner creates objects in public.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA gateway TO gateway_app;
GRANT USAGE ON SCHEMA catalog TO catalog_app, orders_app;
GRANT USAGE ON SCHEMA orders  TO orders_app;
