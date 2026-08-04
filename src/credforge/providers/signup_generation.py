"""SignupFormAnalyzer: the LLM-facing interface DISCOVER_SIGNUP uses to turn
a reduced DOM into a structured field map, and a post-submit page/email into
a credential-location finding.

Deliberately a separate, narrower Protocol from `providers.llm.Extractor`
rather than two more methods bolted onto it: Extractor's three methods
(discovery/classification/tos-gate) all have a real deterministic fallback
(`HeuristicExtractor`) that every stage can fall back to without
ANTHROPIC_API_KEY. "Classify an arbitrary vendor's signup form" and "find a
credential in an arbitrary page" have no comparable deterministic fallback --
that's the entire reason DISCOVER_SIGNUP exists instead of another hand-
written recipe. Bolting them onto Extractor would force HeuristicExtractor to
either fake a fallback (silently wrong) or hard-fail (making the shared
interface a liar about what it can do for three of its five methods). See
DECISIONS.md D-065.

Only `AnthropicSignupFormAnalyzer` implements this Protocol. DISCOVER_SIGNUP
checks for ANTHROPIC_API_KEY directly and raises before ever constructing one,
the same way `providers/factory.py` already raises before constructing
`PlaywrightBrowserDriver`/`ImapEmailProvider` without the IMAP settings
`--live` requires.
"""

from typing import Literal, Protocol

from pydantic import BaseModel, Field

FieldPurpose = Literal[
    "email", "password", "password_confirm", "first_name", "last_name", "full_name",
    "company", "phone", "country", "use_case", "other", "unknown",
]

BlockerKind = Literal["captcha", "payment_field", "phone_verification", "sso_only", "none"]


class FormElement(BaseModel):
    """One input/select/textarea/button, as read directly off the rendered
    DOM -- not sent to the LLM as raw HTML (cheaper and more reliable to
    reason over a small structured list than an arbitrary page's markup).
    `selector` is already the best available selector for this element
    (see discover_signup.py's `_build_selector`: `name` preferred over `id`,
    matching the real, hard-won lesson from IPinfo's recipe -- Headless
    UI/Next.js regenerates `id` per render, `name` doesn't)."""

    selector: str
    tag: Literal["input", "select", "textarea", "button"]
    type: str | None = None
    name: str | None = None
    id: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    label_text: str | None = None
    required: bool = False
    # <select> option texts -- lets a required "country"-purpose field get
    # a real, present option as its synthetic default instead of a typed
    # value that might not match any <option>.
    options: list[str] = Field(default_factory=list)


class ClassifiedField(BaseModel):
    selector: str
    purpose: FieldPurpose


class SignupFormClassification(BaseModel):
    field_map: list[ClassifiedField]
    submit_selector: str | None = None
    required_checkboxes: list[str] = Field(default_factory=list)
    blockers: list[BlockerKind] = Field(default_factory=list)
    confidence: float


class RevealCandidate(BaseModel):
    """A visible, clickable link/button found on a page where no signup
    form fields were present yet. Distinct from `FormElement`: this
    describes navigational/action elements (their visible text and href
    matter), not form controls (their placeholder/label/required attributes
    don't apply here -- `<a>` tags in particular never appear in
    `FormElement`, which is scoped to `input, select, textarea, button`).
    See DECISIONS.md D-070."""

    selector: str
    tag: Literal["a", "button"]
    text: str
    href: str | None = None


class RevealTriggerClassification(BaseModel):
    # None means no visible element plausibly leads to a signup/API-key
    # form -- e.g. a page that genuinely has no such flow at all. Never a
    # guess forced onto the least-bad candidate.
    selector: str | None
    reasoning: str | None = None
    confidence: float


class CredentialLocationFinding(BaseModel):
    found: bool
    location: Literal["page", "email", "neither"]
    # Verbatim, stable prose immediately preceding the credential value, AS
    # IT APPEARS in the supplied content -- never the credential value
    # itself, and never a description of its character shape. code (not the
    # LLM) derives an extraction regex from this anchor text and then
    # re.search()es the REAL page/email content with it -- the LLM's own
    # transcription of a long random token is never trusted directly. See
    # discover_signup.py's `_build_anchored_regex` and DECISIONS.md D-065's
    # discussion of why the NASA/Alpha Vantage recipes' shape-anchored
    # regexes broke.
    anchor_text: str | None = None
    detail: str | None = None


class SignupFormAnalyzer(Protocol):
    async def classify_signup_form(self, *, url: str, elements: list[FormElement]) -> SignupFormClassification: ...

    async def locate_credential(self, *, context_label: str, content: str) -> CredentialLocationFinding: ...

    async def identify_reveal_trigger(
        self, *, url: str, candidates: list[RevealCandidate]
    ) -> RevealTriggerClassification: ...
