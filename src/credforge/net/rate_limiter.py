"""Per-domain outbound rate limiting.

One TokenBucket per registrable domain (see utils/domains.py for how the
domain key itself is computed), created lazily the first time a domain is
seen. This is what lets a batch run hit 20 different vendor domains
concurrently without one strict/slow vendor throttling the others. See
DECISIONS.md D-007.
"""

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self._rate = rate_per_sec
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._updated_at = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait_s)


class DomainRateLimiter:
    def __init__(
        self,
        *,
        default_rate_per_sec: float = 0.5,
        default_burst: int = 2,
        overrides: dict[str, tuple[float, int]] | None = None,
    ) -> None:
        self._default_rate = default_rate_per_sec
        self._default_burst = default_burst
        self._overrides = overrides or {}
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_lock = asyncio.Lock()

    async def acquire_for_domain(self, domain: str) -> None:
        bucket = await self._bucket_for(domain)
        await bucket.acquire()

    async def _bucket_for(self, domain: str) -> TokenBucket:
        async with self._buckets_lock:
            bucket = self._buckets.get(domain)
            if bucket is None:
                rate, burst = self._overrides.get(domain, (self._default_rate, self._default_burst))
                bucket = TokenBucket(rate, burst)
                self._buckets[domain] = bucket
            return bucket
