"""Extractor: the interface DISCOVER/CLASSIFY/GATE use to turn crawled docs
text into structured fields.

AnthropicExtractor (Stage 2, used when ANTHROPIC_API_KEY is set) and
HeuristicExtractor (Stage 2, deterministic regex/keyword fallback) both
satisfy this same Protocol -- the pipeline stages that call it never know
or care which one they're holding.
"""

from typing import Literal, Protocol

from pydantic import BaseModel

# D-068: whether a credential is mandatory, optional, or nonexistent for
# this API -- independent of `auth_scheme`. "required" and "none" are the
# two unambiguous ends (every call needs a credential; there is no
# credential mechanism at all); "optional" is the real, genuine third
# state a binary auth_scheme can't represent -- an API that works fully
# anonymously but also offers a real, acquirable credential for higher
# limits or more features (NASA's api.nasa.gov: "You do not need to
# authenticate... However, if you will be intensively using the APIs...
# you should sign up for a NASA developer key"). See DECISIONS.md D-068.
AuthRequirement = Literal["required", "optional", "none"]


class DiscoveryExtraction(BaseModel):
    has_public_api: bool
    base_url: str | None = None
    developer_portal_url: str | None = None
    free_tier_available: bool | None = None
    rate_limit_notes: str | None = None
    pagination_style_hint: str | None = None
    validation_endpoint: str | None = None
    auth_scheme_hint: str | None = None
    evidence_snippets: list[str] = []


class ClassifyExtraction(BaseModel):
    # D-068: `auth_scheme` answers "what credential, if any, is there to
    # acquire" -- NOT "is a credential strictly required for basic access."
    # An API that permits fully anonymous use but also offers a real,
    # optional credential (more quota, more features) is classified by
    # that credential's scheme, never `none` -- `none` is reserved for an
    # API with no credential mechanism to acquire at all (e.g. Open-Meteo).
    # `auth_required` carries the required/optional/none distinction that
    # `auth_scheme` alone can't -- both fields are always set together,
    # consistently: `auth_required="none"` if and only if
    # `auth_scheme="none"`.
    auth_scheme: str
    auth_required: AuthRequirement = "required"
    redirect_uris_required: bool = False
    scopes_available: list[str] = []
    confidence: float


class TosGateExtraction(BaseModel):
    prohibits_automation: bool
    requires_payment: bool
    requires_business_verification: bool
    requires_sales_contact: bool
    requires_phone_verification: bool
    requires_captcha: bool
    requires_sso_only: bool
    evidence_snippets: list[str] = []


class Extractor(Protocol):
    async def extract_discovery(self, *, docs_text: str, docs_url: str) -> DiscoveryExtraction: ...

    async def extract_classification(
        self, *, docs_text: str, docs_url: str, discovery: DiscoveryExtraction
    ) -> ClassifyExtraction: ...

    async def extract_tos_gate_signals(self, *, tos_text: str, tos_url: str) -> TosGateExtraction: ...
