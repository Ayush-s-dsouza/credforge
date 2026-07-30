"""No-API-key search via the `ddgs` package (DuckDuckGo).

DEFAULT search provider (see providers/factory.py) -- Brave killed its free
tier in Feb 2026 (a credit card with no spending cap is now required even
to get a key), so this exists to let credforge run RESOLVE/DISCOVER
research with zero billing setup. Brave is used instead whenever
BRAVE_API_KEY is set; nothing else about the pipeline changes based on
which one is active, since both satisfy the same SearchProvider protocol.

`ddgs` is a synchronous library with no async client, so every call is
offloaded to a thread via asyncio.to_thread rather than blocking the event
loop. It has no official API contract, no SLA, and (confirmed by reading
its own docs) no documented rate limit or backoff guidance -- being
unofficial is exactly the tradeoff for having no key and no bill. See
DECISIONS.md D-024.

Because it throttles aggressively and undocumented, every call is metered
through the same DomainRateLimiter every other fetch goes through, plus a
bounded retry-with-backoff specifically for RatelimitException. A
throttled search raises SearchProviderError -- it never silently returns
an empty list.
"""

import asyncio
import random

from ..net.rate_limiter import DomainRateLimiter
from .search import SearchProviderError, SearchResult

# The single bucket key this provider's calls are metered under -- ddgs
# itself fans out to whichever backend engine it picks, but from our side
# it's one logical rate-limited resource.
_RATE_LIMIT_DOMAIN = "duckduckgo.com"

_MAX_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 2.0


class _RetryableError(Exception):
    """Internal signal: a rate-limit response, distinct from any other
    failure -- only this one triggers a retry-with-backoff."""


def _search_sync(query: str, count: int) -> list[dict]:
    from ddgs import DDGS
    from ddgs.exceptions import RatelimitException

    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=count))
    except RatelimitException as exc:
        raise _RetryableError(str(exc)) from exc


class DdgSearchProvider:
    def __init__(self, *, rate_limiter: DomainRateLimiter) -> None:
        self._rate_limiter = rate_limiter

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            await self._rate_limiter.acquire_for_domain(_RATE_LIMIT_DOMAIN)
            try:
                raw_results = await asyncio.to_thread(_search_sync, query, count)
            except _RetryableError as exc:
                last_error = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    backoff = _BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 1)
                    await asyncio.sleep(backoff)
                    continue
                raise SearchProviderError(
                    f"DuckDuckGo rate-limited query {query!r} after {_MAX_ATTEMPTS} attempts"
                ) from exc
            except Exception as exc:
                raise SearchProviderError(f"DuckDuckGo search failed for query={query!r}: {exc}") from exc
            else:
                return [
                    SearchResult(title=r.get("title", ""), url=r["href"], snippet=r.get("body", ""), rank=i + 1)
                    for i, r in enumerate(raw_results[:count])
                ]

        raise SearchProviderError(f"DuckDuckGo search exhausted retries for query={query!r}") from last_error
