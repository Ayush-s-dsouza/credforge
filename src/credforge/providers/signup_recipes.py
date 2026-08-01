"""Known, real per-vendor SignupRecipes for PlaywrightBrowserDriver's
`--live` path. Each recipe's selectors were read directly off that
vendor's real, rendered signup page -- never guessed from convention.
See DECISIONS.md D-042.
"""

from ..enums import CredentialType
from .playwright_browser import SignupRecipe

# api.nasa.gov -- a form (first name, last name, email) -> the API key is
# emailed directly. No login, no password, no page to scrape a credential
# from at all.
#
# Selectors read from the real, rendered DOM, not the static page source:
# the signup form is injected by a third-party embed script
# (api.data.gov's signup_embed.js) with no form markup in the raw HTML at
# all -- confirmed by fetching the page directly and finding only a
# "Loading signup form..." placeholder. Rendering with Playwright and
# dumping the live DOM found the real form:
#   <form id="api_umbrella_signup_form">
#     <input id="user_first_name" name="user[first_name]" type="text">
#     <input id="user_last_name"  name="user[last_name]"  type="text">
#     <input id="user_email"      name="user[email]"      type="email">
#     <button type="submit" class="btn btn-lg btn-primary">
#   </form>
NASA_API = SignupRecipe(
    email_field_selector="#user_email",
    first_name_field_selector="#user_first_name",
    last_name_field_selector="#user_last_name",
    submit_selector="#api_umbrella_signup_form button[type='submit']",
    credential_type=CredentialType.API_KEY,
    # The signup form and the real API docs live on the same page --
    # confirmed by having actually navigated here for the live run.
    docs_url="https://api.nasa.gov/",
    # api.nasa.gov runs on api_umbrella (the same open-source platform
    # behind api.data.gov generally); the embed widget's own example key
    # visible in the page config (`apiKey: 'jfr9uihqvncOuii7lda5bDlsvOIDePcKTLWlzLte'`)
    # is 40 alphanumeric characters -- that's what real issued keys look
    # like on this platform. Confirmed live: matched the real delivered
    # key on the first attempt, no correction needed.
    api_key_email_regex=r"\b([A-Za-z0-9]{40})\b",
    # The page's own signup config states the welcome email's subject
    # template as "Your {{siteName}} API key" (siteName='https://api.nasa.gov')
    # -- "API key" is the stable substring to match on regardless of the
    # exact siteName rendering.
    email_subject_contains="API key",
)

# home.openweathermap.org/users/sign_up -- username + email + password +
# password confirmation, two required consent checkboxes (age
# confirmation, terms acceptance), THEN a visible Google reCAPTCHA v2
# widget (a real `div.g-recaptcha[data-sitekey]` + the classic
# `#g-recaptcha-response` hidden textarea reCAPTCHA's own JS populates
# after a human solves the challenge). Real selectors from the real
# rendered DOM (form id="new_user"):
#   <input id="user_username"              name="user[username]">
#   <input id="user_email"                 name="user[email]" type="email">
#   <input id="user_password"              name="user[password]" type="password">
#   <input id="user_password_confirmation" name="user[password_confirmation]" type="password">
#   <input id="agreement_is_age_confirmed" type="checkbox">
#   <input id="agreement_is_accepted"      type="checkbox">
#   <input name="commit" type="submit" class="btn-submit">
#
# Built and field-complete, but NOT provisionable as-is: submitting
# leaves `g-recaptcha-response` empty, which the server rejects. Every
# field this recipe fills is confirmed correct -- the CAPTCHA is the only
# blocker, isolated by filling everything else correctly first and
# observing that submission still fails. See DECISIONS.md D-046 and
# OPS.md's "what makes a vendor recipe-able" section for the full
# analysis this finding is part of. Registered here (not omitted) so the
# failure is `PROVISION_FAILED` with a real, specific reason the next
# time someone points `--live` at openweathermap.org, not a silent
# "no recipe" that looks like the vendor was never even investigated.
OPENWEATHERMAP = SignupRecipe(
    email_field_selector="#user_email",
    username_field_selector="#user_username",
    password_field_selector="#user_password",
    password_confirm_field_selector="#user_password_confirmation",
    checkbox_selectors=["#agreement_is_age_confirmed", "#agreement_is_accepted"],
    submit_selector="input[name='commit']",
    credential_type=CredentialType.API_KEY,
    # The real API reference -- distinct from the signup form above, and
    # from the narrower _DOCS_PATH_GUESSES ("/docs", "/developers") in
    # resolve.py, which don't include "/api" and so never find this on
    # their own (a real, separate gap noted in OPS.md). Reverified live
    # (200, current) before being hardcoded here.
    docs_url="https://openweathermap.org/api",
    # Never reached live -- signup itself is blocked by the reCAPTCHA
    # before any email is ever sent. Left unset rather than guessed.
    api_key_email_regex=None,
)

