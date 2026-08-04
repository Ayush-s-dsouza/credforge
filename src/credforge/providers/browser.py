"""BrowserDriver: the interface PROVISION uses to drive signup + app creation.

MockBrowserDriver (Stage 5, default) simulates the flow without a real
browser. PlaywrightBrowserDriver (Stage 5, --live only) drives a real
browser. Both satisfy this same Protocol.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from ..enums import CredentialType
from ..pipeline.explain import NULL_EXPLAIN, ExplainSink
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

    # D-075: which recipe drove this attempt, mirrored straight off
    # `SignupRecipe.generated_by`/`generated_at` (None/None means
    # hand-authored -- every hand-written recipe in signup_recipes.py
    # leaves these unset). Set once a recipe is actually found, so even a
    # FAILED attempt records which recipe was used, not just successes.
    # Threaded through ProvisionResult and into the artifact's
    # CredentialInfo so a viewer can tell "DISCOVER_SIGNUP wrote this
    # recipe" apart from "a human verified this by hand" without reading
    # source code -- see DECISIONS.md D-075.
    recipe_generated_by: str | None = None
    recipe_generated_at: datetime | None = None

    # D-076: populated only on a failed attempt (success=False), so a
    # viewer of a live/streamed run can see what actually went wrong
    # without SSH/log access -- found necessary debugging a real deployed
    # failure (Alpha Vantage on Railway) that produced no exception, no
    # server-side detail, and a client-visible result of just
    # `credential: null`. `failure_screenshot_path` is a filename only
    # (not a full path) -- the caller decides where screenshots actually
    # live; None means no screenshot directory was configured (the default,
    # CLI-safe case) or the screenshot capture itself failed.
    failure_current_url: str | None = None
    failure_page_text_excerpt: str | None = None
    failure_screenshot_path: str | None = None


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
        explain: ExplainSink = NULL_EXPLAIN,
    ) -> ProvisionOutcome: ...
