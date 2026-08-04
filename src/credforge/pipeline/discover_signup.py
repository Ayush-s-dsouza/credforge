"""DISCOVER_SIGNUP: generates a SignupRecipe for an unseen vendor instead of
requiring one to be hand-authored.

The four recipes in `providers/signup_recipes.py` don't generalize -- each
one exists because a human opened that exact vendor's page and read its DOM.
That doesn't scale past a handful of vendors. DISCOVER_SIGNUP inverts the
cost: explore an unseen vendor ONCE, expensively and non-deterministically
(a real LLM reads the rendered form and the post-submit page/email), and
EMIT a SignupRecipe as the output of that exploration. Every subsequent run
for that vendor replays the generated recipe through the exact same
deterministic code path as a hand-authored one (`playwright_browser.py`'s
`signup_and_create_app`, keyed purely on `self._recipes.get(domain)` -- it
has no idea whether a recipe was typed by a human or written here). See
DECISIONS.md D-065.

What the LLM decides vs what code decides, at each step:
  - Locating the signup page: 100% code (anchor-link discovery, same
    domain-filtered pattern GATE already uses for the ToS page).
  - Classifying the form's fields: the LLM (this genuinely has no
    deterministic fallback -- see `providers/signup_generation.py`'s
    docstring for why this is a separate Protocol from `Extractor`, not two
    more methods bolted onto it).
  - Whether to proceed past a CAPTCHA/payment/low-confidence/no-email
    finding: code (`_check_llm_hard_stops`), reading the LLM's own
    structured judgment -- plus one hard stop that is NEVER delegated to
    the LLM at all (`_check_structural_payment_fields`, a raw regex over
    DOM attribute names, independent of whatever the LLM's `blockers` field
    says).
  - Filling and submitting: 100% code, driven by the LLM's classification
    but with a fixed dispatch table of synthetic values per purpose.
  - Locating a credential's anchor text: the LLM.
  - Turning that anchor into an extraction regex, and actually extracting
    the value: 100% code. The LLM's own transcription of a long credential
    string is never trusted directly -- see `_build_anchored_regex`'s
    docstring for why (the exact failure this project already hit twice
    with hand-authored, shape-anchored regexes, D-061).

Trigger and safety rails (all non-negotiable, per DECISIONS.md D-065):
  - Only ever called after GATE has independently cleared an app AUTO --
    reused via `orchestrator.run_app(dry_run=True)`, not re-derived. GATE's
    verdict is exactly as binding here as it is for a hand-authored recipe.
  - Dry-run is the default; live requires the caller to explicitly pass
    `live=True` (wired to the CLI's `--live` flag, nothing more permissive).
  - The registry's idempotency guard (`find_open_provision`) is checked
    before any browser action, identical to `provision()`'s own guard.
  - Every generated password is registered with the redaction filter the
    moment it exists, before it's ever handed to Playwright -- same
    ordering `provision.py` already uses.
"""

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..enums import AuthScheme, CredentialType, PipelineStage, Status, StageStatus
from ..models.registry_entities import RegistryEntry
from ..providers.browser import BrowserDriver  # noqa: F401 -- documents the relationship; not called directly here
from ..providers.email import EmailTimeoutError
from ..providers.factory import ProviderBundle
from ..providers.fetch import FetchException, FetchProvider
from ..providers.playwright_browser import (
    EXTRA_FIELD_DEFAULTS,
    FORM_READY_TIMEOUT_MS,
    SIGNUP_COMPANY,
    SIGNUP_FIRST_NAME,
    SIGNUP_FULL_NAME,
    SIGNUP_LAST_NAME,
    PageNavigatedUnexpectedlyError,
    SignupRecipe,
    dismiss_cookie_banner,
    evaluate_with_navigation_retry,
    wait_for_signup_form_ready,
)
from ..providers.signup_generation import (
    ClassifiedField,
    FormElement,
    RevealCandidate,
    SignupFormAnalyzer,
    SignupFormClassification,
)
from ..redaction import register_secret
from ..registry.store import AppendOnlyRegistry
from ..utils.domains import registrable_domain
from ..vault.crypto_vault import FernetVault
from ..vault.secret_ref import make_vault_ref
from .explain import NULL_EXPLAIN, ExplainEvent, ExplainSink
from .orchestrator import run_app, settings_fingerprint
from .validate import validate

logger = logging.getLogger("credforge.discover_signup")

# A form classification this uncertain isn't worth acting on -- high on
# purpose, since the next step is submitting a real form to a real vendor.
_FORM_CLASSIFICATION_CONFIDENCE_THRESHOLD = 0.75

_ACCOUNT_PASSWORD_BYTES = 18  # matches provision.py's _PASSWORD_BYTES


class HardStopError(Exception):
    """One of DISCOVER_SIGNUP's abort conditions fired. Caught at the top
    level and turned into a `DiscoverSignupResult.stopped_reason` -- a
    finding to report, never a crash."""

    def __init__(self, reason: str, *, evidence: str | None = None) -> None:
        self.reason = reason
        self.evidence = evidence
        super().__init__(reason)


class DiscoverSignupResult(BaseModel):
    identity_key: str
    dry_run: bool
    # Set whenever generation stopped before producing a recipe -- GATE
    # wasn't AUTO, a recipe already exists, a hard stop fired, the idempotency
    # guard found an open provision, or the auth_scheme is outside this
    # generator's scope. None means generation reached the end of its path
    # (a completed dry-run report, or a live recipe + credential).
    stopped_reason: str | None = None

    signup_url: str | None = None
    # D-071: which tier of _locate_signup_page's candidate list actually
    # verified in the browser -- "developer_portal_url" | "link_discovery"
    # | "guess_list". Surfaced here for the same reason reveal_click_selector
    # is: so which path won is visible on the result itself, not just in logs.
    signup_page_source: str | None = None
    field_map: list[ClassifiedField] = Field(default_factory=list)
    # Dry-run only: selector -> the value that WOULD be filled if this were live.
    intended_fills: dict[str, str] = Field(default_factory=dict)
    required_checkboxes: list[str] = Field(default_factory=list)
    submit_selector: str | None = None
    # D-070: set whenever the signup widget didn't render passively and an
    # LLM-identified click was needed to reveal it -- surfaced here (not
    # just in the emitted recipe) so a dry-run report shows the reveal
    # decision before anything ever goes live.
    reveal_click_selector: str | None = None

    # Live only, once a credential was actually extracted and vaulted.
    credential_vault_ref: str | None = None
    recipe_path: str | None = None
    validate_status: str | None = None
    validate_detail: str | None = None


