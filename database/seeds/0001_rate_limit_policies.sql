-- Default policies (idempotent). Policies are immutable, so ON CONFLICT DO NOTHING
-- never changes the parameters of an existing policy.
INSERT INTO gateway.rate_limit_policies (name, algorithm, capacity, refill_tokens, refill_period_ms, window_ms)
VALUES
    ('default-token-bucket',   'token_bucket',   20,  10, 1000, NULL),
    ('default-sliding-window', 'sliding_window', 600, NULL, NULL, 60000),
    ('benchmark-unlimited',    'token_bucket',   100000, 100000, 1, NULL)
ON CONFLICT (name) DO NOTHING;
