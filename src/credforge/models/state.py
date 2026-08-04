"""Per-app pipeline state and per-stage result models.

AppPipelineState is the mutable, per-app working record filled in one
stage at a time as the pipeline runs -- NOT the same model as the frozen
HandoffArtifact assembled at EMIT (see DECISIONS.md D-002). This file
grows: each stage after RESOLVE adds its own result model here
(DiscoveryResult at Stage 2, ClassifyResult at Stage 3, ...) and a
matching field on AppPipelineState, rather than being written once, fully,
up front.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..enums import ApiStyle, AuthScheme, CredentialType, ReasonCode, SourceTier, Status
from ..providers.browser import ProvisionStepResult
from ..providers.llm import AuthRequirement, DiscoveryExtraction


class ResolveCandidate(BaseModel):
    domain: str
    docs_url: str | None = None  # best (highest-ranked, content-verified) candidate; see D-027
    docs_url_candidates: list[str] = Field(default_factory=list)  # full ranked, verified list -- DISCOVER falls back through it (D-028)
    docs_url_reason: str | None = None  # why the winning URL was chosen -- auditability, per D-027
    confidence: float
    evidence_url: str
    evidence_snippet: str


class ResolveResult(BaseModel):
    resolved: bool
    reason_code: ReasonCode | None = None  # set only when resolved is False
    chosen: ResolveCandidate | None = None
    alternates: list[ResolveCandidate] = Field(default_factory=list)
    # D-082: True when `chosen` was NOT the winner of RESOLVE's own scoring
    # -- RESOLVE was ambiguous (top two candidates too close) and DISCOVER
    # settled it instead, by actually reading each candidate's page until
    # one verified as a real public API. False (the default) covers every
    # other path, including a normal unambiguous resolve.
    resolved_via_content_fallback: bool = False


class DiscoveryResult(BaseModel):
    # None = clean discovery; DISCOVERY_FAILED (couldn't crawl anything usable)
    # or DISCOVERY_INCOMPLETE (crawled fine, claimed has_public_api but gave us
    # nothing actionable) otherwise.
    reason_code: ReasonCode | None = None
    docs_url: str | None = None  # the candidate that actually worked -- may not be RESOLVE's top-ranked one (D-028)
    docs_text: str | None = None  # carried forward so CLASSIFY never has to re-fetch the same page
    extraction: DiscoveryExtraction | None = None  # None only when reason_code == DISCOVERY_FAILED
    detail: str | None = None  # e.g. "tried 4 candidate docs URL(s), none yielded a usable API page"

    # D-054: computed exactly once, here, from `docs_url` above -- the
    # actual URL this stage settled on, not RESOLVE's original top-ranked
    # candidate (which can differ, D-028, and which used to be described
    # separately and inconsistently in the artifact's evidence). CLASSIFY
    # and EMIT both read these two fields rather than recomputing the
    # tier themselves, so the artifact's evidence array and its `api`
    # block can never disagree about the tier of the same page again.
    source_tier: SourceTier | None = None  # set whenever docs_url is set; None otherwise
    source_tier_reason: str | None = None

    # D-066: True when `docs_url`'s content came from a real headless-browser
    # render, not a plain HTTP fetch -- the plain fetch's extracted text
    # looked like a client-rendered app shell (near-zero visible text despite
    # substantial script content), so HttpxFetchProvider re-fetched the same
    # URL with Playwright. Carried through to EMIT's evidence so the
    # artifact stays auditable about which pages required this.
    docs_rendered_with_browser: bool = False

    # D-055: REST vs. GraphQL vs. unknown, detected from docs_url/docs_text
    # once discovery settles on a real page. UNKNOWN is the conservative
    # default -- it changes nothing downstream, unlike a confirmed
    # GRAPHQL classification (which suppresses REST-specific completeness
    # gaps, see gate.py).
    api_style: ApiStyle | None = None  # set whenever docs_url is set; None otherwise


class ClassifyResult(BaseModel):
    # None = confident classification; CLASSIFY_LOW_CONFIDENCE otherwise.
    reason_code: ReasonCode | None = None
    auth_scheme: AuthScheme | None = None  # None only if the extractor's answer was unparseable
    # D-068: required/optional/none -- independent of auth_scheme, which
    # answers "what credential is there to acquire," not "is one strictly
    # necessary." None only alongside auth_scheme=None (extractor's answer
    # was unparseable) or when DISCOVER found no public API at all.
    auth_required: AuthRequirement | None = None
    redirect_uris_required: bool = False
    scopes_available: list[str] = Field(default_factory=list)
    # The *adjusted* confidence -- source-tier weighting (D-049) already
    # applied, not the extractor's raw number. source_tier records which
    # tier drove that adjustment, so a consumer can tell a 0.6 earned from
    # an official reference apart from a 0.6 dragged down from a blog post.
    confidence: float = 1.0
    source_tier: SourceTier | None = None  # None only when DISCOVER found no public API at all


class EvidenceItem(BaseModel):
    """Matches the handoff artifact's evidence[] entry shape exactly, so
    EMIT (Stage 7) can reuse these objects verbatim rather than converting."""

    claim: str
    source_url: str
    snippet: str


class CompletenessGap(BaseModel):
    """One expected field DISCOVER could not populate. Carried forward so
    the downstream toolkit-generation agent knows what it's inheriting
    instead of finding out at runtime -- see DECISIONS.md D-029. A gap is
    informational, never a reason to route to HITL on its own."""

    field: str
    reason: str


# D-067: what GATE actually learned about this vendor's ToS/policy --
# independent of `status`/`reason_code` below, and always set once GATE
# reaches its ToS-checking logic at all (None only when a precondition,
# e.g. DISCOVERY_FAILED/NO_PUBLIC_API/CLASSIFY_LOW_CONFIDENCE, returned
# before GATE ever got there). "unverifiable" is a real, distinct outcome
# from either verified state -- it no longer blocks AUTO on its own, see
# `gate()`'s docstring and DECISIONS.md D-067.
TosStatus = Literal["verified_permitted", "verified_prohibited", "unverifiable"]


class GateResult(BaseModel):
    status: Status
    reason_code: ReasonCode
    # Scoped to what GATE itself found (the ToS check) -- evidence from
    # earlier stages (RESOLVE's domain match, DISCOVER's docs signals)
    # lives on their own result objects; EMIT merges all of it together.
    evidence: list[EvidenceItem] = Field(default_factory=list)
    completeness_gaps: list[CompletenessGap] = Field(default_factory=list)
    tos_status: TosStatus | None = None
    tos_checked_url: str | None = None
    tos_discovery_method: str | None = None  # "link_discovery" | "guess_list" | None (never found)


class ProvisionResult(BaseModel):
    # "already_provisioned" is the idempotency-guard branch -- an open
    # credential already existed for this identity_key, so no email/browser
    # provider was ever called. "failed" means the browser flow itself
    # reported failure (see ProvisionOutcome.success) -- never partially
    # written to the vault or registry either way. See DECISIONS.md D-031.
    status: Literal["provisioned", "already_provisioned", "failed"]
    reason_code: ReasonCode | None = None  # set only when status == "failed"

    # Account-level: the developer-portal login credforge created, if this
    # vendor's flow has one at all. account_password_ref is None both on
    # failure AND when the vendor's flow genuinely has no password (e.g.
    # NASA's key-by-email flow) -- the two are distinguished by `status`,
    # not by this field alone. See DECISIONS.md D-041.
    account_email: str | None = None
    account_password_ref: str | None = None

    # API-level: what the integration actually authenticates requests
    # with. Exactly the field(s) matching credential_type are populated.
    credential_type: CredentialType | None = None
    api_key_ref: str | None = None
    client_id: str | None = None  # not secret -- kept plaintext, not vaulted
    client_secret_ref: str | None = None
    bearer_token_ref: str | None = None

    console_url: str | None = None
    steps: list[ProvisionStepResult] = Field(default_factory=list)

    # D-075: which recipe drove a successful provision -- None/None means
    # hand-authored (or, for a mocked/already-provisioned result, simply
    # not tracked). Mirrors ProvisionOutcome's own fields straight through.
    recipe_generated_by: str | None = None
    recipe_generated_at: datetime | None = None


class ValidateResult(BaseModel):
    # "invalid" covers every VALIDATION_FAILED_* reason -- the specific one
    # is always in reason_code, never inferred from status alone.
    status: Literal["valid", "invalid"]
    reason_code: ReasonCode | None = None  # set only when status == "invalid"
    http_status_code: int | None = None
    checked_url: str | None = None
    detail: str | None = None


class AppPipelineState(BaseModel):
    app_name: str
    input_key: str
    identity_key: str
    run_id: str
    started_at: datetime

    resolve: ResolveResult | None = None
    discovery: DiscoveryResult | None = None
    classify: ClassifyResult | None = None
    gate: GateResult | None = None
    provision: ProvisionResult | None = None
    # Named "validation", not "validate" -- pydantic v2's BaseModel still
    # carries a deprecated "validate" attribute for v1-compat, and a field
    # of that exact name triggers a shadowing warning on every import.
    validation: ValidateResult | None = None