# --- Step 1: locate the signup page -------------------------------------
#
# Same shape as GATE's ToS-page discovery (gate.py's _find_tos_page): real
# anchor-link discovery, filtered to the vendor's own registrable domain --
# not a bigger keyword-guess list, and never trusting an off-domain link a
# docs page happens to also contain (GATE's own docstring notes Alpha
# Vantage's real docs page linking to the Federal Reserve's and the IMF's
# own "terms" pages ahead of its own). A module-local, independent
# implementation rather than importing gate.py's private helpers -- every
# stage in this codebase keeps its link-discovery helpers to itself
# (RESOLVE and GATE each already have their own, similarly-shaped but
# separately-implemented version); this follows that same precedent rather
# than introducing the first cross-module private dependency.

_SIGNUP_LINK_RE = re.compile(r"sign.?up|register|get.?started|api.?key|get.?a.?key|request.?access", re.IGNORECASE)

# Last-resort fallback, tried only if real link discovery finds nothing --
# same two-tier shape as GATE's ToS discovery (D-059): a modern marketing
# site is frequently a client-rendered SPA (React/Next.js/Vue), where the
# real navigation links only exist after JS hydration and simply aren't
# present in the raw HTML `FetchProvider` sees -- found live, repeatedly,
# probing real candidate vendors for this project (api-ninjas.com,
# twelvedata.com, ipapi.co all have a real signup page but no discoverable
# static link to it). A wrong guess here is safe, not just tolerated: the
# classification confidence/no-email-field hard stops downstream catch a
# guess that landed on the wrong page before anything is ever submitted.
_SIGNUP_PATH_GUESSES = ("/signup", "/sign-up", "/register", "/get-started", "/developers", "/pricing", "/join")

_MIN_USABLE_TEXT_LENGTH = 200

_SOFT_404_MARKERS = (
    "page not found", "we can't find", "we cannot find", "doesn't exist",
    "does not exist", "no longer available", "404 error", "404 not found",
)


def _looks_like_a_real_page(text: str) -> bool:
    lower = text[:500].lower()
    return not any(marker in lower for marker in _SOFT_404_MARKERS)


class _AnchorLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._current_href = dict(attrs).get("href")
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href is not None:
            self.links.append((self._current_href, "".join(self._current_text_parts)))
            self._current_href = None
            self._current_text_parts = []


def _extract_signup_candidate_links(html_text: str, *, base_url: str, vendor_domain: str) -> list[str]:
    parser = _AnchorLinkParser()
    try:
        parser.feed(html_text)
    except Exception:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for href, text in parser.links:
        if not href or href.lower().startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        haystack = f"{href} {text}"
        if not _SIGNUP_LINK_RE.search(haystack):
            continue
        absolute = urljoin(base_url, href)
        # Never trust an off-domain signup link -- same reasoning as GATE's
        # ToS-link domain filter (a docs page can legitimately link to a
        # third-party's signup flow, e.g. an SSO provider, that is not this
        # vendor's own account creation).
        if registrable_domain(absolute) != vendor_domain:
            continue
        if absolute not in seen:
            seen.add(absolute)
            candidates.append(absolute)
    return candidates


async def _fetch_usable_page(url: str, *, fetch: FetchProvider) -> tuple[str, str] | None:
    try:
        result = await fetch.fetch(url, method="GET")
    except FetchException:
        return None
    if (
        200 <= result.status_code < 300
        and result.text
        and len(result.text) >= _MIN_USABLE_TEXT_LENGTH
        and _looks_like_a_real_page(result.text)
    ):
        return url, result.text
    return None


