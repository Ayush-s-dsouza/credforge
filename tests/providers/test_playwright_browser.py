import pytest

from credforge.enums import CredentialType
from credforge.providers.mock_email import MockEmailProvider
from credforge.providers.playwright_browser import PlaywrightBrowserDriver, SignupRecipe

# A minimal real signup form, served as a data: URL -- no local server or
# file-path handling needed, and Playwright navigates and fills it exactly
# like a real vendor page. Deliberately has no element that could hold a
# rendered credential -- this is the fixture for the
# no-extraction-mechanism-configured test below.
_BARE_FORM_HTML = (
    "data:text/html,"
    "<form>"
    "<input id='email'>"
    "<button id='submit' type='button' onclick=\"document.title='submitted'\">Sign up</button>"
    "</form>"
)


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


@pytest.mark.asyncio
async def test_recipe_with_no_extraction_mechanism_fails_instead_of_reporting_empty_success() -> None:
    # D-053: a recipe that declares a real credential_type (not NONE) but
    # sets none of api_key_email_regex/client_id_selector/
    # client_secret_selector/api_key_page_selector -- the real shape of
    # ipinfo.io's registered recipe, where signup is known to be blocked by
    # a reCAPTCHA before any credential page is ever reached -- must fail
    # explicitly rather than silently falling through to success=True with
    # an empty raw_api_credential (which PROVISION would then vault as
    # nothing while still claiming "provisioned").
    recipe = SignupRecipe(
        email_field_selector="#email",
        submit_selector="#submit",
        credential_type=CredentialType.API_KEY,
        docs_url="https://developer.example.com/docs",
    )
    # registrable_domain() has no real host to extract from a data: URL, so
    # it falls back to returning the URL itself -- the recipe is keyed the
    # same way the driver will look it up.
    driver = PlaywrightBrowserDriver(recipes={_BARE_FORM_HTML: recipe})

    outcome = await driver.signup_and_create_app(
        developer_portal_url=_BARE_FORM_HTML,
        email_alias="credforge+example@example.com",
        email_provider=MockEmailProvider(),
        app_display_name="Example App",
        redirect_uris=[],
        account_password="generated-pw-abc123",
        headed=False,
    )

    assert outcome.success is False
    assert outcome.credential_type == CredentialType.NONE
    assert outcome.raw_api_credential == {}
    assert "no extraction mechanism" in outcome.failure_reason
    extract_step = next(s for s in outcome.steps if s.step == "extract_credential")
    assert extract_step.success is False
