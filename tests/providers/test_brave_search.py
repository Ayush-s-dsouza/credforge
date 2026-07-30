import httpx
import pytest
import respx

from credforge.providers.brave_search import BraveSearchProvider


@pytest.mark.asyncio
@respx.mock
async def test_search_parses_web_results_into_search_results() -> None:
    route = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "GitHub",
                            "url": "https://github.com/",
                            "description": "Where the world builds software",
                        },
                        {
                            "title": "GitHub Docs",
                            "url": "https://docs.github.com/",
                            "description": "REST API docs",
                        },
                    ]
                }
            },
        )
    )
    provider = BraveSearchProvider(api_key="test-key")
    results = await provider.search("github official website", count=10)

    assert route.calls.last.request.headers["X-Subscription-Token"] == "test-key"
    assert len(results) == 2
    assert results[0].url == "https://github.com/"
    assert results[0].rank == 1
    assert results[1].rank == 2


@pytest.mark.asyncio
@respx.mock
async def test_http_error_status_propagates() -> None:
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(return_value=httpx.Response(401))
    provider = BraveSearchProvider(api_key="bad-key")
    with pytest.raises(httpx.HTTPStatusError):
        await provider.search("query")