async def _locate_signup_page(
    identity_key: str,
    *,
    developer_portal_url: str | None,
    docs_url: str | None,
    docs_html: str | None,
    fetch: FetchProvider,
) -> list[tuple[str, str]]:
    """Gathers every REACHABLE candidate signup-page URL, in tier order
    (`developer_portal_url` first, then real anchor-link discovery, then
    the guess-list fallback) -- but does NOT decide which one actually has
    a real signup form on it. See DECISIONS.md D-071: the previous version
    returned the FIRST fetchable `developer_portal_url` unconditionally,
    trusting a plain HTTP fetch's mere reachability as proof it was the
    right page. Found live: Alpha Vantage's real `developer_portal_url` is
    its support page (`/support/#api-key` -- correct, as it happens, but
    only by luck), while Finnhub's and CoinGecko's were docs/SDK reference
    pages, not their real `/signup` forms -- DISCOVER_SIGNUP never even
    tried the other tiers. A plain fetch can only confirm a candidate is a
    live, real page (not a 404, not empty) -- it can't confirm a form
    exists on it, since a real signup form is very often entirely
    JS-rendered (D-066's shell-detection lesson, again) and invisible to a
    fetch no matter how reachable the URL is. That confirmation now happens
    downstream, against a REAL browser, in `_verify_candidates_in_browser`,
    trying each candidate this function returns in order until one
    actually shows a real field."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _record(url: str, tier: str) -> None:
        if url not in seen:
            seen.add(url)
            candidates.append((url, tier))

    if developer_portal_url:
        found = await _fetch_usable_page(developer_portal_url, fetch=fetch)
        if found is not None:
            _record(found[0], "developer_portal_url")

    link_urls: list[str] = []
    link_seen: set[str] = set()

    def _add_links(html_text: str, *, base_url: str) -> None:
        for url in _extract_signup_candidate_links(html_text, base_url=base_url, vendor_domain=identity_key):
            if url not in link_seen:
                link_seen.add(url)
                link_urls.append(url)

    if docs_html and docs_url:
        _add_links(docs_html, base_url=docs_url)

    homepage_url = f"https://{identity_key}"
    try:
        homepage = await fetch.fetch(homepage_url, method="GET")
    except FetchException:
        homepage = None
    if homepage is not None and 200 <= homepage.status_code < 300 and homepage.text:
        _add_links(homepage.text, base_url=homepage.final_url or homepage_url)

    for url in link_urls:
        found = await _fetch_usable_page(url, fetch=fetch)
        if found is not None:
            _record(found[0], "link_discovery")

    for path in _SIGNUP_PATH_GUESSES:
        found = await _fetch_usable_page(f"https://{identity_key}{path}", fetch=fetch)
        if found is not None:
            _record(found[0], "guess_list")

    return candidates


async def _verify_candidates_in_browser(
    page, candidates: list[tuple[str, str]], *, analyzer: SignupFormAnalyzer, identity_key: str,
    explain: ExplainSink = NULL_EXPLAIN,
) -> tuple[str, str, bool, list[str]] | None:
    """Tries each `_locate_signup_page` candidate against a REAL browser, in
    the order given -- the actual acceptance test, not a keyword match on
    the URL or a raw-HTML regex, using the exact same generic readiness
    check (`wait_for_signup_form_ready`, plus a reveal-click attempt on
    timeout) already trusted downstream to tell a real form apart from page
    chrome. Falls through to the next candidate whenever the current one
    doesn't verify -- never trusts the first merely-reachable URL. Returns
    (accepted_url, source_tier, required_waiting, reveal_clicks) for the
    first candidate that shows a real field, or None if none of them ever
    did."""
    for url, tier in candidates:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.PROVISION, identity_key=identity_key,
                message=f"trying candidate signup page: {url} [{tier}]",
            )
        )
        try:
            await page.goto(url)
        except Exception as exc:
            logger.info(
                "signup-page candidate unreachable in browser",
                extra={"identity_key": identity_key, "url": url, "source_tier": tier, "error": str(exc)},
            )
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message=f"candidate unreachable in browser: {url} ({exc})",
                )
            )
            continue
        await dismiss_cookie_banner(page)
        form_ready, waited = await wait_for_signup_form_ready(page)
        reveal_clicks: list[str] = []
        if not form_ready:
            form_ready, reveal_clicks, waited_after_click = await _reveal_signup_form(page, analyzer=analyzer, url=url)
            waited = waited or waited_after_click
        logger.info(
            "signup-page candidate accepted" if form_ready else "signup-page candidate rejected",
            extra={"identity_key": identity_key, "url": url, "source_tier": tier, "accepted": form_ready},
        )
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.PROVISION, identity_key=identity_key,
                message=(
                    f"candidate {'ACCEPTED' if form_ready else 'rejected (no real form field found)'}: {url}"
                    + (f" (clicked to reveal: {reveal_clicks})" if reveal_clicks and form_ready else "")
                ),
            )
        )
        if form_ready:
            return url, tier, waited, reveal_clicks
    return None


# --- Step 2: reduce the rendered DOM to a structured element list -------

_DOM_REDUCTION_JS = """
() => {
  const out = [];
  const els = document.querySelectorAll('input, select, textarea, button');
  for (const el of els) {
    if (el.disabled) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const name = el.getAttribute('name');
    const id = el.getAttribute('id');
    // A hidden field is normally pure plumbing (csrf token, form state)
    // and gets skipped -- EXCEPT a hidden CAPTCHA-response field
    // (cf-turnstile-response, g-recaptcha-response), which is real signal,
    // not noise: it's often the ONLY DOM trace a checkbox-style CAPTCHA
    // (Cloudflare Turnstile) leaves, found live against twelvedata.com.
    // Kept so the structural CAPTCHA check downstream can actually see it.
    if (tag === 'input' && type === 'hidden' && !/captcha|turnstile/i.test((name || '') + ' ' + (id || ''))) continue;
    const placeholder = el.getAttribute('placeholder');
    const ariaLabel = el.getAttribute('aria-label');
    let labelText = null;
    if (id) {
      try {
        const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (lbl) labelText = lbl.textContent.trim();
      } catch (e) {}
    }
    if (!labelText) {
      const parentLabel = el.closest('label');
      if (parentLabel) labelText = parentLabel.textContent.trim();
    }
    const required = el.hasAttribute('required') || el.getAttribute('aria-required') === 'true';
    let options = [];
    if (tag === 'select') {
      options = Array.from(el.options).map(o => o.textContent.trim()).filter(Boolean);
    }
    out.push({tag, type: type || null, name, id, placeholder, ariaLabel, labelText, required, options});
  }
  return out;
}
"""


def _build_selector(raw: dict) -> str:
    """`name` preferred over `id` -- the real, hard-won lesson from
    IPinfo's hand-authored recipe (`signup_recipes.py`): Headless UI/
    Next.js-style frameworks regenerate `id` per render
    (`headlessui-control-_R_hmmm56_`), but `name` is part of the form's own
    static markup and survives across loads. A generated recipe has no
    human re-checking it before every future replay, so this matters more
    here than it did for the one hand-authored recipe that already learned
    it the hard way."""
    name = raw.get("name")
    id_ = raw.get("id")
    if name:
        return f"[name='{name}']"
    if id_:
        return f"#{id_}"
    tag = raw["tag"]
    type_ = raw.get("type")
    return f"{tag}[type='{type_}']" if type_ else tag


async def _reduce_dom(page) -> list[FormElement]:
    raw_elements = await evaluate_with_navigation_retry(page, _DOM_REDUCTION_JS)
    elements: list[FormElement] = []
    for raw in raw_elements:
        elements.append(
            FormElement(
                selector=_build_selector(raw),
                tag=raw["tag"],
                type=raw.get("type"),
                name=raw.get("name"),
                id=raw.get("id"),
                placeholder=raw.get("placeholder"),
                aria_label=raw.get("ariaLabel"),
                label_text=raw.get("labelText"),
                required=bool(raw.get("required")),
                options=raw.get("options") or [],
            )
        )
    return elements


# --- Step 2b: reveal an interaction-gated signup widget -------------------
#
# D-070: found live against NASA, after D-069's readiness poll was verified
# NOT to be the whole story -- api.nasa.gov's real signup widget doesn't
# render on ANY timer at all (a direct 38-second poll, well past D-069's
# 15s cap, showed zero DOM change). It only mounts after its own "Generate
# API Key" nav link (href="#signUp") is clicked. Structurally different
# from D-069's problem (a timing race, fixed by waiting): this is an
# interaction gate, fixed by finding and clicking the right trigger.
#
# Deliberately NOT a hardcoded keyword list ("sign up"|"register"|"get
# started"...) -- that's exactly the kind of hand-maintained artifact that
# generalizes only until the next vendor phrases it differently, the same
# reasoning D-065 already applies to form-field classification. The LLM is
# shown every visible clickable element's real text/href and asked which
# one, if any, plausibly reveals a signup form -- same "let the model make
# the judgment call code can't reliably encode" pattern as
# `classify_signup_form`, applied one step earlier.

_REVEAL_CANDIDATE_JS = """
() => {
  const out = [];
  const els = document.querySelectorAll('a, button');
  for (const el of els) {
    if (el.disabled) continue;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (rect.width === 0 || rect.height === 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    const text = (el.innerText || el.textContent || '').trim();
    if (!text) continue;
    const tag = el.tagName.toLowerCase();
    out.push({
      tag, text: text.slice(0, 80),
      id: el.getAttribute('id'), name: el.getAttribute('name'), href: el.getAttribute('href'),
    });
  }
  return out;
}
"""

_MAX_REVEAL_ATTEMPTS = 2


def _build_reveal_selector(raw: dict) -> str:
    """Same name/id-preferred precedence as `_build_selector`, extended
    with two fallbacks `_build_selector` doesn't need: a real `<a>` often
    has neither name nor id (NASA's own "Generate API Key" link is exactly
    this shape), so href and finally visible text (via Playwright's own
    `:has-text()` selector extension, not plain CSS) keep every candidate
    uniquely clickable rather than falling through to a bare, ambiguous
    `tag` selector that could match dozens of elements."""
    name = raw.get("name")
    if name:
        return f"[name='{name}']"
    id_ = raw.get("id")
    if id_:
        return f"#{id_}"
    href = raw.get("href")
    if href and not href.startswith("javascript:"):
        return f"{raw['tag']}[href='{href}']"
    text = (raw.get("text") or "").replace('"', '\\"')
    return f'{raw["tag"]}:has-text("{text}")'


async def _gather_reveal_candidates(page) -> list[RevealCandidate]:
    raw_elements = await evaluate_with_navigation_retry(page, _REVEAL_CANDIDATE_JS)
    return [
        RevealCandidate(selector=_build_reveal_selector(raw), tag=raw["tag"], text=raw["text"], href=raw.get("href"))
        for raw in raw_elements
    ]


async def _reveal_signup_form(page, *, analyzer: SignupFormAnalyzer, url: str) -> tuple[bool, list[str], bool]:
    """Called only once `wait_for_signup_form_ready` has already timed out.
    Tries at most `_MAX_REVEAL_ATTEMPTS` LLM-identified clicks, re-polling
    after each -- gathers candidates fresh every iteration in case the
    first click was itself a navigation step that exposed a more specific
    trigger. Returns (found, [selectors clicked, in order], whether the
    successful post-click check itself needed to poll rather than seeing
    the field immediately); an empty selector list means either no
    plausible candidate existed or the LLM found none."""
    clicked: list[str] = []
    for _ in range(_MAX_REVEAL_ATTEMPTS):
        candidates = await _gather_reveal_candidates(page)
        if not candidates:
            break
        trigger = await analyzer.identify_reveal_trigger(url=url, candidates=candidates)
        if not trigger.selector:
            break
        try:
            await page.click(trigger.selector, timeout=5_000)
        except Exception:
            break
        clicked.append(trigger.selector)
        found, waited = await wait_for_signup_form_ready(page)
        if found:
            return True, clicked, waited
    return False, clicked, False


# --- Step 3: hard stops ---------------------------------------------------
#
# Two independent layers, matching this project's established "don't rely
# on a single check for something this consequential" pattern (D-004's two
# independent redaction layers is the precedent): the LLM's own `blockers`
# judgment, AND a structural, code-only regex over raw DOM attributes that
# fires regardless of what the LLM's blockers field says. `purpose` (the
# LLM's field classification) has no "payment"/"card" value in its own enum
# -- payment detection is deliberately not something the classification
# step is trusted to self-report; it's checked independently instead.

_CARD_FIELD_RE = re.compile(
    r"card.?number|cc.?number|\bcvv\b|\bcvc\b|card.?expir|expir.?date|exp.?date|billing.?address",
    re.IGNORECASE,
)


def _check_structural_payment_fields(elements: list[FormElement]) -> None:
    for el in elements:
        haystack = " ".join(v for v in (el.name, el.id, el.placeholder, el.aria_label, el.label_text) if v)
        if _CARD_FIELD_RE.search(haystack):
            raise HardStopError(
                "payment/card field detected in the raw DOM -- a structural check, independent of "
                "whatever the LLM's own blockers judgment reports",
                evidence=f"selector={el.selector!r} name={el.name!r} id={el.id!r} label={el.label_text!r}",
            )


# Found live against twelvedata.com: a Cloudflare Turnstile CAPTCHA left no
# visible widget the LLM's blockers judgment could reasonably have caught
# from the reduced DOM -- its only trace is the hidden
# `cf-turnstile-response` field _reduce_dom now deliberately keeps (see
# _DOM_REDUCTION_JS). Checked the same way payment fields are: structurally,
# regardless of what the LLM's own blockers list says.
_CAPTCHA_FIELD_RE = re.compile(r"captcha|turnstile", re.IGNORECASE)


def _check_structural_captcha_fields(elements: list[FormElement]) -> None:
    for el in elements:
        haystack = " ".join(v for v in (el.name, el.id) if v)
        if _CAPTCHA_FIELD_RE.search(haystack):
            raise HardStopError(
                "CAPTCHA-response field detected in the raw DOM -- a structural check, independent of "
                "whatever the LLM's own blockers judgment reports",
                evidence=f"selector={el.selector!r} name={el.name!r} id={el.id!r}",
            )


def _check_llm_hard_stops(classification: SignupFormClassification, *, confidence_threshold: float) -> None:
    real_blockers = [b for b in classification.blockers if b != "none"]
    if real_blockers:
        raise HardStopError(f"blocker(s) reported by form classification: {', '.join(real_blockers)}")
    if classification.confidence < confidence_threshold:
        raise HardStopError(
            f"form classification confidence {classification.confidence} is below the "
            f"{confidence_threshold} threshold required before submitting a real form"
        )
    if not any(f.purpose == "email" for f in classification.field_map):
        raise HardStopError("no field classified as email -- cannot receive a credential without one")
    if not classification.submit_selector:
        raise HardStopError("no submit button identified -- cannot build a working recipe without one")


# --- Step 4: fill and submit (live only) ---------------------------------

_PURPOSE_TO_RECIPE_FIELD: dict[str, str] = {
    "email": "email_field_selector",
    "password": "password_field_selector",
    "password_confirm": "password_confirm_field_selector",
    "first_name": "first_name_field_selector",
    "last_name": "last_name_field_selector",
    "full_name": "full_name_field_selector",
    "company": "company_field_selector",
}

# Purposes filled with the same generated account password -- kept as a set
# rather than folded into _PURPOSE_TO_FILL_VALUE, which holds fixed
# constant strings; the password itself is generated per run, not a constant.
_PASSWORD_PURPOSES = frozenset({"password", "password_confirm"})

_PURPOSE_TO_FILL_VALUE: dict[str, str] = {
    "first_name": SIGNUP_FIRST_NAME,
    "last_name": SIGNUP_LAST_NAME,
    "full_name": SIGNUP_FULL_NAME,
    "company": SIGNUP_COMPANY,
}


def _describe_intended_fills(classification: SignupFormClassification, *, email_alias: str) -> dict[str, str]:
    """Dry-run only -- what WOULD be filled, without touching the page."""
    intended: dict[str, str] = {}
    for f in classification.field_map:
        if f.purpose == "email":
            intended[f.selector] = email_alias
        elif f.purpose in _PASSWORD_PURPOSES:
            intended[f.selector] = "<generated password, not shown>"
        elif f.purpose in _PURPOSE_TO_FILL_VALUE:
            intended[f.selector] = _PURPOSE_TO_FILL_VALUE[f.purpose]
        else:
            intended[f.selector] = f"<synthetic default for {f.purpose!r} if required, else left blank>"
    return intended


async def _fill_classified_fields(
    page, classification: SignupFormClassification, elements: list[FormElement], *, email_alias: str, account_password: str
) -> None:
    elements_by_selector = {el.selector: el for el in elements}
    for f in classification.field_map:
        if f.purpose == "email":
            await page.fill(f.selector, email_alias)
        elif f.purpose in _PASSWORD_PURPOSES:
            await page.fill(f.selector, account_password)
        elif f.purpose in _PURPOSE_TO_FILL_VALUE:
            await page.fill(f.selector, _PURPOSE_TO_FILL_VALUE[f.purpose])
        else:
            el = elements_by_selector.get(f.selector)
            if el is not None and el.required:
                await page.fill(f.selector, EXTRA_FIELD_DEFAULTS.get(f.purpose, "N/A"))


# --- Step 5: extraction regex, anchored on surrounding text --------------


def _build_anchored_regex(anchor_text: str) -> str:
    """Anchored on literal, escaped surrounding prose, with a generic
    capture group -- NOT on the credential's character shape. This is the
    exact lesson D-061 already paid for twice with hand-authored recipes:
    NASA's and Alpha Vantage's original regexes matched a specific
    character class (`[A-Z0-9]{16}`, a 40-char alphanumeric pattern)
    observed from one real key, and broke the moment either the vendor's
    copy changed (Alpha Vantage) or a differently-shaped key was issued.
    Anchoring on stable prose next to the value, with `[^\\s<"']+` as the
    capture, survives both failure modes: the vendor would have to change
    its own label text, not just the credential format, to break this."""
    escaped = re.escape(anchor_text.strip())
    return rf"{escaped}\s*([^\s<\"']+)"


# Ordinary sentence punctuation that can immediately follow a credential in
# prose ("Your API key is: XJ7QP2KD9M. Keep it safe.") but was never part
# of the value itself. `[^\s<"']+` (the capture group above) is generic on
# purpose -- it doesn't know a credential's real alphabet -- so it happily
# swallows a trailing period/comma too; stripped here rather than folded
# into the regex, so the regex stored in the emitted recipe stays simple
# and this correction applies uniformly to both the page and email paths.
_TRAILING_PUNCTUATION = ".,;:)]}'\""


def _extract_anchored_value(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    if match is None:
        return None
    return match.group(1).rstrip(_TRAILING_PUNCTUATION)


# --- Step 6: emit the recipe ----------------------------------------------


def _generated_recipes_dir(data_dir: Path) -> Path:
    return data_dir / "generated_recipes"


def save_generated_recipe(data_dir: Path, domain: str, recipe: SignupRecipe) -> Path:
    directory = _generated_recipes_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{domain}.json"
    path.write_text(recipe.model_dump_json(indent=2), encoding="utf-8")
    return path


def merge_recipes(
    generated: dict[str, SignupRecipe], hand_authored: dict[str, SignupRecipe]
) -> dict[str, SignupRecipe]:
    """`generated` now wins on a key collision -- reversed from this
    function's original D-065 rule ("hand_authored always wins," a
    generated recipe never overriding one a human already verified by
    hand). Superseded by D-075: a generated recipe only ever reaches this
    function's `generated` argument from one of two sources -- a fresh,
    this-process run, or the committed `examples/generated_recipes/`
    directory a human deliberately reviewed and checked in, the same
    scrutiny a hand-written recipe already gets before it's trusted. The
    demonstrable claim this project makes is that DISCOVER_SIGNUP's own
    output is what actually runs for a vendor it has generated a recipe
    for -- a hand-written recipe silently winning over a committed
    generated one would falsify that claim for any vendor with both (e.g.
    Alpha Vantage). Extracted as its own pure function (rather than an
    inline dict-merge expression in factory.py) specifically so this
    precedence rule has a direct unit test, not just an implicit property
    of merge order in a two-line expression."""
    return {**hand_authored, **generated}


def load_generated_recipes(data_dir: Path) -> dict[str, SignupRecipe]:
    """A corrupted generated-recipe file is skipped, not fatal -- same
    "one bad entry doesn't sink the rest" principle as the registry's and
    REPORT's own loaders."""
    directory = _generated_recipes_dir(data_dir)
    if not directory.exists():
        return {}
    recipes: dict[str, SignupRecipe] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            recipes[path.stem] = SignupRecipe.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError):
            continue
    return recipes


