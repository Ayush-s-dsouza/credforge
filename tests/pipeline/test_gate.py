from datetime import datetime, timezone

import pytest

from credforge.enums import ApiStyle, AuthScheme, ReasonCode, Status
from credforge.models.state import ClassifyResult, DiscoveryResult
from credforge.pipeline.gate import gate
from credforge.providers.fetch import FetchException, FetchError, FetchResult
from credforge.providers.llm import DiscoveryExtraction, TosGateExtraction


class FakeFetchProvider:
    def __init__(self, responses: dict[str, FetchResult] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    async def fetch(self, url: str, *, method: str = "GET", headers=None) -> FetchResult:
        self.calls.append(url)
        if url not in self._responses:
            raise FetchException(FetchError(url=url, reason="connection_error"))
        return self._responses[url]


class FakeExtractor:
    def __init__(self, tos_result: TosGateExtraction | None = None) -> None:
        self._tos_result = tos_result
        self.tos_calls = 0

    async def extract_discovery(self, **kwargs):
        raise NotImplementedError

    async def extract_classification(self, **kwargs):
        raise NotImplementedError

    async def extract_tos_gate_signals(self, *, tos_text, tos_url) -> TosGateExtraction:
        self.tos_calls += 1
        return self._tos_result


def _ok(text: str) -> FetchResult:
    return FetchResult(
        url="x", final_url="x", status_code=200, content_type="text/plain",
        text=text, fetched_at=datetime.now(timezone.utc),
    )


def _clean_tos() -> TosGateExtraction:
    return TosGateExtraction(
        prohibits_automation=False, requires_payment=False, requires_business_verification=False,
        requires_sales_contact=False, requires_phone_verification=False, requires_captcha=False,
        requires_sso_only=False,
    )


class PerUrlExtractor:
    """Returns a different TosGateExtraction depending on which URL was
    scanned -- needed to test cross-source signal collection, where the
    docs page and the ToS page must be able to disagree."""

    def __init__(self, results_by_url: dict[str, TosGateExtraction]) -> None:
        self._results_by_url = results_by_url
        self.calls: list[str] = []

    async def extract_discovery(self, **kwargs):
        raise NotImplementedError

    async def extract_classification(self, **kwargs):
        raise NotImplementedError

    async def extract_tos_gate_signals(self, *, tos_text, tos_url) -> TosGateExtraction:
        self.calls.append(tos_url)
        return self._results_by_url[tos_url]


DISCOVERED_OK = DiscoveryResult(reason_code=None, docs_url="https://docs.example.com", extraction=DiscoveryExtraction(has_public_api=True))
CLASSIFIED_OK = ClassifyResult(auth_scheme=AuthScheme.API_KEY, confidence=0.95)

LONG_TOS_TEXT = "These are the terms of service. " * 10


@pytest.mark.asyncio
async def test_discovery_failed_short_circuits_with_zero_fetch_calls() -> None:
    discovery = DiscoveryResult(reason_code=ReasonCode.DISCOVERY_FAILED)
    fetch = FakeFetchProvider()
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.DISCOVERY_FAILED
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_no_public_api_is_unsupported() -> None:
    discovery = DiscoveryResult(reason_code=None, extraction=DiscoveryExtraction(has_public_api=False))
    fetch = FakeFetchProvider()
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.UNSUPPORTED
    assert result.reason_code == ReasonCode.NO_PUBLIC_API
    assert fetch.calls == []  # never even tries to find a ToS page


@pytest.mark.asyncio
async def test_low_confidence_classification_routes_to_hitl_before_any_tos_check() -> None:
    classify = ClassifyResult(auth_scheme=None, reason_code=ReasonCode.CLASSIFY_LOW_CONFIDENCE)
    fetch = FakeFetchProvider()
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=classify, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.CLASSIFY_LOW_CONFIDENCE
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_tos_unverifiable_when_no_tos_page_can_be_found() -> None:
    fetch = FakeFetchProvider({})  # every guess fails
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.TOS_UNVERIFIABLE
    assert extractor.tos_calls == 0  # nothing to extract from


@pytest.mark.asyncio
async def test_soft_404_tos_page_is_skipped_in_favor_of_the_next_guess() -> None:
    # Found live, running Spotify through the real pipeline: GATE's first
    # ToS guess (/developers/terms) returned HTTP 200 with a genuine
    # "page not found" body -- a soft 404 -- and the old code treated the
    # absence of a prohibition clause on a page that isn't really there as
    # a clean pass, producing a false AUTO. D-035.
    fetch = FakeFetchProvider(
        {
            "https://example.com/developers/terms": _ok(
                "Page not found. We can't find the page you're looking for. Check the link and try again. " * 3
            ),
            "https://example.com/developer-terms": _ok(LONG_TOS_TEXT),
        }
    )
    extractor = FakeExtractor(_clean_tos())

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.evidence[0].source_url == "https://example.com/developer-terms"


@pytest.mark.asyncio
async def test_tos_unverifiable_when_every_guess_is_a_soft_404() -> None:
    fetch = FakeFetchProvider(
        {f"https://example.com{path}": _ok("Page not found. " * 20) for path in [
            "/developers/terms", "/developer-terms", "/legal/developer-terms",
            "/terms-of-service", "/terms", "/tos", "/legal/terms",
        ]}
    )
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.TOS_UNVERIFIABLE
    assert extractor.tos_calls == 0


@pytest.mark.asyncio
async def test_clean_tos_clears_to_auto() -> None:
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(_clean_tos())

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert result.evidence[0].source_url == "https://example.com/terms"
    assert result.evidence[0].snippet  # real snippet, not empty


@pytest.mark.parametrize(
    "flag_name,expected_reason",
    [
        ("prohibits_automation", ReasonCode.TOS_PROHIBITS_AUTOMATION),
        ("requires_payment", ReasonCode.REQUIRES_PAYMENT),
        ("requires_business_verification", ReasonCode.REQUIRES_BUSINESS_VERIFICATION),
        ("requires_sales_contact", ReasonCode.REQUIRES_SALES_CONTACT),
        ("requires_phone_verification", ReasonCode.REQUIRES_PHONE_VERIFICATION),
        ("requires_captcha", ReasonCode.REQUIRES_CAPTCHA),
        ("requires_sso_only", ReasonCode.REQUIRES_SSO_ONLY),
    ],
)
@pytest.mark.asyncio
async def test_each_tos_flag_routes_to_its_own_reason_code(flag_name: str, expected_reason: ReasonCode) -> None:
    tos = _clean_tos().model_copy(update={flag_name: True})
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(tos)

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == expected_reason


@pytest.mark.asyncio
async def test_tos_prohibition_outranks_payment_when_both_signals_fire() -> None:
    # The explicit ordering test the verification plan calls for: (a) must
    # win over (b) even when both are true in the same ToS extraction.
    tos = _clean_tos().model_copy(update={"prohibits_automation": True, "requires_payment": True})
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(tos)

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.reason_code == ReasonCode.TOS_PROHIBITS_AUTOMATION


@pytest.mark.asyncio
async def test_payment_outranks_captcha_when_both_signals_fire() -> None:
    # (b) must win over (c).
    tos = _clean_tos().model_copy(update={"requires_payment": True, "requires_captcha": True})
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(tos)

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.reason_code == ReasonCode.REQUIRES_PAYMENT


@pytest.mark.asyncio
async def test_discovery_incomplete_does_not_block_auto() -> None:
    # The explicit decision: a missing prose field is not a reason to
    # route to a human. GATE must still clear this to AUTO.
    incomplete_discovery = DiscoveryResult(
        reason_code=ReasonCode.DISCOVERY_INCOMPLETE,
        docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(has_public_api=True, base_url=None, validation_endpoint=None),
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(_clean_tos())

    result = await gate(
        "example.com", discovery=incomplete_discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO


@pytest.mark.asyncio
async def test_completeness_gaps_list_every_missing_expected_field() -> None:
    incomplete_discovery = DiscoveryResult(
        reason_code=ReasonCode.DISCOVERY_INCOMPLETE,
        docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(
            has_public_api=True,
            base_url=None,
            developer_portal_url="https://developer.example.com",  # present -- not a gap
            rate_limit_notes=None,
            pagination_style_hint=None,
            validation_endpoint=None,
        ),
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(_clean_tos())

    result = await gate(
        "example.com", discovery=incomplete_discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    gap_fields = {g.field for g in result.completeness_gaps}
    assert gap_fields == {"base_url", "rate_limit_notes", "pagination_style_hint", "validation_endpoint"}
    assert "developer_portal_url" not in gap_fields  # was actually present, not a gap


@pytest.mark.asyncio
async def test_clean_discovery_has_no_completeness_gaps() -> None:
    complete_discovery = DiscoveryResult(
        reason_code=None,
        docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(
            has_public_api=True,
            base_url="https://api.example.com",
            developer_portal_url="https://developer.example.com",
            rate_limit_notes="1000 req/hour",
            pagination_style_hint="cursor",
            validation_endpoint="/v1/me",
        ),
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(_clean_tos())

    result = await gate(
        "example.com", discovery=complete_discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    assert result.completeness_gaps == []


@pytest.mark.asyncio
async def test_graphql_api_style_suppresses_only_the_rest_specific_completeness_gaps() -> None:
    # D-055: Linear's real case -- a single GraphQL endpoint, cursor-based
    # connections, no REST-style base_url-plus-many-paths shape.
    # pagination_style_hint and validation_endpoint are REST-specific by
    # current design (see gate.py's comment) and must be suppressed for a
    # confirmed GraphQL API; base_url and rate_limit_notes are universal
    # and must still be reported as real gaps when missing.
    graphql_discovery = DiscoveryResult(
        reason_code=ReasonCode.DISCOVERY_INCOMPLETE,
        docs_url="https://linear.app/developers/graphql",
        extraction=DiscoveryExtraction(
            has_public_api=True,
            base_url=None,
            developer_portal_url="https://linear.app/settings/api",
            rate_limit_notes=None,
            pagination_style_hint=None,
            validation_endpoint=None,
        ),
        api_style=ApiStyle.GRAPHQL,
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(_clean_tos())

    result = await gate(
        "linear.app", discovery=graphql_discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    gap_fields = {g.field for g in result.completeness_gaps}
    assert gap_fields == {"base_url", "rate_limit_notes"}
    assert "pagination_style_hint" not in gap_fields
    assert "validation_endpoint" not in gap_fields


@pytest.mark.asyncio
async def test_unknown_api_style_keeps_todays_rest_assuming_completeness_gaps() -> None:
    # UNKNOWN must NOT suppress anything -- only a *confirmed* GraphQL
    # classification changes behavior; an unconfirmed style keeps the
    # existing, conservative REST-assuming gap list.
    unknown_style_discovery = DiscoveryResult(
        reason_code=ReasonCode.DISCOVERY_INCOMPLETE,
        docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(
            has_public_api=True, base_url=None, rate_limit_notes=None,
            pagination_style_hint=None, validation_endpoint=None,
        ),
        api_style=ApiStyle.UNKNOWN,
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(_clean_tos())

    result = await gate(
        "example.com", discovery=unknown_style_discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    gap_fields = {g.field for g in result.completeness_gaps}
    assert gap_fields == {
        "base_url", "developer_portal_url", "rate_limit_notes", "pagination_style_hint", "validation_endpoint",
    }



# --- D-057: scoped vs. unscoped payment language ---------------------------

_ALPHA_VANTAGE_SHAPED_TOS = TosGateExtraction(
    prohibits_automation=False, requires_payment=True, requires_business_verification=False,
    requires_sales_contact=False, requires_phone_verification=False, requires_captcha=False,
    requires_sso_only=False,
    evidence_snippets=["This is a premium endpoint. Please subscribe to a premium membership plan."],
)
_ALPHA_VANTAGE_SHAPED_TEXT = (
    "This is a premium endpoint. If you would like to access realtime, 15-minute delayed, "
    "and/or historical intraday data, please subscribe to a premium membership plan. "
    "Get your free API key today to get started with our standard endpoints."
)


@pytest.mark.asyncio
async def test_scoped_payment_language_does_not_trigger_requires_payment() -> None:
    # The real live bug: Alpha Vantage's real docs state exactly this --
    # payment scoped to specific realtime/historical endpoints, on a page
    # that also states a free API key is available. Before this fix, GATE
    # blocked PROVISION on a vendor with a genuine, working free tier.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(has_public_api=True, developer_portal_url="https://example.com/support/#api-key"),
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(_ALPHA_VANTAGE_SHAPED_TEXT)})
    extractor = FakeExtractor(_ALPHA_VANTAGE_SHAPED_TOS)

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO


@pytest.mark.asyncio
async def test_unscoped_payment_language_still_triggers_requires_payment() -> None:
    text = (
        "API access requires a paid plan. There is no way to use the API without subscribing. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"requires_payment": True, "evidence_snippets": ["API access requires a paid plan."]})
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.REQUIRES_PAYMENT


@pytest.mark.asyncio
async def test_free_tier_override_suppresses_requires_payment_with_no_scoped_marker_present() -> None:
    # Neither a SCOPED nor an UNSCOPED marker literally appears -- the
    # extractor flagged requires_payment=True from language this override
    # doesn't explicitly recognize, but the same page also advertises a
    # free tier. FREE-TIER OVERRIDE is an independent path to suppression,
    # not conditional on a SCOPED marker also matching.
    text = (
        "Pricing varies by plan and use case. Get started for free -- no signup fee. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"requires_payment": True, "evidence_snippets": ["Pricing varies by plan."]})
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO


@pytest.mark.asyncio
async def test_developer_portal_url_containing_api_key_counts_as_free_tier_evidence() -> None:
    # No free-tier text marker on the page itself -- the developer_portal_url
    # alone (the free-key signup page) is enough to suppress, per the
    # FREE-TIER OVERRIDE's explicit "or the developer portal URL" clause.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(has_public_api=True, developer_portal_url="https://example.com/support/#api-key"),
    )
    text = (
        "This is a premium endpoint requiring a subscription. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"requires_payment": True, "evidence_snippets": ["premium endpoint"]})
    )

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO


@pytest.mark.asyncio
async def test_unscoped_language_wins_even_when_scoped_and_free_tier_language_also_present() -> None:
    # Open-Meteo-shaped: a real, mixed-tier vendor -- free for non-
    # commercial use, but a genuine unscoped statement that a paid account
    # is required to obtain a key for commercial use at all. Unscoped must
    # win regardless of the free-tier and scoped ("for commercial use")
    # language appearing in the very same text.
    text = (
        "Open-Meteo's free tier is for non-commercial use. A paid account is required to "
        "obtain an API key for commercial applications. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"requires_payment": True, "evidence_snippets": [text]})
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.REQUIRES_PAYMENT


@pytest.mark.asyncio
async def test_scoped_payment_override_also_applies_to_the_developer_docs_signal_source() -> None:
    # The override must be wired to BOTH signal sources gate() scans (D-040:
    # the already-crawled docs page, and the dedicated ToS page), not just
    # whichever one the earlier tests happened to exercise.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com",
        docs_text=_ALPHA_VANTAGE_SHAPED_TEXT,
        extraction=DiscoveryExtraction(has_public_api=True, developer_portal_url="https://example.com/support/#api-key"),
    )
    fetch = FakeFetchProvider({})  # no dedicated ToS page reachable -- docs-page signal alone must clear it
    extractor = FakeExtractor(_ALPHA_VANTAGE_SHAPED_TOS)

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    # TOS_UNVERIFIABLE, not requires_payment and not AUTO -- the dedicated
    # ToS page genuinely couldn't be found (D-021 still applies), but the
    # docs-page requires_payment signal must have been suppressed rather
    # than blocking outright.
    assert result.reason_code == ReasonCode.TOS_UNVERIFIABLE


# --- D-057 regression guard: real vendor blocks must still fire ------------


@pytest.mark.asyncio
async def test_etsy_shaped_unscoped_prohibition_still_fires() -> None:
    # Not payment-related at all -- prohibits_automation is untouched by
    # this fix, but a regression guard against this override accidentally
    # affecting a different flag or GATE's precedence order.
    text = (
        "You may not use bots, scripts, or other automated means to create an Etsy account "
        "or access the Etsy API. These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"prohibits_automation": True, "evidence_snippets": [text]})
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.TOS_PROHIBITS_AUTOMATION


@pytest.mark.asyncio
async def test_open_meteo_shaped_unscoped_payment_requirement_still_fires() -> None:
    text = (
        "Open-Meteo offers a free tier for non-commercial use. For commercial use, "
        "a paid account is required to obtain an API key; no free tier is available for business use. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"requires_payment": True, "evidence_snippets": [text]})
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.REQUIRES_PAYMENT


@pytest.mark.asyncio
async def test_completeness_gaps_are_still_carried_on_a_hitl_result() -> None:
    incomplete_discovery = DiscoveryResult(
        reason_code=ReasonCode.DISCOVERY_INCOMPLETE,
        docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(has_public_api=True, base_url=None, validation_endpoint=None),
    )
    tos = _clean_tos().model_copy(update={"requires_payment": True})
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(tos)

    result = await gate(
        "example.com", discovery=incomplete_discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.REQUIRES_PAYMENT
    assert len(result.completeness_gaps) > 0  # the gap doesn't disappear just because HITL fired for another reason


# --- D-040: cross-source signal collection ---------------------------------


LONG_DOCS_TEXT = "This page documents our REST API endpoints and authentication. " * 10


@pytest.mark.asyncio
async def test_payment_signal_in_docs_page_wins_even_when_tos_page_is_unfindable() -> None:
    # The real property under test: TOS_UNVERIFIABLE (a detection failure)
    # must never mask a real signal that's already in hand from the docs
    # page, even when the dedicated ToS page can't be found at all.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com", docs_text=LONG_DOCS_TEXT,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({})  # every ToS guess fails
    extractor = PerUrlExtractor(
        {"https://docs.example.com": _clean_tos().model_copy(update={"requires_payment": True})}
    )

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.REQUIRES_PAYMENT
    assert result.reason_code != ReasonCode.TOS_UNVERIFIABLE
    assert result.evidence[0].source_url == "https://docs.example.com"


@pytest.mark.asyncio
async def test_docs_page_prohibition_outranks_tos_page_payment_signal_across_sources() -> None:
    # Precedence is applied across the union of both sources, not
    # per-source: a higher-precedence signal found in the docs page beats
    # a lower-precedence one found in the (separately findable) ToS page.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com", docs_text=LONG_DOCS_TEXT,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = PerUrlExtractor(
        {
            "https://docs.example.com": _clean_tos().model_copy(update={"prohibits_automation": True}),
            "https://example.com/terms": _clean_tos().model_copy(update={"requires_payment": True}),
        }
    )

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.reason_code == ReasonCode.TOS_PROHIBITS_AUTOMATION


@pytest.mark.asyncio
async def test_tos_page_prohibition_still_outranks_docs_page_payment_signal() -> None:
    # Same precedence check, sources reversed -- confirms it's the flag's
    # rank that decides, never which source happened to be scanned first.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com", docs_text=LONG_DOCS_TEXT,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = PerUrlExtractor(
        {
            "https://docs.example.com": _clean_tos().model_copy(update={"requires_payment": True}),
            "https://example.com/terms": _clean_tos().model_copy(update={"prohibits_automation": True}),
        }
    )

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.reason_code == ReasonCode.TOS_PROHIBITS_AUTOMATION


@pytest.mark.asyncio
async def test_docs_page_clean_and_tos_unfindable_is_still_unverifiable_not_auto() -> None:
    # A clean docs-page scan is not sufficient evidence of a clean ToS --
    # D-021's principle still holds. TOS_UNVERIFIABLE only fires here,
    # after confirming no signal was found anywhere, never before.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com", docs_text=LONG_DOCS_TEXT,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({})  # every ToS guess fails
    extractor = PerUrlExtractor({"https://docs.example.com": _clean_tos()})

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.TOS_UNVERIFIABLE


@pytest.mark.asyncio
async def test_both_sources_clean_clears_to_auto() -> None:
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com", docs_text=LONG_DOCS_TEXT,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(LONG_TOS_TEXT)})
    extractor = PerUrlExtractor(
        {"https://docs.example.com": _clean_tos(), "https://example.com/terms": _clean_tos()}
    )

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert extractor.calls == ["https://docs.example.com", "https://example.com/terms"]  # both sources really scanned
