"""robots.txt compliance layer.

RobotsCache doesn't know how to make an HTTP request itself -- it takes a
raw_fetch callable (an async function url -> (status_code, body_text)) so
it has zero dependency on the concrete FetchProvider built in Stage 1.
HttpxFetchProvider will supply that callable internally and call
is_allowed() before every real fetch.
"""

import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.robotparser import RobotFileParser

RawFetch = Callable[[str], Awaitable[tuple[int, str]]]


@dataclass
class _CacheEntry:
    parser: RobotFileParser | None  # None means "treat as fully disallowed"
    fetched_at: float


class RobotsCache:
    def __init__(
        self,
        *,
        raw_fetch: RawFetch,
        ttl_seconds: float = 3600.0,
        user_agent: str = "credforge-bot/0.1",
    ) -> None:
        self._raw_fetch = raw_fetch
        self._ttl = ttl_seconds
        self._user_agent = user_agent
        self._cache: dict[str, _CacheEntry] = {}

    async def is_allowed(self, url: str, origin: str) -> bool:
        entry = self._cache.get(origin)
        now = time.monotonic()
        if entry is None or (now - entry.fetched_at) > self._ttl:
            entry = await self._refresh(origin, now)
            self._cache[origin] = entry

        if entry.parser is None:
            return False
        return entry.parser.can_fetch(self._user_agent, url)

    async def _refresh(self, origin: str, now: float) -> _CacheEntry:
        robots_url = origin.rstrip("/") + "/robots.txt"
        try:
            status, body = await self._raw_fetch(robots_url)
        except Exception:
            # Can't confirm the policy -- fail closed, not open. See DECISIONS.md D-008.
            return _CacheEntry(parser=None, fetched_at=now)

        if status == 404:
            parser = RobotFileParser()
            parser.parse([])  # no robots.txt on record == no restrictions
            return _CacheEntry(parser=parser, fetched_at=now)

        if 200 <= status < 300:
            parser = RobotFileParser()
            parser.parse(body.splitlines())
            return _CacheEntry(parser=parser, fetched_at=now)

        # Any other status (403, 5xx, ...) -- can't confirm the policy either.
        return _CacheEntry(parser=None, fetched_at=now)