def _build_recipe_from_classification(
    classification: SignupFormClassification,
    elements: list[FormElement],
    *,
    domain: str,
    docs_url: str,
    signup_url: str,
    credential_type: CredentialType,
    page_regex: str | None,
    email_regex: str | None,
    requires_email_verification: bool,
    requires_async_form_render: bool = False,
    reveal_click_selector: str | None = None,
) -> SignupRecipe:
    kwargs: dict[str, str] = {}
    extra_field_selectors: dict[str, str] = {}
    elements_by_selector = {el.selector: el for el in elements}

    for f in classification.field_map:
        recipe_field = _PURPOSE_TO_RECIPE_FIELD.get(f.purpose)
        if recipe_field:
            kwargs[recipe_field] = f.selector
            continue
        el = elements_by_selector.get(f.selector)
        if el is not None and el.required:
            extra_field_selectors[f.purpose] = f.selector

    assert classification.submit_selector is not None  # guaranteed by _check_llm_hard_stops
    return SignupRecipe(
        submit_selector=classification.submit_selector,
        credential_type=credential_type,
        docs_url=docs_url,
        checkbox_selectors=list(classification.required_checkboxes),
        requires_email_verification=requires_email_verification,
        api_key_page_selector="body" if page_regex else None,
        api_key_page_regex=page_regex,
        api_key_email_regex=email_regex,
        developer_portal_url_fallback=signup_url,
        extra_field_selectors=extra_field_selectors,
        requires_async_form_render=requires_async_form_render,
        reveal_click_selector=reveal_click_selector,
        generated_by="discover_signup",
        generated_at=datetime.now(timezone.utc),
        **kwargs,
    )


