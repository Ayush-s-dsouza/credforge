"""Real Brave Search API client (SearchProvider).

Requires BRAVE_API_KEY. This is the only concrete SearchProvider that hits
a real network endpoint outside of tests -- CassetteSearchProvider
(cassette.py) replaces it entirely in the test suite.
"""

import httpx

from .search import SearchResult

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider:
    def __init__(self, *, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        response = await self._client.get(
            _ENDPOINT,
            params={"q": query, "count": count},
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        web_results = payload.get("web", {}).get("results", [])
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item["url"],
                snippet=item.get("description", ""),
                rank=idx + 1,
            )
            for idx, item in enumerate(web_results[:count])
        ]
