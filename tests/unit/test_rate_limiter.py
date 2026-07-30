import time

import pytest

from credforge.net.rate_limiter import DomainRateLimiter, TokenBucket


@pytest.mark.asyncio
async def test_token_bucket_allows_burst_then_throttles() -> None:
    bucket = TokenBucket(rate_per_sec=5.0, burst=2)
    start = time.monotonic()
    await bucket.acquire()  # burst token 1/2, ~instant
    await bucket.acquire()  # burst token 2/2, ~instant
    await bucket.acquire()  # bucket empty, must wait ~1/5s for a token to regenerate
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15  # allow scheduling slack under the ~0.2s expected wait


@pytest.mark.asyncio
async def test_domains_are_rate_limited_independently() -> None:
    # Failure drill (1/2): without per-domain keying, a slow/strict domain
    # would throttle every other domain sharing its bucket.
    limiter = DomainRateLimiter(default_rate_per_sec=2.0, default_burst=1)

    start = time.monotonic()
    await limiter.acquire_for_domain("a.com")
    await limiter.acquire_for_domain("b.com")
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_domain_override_rate_is_respected() -> None:
    # Failure drill (2/2): a per-domain override (e.g. a vendor with a strict
    # published Crawl-delay) must actually bind that domain, not just be
    # ignored in favor of the default.
    limiter = DomainRateLimiter(
        default_rate_per_sec=100.0,
        default_burst=1,
        overrides={"slow.example.com": (1.0, 1)},
    )
    await limiter.acquire_for_domain("slow.example.com")
    start = time.monotonic()
    await limiter.acquire_for_domain("slow.example.com")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.7