# --- Top-level entry point ------------------------------------------------


async def discover_signup(
    app_name: str,
    *,
    providers: ProviderBundle,
    settings: Settings,
    registry: AppendOnlyRegistry,
    vault: FernetVault | None,
    run_id: str,
    live: bool,
    headed: bool = False,
    explain: ExplainSink = NULL_EXPLAIN,
) -> DiscoverSignupResult:
    # D-076: this is the actual demo -- every stop, not just successes,
    # gets streamed with its stopped_reason so a viewer can watch WHY
    # generation didn't reach a recipe, not just that it didn't.
    # identity_key isn't known until after the first `run_app` call below,
    # so this closure is defined there, not at the top of the function.
    def _result(identity_key: str, **kwargs) -> DiscoverSignupResult:
        result = DiscoverSignupResult(identity_key=identity_key, dry_run=not live, **kwargs)
        if result.stopped_reason:
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message=f"generation stopped: {result.stopped_reason}",
                )
            )
        return result

    # Reuse the real pipeline through GATE rather than re-deriving it --
    # dry_run=True means PROVISION never runs and nothing has touched a
    # vault/browser/email yet, exactly the precondition state
    # DISCOVER_SIGNUP's own trigger requires. GATE's verdict, once reached
    # this way, is exactly as binding as it is for any hand-authored
    # recipe -- nothing below can change it.
    state, _artifact = await run_app(
        app_name, providers=providers, settings=settings, registry=registry, run_id=run_id,
        data_dir=settings.data_dir, vault=None, dry_run=True, live=False, explain=explain,
    )

    identity_key = state.identity_key

    if state.gate is None or state.gate.status != Status.AUTO:
        gate_status = state.gate.status if state.gate else None
        return _result(
            identity_key,
            stopped_reason=f"GATE did not clear AUTO (status={gate_status!r}) -- nothing to generate",
        )

    from ..providers.signup_recipes import LIVE_SIGNUP_RECIPES

    if identity_key in LIVE_SIGNUP_RECIPES:
        return _result(
            identity_key,
            stopped_reason=f"a hand-authored recipe already exists for {identity_key!r} -- nothing to generate",
        )
    if identity_key in load_generated_recipes(settings.data_dir):
        return _result(
            identity_key,
            stopped_reason=f"a generated recipe already exists for {identity_key!r} -- nothing to generate",
        )

    if registry.find_open_provision(identity_key) is not None:
        return _result(
            identity_key,
            stopped_reason=(
                f"{identity_key!r} already has an open provisioned credential in the registry -- "
                "idempotency guard, no browser action taken"
            ),
        )

    auth_scheme = state.classify.auth_scheme if state.classify else None
    if auth_scheme not in (AuthScheme.API_KEY, AuthScheme.BEARER_STATIC):
        return _result(
            identity_key,
            stopped_reason=(
                f"classified auth_scheme={auth_scheme!r} -- DISCOVER_SIGNUP only covers the "
                "signup-form-to-key/token archetype every existing recipe already uses; an OAuth "
                "app-creation console is a different archetype this generator does not build recipes for"
            ),
        )

    extraction = state.discovery.extraction if state.discovery else None
    developer_portal_url = extraction.developer_portal_url if extraction else None
    docs_url = state.discovery.docs_url if state.discovery else None
    docs_html = state.discovery.docs_text if state.discovery else None

    explain.emit(
        ExplainEvent(
            stage=PipelineStage.PROVISION, identity_key=identity_key,
            message="locating signup page: gathering candidates (developer_portal_url, link discovery, guess-list)",
        )
    )
    candidates = await _locate_signup_page(
        identity_key, developer_portal_url=developer_portal_url, docs_url=docs_url, docs_html=docs_html,
        fetch=providers.fetch,
    )
    explain.emit(
        ExplainEvent(
            stage=PipelineStage.PROVISION, identity_key=identity_key,
            message=f"located {len(candidates)} candidate signup page(s)",
        )
    )
    if not candidates:
        return _result(
            identity_key,
            stopped_reason=(
                "could not locate any reachable candidate signup page -- developer_portal_url was "
                "absent or unreachable, and no signup-shaped link was found on the docs page, "
                "homepage, or the guess-list fallback paths"
            ),
        )

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "DISCOVER_SIGNUP requires ANTHROPIC_API_KEY -- exploring an unseen vendor's signup form is "
            "exactly the task this project reserves for a real LLM call; there is no deterministic "
            "fallback for it (see providers/signup_generation.py's docstring)"
        )
    from ..providers.anthropic_signup_analyzer import AnthropicSignupFormAnalyzer

    analyzer = AnthropicSignupFormAnalyzer(api_key=settings.anthropic_api_key.get_secret_value())

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        try:
            page = await browser.new_page()

            # D-071: verify each candidate against a REAL browser render --
            # a plain fetch's mere reachability was never proof a candidate
            # actually had a signup form on it (found live: Finnhub's and
            # CoinGecko's developer_portal_url pointed at a docs/SDK
            # reference page, not their real /signup form, and the old code
            # trusted it unconditionally). Falls through developer_portal_url
            # -> link_discovery -> guess_list, verifying each with the same
            # readiness+reveal check (D-069/D-070) already trusted downstream
            # to tell a real form apart from page chrome.
            accepted = await _verify_candidates_in_browser(
                page, candidates, analyzer=analyzer, identity_key=identity_key, explain=explain,
            )
            if accepted is None:
                displayed = ", ".join(f"{u!r} [{t}]" for u, t in candidates[:10])
                more = ", ..." if len(candidates) > 10 else ""
                return _result(
                    identity_key,
                    stopped_reason=(
                        f"SIGNUP_FORM_NOT_RENDERED: tried {len(candidates)} candidate signup page(s), "
                        f"each waiting up to {FORM_READY_TIMEOUT_MS / 1000:.0f}s and attempting a reveal "
                        f"click before moving on, ({displayed}{more}) -- none ever showed a real "
                        "text/email/password form field. This is a distinct outcome from a low-confidence "
                        "classification: every reachable candidate was tried against a real render, not "
                        "just the first one."
                    ),
                )
            signup_url, signup_page_source, required_waiting, reveal_clicks = accepted
            reveal_click_selector = reveal_clicks[-1] if reveal_clicks else None

            elements = await _reduce_dom(page)

            _check_structural_payment_fields(elements)
            _check_structural_captcha_fields(elements)

            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message=f"reading form: {len(elements)} DOM element(s) found on {signup_url}, classifying...",
                )
            )
            classification = await analyzer.classify_signup_form(url=signup_url, elements=elements)
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message=(
                        f"classified {len(classification.field_map)} field(s) "
                        f"(confidence={classification.confidence}, blockers={classification.blockers})"
                    ),
                )
            )
            _check_llm_hard_stops(classification, confidence_threshold=_FORM_CLASSIFICATION_CONFIDENCE_THRESHOLD)

            # A short, per-run suffix -- NOT the stable alias a registered
            # recipe uses -- so repeat generation attempts against the same
            # unproven vendor don't collide with vendor-side email dedup.
            # See email.py's alias_for() docstring and DECISIONS.md D-065.
            email_alias = providers.email.alias_for(identity_key, suffix=run_id[-8:])

            if not live:
                return DiscoverSignupResult(
                    identity_key=identity_key, dry_run=True, signup_url=signup_url,
                    signup_page_source=signup_page_source,
                    field_map=classification.field_map,
                    intended_fills=_describe_intended_fills(classification, email_alias=email_alias),
                    required_checkboxes=list(classification.required_checkboxes),
                    submit_selector=classification.submit_selector,
                    reveal_click_selector=reveal_click_selector,
                )

            account_password = _generate_account_password()
            register_secret(account_password)  # before it's ever handed to Playwright

            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message=f"filling {len(classification.field_map)} field(s) with alias {email_alias}",
                )
            )
            await _fill_classified_fields(
                page, classification, elements, email_alias=email_alias, account_password=account_password
            )
            for checkbox_selector in classification.required_checkboxes:
                await page.check(checkbox_selector)
            await page.click(classification.submit_selector)
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key, message="submitted signup form",
                )
            )

            # Same reasoning as PlaywrightBrowserDriver's own AJAX-poll
            # (playwright_browser.py) -- a same-page credential render has
            # nothing to wait_for_navigation on.
            await page.wait_for_timeout(1500)
            post_submit_text = await page.text_content("body") or ""

            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message="extracting credential: reading the post-submit page",
                )
            )
            finding = await analyzer.locate_credential(context_label="the post-submit page", content=post_submit_text)

            page_regex: str | None = None
            email_regex: str | None = None
            requires_email_verification = False

            if finding.found and finding.location == "page" and finding.anchor_text:
                page_regex = _build_anchored_regex(finding.anchor_text)
                extracted = _extract_anchored_value(page_regex, post_submit_text)
                if extracted is None:
                    return _result(
                        identity_key, signup_url=signup_url,
                        stopped_reason=(
                            f"the LLM reported a credential on the page anchored on {finding.anchor_text!r}, "
                            "but the derived regex found no match against the real page text -- the LLM's "
                            "own transcription of the value is never trusted directly"
                        ),
                    )
                raw_credential_value = extracted
            elif finding.location == "email":
                requires_email_verification = True
                explain.emit(
                    ExplainEvent(
                        stage=PipelineStage.PROVISION, identity_key=identity_key,
                        message=f"credential delivered by email -- waiting for a message to {email_alias}",
                    )
                )
                try:
                    message = await providers.email.wait_for_message(to_addr=email_alias)
                except EmailTimeoutError:
                    return _result(
                        identity_key, signup_url=signup_url,
                        stopped_reason=f"page said to check email, but no message arrived at {email_alias!r} in time",
                    )
                email_finding = await analyzer.locate_credential(
                    context_label="the verification email", content=message.body_text
                )
                if not email_finding.found or not email_finding.anchor_text:
                    return _result(
                        identity_key, signup_url=signup_url,
                        stopped_reason=(
                            f"email arrived but no credential anchor identified in its body: "
                            f"{email_finding.detail or 'no detail given'}"
                        ),
                    )
                email_regex = _build_anchored_regex(email_finding.anchor_text)
                extracted = _extract_anchored_value(email_regex, message.body_text)
                if extracted is None:
                    return _result(
                        identity_key, signup_url=signup_url,
                        stopped_reason=(
                            f"the LLM reported a credential in the email anchored on "
                            f"{email_finding.anchor_text!r}, but the derived regex found no match against "
                            "the real email body"
                        ),
                    )
                raw_credential_value = extracted
            else:
                return _result(
                    identity_key, signup_url=signup_url,
                    stopped_reason=f"no credential found on the page or in email: {finding.detail or 'no detail given'}",
                )

            register_secret(raw_credential_value)
            assert vault is not None  # required by the caller whenever live=True, same as provision()
            api_key_ref = make_vault_ref(identity_key, "api_key")
            vault.store(api_key_ref, {"api_key": raw_credential_value})
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message="credential extracted and vaulted",
                )
            )

            credential_type = CredentialType.API_KEY if auth_scheme == AuthScheme.API_KEY else CredentialType.BEARER_TOKEN

            recipe = _build_recipe_from_classification(
                classification, elements,
                domain=identity_key, docs_url=docs_url or signup_url, signup_url=signup_url,
                credential_type=credential_type, page_regex=page_regex, email_regex=email_regex,
                requires_email_verification=requires_email_verification,
                requires_async_form_render=required_waiting,
                reveal_click_selector=reveal_click_selector,
            )
            recipe_path = save_generated_recipe(settings.data_dir, identity_key, recipe)
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.PROVISION, identity_key=identity_key,
                    message=f"recipe written: {recipe_path}",
                )
            )

            registry.append(
                RegistryEntry(
                    identity_key=identity_key, app_name=app_name, run_id=run_id,
                    stage=PipelineStage.PROVISION, stage_status=StageStatus.COMPLETED,
                    settings_fingerprint=settings_fingerprint(settings, live=live),
                    recorded_at=datetime.now(timezone.utc),
                    email_alias=email_alias, console_url=page.url,
                    credential_type=credential_type, api_key_ref=api_key_ref,
                )
            )

            # VALIDATE as normal -- whatever DISCOVER's own real extraction
            # found this run, nothing invented for the occasion. A
            # generated recipe that yields a credential VALIDATE can't
            # confirm is an honest, reportable limitation (the same one
            # D-063 documented for a hand-authored recipe before its own
            # fallback existed), not a reason to fabricate an endpoint.
            validate_result = await validate(
                identity_key,
                validation_endpoint=extraction.validation_endpoint if extraction else None,
                base_url=extraction.base_url if extraction else None,
                auth_scheme=auth_scheme.value,
                credential={"api_key": raw_credential_value},
                fetch=providers.fetch,
            )

            return DiscoverSignupResult(
                identity_key=identity_key, dry_run=False, signup_url=signup_url,
                signup_page_source=signup_page_source, reveal_click_selector=reveal_click_selector,
                field_map=classification.field_map, submit_selector=classification.submit_selector,
                required_checkboxes=list(classification.required_checkboxes),
                credential_vault_ref=api_key_ref, recipe_path=str(recipe_path),
                validate_status=validate_result.status, validate_detail=validate_result.detail,
            )
        except HardStopError as exc:
            return _result(
                identity_key,
                stopped_reason=f"hard stop: {exc.reason}" + (f" ({exc.evidence})" if exc.evidence else ""),
            )
        except PageNavigatedUnexpectedlyError as exc:
            # Found live against SendGrid: a client-side redirect fired
            # mid-poll and crashed the next DOM check. A vendor's own
            # redirect shouldn't take the generator down -- this fires only
            # after evaluate_with_navigation_retry's own settle-and-retry
            # already failed once, so it's a genuine, distinct outcome, not
            # folded into the generic catch-all below.
            return _result(identity_key, stopped_reason=f"PAGE_NAVIGATED_UNEXPECTEDLY: {exc}")
        except Exception as exc:  # noqa: BLE001 -- an unexpected Playwright/page error is a real, reportable
            # finding, not a crash -- same "one exploration attempt's failure never takes down the caller"
            # principle signup_and_create_app's own broad except already applies for a hand-authored recipe.
            return _result(
                identity_key, stopped_reason=f"unexpected error during generation: {type(exc).__name__}: {exc}",
            )
        finally:
            await browser.close()


def _generate_account_password() -> str:
    import secrets

    return secrets.token_urlsafe(_ACCOUNT_PASSWORD_BYTES)
