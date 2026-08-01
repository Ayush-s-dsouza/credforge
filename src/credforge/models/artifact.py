"""HandoffArtifact: the frozen, final JSON output EMIT produces -- the
contract this whole project exists to hand to a downstream toolkit-
generation agent.

A separate model family from AppPipelineState (D-002), not a subset view
of it: AppPipelineState is a working record that's legitimately partial
at every point before GATE finishes; HandoffArtifact is the thing that
gets handed to a consumer outside this codebase, so every field here is
required-or-explicitly-optional by deliberate schema design, not
"whatever happens to be set right now." `frozen=True` enforces that once
built, it's never mutated in place -- if a later stage's finding should
change it, that's a new artifact, not a patched one.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..enums import AuthScheme, CredentialType, ReasonCode, SourceTier, Status
from .state import CompletenessGap, EvidenceItem


class AppInfo(BaseModel):
    app_name: str
    identity_key: str  # canonical domain once resolved, e.g. "github.com"


class ApiInfo(BaseModel):
    docs_url: str | None = None
    base_url: str | None = None
    developer_portal_url: str | None = None
    auth_scheme: AuthScheme | None = None
    rate_limit_notes: str | None = None
    pagination_style_hint: str | None = None
    validation_endpoint: str | None = None
    scopes_available: list[str] = Field(default_factory=list)
    redirect_uris_required: bool = False
    # Which authority tier the docs page CLASSIFY actually read belongs
    # to (D-049) -- lets a consumer tell a HIGH-tier-derived auth_scheme
    # apart from a LOW-tier one, even at the same numeric confidence.
    source_tier: SourceTier | None = None
    # The *adjusted* confidence (source-tier weighting already applied,
    # D-049) -- was computed by CLASSIFY all along but never previously
    # exposed anywhere in the artifact itself, only in-process.
    classify_confidence: float | None = None


class CredentialInfo(BaseModel):
    """Account-level and API-level credentials are kept explicitly
    separate -- the deliverable is the credential, and a downstream
    consumer must never have to guess which of these fields is "the app
    logs into the vendor's console with this" vs. "the integration
    authenticates API requests with this." Only *_ref fields (vault refs)
    ever leave the vault module -- the raw secret never does; client_id is
    the one exception, kept plaintext because OAuth client IDs are meant
    to be public. See DECISIONS.md D-041.
    """

    # Account-level: the developer-portal login credforge created, if
    # this vendor's flow has one at all. Both None together means this
    # vendor's flow has no account/login concept whatsoever (e.g. NASA's
    # key-by-email flow) -- an explicit state, not an omission.
    account_email: str | None = None
    account_password_ref: str | None = None

    # API-level: what the integration actually authenticates requests
    # with. credential_type is always set, including NONE for a
    # genuinely open API with nothing to acquire -- exactly the field(s)
    # matching it are populated, the rest stay None.
    credential_type: CredentialType
    api_key_ref: str | None = None
    client_id: str | None = None
    client_secret_ref: str | None = None
    bearer_token_ref: str | None = None

    console_url: str | None = None


class ValidationInfo(BaseModel):
    status: str  # "valid" | "invalid" -- mirrors ValidateResult.status
    reason_code: ReasonCode | None = None
    http_status_code: int | None = None
    checked_url: str | None = None


class Provenance(BaseModel):
    run_id: str
    resolved_at: datetime
    emitted_at: datetime
    credforge_version: str


class HandoffArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Status
    reason_code: ReasonCode
    app: AppInfo
    # None when the pipeline never found enough to describe an API at all
    # (e.g. NO_PUBLIC_API) -- not every artifact has one.
    api: ApiInfo | None = None
    # None until an AUTO app has actually been provisioned -- a dry-run or
    # research-only artifact legitimately has no credential yet.
    credential: CredentialInfo | None = None
    validation: ValidationInfo | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    completeness_gaps: list[CompletenessGap] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def _check_status_reason_code_consistency(self) -> "HandoffArtifact":
        # The exact invariant D-001 named as the reason for choosing
        # pydantic in the first place -- enforced here, at construction, so
        # an inconsistent artifact can never exist rather than being
        # merely undesirable.
        if self.status == Status.AUTO and self.reason_code != ReasonCode.ELIGIBLE_AUTO:
            raise ValueError(f"status=AUTO requires reason_code=eligible_auto, got {self.reason_code!r}")
        if self.status == Status.UNSUPPORTED and self.reason_code != ReasonCode.NO_PUBLIC_API:
            raise ValueError(f"status=UNSUPPORTED requires reason_code=no_public_api, got {self.reason_code!r}")
        if self.status == Status.HITL and self.reason_code in (ReasonCode.ELIGIBLE_AUTO, ReasonCode.NO_PUBLIC_API):
            raise ValueError(f"status=HITL cannot carry reason_code={self.reason_code!r}")
        if self.status != Status.AUTO and self.credential is not None:
            raise ValueError("credential must be None unless status=AUTO -- PROVISION only ever runs after an AUTO gate")
        return self
