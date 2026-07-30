"""Cassette replay + recording for SearchProvider/FetchProvider.

Cassettes are content-addressed (hash of the query, or of method+url)
under a per-app directory -- this is what makes "which app does this
fixture belong to" a directory lookup rather than something encoded in
the filename, and correctly dedups a URL that two different stages both
happen to fetch (DISCOVER's crawl and GATE's ToS fetch might hit the same
domain).

CassetteSearchProvider/CassetteFetchProvider replay only -- an unknown key
is a hard error (CassetteMissError), never a silent live network call, so
the test suite stays deterministic and offline.

RecordingSearchProvider/RecordingFetchProvider wrap a *real* provider and
write a cassette file the first time a given key is seen (never
overwriting an existing one). These are only ever used by
scripts/build_fixtures.py (Stage 9), never by the shipped CLI or the test
suite.
"""

import hashlib
import json
from pathlib import Path

from .fetch import FetchProvider, FetchResult
from .search import SearchProvider, SearchResult


class CassetteMissError(Exception):
    def __init__(self, kind: str, key: str, cassette_dir: Path) -> None:
        self.kind = kind
        self.key = key
        super().__init__(
            f"no {kind} cassette for key {key!r} in {cassette_dir} -- "
            "run scripts/build_fixtures.py to record it, or this test is missing a fixture"
        )


def _hash_key(*parts: str) -> str:
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


class CassetteSearchProvider:
    def __init__(self, cassette_dir: Path) -> None:
        self._dir = cassette_dir

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        key = _hash_key(query)
        path = self._dir / f"{key}.json"
        if not path.exists():
            raise CassetteMissError("search", query, self._dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [SearchResult(**item) for item in payload["results"][:count]]


class RecordingSearchProvider:
    def __init__(self, inner: SearchProvider, cassette_dir: Path) -> None:
        self._inner = inner
        self._dir = cassette_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        key = _hash_key(query)
        path = self._dir / f"{key}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [SearchResult(**item) for item in payload["results"][:count]]
        results = await self._inner.search(query, count=count)
        path.write_text(
            json.dumps({"query": query, "results": [r.model_dump() for r in results]}, indent=2),
            encoding="utf-8",
        )
        return results


class CassetteFetchProvider:
    def __init__(self, cassette_dir: Path) -> None:
        self._dir = cassette_dir

    async def fetch(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> FetchResult:
        key = _hash_key(method, url)
        path = self._dir / f"{key}.json"
        if not path.exists():
            raise CassetteMissError("fetch", f"{method} {url}", self._dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return FetchResult(**payload["result"])


class RecordingFetchProvider:
    def __init__(self, inner: FetchProvider, cassette_dir: Path) -> None:
        self._inner = inner
        self._dir = cassette_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    async def fetch(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> FetchResult:
        key = _hash_key(method, url)
        path = self._dir / f"{key}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return FetchResult(**payload["result"])
        result = await self._inner.fetch(url, method=method, headers=headers)
        path.write_text(
            json.dumps({"url": url, "method": method, "result": result.model_dump(mode="json")}, indent=2),
            encoding="utf-8",
        )
        return result
