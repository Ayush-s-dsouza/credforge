"""BrowserDriver: the interface PROVISION uses to drive signup + app creation.

MockBrowserDriver (Stage 5, default) simulates the flow without a real
browser. PlaywrightBrowserDriver (Stage 5, --live only) drives a real
browser. Both satisfy this same Protocol.
"""

from typing import Protocol

from pydantic import BaseModel

from ..enums import CredentialType
from .email import EmailProvider


class ProvisionStepResult(BaseModel):
    step: str
    success: bool
    detail: str | None = None


class ProvisionOutcome(BaseModel):
    success: bool
    credential_type: CredentialType
    # The API-level secret(s) the signup flow actually produced -- keys
    # are one or more of "api_key", "client_id", "client_secret",
    # "bearer_token", matching whichever credential_type applies.
    # client_id is not secret but travels the same dict for simplicity;
    # PROVISION is what splits it back out to plaintext. See D-041.
    raw_api_credential: dict[str, str] = {}
    # Echoed back only if the vendor's flow actually set a password on a
    # real form field -- None means this vendor's flow has no account
    # password at all (e.g. NASA's key-by-email flow has no login),
    # which is a different, equally real state from "we have one but
    # don't know it."
    account_password_used: str | None = None
    console_url: str | None = None
    steps: list[ProvisionStepResult] = []
    failure_reason: str | None = None


class BrowserDriver(Protocol):
    async def signup_and_create_app(
        self,
        *,
        developer_portal_url: str,
        email_alias: str,
        email_provider: EmailProvider,
        app_display_name: str,
        redirect_uris: list[str],
        account_password: str,
        headed: bool,
    ) -> ProvisionOutcome: ...
