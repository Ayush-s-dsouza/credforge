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
async def test_tos_unverifiable_no_longer_blocks_auto() -> None:
    # D-067: an unfindable ToS page is a detection limitation, not a
    # vendor decision -- AUTO, with the uncertainty recorded, not HITL.
    fetch = FakeFetchProvider({})  # every guess fails
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert result.tos_status == "unverifiable"
    assert result.tos_checked_url is None
    assert "tos_status" in {g.field for g in result.completeness_gaps}
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
    # D-067: still correctly detected as unverifiable (every guess is a
    # soft 404, none usable) -- but that no longer means HITL.
    fetch = FakeFetchProvider(
        {f"https://example.com{path}": _ok("Page not found. " * 20) for path in [
            "/developers/terms", "/developer-terms", "/legal/developer-terms",
            "/terms-of-service", "/terms", "/tos", "/legal/terms",
        ]}
    )
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert result.tos_status == "unverifiable"
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
    assert result.tos_status == "verified_permitted"
    assert result.tos_checked_url == "https://example.com/terms"


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
    # Domain matches the identity_key under test ("linear.app") so the ToS
    # page is actually found -- keeps this test's real assertion (which
    # completeness gaps get suppressed for a confirmed GraphQL API) clean
    # of the separate tos_status gap D-067 would otherwise add here too.
    fetch = FakeFetchProvider({"https://linear.app/terms": _ok(LONG_TOS_TEXT)})
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

    # AUTO (D-067: an unfindable dedicated ToS page no longer blocks on its
    # own), with tos_status recording the real uncertainty -- and,
    # unaffected by that change, the docs-page requires_payment signal
    # must still have been suppressed rather than blocking outright.
    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert result.tos_status == "unverifiable"


# --- D-081: price-shaped free-tier evidence ($0/$0.00/month + "free" as a --
# pricing-tier label), not just the literal phrases D-057/D-058 already
# recognized -- found live against TheCatAPI's real pricing page.

_THECATAPI_SHAPED_TEXT = (
    "The Cat API - Cat as a Service. Start smart, scale efficiently. "
    "$0.00/Month Free FREE cats to help developers learn. 10,000 requests a month. "
    "Join our helpful community. Free code samples. Images and breed information. "
    "Get Commercial Access -- Contact us for pricing. Enterprise: built for your "
    "business needs with unlimited requests and bandwidth. "
)


@pytest.mark.asyncio
async def test_price_shaped_free_tier_suppresses_requires_payment_thecatapi_shaped() -> None:
    # The real live bug: TheCatAPI's real pricing page has a genuine,
    # permanent $0.00/month free tier (10,000 requests/month) -- but
    # matches none of the literal _FREE_TIER_OVERRIDE_MARKERS phrases
    # ("free tier", "free api key", ...), so GATE blocked a vendor with a
    # real, working free signup path on requires_payment, triggered by the
    # separate "Contact us for pricing" enterprise-tier language.
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(_THECATAPI_SHAPED_TEXT)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"requires_payment": True, "evidence_snippets": ["Contact us for pricing."]})
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO


# --- D-081 regression guard, real vendor: price-shaped text must never -----
# touch a flag D-058 already excludes from suppression entirely.

_POSTMARK_SHAPED_TEXT = (
    "Postmark Pricing and Free Trial. Everyone starts on our free Developer "
    "tier $0.00 /mo which includes 100 emails every month, and it never "
    "expires or runs out. These are the terms of service governing use of "
    "this API: Access the Service by any means other than through the "
    "standard industry-accepted or AC PM-approved application program "
    "interfaces is prohibited. " * 2
)


@pytest.mark.asyncio
async def test_price_shaped_free_tier_text_does_not_suppress_a_real_prohibition_postmark_shaped() -> None:
    # Postmark's real pricing page genuinely contains the exact price-shaped
    # free-tier language D-081 now detects ("$0.00 /mo" next to "free") --
    # and its real ToS separately, genuinely prohibits automated account
    # creation (live-verified: GATE correctly blocks Postmark today on
    # tos_prohibits_automation). prohibits_automation is structurally
    # excluded from _SCOPE_SUPPRESSIBLE_FLAGS -- never legitimately "true
    # only for a paid tier" -- so price-shaped evidence anywhere in the
    # SAME source text must never touch it, even sitting right next to it.
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(_POSTMARK_SHAPED_TEXT)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={
            "prohibits_automation": True,
            "evidence_snippets": [
                "Access the Service by any means other than through the standard "
                "industry-accepted or AC PM-approved application program interfaces;"
            ],
        })
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.TOS_PROHIBITS_AUTOMATION


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
    # D-067: a found prohibition is a definitive, real verdict -- always
    # "verified_prohibited", completely unaffected by the unverifiable-ToS
    # change (this ToS page WAS found and read; it just said no).
    assert result.tos_status == "verified_prohibited"
    assert result.tos_checked_url == "https://example.com/terms"


