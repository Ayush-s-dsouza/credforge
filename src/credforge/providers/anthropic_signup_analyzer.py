"""LLM-backed SignupFormAnalyzer, real Anthropic API. Only ever imported from
inside DISCOVER_SIGNUP's own ANTHROPIC_API_KEY check -- same lazy-module-
import discipline as `anthropic_extractor.py` (see that file's docstring),
so the `anthropic` optional extra is never required outside this one path.

Same model choice as `anthropic_extractor.py` (D-014) and the same reasoning:
Sonnet-tier, not the flagship (this is "read a small structured DOM summary,
fill in a known schema" -- not open-ended reasoning), and not Haiku (the
blockers judgment -- did this vendor's form show a CAPTCHA, does a field
smell like a payment field -- has the same real-consequence shape as GATE's
`prohibits_automation` judgment: a false negative here means DISCOVER_SIGNUP
tries to submit a form it should have refused to touch).
"""

try:
    import anthropic
except ImportError as exc:  # pragma: no cover -- exercised only without the optional extra installed
    raise ImportError(
        "ANTHROPIC_API_KEY is set but the 'anthropic' package isn't installed -- "
        "run `pip install credforge[llm]`"
    ) from exc

from .signup_generation import (
    CredentialLocationFinding,
    FormElement,
    RevealCandidate,
    RevealTriggerClassification,
    SignupFormClassification,
)

_MODEL = "claude-sonnet-5"

# A reduced-DOM field list or a post-submit page/email is small by
# construction (DOM reduction already strips it down to form controls and
# their attributes; a signup confirmation page/email is not a full docs
# page) -- bounded anyway, same defensive reasoning as
# `anthropic_extractor.py`'s _MAX_DOCS_CHARS, at a smaller size because the
# inputs here are inherently smaller.
_MAX_CONTENT_CHARS = 8_000


class AnthropicSignupFormAnalyzer:
    def __init__(self, *, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.calls: list[dict[str, int | str | None]] = []

    def _record(self, kind: str, response) -> None:
        usage = getattr(response, "usage", None)
        self.calls.append(
            {
                "kind": kind,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            }
        )

    async def classify_signup_form(self, *, url: str, elements: list[FormElement]) -> SignupFormClassification:
        elements_json = "\n".join(el.model_dump_json() for el in elements)
        response = await self._client.messages.parse(
            model=_MODEL,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"This is the reduced form-control DOM of a SaaS vendor's signup page ({url}), "
                        "one JSON object per line: an input/select/textarea/button element with its "
                        "selector, tag, type, name, id, placeholder, aria_label, associated label_text, "
                        "whether it's required, and (for a <select>) its option texts.\n\n"
                        "Classify each field's real-world purpose. If a form has two password-shaped "
                        "fields, classify the first (primary) one as \"password\" and a second field whose "
                        "label/placeholder/name indicates it re-enters or confirms the password (e.g. "
                        "\"Confirm Password\", \"Repeat Password\", a name containing \"confirm\" or "
                        "\"password2\") as \"password_confirm\" -- never classify both as plain \"password\". "
                        "Identify the submit button's selector. "
                        "List any consent/terms checkboxes that must be checked before submit. Report "
                        "blockers: a visible CAPTCHA/bot-challenge widget, a payment/card field, a phone "
                        "verification step implied by the form, or SSO-only signup (no email+password "
                        "option at all) -- \"none\" if you see none of these. Only report a blocker you can "
                        "actually see evidence for in this element list; do not guess one might exist "
                        "elsewhere on the page. Give your overall confidence (0-1) that this classification "
                        "is correct and complete.\n\n"
                        f"--- reduced DOM ---\n{elements_json[:_MAX_CONTENT_CHARS]}"
                    ),
                }
            ],
            output_format=SignupFormClassification,
        )
        self._record("classify_signup_form", response)
        return response.parsed_output

    async def locate_credential(self, *, context_label: str, content: str) -> CredentialLocationFinding:
        response = await self._client.messages.parse(
            model=_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"This is the visible text of {context_label} after submitting a real signup "
                        "form. Is an API credential (a key, token, or secret) visible here? If so, quote "
                        "the exact, verbatim stable prose that immediately precedes the credential value "
                        "as anchor_text -- e.g. \"Your API key is:\" or \"API Key:\" -- NOT the credential "
                        "value itself, and NOT a description of what the value looks like (do not say "
                        "\"a 16-character alphanumeric string\"; quote the literal label text next to it). "
                        "If no credential is visible but the text says to check an email instead, set "
                        "location to \"email\" and found to false. If neither, set location to \"neither\" "
                        "and describe in detail what the page/email actually says.\n\n"
                        f"--- {context_label} ---\n{content[:_MAX_CONTENT_CHARS]}"
                    ),
                }
            ],
            output_format=CredentialLocationFinding,
        )
        self._record("locate_credential", response)
        return response.parsed_output

    async def identify_reveal_trigger(
        self, *, url: str, candidates: list[RevealCandidate]
    ) -> RevealTriggerClassification:
        candidates_json = "\n".join(c.model_dump_json() for c in candidates)
        response = await self._client.messages.parse(
            model=_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"No signup or API-key request form fields were found on this vendor's page "
                        f"({url}) after loading and waiting for asynchronous content. Some signup widgets "
                        "only render after a specific link or button is clicked (e.g. a 'Sign Up' or "
                        "'Generate API Key' nav link that reveals an embedded form on click, rather than "
                        "on a timer). Below is every visible, clickable link/button on the page, one JSON "
                        "object per line: its selector, tag, visible text, and href (if any).\n\n"
                        "Which single element, if clicked, is MOST likely to reveal a signup or API-key "
                        "request form? Only answer with an element that plausibly leads to account or "
                        "API-key creation -- not general navigation, search, documentation links, social "
                        "media, or unrelated site sections. If nothing here plausibly reveals a signup "
                        "form, set selector to null rather than guessing at the least-bad option. Give "
                        "your confidence (0-1).\n\n"
                        f"--- clickable elements ---\n{candidates_json[:_MAX_CONTENT_CHARS]}"
                    ),
                }
            ],
            output_format=RevealTriggerClassification,
        )
        self._record("identify_reveal_trigger", response)
        return response.parsed_output
