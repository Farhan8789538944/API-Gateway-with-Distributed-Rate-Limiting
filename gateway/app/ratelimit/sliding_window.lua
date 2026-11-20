-- Sliding-window log, one atomic decision + mutation.
-- KEYS[1]  sorted set of ACCEPTED events: score = acceptance time (us), member unique
-- ARGV[1]  limit (max accepted events per window), ARGV[2] window_ms,
-- ARGV[3]  per-request random nonce (128 bits) making members collision resistant
-- `now_us` is defined by the time-source preamble (Redis TIME in production).
-- Window for a decision at time now: events with  now - window < score <= now
-- (an event stops counting exactly window_us after it was accepted).
-- Returns { allowed (0|1), remaining, retry_after_us }.
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local nonce = ARGV[3]
if not limit or not window_ms or limit < 1 or window_ms < 1
   or not nonce or string.len(nonce) < 16 then
  return redis.error_reply('ERR invalid sliding window parameters')
end
local window_us = window_ms * 1000

-- Drop expired events: score <= now - window (ZREMRANGEBYSCORE bounds are inclusive).
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', string.format('%.0f', now_us - window_us))
local count = redis.call('ZCARD', KEYS[1])

local allowed = 0
local retry_us = 0
if count < limit then
  -- Only allowed requests are recorded. Timestamp + random nonce keeps
  -- members unique even when several requests share a microsecond.
  local member = string.format('%.0f', now_us) .. ':' .. nonce
  if redis.call('ZADD', KEYS[1], 'NX', string.format('%.0f', now_us), member) ~= 1 then
    return redis.error_reply('ERR duplicate sliding window member')
  end
  count = count + 1
  allowed = 1
else
  -- Allowed again once (count - limit + 1) events have expired, i.e. when the
  -- event at 0-based index (count - limit) leaves the window.
  local idx = count - limit
  local entry = redis.call('ZRANGE', KEYS[1], idx, idx, 'WITHSCORES')
  retry_us = tonumber(entry[2]) + window_us - now_us
end

-- The newest event stops mattering window_us after its own score. Using the
-- newest score (not now) stays correct if the clock moved backwards.
local newest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
if newest[2] then
  local ttl_ms = math.ceil((tonumber(newest[2]) + window_us - now_us) / 1000) + 1000
  redis.call('PEXPIRE', KEYS[1], string.format('%.0f', ttl_ms))
end

return { allowed, math.max(0, limit - count), retry_us }
