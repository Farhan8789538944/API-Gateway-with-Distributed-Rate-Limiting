-- Product catalog (read by the product service; read-only lookup by orders).

CREATE TABLE catalog.products (
    id           bigint      PRIMARY KEY CHECK (id > 0),
    sku          text        NOT NULL UNIQUE CHECK (char_length(sku) BETWEEN 1 AND 64),
    name         text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    description  text        NOT NULL DEFAULT '' CHECK (char_length(description) <= 2000),
    price_cents  bigint      NOT NULL CHECK (price_cents >= 0),
    currency     char(3)     NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
    created_at   timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON catalog.products TO catalog_app, orders_app;
