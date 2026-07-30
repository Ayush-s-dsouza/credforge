"""Deterministic regex/keyword extraction -- the fallback Extractor used when
ANTHROPIC_API_KEY is not set. This is a real, fully-functional path, not a
stub: extraction must work with zero external dependencies and zero cost on
the default/offline path (tests only ever exercise this or a FakeExtractor,
never the live LLM). See DECISIONS.md.
"""

import re

from ..enums import AuthScheme
from .llm import ClassifyExtraction, DiscoveryExtraction, TosGateExtraction

_API_BASE_URL_RE = re.compile(
    r"https://(?:api|developer|developers)\.[\w.-]+(?:/v\d+)?", re.IGNORECASE
)
_RATE_LIMIT_RE = re.compile(
    r"\b\d[\d,]*\s*(?:requests|calls|reqs?)\s*(?:per|/)\s*(?:second|minute|hour|day)\b",
    re.IGNORECASE,
)
_FREE_TIER_HINTS = ("free tier", "free plan", "sandbox", "test mode", "trial")
_DOCS_LIKE_HINTS = ("api", "endpoint", "authorization", "authentication", "rest", "graphql")

# Ordered most-specific-first: the first matching hint wins.
_AUTH_HINTS: list[tuple[str, AuthScheme]] = [
    ("oauth 2.0 authorization code", AuthScheme.OAUTH2_AUTH_CODE),
    ("oauth2 authorization code", AuthScheme.OAUTH2_AUTH_CODE),
    ("authorization code grant", AuthScheme.OAUTH2_AUTH_CODE),
    ("client credentials grant", AuthScheme.OAUTH2_CLIENT_CREDENTIALS),
    ("client_credentials", AuthScheme.OAUTH2_CLIENT_CREDENTIALS),
    ("pkce", AuthScheme.OAUTH2_PKCE),
    ("oauth 1.0", AuthScheme.OAUTH1),
    ("oauth1", AuthScheme.OAUTH1),
    ("x-api-key", AuthScheme.API_KEY),
    ("api key", AuthScheme.API_KEY),
    ("api-key", AuthScheme.API_KEY),
    ("basic auth", AuthScheme.BASIC),
    ("authorization: bearer", AuthScheme.BEARER_STATIC),
    ("bearer token", AuthScheme.BEARER_STATIC),
    ("oauth", AuthScheme.OAUTH2_AUTH_CODE),  # generic mention -- lowest-priority match
]

_PAGINATION_HINTS: list[tuple[str, str]] = [
    ("cursor", "cursor"),
    ("next_page_token", "cursor"),
    ("link header", "link_header"),
    ('rel="next"', "link_header"),
    ("offset", "offset_limit"),
    ("page_number", "page_number"),
    ("page=", "page_number"),
]

_TOS_PROHIBITION_HINTS = (
    "may not use automated",
    "automated means",
    "bots or other automated",
    "scripts to create accounts",
    "no automated account creation",
)
_PAYMENT_HINTS = ("credit card required", "paid plan", "contact sales for pricing")
_BUSINESS_VERIFICATION_HINTS = ("business verification", "company registration", "tax id", "ein")
_SALES_CONTACT_HINTS = ("contact sales", "talk to sales", "schedule a demo", "request access")
_PHONE_VERIFICATION_HINTS = ("phone verification", "verify your phone", "sms code")
_CAPTCHA_HINTS = ("captcha", "recaptcha", "hcaptcha")
_SSO_ONLY_HINTS = ("sso only", "single sign-on required", "sign in with your organization")


def _find_snippet(text: str, needle: str, *, context: int = 60) -> str:
    idx = text.lower().find(needle)
    if idx == -1:
        return needle
    start = max(0, idx - context)
    end = min(len(text), idx + len(needle) + context)
    return text[start:end].strip()


def _contains_hint(lower_text: str, hint: str) -> bool:
    """Word-boundary match, not a bare substring check.

    Found via a real run against GitHub's actual ToS page: a naive `in`
    check on "captcha" matched inside "octocaptcha_origin_optimization" --
    a feature-flag identifier embedded in the page, not a real CAPTCHA
    clause. `\\b...\\b` requires the hint to start/end at a word boundary,
    so it can't match as a sub-word of a longer identifier. See
    DECISIONS.md D-022.
    """
    return re.search(rf"\b{re.escape(hint)}\b", lower_text) is not None


