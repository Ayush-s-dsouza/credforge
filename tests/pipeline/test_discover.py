from datetime import datetime, timezone

import pytest

from credforge.enums import ReasonCode
from credforge.pipeline.discover import discover
from credforge.providers.fetch import FetchException, FetchError, FetchResult
from credforge.providers.llm import DiscoveryExtraction


class FakeFetchProvider:
    def __init__(self, responses: dict[str, FetchResult | Exception] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    async def fetch(self, url: str, *, method: str = "GET", headers=None) -> FetchResult:
        self.calls.append(url)
        outcome = self._responses.get(url)
        if outcome is None:
            raise FetchException(FetchError(url=url, reason="connection_error"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeExtractor:
    """Per-URL canned DiscoveryExtraction. Unmapped URLs get has_public_api=False."""

    def __init__(self, results_by_url: dict[str, DiscoveryExtraction] | None = None) -> None:
        self._results_by_url = results_by_url or {}
        self.calls: list[str] = []

    async def extract_discovery(self, *, docs_text: str, docs_url: str) -> DiscoveryExtraction:
        self.calls.append(docs_url)
        return self._results_by_url.get(docs_url, DiscoveryExtraction(has_public_api=False))

    async def extract_classification(self, **kwargs):
        raise NotImplementedError

    async def extract_tos_gate_signals(self, **kwargs):
        raise NotImplementedError


def _ok(text: str) -> FetchResult:
    return FetchResult(
        url="x", final_url="x", status_code=200, content_type="text/html",
        text=text, fetched_at=datetime.now(timezone.utc),
    )


LONG_TEXT = "This page documents our REST API and authentication. " * 10


@pytest.mark.asyncio
async def test_uses_the_top_ranked_candidate_when_it_works() -> None:
    fetch = FakeFetchProvider({"https://docs.github.com/en/rest": _ok(LONG_TEXT)})
    extractor = FakeExtractor(
        {"https://docs.github.com/en/rest": DiscoveryExtraction(has_public_api=True, base_url="https://api.github.com")}
    )

    result = await discover(
        "github.com",
        docs_url_candidates=["https://docs.github.com/en/rest"],
        fetch=fetch,
        extractor=extractor,
    )

    assert result.reason_code is None
    assert result.docs_url == "https://docs.github.com/en/rest"
    assert extractor.calls == ["https://docs.github.com/en/rest"]


@pytest.mark.asyncio
async def test_falls_back_to_next_candidate_when_first_is_unreachable() -> None:
    fetch = FakeFetchProvider(
        {
            "https://docs.example.com/broken": FetchException(FetchError(url="x", reason="timeout")),
            "https://developer.example.com/": _ok(LONG_TEXT),
        }
    )
    extractor = FakeExtractor(
        {"https://developer.example.com/": DiscoveryExtraction(has_public_api=True, base_url="https://api.example.com")}
    )

    result = await discover(
        "example.com",
        docs_url_candidates=["https://docs.example.com/broken", "https://developer.example.com/"],
        fetch=fetch,
        extractor=extractor,
    )

    assert result.reason_code is None
    assert result.docs_url == "https://developer.example.com/"


@pytest.mark.asyncio
async def test_falls_back_to_next_candidate_when_extraction_finds_no_real_api() -> None:
    # The real Salesforce-adjacent case this fixes: the top-ranked
    # candidate resolves fine, but the real extractor concludes there's no
    # actual API on that page (e.g. RESOLVE's cheap filter was wrong for
    # this vendor) -- DISCOVER must try the next candidate, not give up.
    fetch = FakeFetchProvider(
        {
            # long enough to pass the usable-text-length check, so it
            # actually reaches the extractor rather than being skipped
            # for being too short (a different failure mode, tested
            # separately below)
            "https://help.example.com/s/": _ok("Welcome to our help center. " * 10),
            "https://developer.example.com/docs": _ok(LONG_TEXT),
        }
    )
    extractor = FakeExtractor(
        {
            "https://help.example.com/s/": DiscoveryExtraction(has_public_api=False),
            "https://developer.example.com/docs": DiscoveryExtraction(has_public_api=True, base_url="https://api.example.com"),
        }
    )

    result = await discover(
        "example.com",
        docs_url_candidates=["https://help.example.com/s/", "https://developer.example.com/docs"],
        fetch=fetch,
        extractor=extractor,
    )

    assert result.reason_code is None
    assert result.docs_url == "https://developer.example.com/docs"
    assert extractor.calls == ["https://help.example.com/s/", "https://developer.example.com/docs"]


@pytest.mark.asyncio
async def test_falls_back_to_discovers_own_subdomain_guesses_when_resolve_passed_nothing() -> None:
    fetch = FakeFetchProvider({"https://docs.example.com": _ok(LONG_TEXT)})
    extractor = FakeExtractor({"https://docs.example.com": DiscoveryExtraction(has_public_api=True, base_url="https://api.example.com")})

    result = await discover("example.com", docs_url_candidates=[], fetch=fetch, extractor=extractor)

    assert result.reason_code is None
    assert result.docs_url == "https://docs.example.com"


@pytest.mark.asyncio
async def test_discovery_failed_only_after_every_candidate_is_exhausted() -> None:
    fetch = FakeFetchProvider({})  # every fetch fails
    extractor = FakeExtractor()

    result = await discover(
        "nowhere.example",
        docs_url_candidates=["https://a.nowhere.example", "https://b.nowhere.example"],
        fetch=fetch,
        extractor=extractor,
    )

    assert result.reason_code == ReasonCode.DISCOVERY_FAILED
    assert result.extraction is None
    assert extractor.calls == []  # never called an extractor on unreachable pages
    # 2 passed-in candidates + DISCOVER's own 3 subdomain guesses + the
    # bare-domain fallback = 6 tried
    assert "tried 6 candidate docs URL(s)" in result.detail


@pytest.mark.asyncio
async def test_reports_genuine_no_public_api_after_exhausting_candidates_that_were_actually_read() -> None:
    # The real bug this fixes: every candidate coming back has_public_api=
    # False (a genuine "this vendor has no public API" verdict, reached by
    # actually reading real content each time) must NOT collapse to
    # DISCOVERY_FAILED, which implies nothing was ever read at all. Before
    # this fix, GATE's UNSUPPORTED status was unreachable through the real
    # pipeline because of exactly this collapse.
    fetch = FakeFetchProvider(
        {
            "https://a.example.com": _ok(LONG_TEXT),
            "https://b.example.com": _ok(LONG_TEXT),
        }
    )
    extractor = FakeExtractor(
        {
            "https://a.example.com": DiscoveryExtraction(has_public_api=False),
            "https://b.example.com": DiscoveryExtraction(has_public_api=False),
        }
    )

    result = await discover(
        "example.com",
        docs_url_candidates=["https://a.example.com", "https://b.example.com"],
        fetch=fetch,
        extractor=extractor,
    )

    assert result.reason_code is None  # NOT DISCOVERY_FAILED
    assert result.extraction is not None
    assert result.extraction.has_public_api is False
    assert extractor.calls == ["https://a.example.com", "https://b.example.com"]  # every candidate really was read


@pytest.mark.asyncio
async def test_bare_domain_fallback_reveals_a_genuine_no_public_api_verdict() -> None:
    # The real Superhuman case: every docs-shaped subdomain guess is
    # unreachable (robots-disallowed, live), but the bare marketing domain
    # itself has a real, readable homepage with no public API mentioned.
    # Without the bare-domain fallback, this collapses to DISCOVERY_FAILED
    # even though a real page was actually available and readable.
    fetch = FakeFetchProvider(
        {"https://superhuman.com": _ok(LONG_TEXT)}
        # docs./developer./developers.superhuman.com all unreachable (not in responses dict)
    )
    extractor = FakeExtractor({"https://superhuman.com": DiscoveryExtraction(has_public_api=False)})

    result = await discover("superhuman.com", docs_url_candidates=[], fetch=fetch, extractor=extractor)

    assert result.reason_code is None  # NOT DISCOVERY_FAILED
    assert result.extraction is not None
    assert result.extraction.has_public_api is False
    assert result.docs_url == "https://superhuman.com"


@pytest.mark.asyncio
async def test_discovery_incomplete_when_api_claimed_but_nothing_actionable_found() -> None:
    fetch = FakeFetchProvider({"https://docs.example.com": _ok(LONG_TEXT)})
    extractor = FakeExtractor(
        {"https://docs.example.com": DiscoveryExtraction(has_public_api=True, base_url=None, validation_endpoint=None)}
    )

    result = await discover(
        "example.com", docs_url_candidates=["https://docs.example.com"], fetch=fetch, extractor=extractor
    )

    assert result.reason_code == ReasonCode.DISCOVERY_INCOMPLETE


@pytest.mark.asyncio
async def test_too_short_a_page_is_treated_as_unusable() -> None:
    # Failure drill: a docs URL that resolves to a near-empty page (e.g. a
    # redirect to a "coming soon" placeholder) must not be treated as real
    # docs just because the fetch technically succeeded -- try the next one.
    fetch = FakeFetchProvider(
        {
            "https://docs.example.com": _ok("short"),
            "https://developer.example.com": _ok(LONG_TEXT),
        }
    )
    extractor = FakeExtractor(
        {"https://developer.example.com": DiscoveryExtraction(has_public_api=True, base_url="https://api.example.com")}
    )

    result = await discover(
        "example.com",
        docs_url_candidates=["https://docs.example.com", "https://developer.example.com"],
        fetch=fetch,
        extractor=extractor,
    )

    assert result.reason_code is None
    assert result.docs_url == "https://developer.example.com"
