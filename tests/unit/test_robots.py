import pytest

from credforge.net.robots import RobotsCache

ROBOTS_TXT = """
User-agent: *
Disallow: /private/
Allow: /
"""


@pytest.mark.asyncio
async def test_allows_paths_not_disallowed() -> None:
    async def raw_fetch(url: str) -> tuple[int, str]:
        return 200, ROBOTS_TXT

    cache = RobotsCache(raw_fetch=raw_fetch, user_agent="credforge-bot/0.1")
    assert await cache.is_allowed("https://example.com/docs", "https://example.com") is True


@pytest.mark.asyncio
async def test_disallows_matched_path() -> None:
    async def raw_fetch(url: str) -> tuple[int, str]:
        return 200, ROBOTS_TXT

    cache = RobotsCache(raw_fetch=raw_fetch, user_agent="credforge-bot/0.1")
    assert await cache.is_allowed("https://example.com/private/x", "https://example.com") is False


@pytest.mark.asyncio
async def test_missing_robots_txt_means_allow_all() -> None:
    async def raw_fetch(url: str) -> tuple[int, str]:
        return 404, ""

    cache = RobotsCache(raw_fetch=raw_fetch)
    assert await cache.is_allowed("https://example.com/anything", "https://example.com") is True


@pytest.mark.asyncio
async def test_unreachable_robots_txt_fails_closed() -> None:
    # Failure drill (1/2): if we can't confirm what's allowed, we must not
    # guess "yes" -- a network error must deny, not silently permit crawling.
    async def raw_fetch(url: str) -> tuple[int, str]:
        raise ConnectionError("connection reset")

    cache = RobotsCache(raw_fetch=raw_fetch)
    assert await cache.is_allowed("https://example.com/anything", "https://example.com") is False


@pytest.mark.asyncio
async def test_server_error_fails_closed() -> None:
    # Failure drill (2/2): a 5xx on robots.txt itself is not proof of "no
    # policy" -- treat it the same as unreachable, not the same as a 404.
    async def raw_fetch(url: str) -> tuple[int, str]:
        return 503, ""

    cache = RobotsCache(raw_fetch=raw_fetch)
    assert await cache.is_allowed("https://example.com/anything", "https://example.com") is False


@pytest.mark.asyncio
async def test_result_is_cached_within_ttl() -> None:
    calls = []

    async def raw_fetch(url: str) -> tuple[int, str]:
        calls.append(url)
        return 200, ROBOTS_TXT

    cache = RobotsCache(raw_fetch=raw_fetch, ttl_seconds=3600)
    await cache.is_allowed("https://example.com/a", "https://example.com")
    await cache.is_allowed("https://example.com/b", "https://example.com")
    assert len(calls) == 1
