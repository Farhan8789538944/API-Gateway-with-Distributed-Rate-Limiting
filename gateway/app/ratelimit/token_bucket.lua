-- Token bucket, one atomic decision + mutation.
-- KEYS[1]  hash { t = tokens in scaled units, ts = time of last refill (us) }
-- ARGV[1]  capacity (tokens), ARGV[2] refill_tokens, ARGV[3] refill_period_ms
-- `now_us` is defined by the time-source preamble (Redis TIME in production).
-- Returns { allowed (0|1), remaining whole tokens, retry_after_us }.
--
-- Exact integer arithmetic: one token = period_us units, so every elapsed
-- microsecond adds exactly `refill_tokens` units (no rounding, no drift).
-- Policies are immutable and keys include the policy id, so the unit size
-- of a stored bucket never changes. Bounds (enforced by validation):
-- capacity * period_us <= 1e5 * 3.6e9 = 3.6e14 < 2^53, exact in Lua doubles.
local capacity = tonumber(ARGV[1])
local refill_tokens = tonumber(ARGV[2])
local period_ms = tonumber(ARGV[3])
if not capacity or not refill_tokens or not period_ms
   or capacity < 1 or refill_tokens < 1 or period_ms < 1 then
  return redis.error_reply('ERR invalid token bucket parameters')
end

local unit = period_ms * 1000              -- scaled units per token
local cap_units = capacity * unit
-- Microseconds needed to refill an empty bucket completely.
local full_us = math.ceil(cap_units / refill_tokens)

local state = redis.call('HMGET', KEYS[1], 't', 'ts')
local tokens = tonumber(state[1])
local last = tonumber(state[2])
if tokens == nil or last == nil then
  -- Absent state == full bucket (this is what makes expiry safe, see below).
  tokens = cap_units
  last = now_us
end
if tokens > cap_units then tokens = cap_units end
if tokens < 0 then tokens = 0 end

local elapsed = now_us - last
if elapsed > 0 then
  if elapsed >= full_us then
    tokens = cap_units                     -- also avoids large products below
  else
    tokens = math.min(cap_units, tokens + elapsed * refill_tokens)
  end
  last = now_us
end
-- elapsed <= 0: same microsecond or the clock moved backwards. No refill and
-- `last` never moves backwards, so a backward clock step cannot mint tokens.

local allowed = 0
local retry_us = 0
if tokens >= unit then
  tokens = tokens - unit
  allowed = 1
else
  retry_us = math.ceil((unit - tokens) / refill_tokens)
  if last > now_us then retry_us = retry_us + (last - now_us) end
end

redis.call('HSET', KEYS[1], 't', string.format('%.0f', tokens), 'ts', string.format('%.0f', last))

-- Expire only once the bucket would have refilled completely: at that point
-- the absent key (treated as a full bucket) is indistinguishable from the
-- real state, so expiry never grants or removes allowance. +1 s margin.
local to_full_us = math.ceil((cap_units - tokens) / refill_tokens)
if last > now_us then to_full_us = to_full_us + (last - now_us) end
redis.call('PEXPIRE', KEYS[1], string.format('%.0f', math.ceil(to_full_us / 1000) + 1000))

return { allowed, math.floor(tokens / unit), retry_us }
