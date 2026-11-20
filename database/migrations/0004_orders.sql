-- Orders: one product, positive quantity, exact integer price snapshot.

CREATE TABLE orders.orders (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Authenticated client id as forwarded by the gateway. Deliberately no
    -- cross-schema FK to gateway.clients: the order service does not own or
    -- read gateway data. It does reference the catalog (documented coupling).
    client_id         bigint      NOT NULL CHECK (client_id > 0),
    product_id        bigint      NOT NULL REFERENCES catalog.products (id) ON DELETE RESTRICT,
    quantity          integer     NOT NULL CHECK (quantity BETWEEN 1 AND 1000),
    unit_price_cents  bigint      NOT NULL CHECK (unit_price_cents >= 0),
    currency          char(3)     NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    total_cents       bigint      NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT orders_total_matches CHECK (total_cents = unit_price_cents * quantity)
);
CREATE INDEX orders_client_id_idx ON orders.orders (client_id, id);

GRANT SELECT, INSERT ON orders.orders TO orders_app;