# alphavantage.co/support/#api-key -- organization (text) + occupation
# (select, left at its default "Investor" -- not marked required in the
# real DOM) + email. No password, no account, no CAPTCHA (confirmed by
# real DOM/iframe/script inspection). The key appears on the *same page*
# via AJAX a few hundred ms after submit, in prose ("Welcome to Alpha
# Vantage! Your dedicated access key is: <16 uppercase alphanumeric
# chars>. Please record..."), not in an isolable container found by
# inspection -- hence
# `api_key_page_selector="body"` + a regex, not a narrow selector.
# `requires_email_verification=False` because this genuinely has no email
# step at all -- confirmed live: the AJAX response arrives whether or not
# the address is real, deliverable, or ever checked.
#
# Real, rendered DOM (id="post-form"):
#   <select name="occupation" id="occupation-text">...</select>
#   <input type="text" name="organization" id="organization-text" required>
#   <input type="text" name="email" id="email-text" required maxlength="254">
#   <input id="submit-btn" type="submit" value="Get Free API Key">
ALPHA_VANTAGE = SignupRecipe(
    email_field_selector="#email-text",
    app_name_field_selector="#organization-text",  # repurposed: no dedicated "organization" field exists on SignupRecipe
    submit_selector="#submit-btn",
    credential_type=CredentialType.API_KEY,
    docs_url="https://www.alphavantage.co/documentation/",
    requires_email_verification=False,
    api_key_page_selector="body",
    api_key_page_regex=r"Your dedicated access key is:\s*([A-Z0-9]{16})",
)

# ipinfo.io/signup -- first name, last name, email, password (a real
# account_email + account_password archetype, distinct from every other
# recipe here). Fields read from the real rendered DOM; the framework
# (Headless UI/Next.js) generates unstable per-load `id`s
# (`headlessui-control-_R_hmmm56_`), so selectors use the stable `name`
# attribute instead:
#   <input name="firstname">  <input name="lastname">
#   <input name="email">      <input name="password" type="password">
#   <button type="submit">Get started now</button>
#
# NOT provisionable live: a Google reCAPTCHA v2 *invisible* widget
# (`class="grecaptcha-badge"`, `size=invisible`, sitekey
# 6LftmFkUAAAAADydGEH99T-xmZoK69ErtRCzfVFf`) sits on the page and
# auto-executes on submit -- confirmed live, this is not a bare "CAPTCHA
# present" guess. A real submission (real name/email/generated password,
# ordinary headless Chromium, no mouse jitter) did not pass silently on
# behavioral score alone: it escalated to a full interactive image
# challenge ("Select all images with cars"), screenshotted mid-run. That
# answers the specific question this vendor was chosen to test -- whether
# the *invisible* reCAPTCHA variant (as opposed to OpenWeatherMap's
# visible checkbox, D-046) is actually weaker against a well-behaved
# script. It isn't: invisible just moves the challenge from "always shown"
# to "shown when the risk score says so," and a scripted Playwright
# session with no real user signal reliably trips that score. Registered
# here (not omitted) for the same reason as OPENWEATHERMAP: the next
# `--live` attempt against ipinfo.io gets a real, specific
# `PROVISION_FAILED` instead of a silent "no recipe."
IPINFO = SignupRecipe(
    email_field_selector="input[name='email']",
    first_name_field_selector="input[name='firstname']",
    last_name_field_selector="input[name='lastname']",
    password_field_selector="input[name='password']",
    submit_selector="button[type='submit']",
    credential_type=CredentialType.API_KEY,
    docs_url="https://ipinfo.io/developers",
    # Never reached live -- the reCAPTCHA challenge blocks before any
    # dashboard/token page loads, so where the token would actually appear
    # (dashboard vs. email) was never observed.
    api_key_page_selector=None,
)

LIVE_SIGNUP_RECIPES: dict[str, SignupRecipe] = {
    # Keyed by *registrable* domain (registrable_domain("https://api.nasa.gov/")
    # == "nasa.gov", not "api.nasa.gov" -- "api" is a subdomain, dropped
    # by eTLD+1 extraction), matching exactly what PlaywrightBrowserDriver
    # looks up by. Keying this "api.nasa.gov" would silently never match.
    "nasa.gov": NASA_API,
    "openweathermap.org": OPENWEATHERMAP,
    "alphavantage.co": ALPHA_VANTAGE,
    "ipinfo.io": IPINFO,
}
