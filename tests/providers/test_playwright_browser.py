import pytest

from credforge.enums import CredentialType
from credforge.providers.mock_email import MockEmailProvider
from credforge.providers.playwright_browser import (
    PageNavigatedUnexpectedlyError,
    PlaywrightBrowserDriver,
    SignupRecipe,
    evaluate_with_navigation_retry,
)

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

# D-074: a form whose "submit" click injects a credential immediately
# followed by real sentence punctuation into #result -- the exact shape a
# prose-anchored generated regex (`_build_anchored_regex`'s generic
# `[^\s<"']+` capture) matches against, found live on Alpha Vantage's own
# real post-submit page.
_FORM_WITH_PUNCTUATED_CREDENTIAL_HTML = (
    "data:text/html,"
    "<form>"
    "<input id='email'>"
    "<button id='submit' type='button' "
    "onclick=\"document.getElementById('result').innerText="
    "'Here is your API key: XJ7QP2KD9M. Keep it safe.'\">Sign up</button>"
    "</form>"
    "<div id='result'></div>"
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


@pytest.mark.asyncio
async def test_failure_captures_current_url_page_excerpt_and_screenshot(tmp_path) -> None:
    # D-076: found necessary debugging a real deployed failure (Alpha
    # Vantage on Railway) that produced no exception and no server-side
    # detail -- credential=null with nothing to diagnose it by. A failed
    # attempt now records what the page actually looked like.
    recipe = SignupRecipe(
        email_field_selector="#email",
        submit_selector="#submit",
        credential_type=CredentialType.API_KEY,
        docs_url="https://developer.example.com/docs",
    )
    driver = PlaywrightBrowserDriver(recipes={_BARE_FORM_HTML: recipe}, screenshot_dir=tmp_path)

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
    assert outcome.failure_current_url is not None
    assert outcome.failure_page_text_excerpt is not None
    assert outcome.failure_screenshot_path is not None
    screenshot_path = tmp_path / outcome.failure_screenshot_path
    assert screenshot_path.exists()
    assert screenshot_path.stat().st_size > 0


@pytest.mark.asyncio
async def test_no_screenshot_dir_configured_means_no_screenshot_attempted() -> None:
    recipe = SignupRecipe(
        email_field_selector="#email",
        submit_selector="#submit",
        credential_type=CredentialType.API_KEY,
        docs_url="https://developer.example.com/docs",
    )
    driver = PlaywrightBrowserDriver(recipes={_BARE_FORM_HTML: recipe})  # no screenshot_dir

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
    assert outcome.failure_screenshot_path is None
    # Failure URL/excerpt are still captured -- only the screenshot is gated.
    assert outcome.failure_current_url is not None


@pytest.mark.asyncio
async def test_page_based_extraction_strips_trailing_sentence_punctuation() -> None:
    # D-074: found live replaying a DISCOVER_SIGNUP-generated Alpha Vantage
    # recipe -- the real post-submit page read "...API key: XJ7QP2KD9M.
    # Keep it safe.", and the vaulted credential came back as
    # "XJ7QP2KD9M." (trailing period included). discover_signup.py's own
    # `_extract_anchored_value` already strips this at GENERATION time
    # (D-061), but nothing stripped it again here at REPLAY time -- the
    # exact same generic `[^\s<"']+` capture group swallows trailing
    # punctuation on both sides, so both extraction points need the fix.
    recipe = SignupRecipe(
        email_field_selector="#email",
        submit_selector="#submit",
        credential_type=CredentialType.API_KEY,
        docs_url="https://developer.example.com/docs",
        api_key_page_selector="#result",
        api_key_page_regex=r"Here is your API key:\s*([^\s<\"']+)",
    )
    driver = PlaywrightBrowserDriver(recipes={_FORM_WITH_PUNCTUATED_CREDENTIAL_HTML: recipe})

    outcome = await driver.signup_and_create_app(
        developer_portal_url=_FORM_WITH_PUNCTUATED_CREDENTIAL_HTML,
        email_alias="credforge+example@example.com",
        email_provider=MockEmailProvider(),
        app_display_name="Example App",
        redirect_uris=[],
        account_password="generated-pw-abc123",
        headed=False,
    )

    assert outcome.success is True
    assert outcome.raw_api_credential["api_key"] == "XJ7QP2KD9M"
    extract_step = next(s for s in outcome.steps if s.step == "extract_credential")
    assert extract_step.success is True


# --- evaluate_with_navigation_retry (D-073) -------------------------------
#
# A fake page, not a real Playwright one: reproducing a genuine "execution
# context destroyed" race deterministically would mean actually timing a
# real navigation against a real evaluate() call -- inherently flaky. The
# retry/error-classification logic itself is pure control flow and is
# exactly what's under test here, same reasoning _locate_signup_page's
# tests use a fake FetchProvider instead of real network calls.


class _FakePage:
    def __init__(self, evaluate_results: list) -> None:
        self._evaluate_results = list(evaluate_results)
        self.evaluate_calls = 0
        self.settled = False

    async def evaluate(self, script: str):
        self.evaluate_calls += 1
        result = self._evaluate_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        self.settled = True


_NAV_ERROR = Exception("Execution context was destroyed, most likely because of a navigation")


@pytest.mark.asyncio
async def test_evaluate_with_navigation_retry_returns_result_when_no_error() -> None:
    page = _FakePage([True])
    assert await evaluate_with_navigation_retry(page, "script") is True
    assert page.evaluate_calls == 1


@pytest.mark.asyncio
async def test_evaluate_with_navigation_retry_retries_once_after_navigation_error() -> None:
    # Found live against SendGrid: a client-side redirect fired mid-poll,
    # crashing the next scheduled DOM check. One retry, after letting the
    # new page settle, is enough to recover.
    page = _FakePage([_NAV_ERROR, True])
    assert await evaluate_with_navigation_retry(page, "script") is True
    assert page.evaluate_calls == 2
    assert page.settled is True


@pytest.mark.asyncio
async def test_evaluate_with_navigation_retry_raises_distinct_error_when_retry_also_fails() -> None:
    page = _FakePage([_NAV_ERROR, _NAV_ERROR])
    with pytest.raises(PageNavigatedUnexpectedlyError):
        await evaluate_with_navigation_retry(page, "script")


@pytest.mark.asyncio
async def test_evaluate_with_navigation_retry_propagates_unrelated_errors_without_retrying() -> None:
    page = _FakePage([Exception("some unrelated real error")])
    with pytest.raises(Exception, match="some unrelated real error"):
        await evaluate_with_navigation_retry(page, "script")
    assert page.evaluate_calls == 1
