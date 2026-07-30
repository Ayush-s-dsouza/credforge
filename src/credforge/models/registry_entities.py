"""RegistryEntry is the single row type appended to the registry log.

It does double duty: (a) per-stage bookkeeping for resumability (stage,
stage_status, settings_fingerprint), and (b) the spec's required
"account created" record (email_alias, console_url) for entries where
stage == PROVISION. One append-only log, one row type, rather than two
separate files that could drift out of sync.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from ..enums import CredentialType, PipelineStage, StageStatus


class RegistryEntry(BaseModel):
    identity_key: str
    app_name: str
    run_id: str
    stage: PipelineStage
    stage_status: StageStatus
    settings_fingerprint: str
    recorded_at: datetime

    email_alias: str | None = None
    console_url: str | None = None
    # Account-level (the developer-portal login credforge created, if the
    # vendor's flow has one at all) and API-level (what the integration
    # actually authenticates requests with) are separate vault refs, not
    # one generic `vault_ref` -- see DECISIONS.md D-041.
    account_password_vault_ref: str | None = None
    credential_type: CredentialType | None = None
    api_key_ref: str | None = None
    client_id: str | None = None  # not secret -- OAuth client IDs are meant to be public
    client_secret_ref: str | None = None
    bearer_token_ref: str | None = None
    closed: bool = False

    detail: dict = Field(default_factory=dict)