def _check(text: str, lower: str, hints: tuple[str, ...]) -> tuple[bool, list[str]]:
    hits = [h for h in hints if _contains_hint(lower, h)]
    return bool(hits), [_find_snippet(text, h) for h in hits]


class HeuristicExtractor:
    async def extract_discovery(self, *, docs_text: str, docs_url: str) -> DiscoveryExtraction:
        lower = docs_text.lower()
        evidence: list[str] = []

        has_public_api = any(_contains_hint(lower, h) for h in _DOCS_LIKE_HINTS)

        base_url_match = _API_BASE_URL_RE.search(docs_text)
        base_url = base_url_match.group(0) if base_url_match else None
        if base_url:
            evidence.append(base_url)

        free_tier_hit = next((h for h in _FREE_TIER_HINTS if _contains_hint(lower, h)), None)
        if free_tier_hit:
            evidence.append(_find_snippet(docs_text, free_tier_hit))

        rate_match = _RATE_LIMIT_RE.search(docs_text)
        rate_limit_notes = rate_match.group(0) if rate_match else None
        if rate_limit_notes:
            evidence.append(rate_limit_notes)

        pagination_style_hint = next((v for h, v in _PAGINATION_HINTS if _contains_hint(lower, h)), None)
        auth_scheme_hint = next((v.value for h, v in _AUTH_HINTS if _contains_hint(lower, h)), None)

        return DiscoveryExtraction(
            has_public_api=has_public_api,
            base_url=base_url,
            developer_portal_url=None,  # no reliable heuristic signal for this
            free_tier_available=free_tier_hit is not None,
            rate_limit_notes=rate_limit_notes,
            pagination_style_hint=pagination_style_hint,
            validation_endpoint=None,  # no reliable heuristic signal for this
            auth_scheme_hint=auth_scheme_hint,
            evidence_snippets=evidence,
        )

    async def extract_classification(
        self, *, docs_text: str, docs_url: str, discovery: DiscoveryExtraction
    ) -> ClassifyExtraction:
        lower = docs_text.lower()
        auth_scheme = next((v for h, v in _AUTH_HINTS if _contains_hint(lower, h)), None)
        if auth_scheme is None:
            auth_scheme = AuthScheme.NO_PUBLIC_API if not discovery.has_public_api else AuthScheme.API_KEY
        return ClassifyExtraction(
            auth_scheme=auth_scheme.value,
            redirect_uris_required=_contains_hint(lower, "redirect_uri") or _contains_hint(lower, "callback url"),
            scopes_available=[],  # heuristics can't reliably enumerate scopes
            confidence=0.5,  # heuristic classification is always medium-confidence at best
        )

    async def extract_tos_gate_signals(self, *, tos_text: str, tos_url: str) -> TosGateExtraction:
        lower = tos_text.lower()
        prohibits_automation, ev1 = _check(tos_text, lower, _TOS_PROHIBITION_HINTS)
        requires_payment, ev2 = _check(tos_text, lower, _PAYMENT_HINTS)
        requires_business_verification, ev3 = _check(tos_text, lower, _BUSINESS_VERIFICATION_HINTS)
        requires_sales_contact, ev4 = _check(tos_text, lower, _SALES_CONTACT_HINTS)
        requires_phone_verification, ev5 = _check(tos_text, lower, _PHONE_VERIFICATION_HINTS)
        requires_captcha, ev6 = _check(tos_text, lower, _CAPTCHA_HINTS)
        requires_sso_only, ev7 = _check(tos_text, lower, _SSO_ONLY_HINTS)

        return TosGateExtraction(
            prohibits_automation=prohibits_automation,
            requires_payment=requires_payment,
            requires_business_verification=requires_business_verification,
            requires_sales_contact=requires_sales_contact,
            requires_phone_verification=requires_phone_verification,
            requires_captcha=requires_captcha,
            requires_sso_only=requires_sso_only,
            evidence_snippets=ev1 + ev2 + ev3 + ev4 + ev5 + ev6 + ev7,
        )
