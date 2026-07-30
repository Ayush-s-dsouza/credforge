import pytest

from credforge.enums import CredentialType
from credforge.providers.mock_email import MockEmailProvider
from credforge.providers.playwright_browser import PlaywrightBrowserDriver


@pytest.mark.asyncio
async def test_no_recipe_registered_fails_clearly_without_attempting_generic_automation() -> None:
    # The core design decision under test: no per-vendor SignupRecipe means
    # a clear, typed failure -- never a best-effort guess at selectors.
    # This path must not even try to import playwright, so it works without
    # the optional [live] extra installed.
    driver = PlaywrightBrowserDriver()  # no recipes registered

    outcome = await driver.signup_and_create_app(
        developer_portal_url="https://developer.example.com",
        email_alias="credforge+example@example.com",
        email_provider=MockEmailProvider(),
        app_display_name="Example App",
        redirect_uris=[],
        account_password="generated-pw-abc123",
        headed=False,
    )

    assert outcome.success is False
    assert outcome.credential_type == CredentialType.NONE
    assert "example.com" in outcome.failure_reason
    assert outcome.steps[0].step == "lookup_recipe"
    assert outcome.steps[0].success is False