@pytest.mark.asyncio
async def test_prohibition_found_on_docs_page_alone_is_still_verified_prohibited() -> None:
    # D-067: prohibits_automation found on the developer docs page, with
    # the dedicated ToS page unreachable -- tos_status must still be
    # "verified_prohibited", not "unverifiable". The real-world fact (this
    # vendor prohibits automation) doesn't become less true just because
    # the *specific document* that stated it wasn't the one this project
    # calls "the ToS page."
    text = "You may not use bots or automated means to access this API. " * 5
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com", docs_text=text,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({})  # no dedicated ToS page reachable
    extractor = PerUrlExtractor(
        {"https://docs.example.com": _clean_tos().model_copy(update={"prohibits_automation": True, "evidence_snippets": [text]})}
    )

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.TOS_PROHIBITS_AUTOMATION
    assert result.tos_status == "verified_prohibited"
    assert result.tos_checked_url is None  # the dedicated ToS page itself was never found


@pytest.mark.asyncio
async def test_precondition_short_circuits_leave_tos_status_unset() -> None:
    # tos_status is None only when GATE never reached its ToS-checking
    # logic at all -- a precondition (here, DISCOVERY_FAILED) returned
    # first. Distinct from "unverifiable", which means GATE tried and
    # genuinely couldn't find a ToS page.
    discovery = DiscoveryResult(reason_code=ReasonCode.DISCOVERY_FAILED)
    fetch = FakeFetchProvider({})
    extractor = FakeExtractor()

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.DISCOVERY_FAILED
    assert result.tos_status is None
    assert result.tos_checked_url is None


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


# --- D-058: scope suppression generalized to every scope-suppressible flag -

_ALPHA_VANTAGE_FULL_TEXT = (
    "This is a premium endpoint. If you would like to access realtime, 15-minute delayed, "
    "and/or historical intraday data, please subscribe to a premium membership plan for your "
    "personal use. For commercial use, please contact sales. "
    "Get your free API key today to get started with our standard endpoints."
)
_ALPHA_VANTAGE_DEV_PORTAL_URL = "https://www.alphavantage.co/support/#api-key"


@pytest.mark.asyncio
async def test_scoped_sales_contact_language_does_not_trigger_requires_sales_contact() -> None:
    # The exact reported bug: once requires_payment alone was fixed (D-057),
    # the very next clause in the same real sentence -- "For commercial
    # use, please contact sales" -- tripped requires_sales_contact instead.
    # Same scope qualifier ("for commercial use"), different flag.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com",
        extraction=DiscoveryExtraction(has_public_api=True, developer_portal_url=_ALPHA_VANTAGE_DEV_PORTAL_URL),
    )
    text = _ALPHA_VANTAGE_FULL_TEXT + " These are the terms of service governing use of this API. " * 3
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(
            update={"requires_sales_contact": True, "evidence_snippets": ["For commercial use, please contact sales."]}
        )
    )

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO


