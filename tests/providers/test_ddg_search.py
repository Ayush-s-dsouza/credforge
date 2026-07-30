import pytest

from credforge.net.rate_limiter import DomainRateLimiter
from credforge.providers import ddg_search as ddg_search_module
from credforge.providers.ddg_search import DdgSearchProvider, _RetryableError
from credforge.providers.search import SearchProviderError


def _provider() -> DdgSearchProvider:
    limiter = DomainRateLimiter(default_rate_per_sec=100.0, default_burst=10)
    return DdgSearchProvider(rate_limiter=limiter)


@pytest.mark.asyncio
async def test_successful_search_maps_to_search_results(monkeypatch) -> None:
    def fake_search_sync(query: str, count: int) -> list[dict]:
        return [
            {"title": "GitHub", "href": "https://github.com/", "body": "Where the world builds software"},
            {"title": "GitHub Docs", "href": "https://docs.github.com/", "body": "REST API docs"},
        ]

    monkeypatch.setattr(ddg_search_module, "_search_sync", fake_search_sync)
    provider = _provider()

    results = await provider.search("github official website", count=10)

    assert len(results) == 2
    assert results[0].url == "https://github.com/"
    assert results[0].rank == 1
    assert results[1].rank == 2


@pytest.mark.asyncio
async def test_rate_limit_retries_then_succeeds(monkeypatch) -> None:
    # Failure drill (1/2): a transient rate-limit response must be retried
    # with backoff, not immediately surfaced as a failure.
    monkeypatch.setattr(ddg_search_module, "_BASE_BACKOFF_SECONDS", 0.01)
    calls = {"count": 0}

    def flaky_search_sync(query: str, count: int) -> list[dict]:
        calls["count"] += 1
        if calls["count"] < 3:
            raise _RetryableError("202 Ratelimit")
        return [{"title": "t", "href": "https://example.com", "body": "s"}]

    monkeypatch.setattr(ddg_search_module, "_search_sync", flaky_search_sync)
    provider = _provider()

    results = await provider.search("query")

    assert calls["count"] == 3
    assert len(results) == 1


@pytest.mark.asyncio
async def test_rate_limit_exhausting_all_retries_raises_not_empty_list(monkeypatch) -> None:
    # Failure drill (2/2): the critical safety property -- a search that
    # never recovers must raise, never quietly return []. An empty list
    # here would look identical to "the app has no search presence at all"
    # and would misroute RESOLVE to RESOLVE_NOT_FOUND for the wrong reason.
    monkeypatch.setattr(ddg_search_module, "_BASE_BACKOFF_SECONDS", 0.01)

    def always_rate_limited(query: str, count: int) -> list[dict]:
        raise _RetryableError("202 Ratelimit")

    monkeypatch.setattr(ddg_search_module, "_search_sync", always_rate_limited)
    provider = _provider()

    with pytest.raises(SearchProviderError, match="rate-limited"):
        await provider.search("query")


@pytest.mark.asyncio
async def test_non_rate_limit_failure_is_wrapped_immediately_without_retrying(monkeypatch) -> None:
    calls = {"count": 0}

    def broken_search_sync(query: str, count: int) -> list[dict]:
        calls["count"] += 1
        raise ValueError("unexpected ddgs internal error")

    monkeypatch.setattr(ddg_search_module, "_search_sync", broken_search_sync)
    provider = _provider()

    with pytest.raises(SearchProviderError):
        await provider.search("query")

    assert calls["count"] == 1  # no retry for a non-rate-limit failure
