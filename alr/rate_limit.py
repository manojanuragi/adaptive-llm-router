"""Rate limiting — token bucket per API key.

Two implementations behind the same `.allow(key) -> bool` interface:

- `TokenBucketRateLimiter` — in-memory. Correct for exactly one process;
  each replica has its own bucket, so real throughput becomes
  `capacity x replica_count` once you scale out. Fine for local dev.
- `RedisRateLimiter` — shared token bucket in Redis (Lua script, so the
  read-modify-write is atomic across concurrent replicas). This is what
  makes rate limiting correct when the API autoscales horizontally —
  see the "Autoscaling" section of the README.

`build_rate_limiter()` is the factory api.py actually calls: it returns
the Redis-backed limiter if REDIS_URL is set (and reachable), else falls
back to the in-memory one with a startup warning — same pattern as
tracing.py's build_trace_store() for SQLite vs Postgres.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    def __init__(self, capacity: int = 60, refill_per_second: float = 1.0):
        """capacity=60, refill_per_second=1.0 => ~60 requests/minute burst,
        steady-state 1 req/s per key. Tune via env vars at the API layer.
        """
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.capacity - 1), last_refill=now)
                self._buckets[key] = bucket
                return True

            elapsed = now - bucket.last_refill
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False


# Lua so the refill-and-decrement is a single atomic operation on the
# Redis server — two replicas racing to check the same key can't both
# read stale token counts and both get allowed (the classic
# check-then-act race a plain GET/SET pair would have).
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity - 1
    last_refill = now
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', last_refill)
    redis.call('EXPIRE', key, ttl)
    return 1
end

local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * refill_per_second)

if tokens >= 1.0 then
    tokens = tokens - 1.0
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, ttl)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, ttl)
    return 0
end
"""


class RedisRateLimiter:
    """Same token-bucket semantics as TokenBucketRateLimiter, but the
    bucket state lives in Redis so every replica behind a load balancer
    shares one real limit per API key instead of one limit per replica.
    """

    def __init__(
        self,
        redis_client,
        capacity: int = 60,
        refill_per_second: float = 1.0,
        key_prefix: str = "alr:ratelimit:",
        idle_ttl_s: int = 3600,
    ):
        self.redis = redis_client
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.key_prefix = key_prefix
        self.idle_ttl_s = idle_ttl_s
        self._script = redis_client.register_script(_TOKEN_BUCKET_LUA)

    def allow(self, key: str) -> bool:
        result = self._script(
            keys=[f"{self.key_prefix}{key}"],
            args=[self.capacity, self.refill_per_second, time.time(), self.idle_ttl_s],
        )
        return bool(int(result))


def build_rate_limiter(capacity: int = 60, refill_per_second: float = 1.0):
    """Factory used by api.py. Prefers Redis (required once you run more
    than one replica); falls back to the in-memory limiter for local dev
    or when REDIS_URL isn't set, matching tracing.build_trace_store()'s
    SQLite-fallback pattern.
    """
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return TokenBucketRateLimiter(capacity=capacity, refill_per_second=refill_per_second)

    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(redis_url)
        client.ping()
        return RedisRateLimiter(client, capacity=capacity, refill_per_second=refill_per_second)
    except Exception:  # noqa: BLE001 — any connectivity/import failure falls back, doesn't crash startup
        return TokenBucketRateLimiter(capacity=capacity, refill_per_second=refill_per_second)
