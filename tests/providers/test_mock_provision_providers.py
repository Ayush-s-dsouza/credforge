import pytest

from credforge.enums import CredentialType
from credforge.providers.mock_browser import MockBrowserDriver
from credforge.providers.mock_email import MockEmailProvider


def test_mock_email_alias_is_deterministic_and_domain_scoped() -> None:
    provider = MockEmailProvider(alias_domain="creds.example")
    alias = provider.alias_for("github.com")
    assert alias == "credforge+github-com@creds.example"


@pytest.mark.asyncio
async def test_mock_email_wait_for_message_returns_immediately() -> None:
    provider = MockEmailProvider()
    message = await provider.wait_for_message(to_addr="credforge+x@example.com", timeout_s=0.01, poll_interval_s=0.01)
    assert message.to_addr == "credforge+x@example.com"
    assert message.body_text


@pytest.mark.asyncio
async def test_mock_browser_returns_a_successful_outcome_with_a_credential() -> None:
    driver = MockBrowserDriver()
    outcome = await driver.signup_and_create_app(
        developer_portal_url="https://developer.example.com",
        email_alias="credforge+example@example.com",
        email_provider=MockEmailProvider(),
        app_display_name="Example App",
        redirect_uris=["https://credforge.local/callback"],
        account_password="generated-pw-abc123",
        headed=False,
    )
    assert outcome.success is True
    assert outcome.credential_type == CredentialType.OAUTH2_TOKEN_PAIR
    assert "client_id" in outcome.raw_api_credential
    assert "client_secret" in outcome.raw_api_credential
    assert outcome.account_password_used == "generated-pw-abc123"
    assert outcome.console_url == "https://developer.example.com/apps/mock"
    assert all(step.success for step in outcome.steps)
