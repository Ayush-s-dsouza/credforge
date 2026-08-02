import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from credforge.enums import ReasonCode
from credforge.models.state import ResolveResult
from credforge.pipeline.resolve import (
    DOCS_URL_CONFIDENCE_BOOST,
    _rank_and_verify_docs_candidates,
    _score_candidates,
    resolve,
)
from credforge.providers.fetch import FetchException, FetchError, FetchResult
from credforge.providers.search import SearchProviderError, SearchResult


class FakeSearchProvider:
    def __init__(self, responses: dict[str, list[SearchResult]] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        self.calls.append(query)
        return self._responses.get(query, [])


class FakeFetchProvider:
    """Per-URL response map. Unmapped URLs raise connection_error by default."""

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


_API_DOCS_LIKE_TEXT = (
    "API Reference. Authentication: pass your token as Authorization: Bearer <token>. "
    "GET /v1/users returns a list of users. See our OpenAPI specification for the full schema. "
    "This page documents every endpoint available, request/response shapes, and authorization scopes."
)


def _ok(url: str, text: str = _API_DOCS_LIKE_TEXT) -> FetchResult:
    return FetchResult(
        url=url, final_url=url, status_code=200, content_type="text/html",
        text=text, fetched_at=datetime.now(timezone.utc),
    )


def _ok_non_docs(url: str) -> FetchResult:
    """A page that resolves but has no real API-documentation content --
    used to prove the content-verification check actually rejects it."""
    return FetchResult(
        url=url, final_url=url, status_code=200, content_type="text/html",
        text="Welcome to our help center. Contact support for assistance.",
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_malformed_input_never_calls_a_provider() -> None:
    search = AsyncMock()
    fetch = AsyncMock()

    result = await resolve("   ", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.MALFORMED_INPUT
    search.search.assert_not_called()
    fetch.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_control_characters_are_also_malformed() -> None:
    search = AsyncMock()
    fetch = AsyncMock()

    result = await resolve("git\x00hub", search=search, fetch=fetch)

    assert result.reason_code == ReasonCode.MALFORMED_INPUT
    search.search.assert_not_called()


@pytest.mark.asyncio
async def test_not_found_when_search_returns_nothing() -> None:
    search = FakeSearchProvider()
    fetch = FakeFetchProvider()

    result = await resolve("Zzyzxqqq", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.RESOLVE_NOT_FOUND
    assert len(search.calls) == 1  # never attempted docs-url discovery


@pytest.mark.asyncio
async def test_resolves_a_clear_winner_and_confirms_docs_url_on_first_search_query() -> None:
    search = FakeSearchProvider(
        {
            "GitHub official website": [
                SearchResult(title="GitHub", url="https://github.com/", snippet="...", rank=1),
                SearchResult(title="GitHub features", url="https://github.com/features", snippet="...", rank=2),
            ],
            "GitHub developer API documentation": [
                SearchResult(title="REST API docs", url="https://docs.github.com/en/rest", snippet="...", rank=1),
            ],
        }
    )
    fetch = FakeFetchProvider({"https://docs.github.com/en/rest": _ok("https://docs.github.com/en/rest")})

    result = await resolve("GitHub", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "github.com"
    assert result.chosen.docs_url == "https://docs.github.com/en/rest"
    # candidate collection always queries both docs-search templates up
    # front -- ranking needs the full candidate pool before picking a
    # winner, unlike the old first-match design. See DECISIONS.md D-027.
    assert search.calls == [
        "GitHub official website",
        "GitHub developer API documentation",
        "GitHub developer docs",
    ]


@pytest.mark.asyncio
async def test_docs_url_falls_back_to_second_search_query_when_first_finds_nothing() -> None:
    search = FakeSearchProvider(
        {
            "Example official website": [
                SearchResult(title="Example", url="https://example.com/", snippet="...", rank=1),
                SearchResult(title="Example about", url="https://example.com/about", snippet="...", rank=2),
            ],
            "Example developer API documentation": [],  # nothing on-domain
            "Example developer docs": [
                SearchResult(title="Dev docs", url="https://developer.example.com/", snippet="...", rank=1),
            ],
        }
    )
    fetch = FakeFetchProvider({"https://developer.example.com/": _ok("https://developer.example.com/")})

    result = await resolve("Example", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.docs_url == "https://developer.example.com/"
    assert search.calls == [
        "Example official website",
        "Example developer API documentation",
        "Example developer docs",
    ]


@pytest.mark.asyncio
async def test_docs_url_falls_back_to_subdomain_and_path_guessing_when_search_finds_nothing() -> None:
    search = FakeSearchProvider(
        {
            "Example official website": [
                SearchResult(title="Example", url="https://example.com/", snippet="...", rank=1),
                SearchResult(title="Example about", url="https://example.com/about", snippet="...", rank=2),
            ],
            # both docs-search templates return nothing usable
        }
    )
    # docs.example.com and developer.example.com both fail; api.example.com succeeds
    fetch = FakeFetchProvider({"https://api.example.com": _ok("https://api.example.com")})

    result = await resolve("Example", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.docs_url == "https://api.example.com"
    # guesses were tried in order: docs., developer., api., /docs, /developers
    assert "https://docs.example.com" in fetch.calls
    assert "https://developer.example.com" in fetch.calls
    assert "https://api.example.com" in fetch.calls


@pytest.mark.asyncio
async def test_docs_url_prefers_a_docs_subdomain_over_a_same_domain_topic_page() -> None:
    # Regression (Stage 1): a same-domain hit on a docs/developer/api
    # subdomain must win over a higher-ranked but unrelated same-domain page.
    search = FakeSearchProvider(
        {
            "GitHub official website": [
                SearchResult(title="GitHub", url="https://github.com/", snippet="...", rank=1),
                SearchResult(title="GitHub features", url="https://github.com/features", snippet="...", rank=2),
            ],
            "GitHub developer API documentation": [
                SearchResult(
                    title="api-documentation topic",
                    url="https://github.com/topics/api-documentation",
                    snippet="tag page, not real docs",
                    rank=1,
                ),
                SearchResult(
                    title="GitHub Developer Guide",
                    url="https://developer.github.com/changes/5/",
                    snippet="real developer subdomain",
                    rank=2,
                ),
            ],
        }
    )
    fetch = FakeFetchProvider({"https://developer.github.com/changes/5/": _ok("https://developer.github.com/changes/5/")})

    result = await resolve("GitHub", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.docs_url == "https://developer.github.com/changes/5/"


@pytest.mark.asyncio
async def test_help_center_url_is_never_accepted_even_as_the_only_candidate() -> None:
    # The real bug: RESOLVE picked help.salesforce.com (the end-user
    # support portal) because it was the *only* same-domain hit for the
    # docs-search query, and the old code accepted whatever it found with
    # no negative-signal check. A help/support URL must be rejected
    # outright, not merely "not preferred."
    search = FakeSearchProvider(
        {
            "Salesforce official website": [
                SearchResult(title="Salesforce", url="https://salesforce.com/", snippet="...", rank=1),
            ],
            "Salesforce developer API documentation": [
                SearchResult(title="Salesforce Help", url="https://help.salesforce.com/s/", snippet="...", rank=1),
            ],
            "Salesforce developer docs": [
                SearchResult(title="Salesforce Help", url="https://help.salesforce.com/s/", snippet="...", rank=1),
            ],
        }
    )
    # help.salesforce.com would resolve fine (and even has API-ish content
    # here, to isolate that the negative-signal check -- not the content
    # check -- is what's rejecting it).
    fetch = FakeFetchProvider({"https://help.salesforce.com/s/": _ok("https://help.salesforce.com/s/")})

    result = await resolve("Salesforce", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.docs_url is None  # rejected outright, not accepted as a fallback
    assert "https://help.salesforce.com/s/" not in result.chosen.docs_url_candidates


@pytest.mark.asyncio
async def test_content_verification_rejects_a_page_with_no_api_markers() -> None:
    # A URL that resolves and even has a plausible-looking path, but whose
    # actual content has no real API-documentation markers, must be
    # rejected rather than accepted on URL shape alone.
    search = FakeSearchProvider(
        {
            "Widgetco official website": [
                SearchResult(title="Widgetco", url="https://widgetco.com/", snippet="...", rank=1),
            ],
            "Widgetco developer API documentation": [
                SearchResult(title="Docs", url="https://docs.widgetco.com/", snippet="...", rank=1),
            ],
            "Widgetco developer docs": [],
        }
    )
    fetch = FakeFetchProvider({"https://docs.widgetco.com/": _ok_non_docs("https://docs.widgetco.com/")})

    result = await resolve("Widgetco", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.docs_url is None


@pytest.mark.asyncio
async def test_docs_url_reason_is_recorded_for_auditability() -> None:
    search = FakeSearchProvider(
        {
            "GitHub official website": [
                SearchResult(title="GitHub", url="https://github.com/", snippet="...", rank=1),
            ],
            "GitHub developer API documentation": [
                SearchResult(title="Docs", url="https://docs.github.com/en/rest", snippet="...", rank=1),
            ],
            "GitHub developer docs": [],
        }
    )
    fetch = FakeFetchProvider({"https://docs.github.com/en/rest": _ok("https://docs.github.com/en/rest")})

    result = await resolve("GitHub", search=search, fetch=fetch)

    assert result.chosen.docs_url_reason is not None
    # docs.github.com/en/rest -- "docs" subdomain alone would be MEDIUM,
    # but the "rest" path segment is a real, official-reference signal (D-049).
    assert "HIGH-tier" in result.chosen.docs_url_reason
    assert "'rest'" in result.chosen.docs_url_reason


@pytest.mark.asyncio
async def test_docs_url_reason_describes_the_post_redirect_url_not_the_pre_redirect_guess() -> None:
    # D-054, the real bug: a conventional subdomain guess
    # ("developer.widgetco.com", HIGH-tier) redirects to a final URL
    # ("widgetco.com/help/api-info", LOW-tier -- no HIGH/MEDIUM path
    # signal) that is NOT the same tier. Before the fix, the reason was
    # computed on the pre-redirect guess and stored against
    # `fetched.final_url` regardless -- describing a URL that isn't the
    # one actually returned. The reason must describe the URL that's
    # actually stored (`docs_url`/`docs_url_candidates[0]`), matching
    # whatever tier that same URL gets when CLASSIFY independently scores
    # it later.
    search = FakeSearchProvider(
        {
            "Widgetco official website": [
                SearchResult(title="Widgetco", url="https://widgetco.com/", snippet="...", rank=1),
            ],
            "Widgetco developer API documentation": [],
        }
    )
    fetch = FakeFetchProvider(
        {
            "https://developer.widgetco.com": FetchResult(
                url="https://developer.widgetco.com",
                final_url="https://widgetco.com/help/api-info",
                status_code=200,
                content_type="text/html",
                text=_API_DOCS_LIKE_TEXT,
                fetched_at=datetime.now(timezone.utc),
            ),
        }
    )

    result = await resolve("Widgetco", search=search, fetch=fetch)

    assert result.chosen.docs_url == "https://widgetco.com/help/api-info"
    assert "LOW-tier" in result.chosen.docs_url_reason
    assert "HIGH-tier" not in result.chosen.docs_url_reason


@pytest.mark.asyncio
async def test_docs_candidate_probes_run_concurrently_not_strictly_sequentially() -> None:
    # D-056: five candidates, each simulating a 100ms round-trip. If they
    # ran one at a time (the old behavior), this takes >=500ms; bounded
    # concurrency (cap of 5, so all fit in one batch) should overlap them,
    # finishing close to 100ms. A generous 300ms ceiling leaves headroom
    # for scheduler jitter while still failing loudly if a future change
    # reintroduces strict sequential awaiting.
    urls = [f"https://example{i}.com/api/reference" for i in range(5)]

    class DelayedFetch:
        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def fetch(self, url: str, *, method: str = "GET", headers=None) -> FetchResult:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.1)
            self.in_flight -= 1
            return _ok(url)

    fetch = DelayedFetch()
    start = time.monotonic()
    verified = await _rank_and_verify_docs_candidates(urls, fetch=fetch)
    elapsed = time.monotonic() - start

    assert len(verified) == 5
    assert elapsed < 0.3, f"probes ran too slowly ({elapsed:.2f}s) -- looks sequential, not concurrent"
    assert fetch.max_in_flight > 1, "no overlap observed -- fetches never ran concurrently"


@pytest.mark.asyncio
async def test_high_tier_reference_wins_over_a_lower_ranked_low_tier_tutorial() -> None:
    # D-049, the actual bug: a Trailhead-shaped tutorial URL that search
    # ranked FIRST must still lose to an official /reference/ page ranked
    # second -- tier beats search rank, not the other way around. Real
    # shape: Salesforce's own Trailhead tutorial out-scored its real API
    # reference under the old flat "has a signal word or not" ranking.
    search = FakeSearchProvider(
        {
            "Widgetco official website": [
                SearchResult(title="Widgetco", url="https://widgetco.com/", snippet="...", rank=1),
            ],
            "Widgetco developer API documentation": [
                SearchResult(
                    title="Get started with the Widgetco API (tutorial)",
                    url="https://trailhead.widgetco.com/get-started-tutorial",
                    snippet="a friendly walkthrough",
                    rank=1,
                ),
                SearchResult(
                    title="Widgetco API Reference",
                    url="https://widgetco.com/api/reference",
                    snippet="the real reference",
                    rank=2,
                ),
            ],
        }
    )
    fetch = FakeFetchProvider(
        {
            "https://trailhead.widgetco.com/get-started-tutorial": _ok("https://trailhead.widgetco.com/get-started-tutorial"),
            "https://widgetco.com/api/reference": _ok("https://widgetco.com/api/reference"),
        }
    )

    result = await resolve("Widgetco", search=search, fetch=fetch)

    assert result.chosen.docs_url == "https://widgetco.com/api/reference"
    assert "HIGH-tier" in result.chosen.docs_url_reason
    # both were content-verified and kept as fallback candidates, in tier order
    assert result.chosen.docs_url_candidates[0] == "https://widgetco.com/api/reference"
    assert "https://trailhead.widgetco.com/get-started-tutorial" in result.chosen.docs_url_candidates


@pytest.mark.asyncio
async def test_medium_tier_docs_subdomain_beats_low_tier_blog_regardless_of_rank() -> None:
    search = FakeSearchProvider(
        {
            "Widgetco official website": [
                SearchResult(title="Widgetco", url="https://widgetco.com/", snippet="...", rank=1),
            ],
            "Widgetco developer API documentation": [
                SearchResult(
                    title="How I integrated Widgetco (blog)", url="https://blog.widgetco.com/how-i-integrated",
                    snippet="a blog post", rank=1,
                ),
                SearchResult(
                    title="Widgetco Docs", url="https://docs.widgetco.com/getting-started",
                    snippet="general docs", rank=2,
                ),
            ],
        }
    )
    fetch = FakeFetchProvider(
        {
            "https://blog.widgetco.com/how-i-integrated": _ok("https://blog.widgetco.com/how-i-integrated"),
            "https://docs.widgetco.com/getting-started": _ok("https://docs.widgetco.com/getting-started"),
        }
    )

    result = await resolve("Widgetco", search=search, fetch=fetch)

    assert result.chosen.docs_url == "https://docs.widgetco.com/getting-started"
    assert "MEDIUM-tier" in result.chosen.docs_url_reason


@pytest.mark.asyncio
async def test_within_a_tier_existing_search_rank_order_is_preserved() -> None:
    # Two candidates, same tier (both LOW -- neither matches any HIGH/MEDIUM
    # signal), both on-domain (docs-candidate collection only keeps hits on
    # the already-resolved company domain) -- the higher-*ranked* one
    # (arrived first) must still win, proving the tier sort is stable, not
    # an unconditional re-sort.
    search = FakeSearchProvider(
        {
            "Widgetco official website": [
                SearchResult(title="Widgetco", url="https://widgetco.com/", snippet="...", rank=1),
            ],
            "Widgetco developer API documentation": [
                SearchResult(title="Widgetco blog writeup", url="https://widgetco.com/blog/integration-writeup", snippet="...", rank=1),
                SearchResult(title="Widgetco forum thread", url="https://widgetco.com/forum/thread-123", snippet="...", rank=2),
            ],
        }
    )
    fetch = FakeFetchProvider(
        {
            "https://widgetco.com/blog/integration-writeup": _ok("https://widgetco.com/blog/integration-writeup"),
            "https://widgetco.com/forum/thread-123": _ok("https://widgetco.com/forum/thread-123"),
        }
    )

    result = await resolve("Widgetco", search=search, fetch=fetch)

    assert result.chosen.docs_url == "https://widgetco.com/blog/integration-writeup"
    assert "LOW-tier" in result.chosen.docs_url_reason


@pytest.mark.asyncio
async def test_resolved_with_no_docs_url_found_anywhere_still_succeeds() -> None:
    search = FakeSearchProvider(
        {
            "GitHub official website": [
                SearchResult(title="GitHub", url="https://github.com/", snippet="...", rank=1),
                SearchResult(title="GitHub features", url="https://github.com/features", snippet="...", rank=2),
            ],
        }
    )
    fetch = FakeFetchProvider()  # every fetch fails -- no docs page findable anywhere

    result = await resolve("GitHub", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "github.com"
    assert result.chosen.docs_url is None


@pytest.mark.asyncio
async def test_aggregator_domains_are_excluded_from_candidacy() -> None:
    # The real bug this fixes: Wikipedia and similar reference sites
    # ranking well for "<app> official website" queries must never become
    # a competing candidate, however high they rank.
    search = FakeSearchProvider(
        {
            "Widgetco official website": [
                SearchResult(title="Widgetco on Wikipedia", url="https://en.wikipedia.org/wiki/Widgetco", snippet="...", rank=1),
                SearchResult(title="Widgetco", url="https://widgetco.com/", snippet="...", rank=2),
                SearchResult(title="Widgetco about", url="https://widgetco.com/about", snippet="...", rank=3),
            ],
        }
    )
    fetch = FakeFetchProvider()

    result = await resolve("Widgetco", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "widgetco.com"
    assert all(alt.domain != "wikipedia.org" for alt in result.alternates)


@pytest.mark.asyncio
async def test_archive_org_snapshots_are_also_blocklisted() -> None:
    # Found live, running "Sage" through the real pipeline: a Wayback
    # Machine snapshot of a Wikipedia page about SAGE Publishing surfaced
    # as a competing candidate under archive.org, the same reference-page
    # problem as wikipedia.org itself.
    search = FakeSearchProvider(
        {
            "Sage official website": [
                SearchResult(title="Sage", url="https://sage.com/", snippet="...", rank=1),
                SearchResult(
                    title="SAGE Publishing - Wikipedia (archived)",
                    url="https://web.archive.org/web/2024/https://en.wikipedia.org/wiki/SAGE_Publishing",
                    snippet="...",
                    rank=2,
                ),
            ],
        }
    )
    fetch = FakeFetchProvider()

    result = await resolve("Sage", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "sage.com"
    assert all(alt.domain != "archive.org" for alt in result.alternates)


@pytest.mark.asyncio
async def test_saashub_comparison_pages_are_also_blocklisted() -> None:
    # Found live, running "OpenWeatherMap" through the real pipeline: a
    # SaaS-comparison directory page (saashub.com, the same reference-page
    # problem as g2.com/capterra.com) scored close enough to the real
    # domain to trip RESOLVE_AMBIGUOUS on an otherwise unambiguous app.
    search = FakeSearchProvider(
        {
            "OpenWeatherMap official website": [
                SearchResult(title="OpenWeatherMap", url="https://openweathermap.org/", snippet="...", rank=1),
                SearchResult(
                    title="OpenWeatherMap vs AccuWeather - SaaSHub",
                    url="https://www.saashub.com/compare-accuweather-vs-openweathermap",
                    snippet="...",
                    rank=2,
                ),
            ],
        }
    )
    fetch = FakeFetchProvider()

    result = await resolve("OpenWeatherMap", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "openweathermap.org"
    assert all(alt.domain != "saashub.com" for alt in result.alternates)


@pytest.mark.asyncio
async def test_same_brand_tld_variant_is_merged_not_treated_as_a_competing_candidate() -> None:
    # The real bug: github.com and github.blog scored as two separate,
    # closely-matched candidates and triggered a false RESOLVE_AMBIGUOUS
    # for the single least-ambiguous app in the seed list.
    search = FakeSearchProvider(
        {
            "GitHub official website": [
                SearchResult(title="GitHub", url="https://github.com/", snippet="...", rank=1),
                SearchResult(title="The GitHub Blog", url="https://github.blog/", snippet="...", rank=2),
            ],
        }
    )
    fetch = FakeFetchProvider()

    result = await resolve("GitHub", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.reason_code is None
    assert result.chosen.domain == "github.com"  # .com preferred as the canonical variant
    assert result.alternates == []  # github.blog was folded in, not left competing


@pytest.mark.asyncio
async def test_family_merge_falls_back_to_best_ranked_variant_when_no_dot_com_exists() -> None:
    candidates = _score_candidates(
        "Exampleapp",
        [
            SearchResult(title="a", url="https://exampleapp.dev/", snippet="", rank=2),
            SearchResult(title="b", url="https://exampleapp.io/", snippet="", rank=1),
        ],
    )
    assert len(candidates) == 1
    assert candidates[0].domain == "exampleapp.io"  # best-ranked variant, since neither is .com


@pytest.mark.asyncio
async def test_ambiguous_when_two_candidates_are_close() -> None:
    search = FakeSearchProvider(
        {
            "Atlas official website": [
                SearchResult(title="Atlas by CompanyA", url="https://atlascorp.com/", snippet="...", rank=1),
                SearchResult(title="Atlas by CompanyB", url="https://atlasapp.io/", snippet="...", rank=2),
            ],
        }
    )
    fetch = FakeFetchProvider()

    result = await resolve("Atlas", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.RESOLVE_AMBIGUOUS
    assert {c.domain for c in result.alternates} == {"atlascorp.com", "atlasapp.io"}


@pytest.mark.asyncio
async def test_docs_url_discovery_is_never_attempted_when_ambiguous() -> None:
    # The explicit safety-property test: a docs-URL boost must never be
    # able to break a genuine tie between two real candidates. Every fetch
    # here would succeed if attempted -- the point is that it never is.
    search = FakeSearchProvider(
        {
            "Atlas official website": [
                SearchResult(title="Atlas by CompanyA", url="https://atlascorp.com/", snippet="...", rank=1),
                SearchResult(title="Atlas by CompanyB", url="https://atlasapp.io/", snippet="...", rank=2),
            ],
        }
    )
    fetch = FakeFetchProvider()  # would raise (no mapped URLs) -- proving it's never called matters more

    result = await resolve("Atlas", search=search, fetch=fetch)

    assert result.reason_code == ReasonCode.RESOLVE_AMBIGUOUS
    assert fetch.calls == []
    assert search.calls == ["Atlas official website"]  # never reached the docs-discovery queries


@pytest.mark.asyncio
async def test_low_confidence_when_only_a_weak_single_match() -> None:
    search = FakeSearchProvider(
        {
            "Zzyzx official website": [
                SearchResult(title="Totally Unrelated Co", url="https://totallyunrelated.com/", snippet="...", rank=1),
            ],
        }
    )
    fetch = FakeFetchProvider()  # docs discovery is attempted (not ambiguous) but finds nothing

    result = await resolve("Zzyzx", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.RESOLVE_LOW_CONFIDENCE
    assert result.alternates[0].domain == "totallyunrelated.com"


@pytest.mark.asyncio
async def test_docs_url_confirmation_boosts_confidence_enough_to_rescue_a_low_confidence_candidate() -> None:
    # Precondition, computed from the real scoring function rather than
    # hand-derived: a single rank-1 hit on "acme.com" for app "Meridian"
    # scores below the 0.7 default threshold but comfortably above
    # MIN_PLAUSIBLE_CONFIDENCE, with no second candidate to trigger ambiguity.
    single_hit = [SearchResult(title="Acme", url="https://acme.com/", snippet="...", rank=1)]
    pre_boost = _score_candidates("Meridian", single_hit)[0].confidence
    assert 0.35 <= pre_boost < 0.7, "test fixture must land in the rescue window"
    assert round(pre_boost + DOCS_URL_CONFIDENCE_BOOST, 4) >= 0.7, "boost must be enough to clear threshold"

    search = FakeSearchProvider({"Meridian official website": single_hit})
    fetch = FakeFetchProvider({"https://docs.acme.com": _ok("https://docs.acme.com")})

    result = await resolve("Meridian", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "acme.com"
    assert result.chosen.docs_url == "https://docs.acme.com"
    assert result.chosen.confidence == round(pre_boost + DOCS_URL_CONFIDENCE_BOOST, 4)


# --- Search-provider-failure fallback (conventional domain construction) ---


class RaisingSearchProvider:
    """Every call raises -- simulates the search provider itself failing
    (D-024's SearchProviderError), not "searched and found nothing"."""

    def __init__(self, error: SearchProviderError | None = None) -> None:
        self._error = error or SearchProviderError("provider failed")
        self.calls: list[str] = []

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        self.calls.append(query)
        raise self._error


@pytest.mark.asyncio
async def test_search_failure_fallback_with_no_docs_url_lands_on_low_confidence_not_a_crash() -> None:
    # Trello, the real app that motivated this fix: DDG raised
    # SearchProviderError for "Trello official website" in a real batch
    # run. Only the bare domain responds here (no docs page found) --
    # base fallback confidence (0.6) alone doesn't clear the default 0.7
    # threshold, so this correctly lands on RESOLVE_LOW_CONFIDENCE with
    # real evidence attached, not a hard resolution and not a crash.
    search = RaisingSearchProvider()
    fetch = FakeFetchProvider({"https://trello.com": _ok("https://trello.com", _API_DOCS_LIKE_TEXT)})

    result = await resolve("Trello", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.RESOLVE_LOW_CONFIDENCE
    assert result.alternates[0].domain == "trello.com"
    assert result.alternates[0].confidence < 0.7
    assert search.calls == ["Trello official website"]  # never retried search after the first failure


@pytest.mark.asyncio
async def test_search_failure_fallback_still_finds_and_verifies_a_docs_url() -> None:
    search = RaisingSearchProvider()
    fetch = FakeFetchProvider(
        {
            "https://trello.com": _ok("https://trello.com", _API_DOCS_LIKE_TEXT),
            "https://docs.trello.com": _ok("https://docs.trello.com", _API_DOCS_LIKE_TEXT),
        }
    )

    result = await resolve("Trello", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.docs_url == "https://docs.trello.com"


@pytest.mark.asyncio
async def test_search_failure_fallback_is_ambiguous_when_multiple_tlds_respond() -> None:
    # No rank or consensus signal exists in the fallback path -- if more
    # than one conventional-TLD guess responds for real, there's no way
    # to tell which is the real vendor, so it's ambiguous, not a guess.
    search = RaisingSearchProvider()
    fetch = FakeFetchProvider(
        {
            "https://superapp.com": _ok("https://superapp.com", _API_DOCS_LIKE_TEXT),
            "https://superapp.io": _ok("https://superapp.io", _API_DOCS_LIKE_TEXT),
        }
    )

    result = await resolve("SuperApp", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.RESOLVE_AMBIGUOUS
    assert {alt.domain for alt in result.alternates} == {"superapp.com", "superapp.io"}


@pytest.mark.asyncio
async def test_search_failure_fallback_reports_not_found_when_nothing_responds() -> None:
    search = RaisingSearchProvider()
    fetch = FakeFetchProvider({})  # every conventional-TLD guess fails

    result = await resolve("TotallyMadeUpAppXyz", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.RESOLVE_NOT_FOUND


@pytest.mark.asyncio
async def test_search_failure_still_produces_a_real_artifact_shaped_result_not_a_crash() -> None:
    # The actual bug this fixes: a raw SearchProviderError used to
    # propagate uncaught out of resolve() entirely (an ERROR/internal_error
    # in batch output, no artifact). Now it always returns a real
    # ResolveResult -- resolved or not -- never raises.
    search = RaisingSearchProvider()
    fetch = FakeFetchProvider({})

    result = await resolve("AnythingAtAll", search=search, fetch=fetch)

    assert isinstance(result, ResolveResult)


class SearchThatFailsOnlyOnDocsQueries:
    """D-051's real shape: the identification search ("<app> official
    website") succeeds fine -- a company was found -- but the *second*,
    later search for developer docs fails for real. A categorically
    different scenario from RaisingSearchProvider (which fails on the
    very first call): this proves resolve() as a whole degrades
    gracefully even when the failure happens well past the ambiguity
    check, not just at the very start."""

    def __init__(self, identification_results: list[SearchResult]) -> None:
        self._identification_results = identification_results
        self.calls: list[str] = []

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        self.calls.append(query)
        if query.endswith("official website"):
            return self._identification_results
        raise SearchProviderError("docs search backend timed out")


@pytest.mark.asyncio
async def test_docs_search_failure_degrades_to_conventional_guesses_not_a_crash() -> None:
    # D-051: found live, mid-batch -- a real SearchProviderError from the
    # *docs-URL* search (not the identification search D-024 already
    # covered) used to propagate all the way out of resolve() uncaught.
    search = SearchThatFailsOnlyOnDocsQueries(
        [SearchResult(title="Widgetco", url="https://widgetco.com/", snippet="...", rank=1)]
    )
    fetch = FakeFetchProvider({"https://docs.widgetco.com": _ok("https://docs.widgetco.com", _API_DOCS_LIKE_TEXT)})

    result = await resolve("Widgetco", search=search, fetch=fetch)

    assert isinstance(result, ResolveResult)
    assert result.resolved is True
    assert result.chosen.domain == "widgetco.com"
    # degraded to conventional guessing (docs.widgetco.com) since both
    # docs-search query templates raised
    assert result.chosen.docs_url == "https://docs.widgetco.com"


# --- Bare domain trust (D-047) -------------------------------------------


@pytest.mark.asyncio
async def test_bare_domain_input_is_trusted_directly_without_any_identification_search() -> None:
    # The input already IS a domain -- "acmewidgets.io official website"
    # would be searching for an answer already in hand. No recipe is
    # registered for this domain, so this exercises bare-domain-trust
    # alone, not recipe-pinning.
    search = FakeSearchProvider()  # would return [] for anything -- the point is what's queried at all
    fetch = FakeFetchProvider()  # every fetch fails -- docs discovery is attempted but finds nothing

    result = await resolve("acmewidgets.io", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "acmewidgets.io"
    assert result.chosen.confidence == 1.0
    assert result.chosen.evidence_snippet == "exact domain supplied as input"
    # never issued the identification query -- only docs-discovery queries,
    # which is a real, separate search (proving "skip candidate scoring
    # entirely" specifically, not "skip all search calls")
    assert "acmewidgets.io official website" not in search.calls
    assert search.calls == [
        "acmewidgets.io developer API documentation",
        "acmewidgets.io developer docs",
    ]


@pytest.mark.asyncio
async def test_bare_domain_input_still_finds_and_verifies_a_real_docs_url() -> None:
    search = FakeSearchProvider(
        {
            "acmewidgets.io developer API documentation": [
                SearchResult(title="Docs", url="https://docs.acmewidgets.io/", snippet="...", rank=1),
            ],
        }
    )
    fetch = FakeFetchProvider({"https://docs.acmewidgets.io/": _ok("https://docs.acmewidgets.io/")})

    result = await resolve("acmewidgets.io", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.docs_url == "https://docs.acmewidgets.io/"


@pytest.mark.asyncio
async def test_subdomain_input_is_not_bare_and_does_not_trigger_domain_trust() -> None:
    # "api.widgetvendor.io" has a subdomain -- not "bare" -- so it must
    # fall through to the normal candidate-scoring path, not be blindly
    # trusted. (No recipe is registered for this domain either, so this
    # isolates bare-domain-trust's own subdomain exclusion specifically,
    # not recipe-pinning's separate, broader name-matching.)
    search = FakeSearchProvider(
        {
            "api.widgetvendor.io official website": [
                SearchResult(title="WidgetVendor", url="https://widgetvendor.io/", snippet="...", rank=1),
            ],
        }
    )
    fetch = FakeFetchProvider()

    result = await resolve("api.widgetvendor.io", search=search, fetch=fetch)

    assert search.calls[0] == "api.widgetvendor.io official website"  # normal path was used, not bare-domain-trust


@pytest.mark.asyncio
async def test_bare_domain_trust_does_not_weaken_ambiguity_a_real_company_name_still_halts() -> None:
    # The explicit guard: "Sage" (a real company name, not a domain-shaped
    # string at all) must still go through normal candidate scoring and
    # halt on RESOLVE_AMBIGUOUS when two real candidates are genuinely
    # close -- bare-domain-trust must never be reachable for input that
    # isn't already a domain.
    # Precondition, computed from the real scoring function rather than
    # hand-derived (same pattern as the Meridian/acme.com test above):
    # sageintacct.com (0.6667) and sagepay.com (0.6136) score within
    # AMBIGUITY_MARGIN (0.15) of each other -- both real Sage-brand
    # product domains, a realistic close call.
    search = FakeSearchProvider(
        {
            "Sage official website": [
                SearchResult(title="Sage Intacct", url="https://sageintacct.com/", snippet="...", rank=1),
                SearchResult(title="Sage Pay", url="https://sagepay.com/", snippet="...", rank=2),
            ],
        }
    )
    fetch = FakeFetchProvider()

    result = await resolve("Sage", search=search, fetch=fetch)

    assert result.resolved is False
    assert result.reason_code == ReasonCode.RESOLVE_AMBIGUOUS
    assert {c.domain for c in result.alternates} == {"sageintacct.com", "sagepay.com"}
    assert fetch.calls == []  # docs discovery never attempted on an ambiguous result


# --- Recipe-pinned identity (D-048) --------------------------------------

from credforge.enums import CredentialType  # noqa: E402
from credforge.providers.playwright_browser import SignupRecipe  # noqa: E402

_FAKE_RECIPES = {
    "widgetvendor.com": SignupRecipe(
        email_field_selector="#email",
        submit_selector="#submit",
        credential_type=CredentialType.API_KEY,
        docs_url="https://widgetvendor.com/real-docs",
    ),
}


@pytest.mark.asyncio
async def test_recipe_pinned_domain_input_short_circuits_with_zero_provider_calls(monkeypatch) -> None:
    import credforge.providers.signup_recipes as signup_recipes_module

    monkeypatch.setattr(signup_recipes_module, "LIVE_SIGNUP_RECIPES", _FAKE_RECIPES)
    search = FakeSearchProvider()
    fetch = FakeFetchProvider()

    result = await resolve("widgetvendor.com", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "widgetvendor.com"
    assert result.chosen.docs_url == "https://widgetvendor.com/real-docs"
    assert result.chosen.confidence == 1.0
    # fully short-circuited -- a recipe already IS the docs URL, no
    # discovery search/fetch needed at all, unlike plain bare-domain-trust
    assert search.calls == []
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_recipe_pinned_name_input_also_short_circuits(monkeypatch) -> None:
    # The vendor's common name, not its domain -- the whole point of
    # recipe-pinning beyond plain bare-domain-trust: a name a user would
    # actually type still benefits, not just a literal domain string.
    import credforge.providers.signup_recipes as signup_recipes_module

    monkeypatch.setattr(signup_recipes_module, "LIVE_SIGNUP_RECIPES", _FAKE_RECIPES)
    search = FakeSearchProvider()
    fetch = FakeFetchProvider()

    result = await resolve("WidgetVendor", search=search, fetch=fetch)

    assert result.resolved is True
    assert result.chosen.domain == "widgetvendor.com"
    assert result.chosen.docs_url == "https://widgetvendor.com/real-docs"
    assert search.calls == []
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_recipe_pin_takes_priority_over_plain_bare_domain_trust(monkeypatch) -> None:
    # "widgetvendor.com" is both bare-domain-shaped AND a recipe key --
    # the recipe's own known docs_url must win over generic docs-discovery
    # (which would need real search/fetch calls this test proves never
    # happen).
    import credforge.providers.signup_recipes as signup_recipes_module

    monkeypatch.setattr(signup_recipes_module, "LIVE_SIGNUP_RECIPES", _FAKE_RECIPES)
    search = FakeSearchProvider({"widgetvendor.com developer API documentation": [
        SearchResult(title="wrong", url="https://widgetvendor.com/wrong-docs", snippet="...", rank=1),
    ]})
    fetch = FakeFetchProvider({"https://widgetvendor.com/wrong-docs": _ok("https://widgetvendor.com/wrong-docs")})

    result = await resolve("widgetvendor.com", search=search, fetch=fetch)

    assert result.chosen.docs_url == "https://widgetvendor.com/real-docs"  # recipe's, not discovery's
    assert search.calls == []
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_real_registered_recipes_are_actually_pinnable_not_just_the_test_fixture() -> None:
    # Not a unit test of resolve()'s logic (covered above with fake
    # recipes) -- a guard against the *production* NASA/OpenWeatherMap
    # recipes silently drifting out of sync with what RESOLVE can match.
    search = FakeSearchProvider()
    fetch = FakeFetchProvider()

    nasa = await resolve("NASA API", search=search, fetch=fetch)
    assert nasa.resolved is True
    assert nasa.chosen.domain == "nasa.gov"
    assert nasa.chosen.docs_url == "https://api.nasa.gov/"

    owm = await resolve("OpenWeatherMap", search=search, fetch=fetch)
    assert owm.resolved is True
    assert owm.chosen.domain == "openweathermap.org"
    assert owm.chosen.docs_url == "https://openweathermap.org/api"

    assert search.calls == []
    assert fetch.calls == []
