"""Extractor: the interface DISCOVER/CLASSIFY/GATE use to turn crawled docs
text into structured fields.

AnthropicExtractor (Stage 2, used when ANTHROPIC_API_KEY is set) and
HeuristicExtractor (Stage 2, deterministic regex/keyword fallback) both
satisfy this same Protocol -- the pipeline stages that call it never know
or care which one they're holding.
"""

from typing import Protocol

from pydantic import BaseModel


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
    auth_scheme: str
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
