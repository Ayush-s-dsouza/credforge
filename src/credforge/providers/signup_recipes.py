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
    # Never reached live -- signup itself is blocked by the reCAPTCHA
    # before any email is ever sent. Left unset rather than guessed.
    api_key_email_regex=None,
)

LIVE_SIGNUP_RECIPES: dict[str, SignupRecipe] = {
    # Keyed by *registrable* domain (registrable_domain("https://api.nasa.gov/")
    # == "nasa.gov", not "api.nasa.gov" -- "api" is a subdomain, dropped
    # by eTLD+1 extraction), matching exactly what PlaywrightBrowserDriver
    # looks up by. Keying this "api.nasa.gov" would silently never match.
    "nasa.gov": NASA_API,
    "openweathermap.org": OPENWEATHERMAP,
}
