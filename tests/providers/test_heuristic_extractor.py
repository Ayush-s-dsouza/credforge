import pytest

from credforge.enums import AuthScheme
from credforge.providers.heuristic_extractor import HeuristicExtractor

OAUTH_DOCS = """
# Widget API

Authenticate using OAuth 2.0 authorization code grant. After the user
authorizes your app, redirect to your redirect_uri with a code.

Base URL: https://api.widget.example.com/v1

Rate limits: 5000 requests per hour per access token.

We offer a free tier for development and testing (sandbox mode).

List endpoint: GET /v1/items?cursor=abc123 -- results are paginated by cursor.
"""

API_KEY_DOCS = """
# Simple Weather API

Pass your API key as a query parameter: ?api_key=YOUR_KEY.
No OAuth required. Free plan: 60 calls per minute.
"""

TOS_PROHIBITS_AUTOMATION = """
Section 4: Account Registration. You may not use automated means, bots or
other automated methods to register for or access the Service.
"""

TOS_REQUIRES_SALES = """
API access for production workloads requires a paid plan. Contact sales for
pricing and provisioning.
"""

# Regression fixture: real GitHub ToS text embeds feature-flag identifiers
# like "octocaptcha_origin_optimization" in a JS/config blob. A naive
# substring match on "captcha" fires on this; a real CAPTCHA requirement
# clause should not be confused with it. See DECISIONS.md D-022.
TOS_WITH_EMBEDDED_FEATURE_FLAG_NOISE = """
Section 9: Miscellaneous. This site uses feature flags including
"octocaptcha_origin_optimization" and "recaptcha_disabled_regions" for
internal experimentation purposes only.
"""


@pytest.mark.asyncio
async def test_discovers_oauth_docs() -> None:
    extractor = HeuristicExtractor()
    result = await extractor.extract_discovery(docs_text=OAUTH_DOCS, docs_url="https://docs.widget.example.com")

    assert result.has_public_api is True
    assert result.base_url == "https://api.widget.example.com/v1"
    assert result.free_tier_available is True
    assert result.rate_limit_notes is not None
    assert result.pagination_style_hint == "cursor"
    assert result.auth_scheme_hint == AuthScheme.OAUTH2_AUTH_CODE.value


@pytest.mark.asyncio
async def test_discovers_api_key_docs() -> None:
    extractor = HeuristicExtractor()
    result = await extractor.extract_discovery(docs_text=API_KEY_DOCS, docs_url="https://docs.weather.example.com")

    assert result.has_public_api is True
    assert result.auth_scheme_hint == AuthScheme.API_KEY.value
    assert result.free_tier_available is True


@pytest.mark.asyncio
async def test_classification_falls_back_to_no_public_api_when_nothing_found() -> None:
    extractor = HeuristicExtractor()
    discovery = await extractor.extract_discovery(docs_text="Just a marketing page.", docs_url="https://example.com")
    classification = await extractor.extract_classification(
        docs_text="Just a marketing page.", docs_url="https://example.com", discovery=discovery
    )

    assert discovery.has_public_api is False
    assert classification.auth_scheme == AuthScheme.NO_PUBLIC_API.value
    assert classification.confidence == 0.5


@pytest.mark.asyncio
async def test_classification_always_reports_auth_required_as_required() -> None:
    # D-068: the heuristic has no reliable way to tell required apart from
    # optional auth from keyword matches alone -- "required" is the
    # honest, conservative default it always reports.
    extractor = HeuristicExtractor()
    discovery = await extractor.extract_discovery(docs_text=API_KEY_DOCS, docs_url="https://docs.weather.example.com")
    classification = await extractor.extract_classification(
        docs_text=API_KEY_DOCS, docs_url="https://docs.weather.example.com", discovery=discovery
    )

    assert classification.auth_required == "required"


@pytest.mark.asyncio
async def test_tos_prohibition_is_detected_with_evidence() -> None:
    extractor = HeuristicExtractor()
    result = await extractor.extract_tos_gate_signals(
        tos_text=TOS_PROHIBITS_AUTOMATION, tos_url="https://example.com/tos"
    )

    assert result.prohibits_automation is True
    assert result.requires_payment is False
    assert any("automated" in snippet.lower() for snippet in result.evidence_snippets)


@pytest.mark.asyncio
async def test_captcha_hint_does_not_match_inside_an_unrelated_identifier() -> None:
    # Regression: found by running the real ToS extraction against GitHub's
    # actual terms-of-service page, which embeds
    # "octocaptcha_origin_optimization" as a feature-flag name -- a naive
    # substring match on "captcha" fired here even though there is no real
    # CAPTCHA-requirement clause anywhere on the page.
    extractor = HeuristicExtractor()
    result = await extractor.extract_tos_gate_signals(
        tos_text=TOS_WITH_EMBEDDED_FEATURE_FLAG_NOISE, tos_url="https://example.com/tos"
    )

    assert result.requires_captcha is False


@pytest.mark.asyncio
async def test_tos_sales_contact_and_payment_are_detected() -> None:
    extractor = HeuristicExtractor()
    result = await extractor.extract_tos_gate_signals(
        tos_text=TOS_REQUIRES_SALES, tos_url="https://example.com/tos"
    )

    assert result.prohibits_automation is False
    assert result.requires_payment is True
    assert result.requires_sales_contact is True
