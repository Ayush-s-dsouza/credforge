"""SearchProvider: the interface RESOLVE uses for web search.

Concrete implementations arrive in Stage 1 (BraveSearchProvider, a real
Brave Search API client) and Stage 9 (CassetteSearchProvider /
RecordingSearchProvider, for deterministic tests and fixture-building).
"""

from typing import Protocol

from pydantic import BaseModel


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    rank: int


class SearchProvider(Protocol):
    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]: ...


class SearchProviderError(Exception):
    """A search could not be completed at all -- deliberately distinct from
    "zero results found". Callers (RESOLVE) must never treat the two as
    equivalent: swallowing a provider failure into an empty list would look
    identical to "this app has no search presence" and misroute RESOLVE to
    RESOLVE_NOT_FOUND for a reason that has nothing to do with whether the
    app exists."""