@pytest.mark.asyncio
async def test_structural_guard_suppresses_unrecognized_scoped_phrasing_for_a_recipe_backed_vendor() -> None:
    # Neither a SCOPE_QUALIFIER nor a FREE_TIER marker literally appears --
    # this text is deliberately phrased so no keyword list recognizes it.
    # alphavantage.co is a real, registered SignupRecipe (D-052/D-053: a
    # real credential acquired live, out-of-band proof the free signup
    # flow works) -- the structural guard must suppress anyway.
    text = (
        "Business customers wishing to integrate at scale should reach out to discuss options. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://alphavantage.co/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(
            update={"requires_business_verification": True, "evidence_snippets": ["reach out to discuss options"]}
        )
    )
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.alphavantage.co",
        extraction=DiscoveryExtraction(has_public_api=True),  # no developer_portal_url -- proves the guard doesn't need it
    )

    result = await gate("alphavantage.co", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO


@pytest.mark.asyncio
async def test_structural_guard_does_not_apply_to_a_non_recipe_backed_vendor() -> None:
    # Identical unrecognized phrasing to the test above, but this domain
    # has no registered SignupRecipe -- no out-of-band proof exists, so
    # the block must stand. Proves the structural guard is actually
    # conditioned on recipe-backing, not a blanket keyword-miss pass.
    text = (
        "Business customers wishing to integrate at scale should reach out to discuss options. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://example.com/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(
            update={"requires_business_verification": True, "evidence_snippets": ["reach out to discuss options"]}
        )
    )

    result = await gate("example.com", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.REQUIRES_BUSINESS_VERIFICATION


@pytest.mark.asyncio
async def test_structural_guard_does_not_override_genuinely_unscoped_language_for_a_recipe_backed_vendor() -> None:
    # Even for a proven, recipe-backed vendor, real unscoped-access
    # language must still block -- the structural guard only covers the
    # "no keyword recognized, no unscoped language present" gap, never a
    # genuine unscoped statement. (Hypothetical for alphavantage.co --
    # the real vendor has no such language -- but the code must not treat
    # recipe-backing as a blanket override.)
    text = (
        "A paid account is required to obtain an API key. "
        "These are the terms of service governing use of this API. " * 3
    )
    fetch = FakeFetchProvider({"https://alphavantage.co/terms": _ok(text)})
    extractor = FakeExtractor(
        _clean_tos().model_copy(update={"requires_payment": True, "evidence_snippets": [text]})
    )

    result = await gate("alphavantage.co", discovery=DISCOVERED_OK, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.HITL
    assert result.reason_code == ReasonCode.REQUIRES_PAYMENT


@pytest.mark.asyncio
async def test_alpha_vantage_real_shape_flags_are_suppressed_and_reaches_auto_despite_no_tos_page() -> None:
    # End-to-end regression for the actual reported case: the full real
    # sentence (both the premium-endpoint/payment clause AND the
    # commercial-use/sales-contact clause DISCOVER's crawl would return
    # together) on the real recipe-backed domain, with no dedicated ToS
    # page findable (matching the real live run -- Alpha Vantage has none
    # of the guessed paths). Must reach AUTO, not stall on either flag.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://www.alphavantage.co/documentation/",
        docs_text=_ALPHA_VANTAGE_FULL_TEXT,
        extraction=DiscoveryExtraction(
            has_public_api=True, base_url="https://www.alphavantage.co/query",
            developer_portal_url=_ALPHA_VANTAGE_DEV_PORTAL_URL,
        ),
    )
    fetch = FakeFetchProvider({})  # no dedicated ToS page reachable, matching the real live run
    extractor = FakeExtractor(
        _clean_tos().model_copy(
            update={
                "requires_payment": True,
                "requires_sales_contact": True,
                "evidence_snippets": [
                    "This is a premium endpoint.",
                    "For commercial use, please contact sales.",
                ],
            }
        )
    )

    result = await gate(
        "alphavantage.co", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    # D-067: with no dedicated ToS page reachable under any of GATE's
    # guessed paths (matching the real live run's own log line, "could
    # not locate a dedicated ToS/developer-agreement page"), the app now
    # reaches real AUTO -- an unfindable ToS page no longer blocks on its
    # own (superseding this test's original D-021-era expectation). The
    # uncertainty isn't dropped: tos_status records "unverifiable"
    # explicitly. What D-058 proves here, unchanged: neither payment nor
    # sales_contact is what would have blocked it either.
    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert result.tos_status == "unverifiable"


@pytest.mark.asyncio
async def test_alpha_vantage_shape_reaches_auto_when_a_tos_page_is_findable() -> None:
    # The genuine end-to-end proof the fix is complete: identical scoped
    # payment/sales-contact signals, on the real recipe-backed domain, but
    # with a real, clean, findable dedicated ToS page (unlike Alpha
    # Vantage's actual one, which the test above shows GATE can't find at
    # all -- a separate, pre-existing gap, not something D-058 touches).
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://www.alphavantage.co/documentation/",
        docs_text=_ALPHA_VANTAGE_FULL_TEXT,
        extraction=DiscoveryExtraction(
            has_public_api=True, base_url="https://www.alphavantage.co/query",
            developer_portal_url=_ALPHA_VANTAGE_DEV_PORTAL_URL,
        ),
    )
    clean_tos_text = "These are the terms of service. Standard use is unrestricted. " * 4
    fetch = FakeFetchProvider({"https://alphavantage.co/terms": _ok(clean_tos_text)})
    extractor = PerUrlExtractor(
        {
            "https://www.alphavantage.co/documentation/": _clean_tos().model_copy(
                update={
                    "requires_payment": True,
                    "requires_sales_contact": True,
                    "evidence_snippets": ["This is a premium endpoint.", "please contact sales"],
                }
            ),
            "https://alphavantage.co/terms": _clean_tos(),
        }
    )

    result = await gate(
        "alphavantage.co", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor
    )

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO


# --- D-059: real anchor-link discovery for the ToS page, guess list as fallback

_FOOTER_WITH_TOS_LINK = """
<html><body>
<nav>Home Docs Pricing</nav>
<main>API Documentation content goes here, more than two hundred characters
long so it clears the minimum usable text length check that guards every
page this parser is asked to look at, including this synthetic one here.</main>
<footer>
<a href="/about">About</a>
<a href="/terms_of_service/">Terms of Service</a>
<a href="mailto:support@example.com">Contact</a>
<a href="#top">Back to top</a>
</footer>
</body></html>
"""


def test_extract_tos_candidate_links_matches_href_and_text_keywords_and_resolves_relative_urls() -> None:
    from credforge.pipeline.gate import _extract_tos_candidate_links

    html = """
    <a href="/legal/privacy">Privacy</a>
    <a href="/help">Read our Terms and Conditions</a>
    <a href="https://other.example.com/tos">TOS</a>
    <a href="mailto:legal@example.com">Email legal</a>
    <a href="javascript:void(0)">Cookie settings</a>
    <a href="#section-2">Jump to section 2</a>
    """
    links = _extract_tos_candidate_links(html, base_url="https://example.com/docs", vendor_domain="example.com")

    assert "https://example.com/legal/privacy" in links
    assert "https://example.com/help" in links  # matched on visible text, not href
    assert "https://other.example.com/tos" in links  # a subdomain of the same registrable domain -- still the vendor
    assert not any("mailto:" in link for link in links)
    assert not any(link.startswith(("javascript:", "https://example.com/docs#")) for link in links)


def test_extract_tos_candidate_links_excludes_third_party_domains() -> None:
    from credforge.pipeline.gate import _extract_tos_candidate_links

    # The real bug found live: Alpha Vantage's own docs page cites the
    # Federal Reserve (FRED), the IMF, and Investopedia as data-source
    # attribution -- each with a real, keyword-matching "terms" link, none
    # of which is Alpha Vantage's own ToS. Without a domain filter,
    # _find_tos_page would fetch and trust a third party's terms page as
    # if it were the vendor's own, and would issue real, unsolicited
    # requests to domains that were never the one being resolved.
    html = """
    <a href="https://fred.stlouisfed.org/docs/api/terms_of_use.html" target="_blank">FRED API Terms of Use</a>
    <a href="https://www.imf.org/external/terms.htm" target="_blank">IMF Terms of Use</a>
    <a href="https://www.alphavantage.co/terms_of_service/" target="_blank">Terms of Service</a>
    """
    links = _extract_tos_candidate_links(
        html, base_url="https://www.alphavantage.co/documentation/", vendor_domain="alphavantage.co"
    )

    assert links == ["https://www.alphavantage.co/terms_of_service/"]
    assert not any("fred.stlouisfed.org" in link for link in links)
    assert not any("imf.org" in link for link in links)


def test_extract_tos_candidate_links_does_not_match_tos_as_a_bare_substring() -> None:
    from credforge.pipeline.gate import _extract_tos_candidate_links

    # The second real bug found live, same page: "tos" as a bare
    # substring matches inside "ULTOSC" (Alpha Vantage's real Ultimate
    # Oscillator API function name, used throughout its own example query
    # URLs) -- same class of collision D-022 already fixed for
    # heuristic_extractor.py's keyword matching ("captcha" inside
    # "octocaptcha_origin_optimization"), same fix here.
    html = """
    <a href="https://www.alphavantage.co/query?function=ULTOSC&symbol=IBM&apikey=demo">Try it out</a>
    <a href="https://www.alphavantage.co/terms_of_service/">Terms of Service</a>
    """
    links = _extract_tos_candidate_links(
        html, base_url="https://www.alphavantage.co/documentation/", vendor_domain="alphavantage.co"
    )

    assert links == ["https://www.alphavantage.co/terms_of_service/"]
    assert not any("ULTOSC" in link for link in links)


def test_extract_tos_candidate_links_returns_empty_on_malformed_html_not_a_crash() -> None:
    from credforge.pipeline.gate import _extract_tos_candidate_links

    # Deliberately broken markup -- must degrade to "no candidates found",
    # never raise, so the caller falls back to the guess list.
    links = _extract_tos_candidate_links(
        "<a href='/terms'><a><a>>>><div", base_url="https://example.com", vendor_domain="example.com"
    )
    assert isinstance(links, list)


@pytest.mark.asyncio
async def test_tos_found_via_docs_page_link_discovery_at_an_unconventional_url() -> None:
    # The exact real case: Alpha Vantage's real ToS lives at
    # /terms_of_service/, discoverable from a footer link on the docs
    # page DISCOVER already crawled -- no guess in _TOS_URL_GUESSES needs
    # to have anticipated this specific path.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://www.alphavantage.co/documentation/",
        docs_text=_FOOTER_WITH_TOS_LINK,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    tos_text = "These are the real terms of service for this vendor. " * 5
    fetch = FakeFetchProvider(
        {
            "https://alphavantage.co": FetchResult(
                url="https://alphavantage.co", final_url="https://alphavantage.co", status_code=200,
                content_type="text/html", text="<html><body>homepage, no footer links here</body></html>",
                fetched_at=datetime.now(timezone.utc),
            ),
            "https://www.alphavantage.co/terms_of_service/": FetchResult(
                url="x", final_url="x", status_code=200, content_type="text/html",
                text=tos_text, fetched_at=datetime.now(timezone.utc),
            ),
        }
    )
    extractor = FakeExtractor(_clean_tos())

    result = await gate("alphavantage.co", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.evidence[0].source_url == "https://www.alphavantage.co/terms_of_service/"


@pytest.mark.asyncio
async def test_tos_found_via_homepage_link_discovery_when_docs_page_has_no_footer() -> None:
    # The docs page itself has no footer/ToS link at all -- the homepage
    # is the other real source scanned, and must be enough on its own.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com",
        docs_text="Plain API docs with no links of any kind, just prose about endpoints. " * 5,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    tos_text = "These are the terms of service found via the homepage footer. " * 5
    fetch = FakeFetchProvider(
        {
            "https://example.com": FetchResult(
                url="https://example.com", final_url="https://example.com", status_code=200,
                content_type="text/html", text=_FOOTER_WITH_TOS_LINK, fetched_at=datetime.now(timezone.utc),
            ),
            "https://example.com/terms_of_service/": FetchResult(
                url="x", final_url="x", status_code=200, content_type="text/html",
                text=tos_text, fetched_at=datetime.now(timezone.utc),
            ),
        }
    )
    extractor = FakeExtractor(_clean_tos())

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.evidence[0].source_url == "https://example.com/terms_of_service/"


@pytest.mark.asyncio
async def test_link_discovery_finds_nothing_falls_back_to_the_guess_list() -> None:
    # Neither the docs page nor the homepage has any ToS-shaped link at
    # all -- link discovery must degrade cleanly to the pre-D-059 guess
    # list, not fail outright.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com",
        docs_text="Plain API docs, no links, just prose about endpoints and authentication. " * 5,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider(
        {
            "https://example.com": FetchResult(
                url="https://example.com", final_url="https://example.com", status_code=200,
                content_type="text/html", text="<html><body>No footer links at all here.</body></html>",
                fetched_at=datetime.now(timezone.utc),
            ),
            "https://example.com/terms": _ok(LONG_TOS_TEXT),
        }
    )
    extractor = FakeExtractor(_clean_tos())

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.evidence[0].source_url == "https://example.com/terms"


@pytest.mark.asyncio
async def test_underscore_guess_variants_are_tried_when_link_discovery_finds_nothing() -> None:
    # The narrow fix: even with zero link-discovery candidates, the
    # expanded guess list (including the new underscore variants) still
    # finds a real ToS page a purely-hyphenated guess list would miss.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com",
        docs_text="Plain API docs, no links at all. " * 10,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({"https://example.com/terms_of_service/": _ok(LONG_TOS_TEXT)})
    extractor = FakeExtractor(_clean_tos())

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.evidence[0].source_url == "https://example.com/terms_of_service/"


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
async def test_docs_page_clean_and_tos_unfindable_still_reaches_auto_with_gap_recorded() -> None:
    # D-067: a clean docs-page scan plus an unfindable dedicated ToS page
    # now reaches AUTO -- tos_status="unverifiable" records exactly what
    # D-021's original HITL used to represent, but as carried-forward
    # uncertainty (a completeness gap), not an escalation to a human.
    discovery = DiscoveryResult(
        reason_code=None, docs_url="https://docs.example.com", docs_text=LONG_DOCS_TEXT,
        extraction=DiscoveryExtraction(has_public_api=True),
    )
    fetch = FakeFetchProvider({})  # every ToS guess fails
    extractor = PerUrlExtractor({"https://docs.example.com": _clean_tos()})

    result = await gate("example.com", discovery=discovery, classify=CLASSIFIED_OK, fetch=fetch, extractor=extractor)

    assert result.status == Status.AUTO
    assert result.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert result.tos_status == "unverifiable"
    assert "tos_status" in {g.field for g in result.completeness_gaps}


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
