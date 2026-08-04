"""GATE: decide AUTO vs HITL vs UNSUPPORTED.

Approach: check hard preconditions from earlier stages first (couldn't
crawl docs at all; confirmed no public API; couldn't confidently classify
the auth scheme), then GATE's own checks in the spec's mandated order --
ToS prohibition always first, then payment/business/sales, then
phone/CAPTCHA/SSO-only -- and only reach AUTO if none of them fire.

Deliberate deviation from the spec's literal item ordering: "no public
API" is checked as a precondition (step 2 below), not last, because it's a
different kind of fact than the ToS/payment/phone checks -- those are all
about whether *developer-portal signup* is gated, which presupposes a
developer portal exists at all. See DECISIONS.md D-020.

GATE fetches and checks the actual ToS/developer-agreement text itself --
found the way a human finds it, by real anchor-link discovery on the
already-crawled docs page and the resolved homepage (looking for a
terms/tos/legal/conditions/agreement link, i.e. the footer), falling back
to a small set of common-URL guesses only when link discovery finds
nothing (D-059; a fixed guess list alone missed Alpha Vantage's real ToS
at the unconventional, underscore-separated `/terms_of_service/`). It
never assumes automation is permitted just because no evidence of
prohibition turned up elsewhere.

D-067 (supersedes D-021's original rule): an unfindable ToS page no
longer blocks AUTO on its own. D-021's original reasoning -- "insufficient
evidence to clear the app" -- conflated two different things: a vendor
that has deliberately gated automation (a real HITL, something a human
must act on) and credforge simply failing to *locate* a policy page that
may not meaningfully exist in the form this project looks for (found
live: several real, legitimate government API vendors publish a "Privacy
Policy and Important Notices" or similar page instead of anything titled
"Terms of Service," and some publish nothing distinct at all). The
uncertainty is never silently dropped -- `GateResult.tos_status` records
"unverifiable" explicitly, `tos_checked_url`/`tos_discovery_method` record
what was actually tried, and a `completeness_gaps` entry names the gap --
but it's treated the same way DISCOVERY_INCOMPLETE already is (D-029):
information a downstream consumer inherits, not a reason to stop the
whole app at a human.

GATE collects vendor-policy signals from every source it actually has,
not just the dedicated ToS page: DISCOVER's already-crawled docs page
(`discovery.docs_text`, zero extra fetch cost) is scanned for the same
flags, since a real developer-docs page frequently states pricing or
verification requirements directly. All signals found, from either
source, are collected before precedence is applied -- not "return on the
first hit." A found `prohibits_automation` signal still blocks
absolutely, regardless of which source stated it or whether the dedicated
ToS page was ever found at all -- that one signal's precedence and
blocking behavior are completely unchanged by D-067. See DECISIONS.md
D-040 and D-067.

DISCOVERY_INCOMPLETE (DISCOVER crawled a real API's docs but couldn't pin
down every expected field) is deliberately NOT a precondition that routes
to HITL, unlike DISCOVERY_FAILED. It's carried forward as a `completeness`
gap list instead -- a missing field in prose is not something a human
needs to unblock; see DECISIONS.md D-029.

Four flags -- requires_payment, requires_business_verification,
requires_sales_contact, requires_phone_verification -- are adjusted
before use: language scoped to a specific endpoint, data tier, or usage
class ("this is a premium endpoint", "for commercial use") is not the
same claim as an unscoped statement that access itself requires it ("API
access requires a paid plan"), and only the extractor's raw flag doesn't
distinguish them. The check runs once per source, upstream of any
individual flag -- not one override per flag -- because a real vendor
page can trip more than one of these four from adjacent, identically-
scoped clauses in the same sentence (D-058). prohibits_automation,
requires_captcha, and requires_sso_only are never adjusted: those are
structural facts about the signup mechanism itself, never legitimately
scoped to a paid tier, and always block. A registered SignupRecipe is
independent, out-of-band proof a vendor's free signup already works
end to end (D-052/D-053) -- for a recipe-backed vendor specifically, a
block from one of the four adjustable flags with no unscoped-access
language present is treated as a structural false positive even if no
keyword list happens to recognize the exact phrasing, logged loudly, not
silently. See DECISIONS.md D-057/D-058.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..enums import ApiStyle, PipelineStage, ReasonCode, Status
from ..models.state import ClassifyResult, CompletenessGap, DiscoveryResult, EvidenceItem, GateResult, TosStatus
from ..providers.fetch import FetchException, FetchProvider
from ..utils.domains import registrable_domain
from ..providers.llm import DiscoveryExtraction, Extractor, TosGateExtraction
from .explain import NULL_EXPLAIN, ExplainEvent, ExplainSink

# Fields a fully-populated DiscoveryExtraction would ideally have, and why
# a missing one isn't a human-blocking problem -- the downstream
# toolkit-generation agent can derive most of these from concrete endpoint
# examples elsewhere in the docs rather than from a single prose statement.
_EXPECTED_DISCOVERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("base_url", "not stated in prose on the crawled page; derivable from concrete endpoint examples elsewhere in the docs"),
    ("developer_portal_url", "not stated in prose on the crawled page"),
    ("rate_limit_notes", "not stated in prose on the crawled page; check response headers or a dedicated rate-limits page"),
    ("pagination_style_hint", "not stated in prose on the crawled page; derivable from example list-endpoint responses"),
    ("validation_endpoint", "not stated in prose on the crawled page; the downstream agent can pick any cheap read-only endpoint from the reference"),
)


# D-055: these two fields' *current design* assumes REST specifically --
# pagination_style_hint is a prose hint the extractor looks for in
# REST-shaped terms (query params, page numbers), and validation_endpoint
# is consumed by VALIDATE as a bare "METHOD /path" resolved against
# base_url with an always-GET request (pipeline/validate.py), which has
# no meaning for a GraphQL API's single POST-with-a-query-body endpoint.
# base_url, developer_portal_url, and rate_limit_notes stay universal --
# a GraphQL vendor has exactly one of the first and (usually) prose about
# the other two, same as a REST vendor; a missing one there is still a
# real gap. Suppressing only kicks in for a *confirmed* GraphQL API, never
# for UNKNOWN -- an unconfirmed style keeps today's REST-assuming
# behavior rather than guessing. See DECISIONS.md D-055.
_REST_ONLY_COMPLETENESS_FIELDS = frozenset({"pagination_style_hint", "validation_endpoint"})


def _completeness_gaps(extraction: DiscoveryExtraction, *, api_style: ApiStyle | None) -> list[CompletenessGap]:
    return [
        CompletenessGap(field=field_name, reason=reason)
        for field_name, reason in _EXPECTED_DISCOVERY_FIELDS
        if getattr(extraction, field_name) is None
        and not (api_style == ApiStyle.GRAPHQL and field_name in _REST_ONLY_COMPLETENESS_FIELDS)
    ]

# D-058 (generalizes D-057 from requires_payment alone to every flag that
# can legitimately be phrased in a scope-qualified way). Found live: the
# same Alpha Vantage sentence that produced a false requires_payment also
# produces a false requires_sales_contact once requires_payment alone was
# fixed -- "For commercial use, please contact sales" is the very next
# clause, scoped to commercial use the same way. Whack-a-mole per flag
# doesn't converge, so this check runs ONCE per signal source, upstream of
# any individual flag, not as N separate per-flag overrides.
#
# Only these four flags are ever adjusted -- prohibits_automation,
# requires_captcha, and requires_sso_only are deliberately excluded.
# Those three are structural facts about the signup mechanism itself (a
# form either has a CAPTCHA or doesn't; login is either SSO-only or
# isn't; automation is either prohibited or isn't) -- never legitimately
# "true only for a paid tier" the way a payment/business/sales/phone
# requirement genuinely can be. Generalizing scope-suppression to them
# was considered and rejected; see DECISIONS.md D-058.
_SCOPE_SUPPRESSIBLE_FLAGS: tuple[str, ...] = (
    "requires_payment",
    "requires_business_verification",
    "requires_sales_contact",
    "requires_phone_verification",
)

# Interpretation of "must not trigger on its own" (deliberately explicit,
# since the spec allows more than one reading): a scope qualifier ALONE
# is sufficient to suppress -- it does not additionally require free-tier
# evidence to also be present. FREE-TIER OVERRIDE is a second, independent
# path to the same suppression (covers a page whose blocking language
# doesn't literally match a scope-qualifier marker but where free-tier
# evidence is still present), not a precondition layered on top of it.
# UNSCOPED_ACCESS language always wins over both, checked first,
# regardless of scoped or free-tier language appearing elsewhere on the
# very same page -- a vendor that mentions a free tier AND separately
# states access itself requires payment/verification/a sales call is a
# real, mixed-tier vendor that still needs a human.
_SCOPE_QUALIFIER_MARKERS = (
    "this is a premium endpoint",
    "premium endpoint",
    "this endpoint",
    "this endpoint requires",
    "for commercial use",
    "realtime data",
    "historical data",
    "intraday data",
    "delayed data",
    "for higher limits",
    "enterprise customers",
    "premium membership plan",
    "upgrade to access",
)
_UNSCOPED_ACCESS_MARKERS = (
    "api access requires a paid plan",
    "you must subscribe to use the api",
    "no free tier",
    "a paid account is required to obtain an api key",
    "credit card required to sign up",
)
_FREE_TIER_OVERRIDE_MARKERS = (
    "free api key",
    "claim your free api key",
    "free tier",
    "get started for free",
    "no credit card required",
)

# D-081: found live -- TheCatAPI's real pricing table reads "$0.00/Month
# Free FREE cats to help developers learn. 10,000 requests a month", which
# matches none of the literal phrases above, so a genuine, permanent free
# tier didn't suppress requires_payment. A real pricing table's free-tier
# language is very often just its price ($0/$0.00) plus the word "free" as
# the tier's own label/heading -- not necessarily the phrase "free tier".
# Requires BOTH a zero-price-per-month pattern AND the word "free" within
# a short window (either order -- real tables show it both ways: TheCatAPI
# is price-then-label, Postmark's own pricing page is label-then-price,
# "Free $0.00 /mo") -- deliberately not a bare "free" or bare "$0" alone,
# since "free trial" (time-limited, still eventually paid) must NOT
# suppress on the word "free" alone, and a lone "$0.99"-shaped price
# mention elsewhere on the page is meaningless without "free" nearby.
_PRICE_SHAPED_FREE_TIER_RE = re.compile(
    r"\bfree\b[^.\n]{0,20}\$?0(?:\.00)?\s*/\s*(?:mo|month)\b"
    r"|\$?0(?:\.00)?\s*/\s*(?:mo|month)\b[^.\n]{0,20}\bfree\b",
    re.IGNORECASE,
)


def _adjust_for_scoped_gate_signals(
    signals: TosGateExtraction,
    *,
    source_text: str,
    developer_portal_url: str | None,
    is_recipe_backed: bool,
    identity_key: str,
    label: str,
    explain: ExplainSink,
) -> TosGateExtraction:
    """Suppresses any of _SCOPE_SUPPRESSIBLE_FLAGS this source flagged
    True, when that's explained by scope-qualified language rather than a
    genuine unscoped access requirement. Checked once, against the whole
    source text, upstream of the individual flags. See DECISIONS.md D-058
    for the real case (and the D-057 case it generalizes) this exists to
    fix, D-081 for the price-shaped free-tier extension, and the module
    docstring for why prohibits_automation/requires_captcha/
    requires_sso_only are never touched here."""
    active = [flag for flag in _SCOPE_SUPPRESSIBLE_FLAGS if getattr(signals, flag)]
    if not active:
        return signals

    lower = source_text.lower()
    if any(marker in lower for marker in _UNSCOPED_ACCESS_MARKERS):
        return signals  # a real, unscoped block -- never suppressed, recipe-backed or not

    scoped = any(marker in lower for marker in _SCOPE_QUALIFIER_MARKERS)
    free_tier = (
        any(marker in lower for marker in _FREE_TIER_OVERRIDE_MARKERS)
        or _PRICE_SHAPED_FREE_TIER_RE.search(lower) is not None
        or (developer_portal_url is not None and "api-key" in developer_portal_url.lower())
    )

    if scoped or free_tier:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message=(
                    f"D-058: suppressing scope-qualified signal(s) from {label}: {', '.join(active)} "
                    f"(scope_qualifier={scoped}, free_tier_evidence={free_tier})"
                ),
            )
        )
        return signals.model_copy(update={flag: False for flag in active})

    if is_recipe_backed:
        # D-058 structural guard: no keyword in either list happens to
        # match this particular phrasing, but a registered SignupRecipe
        # is independent, out-of-band proof this vendor's free signup
        # already works end to end -- and no unscoped-access language was
        # found above. A block from a scope-suppressible flag alone, on a
        # vendor we've already proven signs up for free, is a structural
        # false positive by construction, not a judgment call the keyword
        # lists need to have anticipated.
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message=(
                    f"D-058 STRUCTURAL FALSE POSITIVE: {label} flagged {', '.join(active)} with no "
                    "unscoped-access language and no recognized scope-qualifier or free-tier marker -- "
                    "suppressing anyway because a registered SignupRecipe already proves this vendor's "
                    "free signup works end to end"
                ),
            )
        )
        return signals.model_copy(update={flag: False for flag in active})

    return signals  # not explained by anything this override recognizes, and no structural proof either


_TOS_URL_GUESSES = (
    "/developers/terms",
    "/developer-terms",
    "/legal/developer-terms",
    "/terms-of-service",
    "/terms",
    "/tos",
    "/legal/terms",
    # D-059: underscore variants -- Alpha Vantage's real ToS is at
    # /terms_of_service/, which none of the hyphenated/short guesses
    # above ever matched. This is exactly the kind of miss link discovery
    # (below) exists to stop needing to anticipate one vendor at a time;
    # these four stay as the last-resort fallback for when link discovery
    # itself finds nothing.
    "/terms_of_service/",
    "/terms_and_conditions/",
    "/terms_of_use/",
    "/privacy_policy/",
)
_MIN_USABLE_TEXT_LENGTH = 200

# A "soft 404": HTTP 200 with body text that says the page doesn't exist.
# Plain substring match, not word-boundary regex (unlike D-022's hint
# matching) -- these are multi-word phrases, not single words like
# "captcha" that can collide inside an unrelated identifier, so the extra
# regex machinery isn't buying anything here. See DECISIONS.md D-035.
_SOFT_404_MARKERS = (
    "page not found",
    "we can't find",
    "we cannot find",
    "doesn't exist",
    "does not exist",
    "no longer available",
    "404 error",
    "404 not found",
    "there was an error and we couldn't load",  # Spotify's second, differently-worded soft-404 template
    "got tripped up",
)


def _looks_like_a_real_page(text: str) -> bool:
    lower = text[:500].lower()
    return not any(marker in lower for marker in _SOFT_404_MARKERS)


# D-059: real anchor-link discovery -- how a human actually finds a ToS
# page: they look at the footer. Guessing a fixed list of paths cannot
# generalize across real vendors (Alpha Vantage's real ToS lives at
# /terms_of_service/, which none of the seven pre-D-059 guesses, all
# hyphenated or bare, ever matched); link discovery scans real page
# content for real links instead of enumerating URL shapes in advance.
# The guess list is kept as a fallback, not replaced -- link discovery
# finds nothing on a page with no footer at all (a bare API endpoint
# domain with no marketing site, for instance).
_TOS_LINK_KEYWORDS = ("terms", "tos", "legal", "conditions", "agreement")


def _contains_tos_keyword(lower_text: str) -> bool:
    """Word-boundary match, not a bare substring check -- found live,
    scanning Alpha Vantage's real docs page: "tos" as a bare substring
    matches inside "ULTOSC" (a real Ultimate Oscillator API function name
    used throughout the page's example query URLs), producing false-
    positive candidate links that have nothing to do with a ToS page.
    Same class of bug, same fix, as D-022's `_contains_hint` in
    heuristic_extractor.py."""
    return any(re.search(rf"\b{re.escape(keyword)}\b", lower_text) for keyword in _TOS_LINK_KEYWORDS)


class _AnchorLinkParser(HTMLParser):
    """Collects every <a href> on the page paired with its visible text --
    stdlib only, no new dependency for a real, bounded parsing task."""

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


def _extract_tos_candidate_links(html_text: str, *, base_url: str, vendor_domain: str) -> list[str]:
    """Absolute URLs for every <a> whose href OR visible text contains a
    ToS/legal keyword AND whose registrable domain matches `vendor_domain`,
    in document order, deduplicated. Never raises -- malformed HTML
    degrades to an empty list (the caller falls back to the guess list),
    not a crash.

    The domain filter is not optional: a real vendor docs page routinely
    links to *other* companies' terms pages as data-source attribution --
    Alpha Vantage's own real docs page cites the Federal Reserve (FRED),
    the IMF, and Investopedia, each with a real, fetchable, keyword-
    matching "terms" link, ahead of Alpha Vantage's own real ToS link in
    document order. Without this filter, `_find_tos_page` would fetch and
    trust a third party's ToS as if it were the vendor's own -- wrong,
    and it would also mean credforge silently issuing real, unsolicited
    requests to unrelated third-party domains never mentioned as the
    resolved identity. See DECISIONS.md D-059's follow-up correction."""
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
        haystack = f"{href} {text}".lower()
        if not _contains_tos_keyword(haystack):
            continue
        absolute = urljoin(base_url, href)
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
        return url, result.text  # the requested URL, not result.final_url -- matches pre-D-059 behavior
    return None


async def _find_tos_page(
    domain: str, *, fetch: FetchProvider, docs_html: str | None = None, docs_url: str | None = None
) -> tuple[str, str, str] | None:
    """Returns (url, text, discovery_method) -- discovery_method is
    "link_discovery" or "guess_list", carried into GateResult.tos_discovery_method
    (D-067) so the artifact can say not just whether a ToS page was found,
    but how."""
    # Real link discovery first, from every real page already in hand or
    # cheap to fetch: the docs page DISCOVER already crawled (zero extra
    # fetch cost) and the resolved homepage (one extra fetch -- the other
    # place a footer commonly lives, and often the only one for a vendor
    # whose docs page has no footer at all).
    candidate_urls: list[str] = []
    seen_candidates: set[str] = set()

    def _add_candidates(html_text: str, *, base_url: str) -> None:
        for url in _extract_tos_candidate_links(html_text, base_url=base_url, vendor_domain=domain):
            if url not in seen_candidates:
                seen_candidates.add(url)
                candidate_urls.append(url)

    if docs_html and docs_url:
        _add_candidates(docs_html, base_url=docs_url)

    homepage_url = f"https://{domain}"
    try:
        homepage = await fetch.fetch(homepage_url, method="GET")
    except FetchException:
        homepage = None
    if homepage is not None and 200 <= homepage.status_code < 300 and homepage.text:
        _add_candidates(homepage.text, base_url=homepage.final_url or homepage_url)

    for url in candidate_urls:
        found = await _fetch_usable_page(url, fetch=fetch)
        if found is not None:
            return found[0], found[1], "link_discovery"

    # Fallback: link discovery found no candidates, or none of them
    # panned out (unreachable, too short, a soft 404) -- try the
    # conventional guesses, same behavior as before D-059.
    for path in _TOS_URL_GUESSES:
        found = await _fetch_usable_page(f"https://{domain}{path}", fetch=fetch)
        if found is not None:
            return found[0], found[1], "guess_list"
    return None


# (source_label, source_url, extraction) -- one entry per real source
# GATE actually scanned for vendor-policy signals this run.
_SignalSource = tuple[str, str, TosGateExtraction]

# Checked in the spec's mandated precedence order: (a) prohibition always
# outranks (b) payment/business/sales, which always outranks (c)
# phone/CAPTCHA/SSO-only. Applied across the UNION of every source's
# flags (see _blocking_gate_result), not per-source.
_PRECEDENCE: tuple[tuple[str, ReasonCode, str], ...] = (
    ("prohibits_automation", ReasonCode.TOS_PROHIBITS_AUTOMATION, "prohibits automated account creation"),
    ("requires_payment", ReasonCode.REQUIRES_PAYMENT, "requires payment for API access"),
    ("requires_business_verification", ReasonCode.REQUIRES_BUSINESS_VERIFICATION, "requires business verification"),
    ("requires_sales_contact", ReasonCode.REQUIRES_SALES_CONTACT, "requires contacting sales"),
    ("requires_phone_verification", ReasonCode.REQUIRES_PHONE_VERIFICATION, "requires phone verification"),
    ("requires_captcha", ReasonCode.REQUIRES_CAPTCHA, "requires solving a CAPTCHA"),
    ("requires_sso_only", ReasonCode.REQUIRES_SSO_ONLY, "signup is SSO-only"),
)


def _blocking_gate_result(
    sources: list[_SignalSource], *, completeness_gaps: list[CompletenessGap]
) -> GateResult | None:
    """Collects every source's flags first, then applies precedence across
    the union -- a signal found in ANY source (the docs page, the ToS
    page, or both) is checked, not just the first source scanned. Returns
    None only if no source yielded any blocking signal at all. See
    DECISIONS.md D-040."""

    def _first_hit(flag: str) -> _SignalSource | None:
        for source in sources:
            if getattr(source[2], flag):
                return source
        return None

    for flag, reason_code, claim in _PRECEDENCE:
        hit = _first_hit(flag)
        if hit is None:
            continue
        label, url, extraction = hit
        snippet = extraction.evidence_snippets[0] if extraction.evidence_snippets else ""
        return GateResult(
            status=Status.HITL,
            reason_code=reason_code,
            evidence=[EvidenceItem(claim=f"{label} {claim}", source_url=url, snippet=snippet)],
            completeness_gaps=completeness_gaps,
        )
    return None


async def gate(
    identity_key: str,
    *,
    discovery: DiscoveryResult,
    classify: ClassifyResult,
    fetch: FetchProvider,
    extractor: Extractor,
    explain: ExplainSink = NULL_EXPLAIN,
) -> GateResult:
    if discovery.reason_code == ReasonCode.DISCOVERY_FAILED:
        return GateResult(status=Status.HITL, reason_code=ReasonCode.DISCOVERY_FAILED)

    assert discovery.extraction is not None  # guaranteed once DISCOVERY_FAILED is ruled out

    if not discovery.extraction.has_public_api:
        return GateResult(status=Status.UNSUPPORTED, reason_code=ReasonCode.NO_PUBLIC_API)

    # DISCOVERY_INCOMPLETE is deliberately not checked here -- see the
    # module docstring and D-029. It only ever shapes `completeness_gaps`,
    # never the status/reason_code decision.
    completeness_gaps = _completeness_gaps(discovery.extraction, api_style=discovery.api_style)
    if completeness_gaps:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message=(
                    f"{len(completeness_gaps)} field(s) incomplete "
                    f"({', '.join(g.field for g in completeness_gaps)}) -- "
                    "carried forward as completeness gaps, not a HITL trigger"
                ),
            )
        )

    if classify.auth_scheme is None or classify.reason_code == ReasonCode.CLASSIFY_LOW_CONFIDENCE:
        return GateResult(
            status=Status.HITL,
            reason_code=ReasonCode.CLASSIFY_LOW_CONFIDENCE,
            completeness_gaps=completeness_gaps,
        )

    # Collect signals from every real source available before deciding
    # anything -- not "return on the first hit." The docs page DISCOVER
    # already crawled is scanned unconditionally (zero extra fetch cost,
    # already in hand); the dedicated ToS page is scanned too if findable.
    # See DECISIONS.md D-040.
    signal_sources: list[_SignalSource] = []

    # D-057/D-058: the developer_portal_url extractors like AnthropicExtractor
    # report is real signal too (Alpha Vantage's own is literally the
    # free-key signup page) -- available regardless of which source's
    # signals are being adjusted. Lazy import, same pattern RESOLVE
    # already uses for this same registry (D-048) -- avoids a hard
    # top-level dependency from GATE on PROVISION's recipe registry.
    from ..providers.signup_recipes import LIVE_SIGNUP_RECIPES

    developer_portal_url = discovery.extraction.developer_portal_url if discovery.extraction else None
    is_recipe_backed = identity_key in LIVE_SIGNUP_RECIPES

    if discovery.docs_text and discovery.docs_url:
        docs_signals = await extractor.extract_tos_gate_signals(
            tos_text=discovery.docs_text, tos_url=discovery.docs_url
        )
        docs_signals = _adjust_for_scoped_gate_signals(
            docs_signals,
            source_text=discovery.docs_text,
            developer_portal_url=developer_portal_url,
            is_recipe_backed=is_recipe_backed,
            identity_key=identity_key,
            label="developer docs",
            explain=explain,
        )
        signal_sources.append(("developer docs", discovery.docs_url, docs_signals))

    tos_found = await _find_tos_page(
        identity_key, fetch=fetch, docs_html=discovery.docs_text, docs_url=discovery.docs_url
    )
    tos_checked_url: str | None = None
    tos_discovery_method: str | None = None
    if tos_found is not None:
        tos_url, tos_text, tos_discovery_method = tos_found
        tos_checked_url = tos_url
        tos_signals = await extractor.extract_tos_gate_signals(tos_text=tos_text, tos_url=tos_url)
        tos_signals = _adjust_for_scoped_gate_signals(
            tos_signals,
            source_text=tos_text,
            developer_portal_url=developer_portal_url,
            is_recipe_backed=is_recipe_backed,
            identity_key=identity_key,
            label="Terms of Service",
            explain=explain,
        )
        signal_sources.append(("Terms of Service", tos_url, tos_signals))
    else:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message="could not locate a dedicated ToS/developer-agreement page",
            )
        )

    blocked = _blocking_gate_result(signal_sources, completeness_gaps=completeness_gaps)
    if blocked is not None:
        # D-067: prohibits_automation is a definitive, real verdict about
        # this vendor's ToS regardless of which source stated it (docs
        # page or the dedicated ToS page) -- verified_prohibited either
        # way, and this block is completely unaffected by D-067. Any other
        # blocking flag is a business/structural gate, independent of
        # whether the dedicated ToS page itself was ever found.
        tos_status: TosStatus = (
            "verified_prohibited"
            if blocked.reason_code == ReasonCode.TOS_PROHIBITS_AUTOMATION
            else ("verified_permitted" if tos_found is not None else "unverifiable")
        )
        blocked = blocked.model_copy(
            update={
                "tos_status": tos_status,
                "tos_checked_url": tos_checked_url,
                "tos_discovery_method": tos_discovery_method,
            }
        )
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message=f"blocked by gate check: {blocked.reason_code}",
            )
        )
        return blocked

    # D-067: no blocking signal anywhere scanned -- AUTO either way now,
    # whether or not the dedicated ToS page was found. An unfindable ToS
    # page is a detection limitation, not a vendor decision -- HITL is
    # reserved for what a human must actually resolve. The uncertainty is
    # never dropped: recorded as both a distinct tos_status and a
    # completeness gap, same pattern DISCOVERY_INCOMPLETE already
    # established (D-029).
    if tos_found is None:
        tos_status = "unverifiable"
        completeness_gaps = [
            *completeness_gaps,
            CompletenessGap(
                field="tos_status",
                reason=(
                    "no dedicated ToS/developer-agreement page could be located via link "
                    "discovery or common guesses -- automation permission could not be "
                    "independently confirmed one way or the other"
                ),
            ),
        ]
        evidence_item = EvidenceItem(
            claim=(
                "no dedicated ToS page found; no blocking signal found on the developer "
                "docs page either -- proceeding, uncertainty recorded rather than escalated (D-067)"
            ),
            source_url=discovery.docs_url or f"https://{identity_key}",
            snippet="",
        )
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message="no blocking signal found; ToS unverifiable -- D-067: AUTO, not HITL, gap recorded",
            )
        )
    else:
        tos_status = "verified_permitted"
        tos_url, tos_text, _tos_method = tos_found
        evidence_item = EvidenceItem(
            claim="ToS reviewed; no automation prohibition or gating signal found",
            source_url=tos_url,
            snippet=tos_text[:200].strip(),
        )
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message="cleared all checks -- eligible for AUTO",
            )
        )

    return GateResult(
        status=Status.AUTO,
        reason_code=ReasonCode.ELIGIBLE_AUTO,
        evidence=[evidence_item],
        completeness_gaps=completeness_gaps,
        tos_status=tos_status,
        tos_checked_url=tos_checked_url,
        tos_discovery_method=tos_discovery_method,
    )
