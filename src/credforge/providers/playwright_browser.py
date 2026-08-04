"""PlaywrightBrowserDriver: real browser automation (BrowserDriver, --live only).

Generic signup-form automation across arbitrary vendor sites is not a
solved problem -- every vendor's form has different field names, flows,
and anti-bot measures. This driver does the genuinely generic part
(launch a real browser, navigate to the developer portal, wait for email
verification via the supplied EmailProvider, scrape the resulting
credential) and requires a per-vendor `SignupRecipe` -- a small, explicit
set of CSS selectors -- for the vendor-specific fill-and-submit steps. No
recipe registered for a vendor's domain -> a clear `PROVISION_FAILED`,
never a best-effort guess at selectors that might silently submit garbage
into the wrong field. See DECISIONS.md D-031.

Two credential-delivery shapes exist across real vendors, and a recipe
declares which one it uses, not the driver: some vendors show the
credential on a page after signup (`client_id_selector`/
`client_secret_selector`/`api_key_page_selector`); others (NASA) email it
directly with no page to scrape at all (`api_key_email_regex`). See
DECISIONS.md D-042.

`playwright` is an optional extra (`credforge[live]`) -- imported lazily,
inside the method, so a default install (and every stage before this one)
never needs the package or its browser binaries. See OPS.md's Playwright
section for what installing it actually requires on the host.
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from ..enums import CredentialType
from ..utils.domains import registrable_domain
from .browser import ProvisionOutcome, ProvisionStepResult
from .email import EmailProvider

# The identity credforge signs up with when a vendor's form asks for a
# person's name -- not a real person, and deliberately not disguised as
# one (no invented surname pretending to be an employee).
SIGNUP_FIRST_NAME = "Credforge"
SIGNUP_LAST_NAME = "Agent"
SIGNUP_FULL_NAME = f"{SIGNUP_FIRST_NAME} {SIGNUP_LAST_NAME}"
SIGNUP_COMPANY = "Credforge"

# D-065 (DISCOVER_SIGNUP): synthetic defaults for form fields a generated
# recipe classified as required but that don't have a dedicated named
# selector on SignupRecipe -- same "sensible default, honestly synthetic,
# never disguised as a real person" principle as the two constants above.
# Only reached for a field DISCOVER_SIGNUP's LLM classification marked
# `required`; an optional field of one of these purposes is simply left
# blank. See DECISIONS.md D-065.
EXTRA_FIELD_DEFAULTS: dict[str, str] = {
    "phone": "+15555550100",
    "country": "United States",
    "use_case": "API integration testing",
    "other": "N/A",
    "unknown": "N/A",
}


def _generate_username() -> str:
    # Some vendors (OpenWeatherMap) require a globally-unique username
    # separate from email -- random suffix avoids a collision on retry.
    return f"credforge-agent-{uuid.uuid4().hex[:10]}"


# D-065 (DISCOVER_SIGNUP): a cookie-consent overlay is not vendor-specific --
# a generic, GDPR/CCPA-driven pattern that can sit on top of literally any
# vendor's page, found live: api-ninjas.com's real signup form filled
# correctly, but every submit-click attempt failed because `#cc-banner`
# intercepted the pointer event. This is exactly "the genuinely generic
# part" this driver already claims to handle (see this module's own
# docstring) -- not a per-vendor workaround, so it lives here, used by both
# this driver's hand-authored-recipe replay path AND
# pipeline/discover_signup.py's generation-time interaction. Best-effort:
# if no matching button is found, or clicking it fails for any reason,
# this is a silent no-op -- most vendors have no such banner at all.
COOKIE_CONSENT_BUTTON_TEXTS = (
    "Accept All", "Accept all", "Accept", "I Agree", "I agree", "Agree",
    "Got it", "Allow all", "Allow All", "OK", "Ok",
)


async def dismiss_cookie_banner(page) -> None:
    for text in COOKIE_CONSENT_BUTTON_TEXTS:
        try:
            locator = page.get_by_role("button", name=text, exact=False)
            if await locator.count() > 0:
                await locator.first.click(timeout=2000)
                return
        except Exception:
            continue


# D-069 (DISCOVER_SIGNUP): the same class of problem as D-066's JS-shell
# fetch fallback, one stage further down the pipeline -- content arriving
# asynchronously. Found live against NASA: api.nasa.gov's real signup
# widget is injected by a third-party embed script
# (api.data.gov's signup_embed.js, per NASA_API's own recipe comment
# below) that hasn't populated the DOM yet at the moment a fixed-delay
# snapshot is taken -- DOM reduction then only sees page chrome (nav
# buttons, search boxes, API-category filters), and a classifier correctly,
# appropriately reports near-zero confidence on a form that isn't there
# yet, rather than the page never having a real form at all. The
# readiness check is deliberately GENERIC -- a real, visible, non-search
# text/email/password input having appeared anywhere in the DOM -- never
# a vendor-specific selector, which would be exactly the hardcoding this
# generator exists to avoid. Mirrors the SAME polling pattern the
# api_key_page_selector credential-extraction loop already uses further
# down this same file, applied one step earlier in the flow.
FORM_READY_TIMEOUT_MS = 15_000
FORM_READY_POLL_INTERVAL_MS = 500

_FORM_READY_CHECK_JS = """
() => {
  const candidates = document.querySelectorAll('input');
  for (const el of candidates) {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (!['text', 'email', 'password'].includes(type)) continue;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role === 'search') continue;
    const name = el.getAttribute('name') || '';
    const id = el.getAttribute('id') || '';
    const placeholder = el.getAttribute('placeholder') || '';
    const haystack = (name + ' ' + id + ' ' + placeholder).toLowerCase();
    if (/search|query|filter/.test(haystack)) continue;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (rect.width === 0 || rect.height === 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    return true;
  }
  return false;
}
"""


async def wait_for_signup_form_ready(
    page, *, timeout_ms: int = FORM_READY_TIMEOUT_MS, poll_interval_ms: int = FORM_READY_POLL_INTERVAL_MS
) -> tuple[bool, bool]:
    """Polls until at least one visible, non-search text/email/password
    input appears anywhere in the DOM. Returns (found, required_waiting):
    `found` is False only on a genuine timeout -- a real, honest signal to
    the caller, never silently treated as "the form must just be empty
    with no fields." `required_waiting` is True whenever the field wasn't
    already present on the very first check -- callers use this to decide
    whether a generated recipe needs to repeat this same wait on replay."""
    if await page.evaluate(_FORM_READY_CHECK_JS):
        return True, False
    elapsed_ms = 0
    while elapsed_ms < timeout_ms:
        await page.wait_for_timeout(poll_interval_ms)
        elapsed_ms += poll_interval_ms
        if await page.evaluate(_FORM_READY_CHECK_JS):
            return True, True
    return False, True


class SignupRecipe(BaseModel):
    email_field_selector: str
    submit_selector: str
    credential_type: CredentialType

    # The vendor's real, known developer-documentation URL -- not the
    # signup form URL (those can differ; OpenWeatherMap's real docs live
    # at openweathermap.org/api, its signup form at
    # home.openweathermap.org/users/sign_up). A recipe only exists because
    # someone already read this vendor's real page, so RESOLVE can pin
    # straight to it instead of re-deriving it via search. See
    # DECISIONS.md D-048.
    docs_url: str

    # Person-identity fields some signup forms require (NASA does; many
    # OAuth app-registration forms don't ask at all).
    first_name_field_selector: str | None = None
    last_name_field_selector: str | None = None
    username_field_selector: str | None = None

    # None means this vendor's flow has no account password at all -- a
    # password field is only filled if this selector is set.
    password_field_selector: str | None = None
    password_confirm_field_selector: str | None = None

    # Consent/terms checkboxes that must be checked before submit is even
    # clickable -- distinct from the marketing-opt-in checkboxes a real
    # form usually also has, which are deliberately never checked (opting
    # a generated account into marketing email it can't meaningfully
    # consent to receive is not this driver's call to make).
    checkbox_selectors: list[str] = Field(default_factory=list)

    app_name_field_selector: str | None = None
    redirect_uri_field_selector: str | None = None

    # Credential extraction, page-based -- read after signup completes.
    # Whether this vendor's page-based flow needs an email-verification
    # step before the credential becomes visible (an OAuth-console-style
    # dashboard) or not (an immediate same-page AJAX response, e.g. Alpha
    # Vantage's "Your dedicated access key is: X" -- no email step exists
    # at all). Defaults False: the old, previously-untested assumption
    # that every page-based flow needs an email wait first was never
    # actually correct for every vendor shape, just the only one tried.
    requires_email_verification: bool = False
    client_id_selector: str | None = None
    client_secret_selector: str | None = None
    api_key_page_selector: str | None = None
    # Optional: the page-based selector's raw text_content often includes
    # surrounding prose ("Your dedicated access key is: X. Please record
    # ..."), not a clean value -- a regex here extracts just the key,
    # mirroring api_key_email_regex's same need on the email-based path.
    # None means use the selector's raw text_content as-is.
    api_key_page_regex: str | None = None

    # Credential extraction, email-based -- some vendors (NASA) email the
    # credential directly with no page to scrape at all. Exactly one
    # capture group.
    api_key_email_regex: str | None = None
    email_subject_contains: str | None = None

    # D-063: a known-good VALIDATE endpoint for this vendor, used ONLY as
    # a fallback when DISCOVER's own extraction doesn't find one --
    # exactly the same rule as identity pinning (D-048): the recipe
    # supplies a fallback, it never overrides what DISCOVER actually
    # found, and GATE's own verdict is completely untouched (this is
    # consumed by the orchestrator's post-GATE VALIDATE call, nowhere
    # near GATE's decision). A full absolute URL (e.g. "GET
    # https://example.com/query?...") is safest -- it doesn't depend on
    # DISCOVER also having found a correct base_url that same run.
    validation_endpoint_fallback: str | None = None

    # D-064: the real signup/console URL PROVISION's browser should
    # navigate to, used ONLY as a fallback when DISCOVER's own extraction
    # doesn't populate developer_portal_url that run -- same fallback-only
    # rule as validation_endpoint_fallback above. `docs_url` above is
    # deliberately NOT reused as this fallback: this class's own docstring
    # already notes docs_url and the signup form URL can be different
    # pages for the same vendor (confirmed live for alphavantage.co: its
    # docs live at /documentation/, its real signup form at
    # /support/#api-key -- a run where extraction didn't find the latter
    # sent the browser to the former, where none of this recipe's
    # selectors exist, and PROVISION failed).
    developer_portal_url_fallback: str | None = None

    # D-065 (DISCOVER_SIGNUP): fields a *generated* recipe needs that no
    # hand-authored recipe so far has required. `full_name_field_selector`
    # covers a single "Full Name" input (as opposed to split
    # first/last_name_field_selector above); `company_field_selector`
    # covers "organization"/"company" as its own concept, distinct from
    # `app_name_field_selector` (the OAuth *app's* display name -- Alpha
    # Vantage's recipe repurposes app_name_field_selector for its
    # "organization" field for exactly this reason, a workaround this
    # field makes unnecessary for anything generated from here on).
    # `extra_field_selectors` holds every other classified-but-uncommon
    # purpose (phone/country/use_case/other/unknown) that was `required`
    # on the source form, keyed by that purpose string -- filled with a
    # clearly-synthetic default (EXTRA_FIELD_DEFAULTS) rather than either
    # inventing a named field per rare purpose or leaving a required field
    # empty and letting submission fail.
    full_name_field_selector: str | None = None
    company_field_selector: str | None = None
    extra_field_selectors: dict[str, str] = Field(default_factory=dict)

    # D-069: True when this vendor's signup form is injected asynchronously
    # (a third-party embed script, a client-rendered widget) and wasn't yet
    # present in the DOM on first load -- found live, NASA's real signup
    # form only appears after api.data.gov's signup_embed.js finishes
    # running. When set, replay waits for wait_for_signup_form_ready()
    # before attempting to fill any field, same as generation did.
    requires_async_form_render: bool = False

    # Provenance: unset for every hand-authored recipe in this file (all
    # four were read and written by a human directly). Set only by
    # DISCOVER_SIGNUP, so a recipe's origin is always visible just by
    # reading it -- never silently indistinguishable from a hand-verified
    # one. See DECISIONS.md D-065.
    generated_by: str | None = None
    generated_at: datetime | None = None


class PlaywrightBrowserDriver:
    def __init__(self, *, recipes: dict[str, SignupRecipe] | None = None) -> None:
        self._recipes = recipes or {}

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
        domain = registrable_domain(developer_portal_url)
        recipe = self._recipes.get(domain)
        if recipe is None:
            return ProvisionOutcome(
                success=False,
                credential_type=CredentialType.NONE,
                steps=[
                    ProvisionStepResult(
                        step="lookup_recipe", success=False, detail=f"no SignupRecipe registered for {domain!r}"
                    )
                ],
                failure_reason=f"no per-vendor signup recipe registered for {domain!r}",
            )

        from playwright.async_api import async_playwright

        steps: list[ProvisionStepResult] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not headed)
            try:
                page = await browser.new_page()
                await page.goto(developer_portal_url)
                await dismiss_cookie_banner(page)
                steps.append(ProvisionStepResult(step="navigate_to_signup", success=True))

                if recipe.requires_async_form_render:
                    # D-069: this recipe's own generation run needed to
                    # wait for the same asynchronously-injected form --
                    # replay must too, or it fills a page that hasn't
                    # rendered its real fields yet.
                    form_ready, _ = await wait_for_signup_form_ready(page)
                    if not form_ready:
                        steps.append(
                            ProvisionStepResult(
                                step="wait_for_signup_form_ready",
                                success=False,
                                detail=f"no visible form field appeared within {FORM_READY_TIMEOUT_MS / 1000:.0f}s",
                            )
                        )
                        return ProvisionOutcome(
                            success=False,
                            credential_type=CredentialType.NONE,
                            steps=steps,
                            failure_reason=(
                                "SIGNUP_FORM_NOT_RENDERED: recipe expects an asynchronously-rendered "
                                "form, but no visible field appeared within the wait window"
                            ),
                        )
                    steps.append(ProvisionStepResult(step="wait_for_signup_form_ready", success=True))

                await page.fill(recipe.email_field_selector, email_alias)
                if recipe.first_name_field_selector:
                    await page.fill(recipe.first_name_field_selector, SIGNUP_FIRST_NAME)
                if recipe.last_name_field_selector:
                    await page.fill(recipe.last_name_field_selector, SIGNUP_LAST_NAME)
                if recipe.full_name_field_selector:
                    await page.fill(recipe.full_name_field_selector, SIGNUP_FULL_NAME)
                if recipe.username_field_selector:
                    await page.fill(recipe.username_field_selector, _generate_username())
                if recipe.company_field_selector:
                    await page.fill(recipe.company_field_selector, SIGNUP_COMPANY)
                for purpose, selector in recipe.extra_field_selectors.items():
                    await page.fill(selector, EXTRA_FIELD_DEFAULTS.get(purpose, "N/A"))

                account_password_used: str | None = None
                if recipe.password_field_selector:
                    await page.fill(recipe.password_field_selector, account_password)
                    account_password_used = account_password
                    if recipe.password_confirm_field_selector:
                        await page.fill(recipe.password_confirm_field_selector, account_password)

                if recipe.app_name_field_selector:
                    await page.fill(recipe.app_name_field_selector, app_display_name)
                if recipe.redirect_uri_field_selector and redirect_uris:
                    await page.fill(recipe.redirect_uri_field_selector, redirect_uris[0])

                for checkbox_selector in recipe.checkbox_selectors:
                    await page.check(checkbox_selector)

                await page.click(recipe.submit_selector)
                steps.append(ProvisionStepResult(step="fill_and_submit_signup_form", success=True))

                raw_api_credential: dict[str, str] = {}

                if recipe.credential_type != CredentialType.NONE and not (
                    recipe.api_key_email_regex
                    or recipe.client_id_selector
                    or recipe.client_secret_selector
                    or recipe.api_key_page_selector
                ):
                    # A recipe with a real credential_type but no extraction
                    # mechanism at all can never capture a credential --
                    # usually because signup itself is known to be blocked
                    # (e.g. a CAPTCHA wall) before any credential page is
                    # ever reached, so no selector was ever observable to
                    # record. Without this check, control falls through to
                    # the success return below with an empty
                    # raw_api_credential -- a false "provisioned" that
                    # vaults nothing. See DECISIONS.md D-053.
                    steps.append(
                        ProvisionStepResult(
                            step="extract_credential",
                            success=False,
                            detail="recipe has no configured extraction mechanism for this credential_type",
                        )
                    )
                    return ProvisionOutcome(
                        success=False,
                        credential_type=CredentialType.NONE,
                        steps=steps,
                        failure_reason=(
                            f"no extraction mechanism configured for {domain!r} -- "
                            "signup form was submitted but this recipe cannot capture "
                            "a credential (see the recipe's registration comment for why)"
                        ),
                    )

                if recipe.api_key_email_regex:
                    message = await email_provider.wait_for_message(
                        to_addr=email_alias, subject_contains=recipe.email_subject_contains
                    )
                    steps.append(
                        ProvisionStepResult(
                            step="receive_credential_email", success=True, detail=f"message_id={message.message_id}"
                        )
                    )
                    match = re.search(recipe.api_key_email_regex, message.body_text)
                    if match is None:
                        steps.append(
                            ProvisionStepResult(
                                step="extract_credential_from_email",
                                success=False,
                                detail="regex did not match the email body",
                            )
                        )
                        return ProvisionOutcome(
                            success=False,
                            credential_type=CredentialType.NONE,
                            steps=steps,
                            failure_reason="signup email arrived but the credential regex found no match",
                        )
                    raw_api_credential["api_key"] = match.group(1)
                    steps.append(ProvisionStepResult(step="extract_credential_from_email", success=True))
                elif recipe.client_id_selector or recipe.client_secret_selector or recipe.api_key_page_selector:
                    if recipe.requires_email_verification:
                        message = await email_provider.wait_for_message(
                            to_addr=email_alias, subject_contains=recipe.email_subject_contains or "verify"
                        )
                        steps.append(
                            ProvisionStepResult(
                                step="verify_email", success=True, detail=f"message_id={message.message_id}"
                            )
                        )
                        # A real recipe would also extract and visit the verification
                        # link from message.body_text here. Left as the natural next
                        # step once a real recipe exists to test it against -- see
                        # DECISIONS.md D-031's "Revisit if" note.
                    elif recipe.api_key_page_selector:
                        # No email step -- the credential renders into the page via
                        # AJAX shortly after submit, not synchronously with the
                        # click. Poll for the actual pattern, not just "selector
                        # has any text" -- a broad selector (e.g. the whole page
                        # body, needed when the AJAX response's exact container
                        # isn't reliably identifiable in advance) already has
                        # plenty of static text before the credential ever loads,
                        # so a bare non-empty check would exit immediately.
                        for _ in range(20):
                            probe_text = await page.text_content(recipe.api_key_page_selector) or ""
                            if recipe.api_key_page_regex:
                                if re.search(recipe.api_key_page_regex, probe_text):
                                    break
                            elif probe_text.strip():
                                break
                            await page.wait_for_timeout(500)

                    if recipe.client_id_selector:
                        raw_api_credential["client_id"] = await page.text_content(recipe.client_id_selector) or ""
                    if recipe.client_secret_selector:
                        raw_api_credential["client_secret"] = await page.text_content(recipe.client_secret_selector) or ""
                    if recipe.api_key_page_selector:
                        page_text = await page.text_content(recipe.api_key_page_selector) or ""
                        if recipe.api_key_page_regex:
                            page_match = re.search(recipe.api_key_page_regex, page_text)
                            if page_match is None:
                                steps.append(
                                    ProvisionStepResult(
                                        step="extract_credential",
                                        success=False,
                                        detail="api_key_page_regex did not match the page content",
                                    )
                                )
                                return ProvisionOutcome(
                                    success=False,
                                    credential_type=CredentialType.NONE,
                                    steps=steps,
                                    failure_reason="signup page loaded but the credential regex found no match",
                                )
                            raw_api_credential["api_key"] = page_match.group(1)
                        else:
                            raw_api_credential["api_key"] = page_text
                    steps.append(ProvisionStepResult(step="extract_credential", success=True))

                return ProvisionOutcome(
                    success=True,
                    credential_type=recipe.credential_type,
                    raw_api_credential=raw_api_credential,
                    account_password_used=account_password_used,
                    console_url=page.url,
                    steps=steps,
                )
            except Exception as exc:
                steps.append(ProvisionStepResult(step="browser_automation", success=False, detail=str(exc)))
                return ProvisionOutcome(
                    success=False, credential_type=CredentialType.NONE, steps=steps, failure_reason=str(exc)
                )
            finally:
                await browser.close()
