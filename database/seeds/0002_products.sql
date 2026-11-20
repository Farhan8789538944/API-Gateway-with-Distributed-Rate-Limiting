-- 1000 deterministic products (same data on every run; idempotent).
INSERT INTO catalog.products (id, sku, name, description, price_cents, currency)
SELECT g,
       'SKU-' || lpad(g::text, 5, '0'),
       'Product ' || g,
       'Deterministic seed product number ' || g || '.',
       100 + ((g * 7919) % 99900),
       'USD'
FROM generate_series(1, 1000) AS g
ON CONFLICT (id) DO NOTHING;
