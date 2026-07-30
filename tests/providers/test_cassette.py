from datetime import datetime, timezone

import pytest

from credforge.providers.cassette import (
    CassetteFetchProvider,
    CassetteMissError,
    CassetteSearchProvider,
    RecordingFetchProvider,
    RecordingSearchProvider,
)
from credforge.providers.fetch import FetchResult
from credforge.providers.search import SearchResult


class _FakeSearch:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        self.calls += 1
        return [SearchResult(title="t", url="https://example.com", snippet="s", rank=1)]


class _FakeFetch:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, url: str, *, method: str = "GET", headers=None) -> FetchResult:
        self.calls += 1
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            text="<html></html>",
            fetched_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_recording_then_replay_roundtrip(tmp_path) -> None:
    real = _FakeSearch()
    recorder = RecordingSearchProvider(real, tmp_path)
    results = await recorder.search("github official website")
    assert real.calls == 1

    replay = CassetteSearchProvider(tmp_path)
    replayed = await replay.search("github official website")
    assert replayed == results


@pytest.mark.asyncio
async def test_recording_never_overwrites_an_existing_cassette(tmp_path) -> None:
    real = _FakeSearch()
    recorder = RecordingSearchProvider(real, tmp_path)
    await recorder.search("q")
    assert real.calls == 1
    await recorder.search("q")  # must replay from disk, not call the real provider again
    assert real.calls == 1


@pytest.mark.asyncio
async def test_cassette_miss_raises_instead_of_silently_fetching(tmp_path) -> None:
    replay = CassetteSearchProvider(tmp_path)
    with pytest.raises(CassetteMissError):
        await replay.search("never recorded")


@pytest.mark.asyncio
async def test_fetch_cassette_roundtrip(tmp_path) -> None:
    real = _FakeFetch()
    recorder = RecordingFetchProvider(real, tmp_path)
    result = await recorder.fetch("https://example.com/docs")
    assert real.calls == 1

    replay = CassetteFetchProvider(tmp_path)
    replayed = await replay.fetch("https://example.com/docs")
    assert replayed == result
