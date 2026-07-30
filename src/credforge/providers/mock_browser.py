"""MockBrowserDriver: default (non---live) BrowserDriver.

Simulates a successful signup + app-creation flow deterministically -- no
real browser, no real vendor console. Exists so PROVISION's control flow
(idempotency guard, vault write, registry append, failure handling) can be
built and tested without Playwright or a live target, per the locked
stubbed-by-default scoping decision (see DECISIONS.md D-031).
"""

import uuid

from ..enums import CredentialType
from .browser import ProvisionOutcome, ProvisionStepResult
from .email import EmailProvider


class MockBrowserDriver:
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
    ) -> ProvisionOutcome:
        steps = [
            ProvisionStepResult(step="navigate_to_signup", success=True),
            ProvisionStepResult(step="fill_signup_form", success=True, detail=f"email={email_alias}"),
            ProvisionStepResult(step="verify_email", success=True),
            ProvisionStepResult(step="create_oauth_app", success=True, detail=f"name={app_display_name}"),
        ]
        return ProvisionOutcome(
            success=True,
            credential_type=CredentialType.OAUTH2_TOKEN_PAIR,
            raw_api_credential={
                "client_id": f"mock-client-{uuid.uuid4().hex[:8]}",
                "client_secret": f"mock-secret-{uuid.uuid4().hex[:16]}",
            },
            account_password_used=account_password,  # the mock signup "used" it, same as a real password-based flow would
            console_url=f"{developer_portal_url.rstrip('/')}/apps/mock",
            steps=steps,
        )
