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

GATE fetches and checks the actual ToS/developer-agreement text itself
(a small set of common-URL guesses, same pattern as DISCOVER's
docs-subdomain guessing) -- it never assumes automation is permitted just
because no evidence of prohibition turned up elsewhere. If no ToS page can
be found at all, that is treated as insufficient evidence to clear the app
for AUTO, not as a pass. See DECISIONS.md D-021.

GATE collects vendor-policy signals from every source it actually has,
not just the dedicated ToS page: DISCOVER's already-crawled docs page
(`discovery.docs_text`, zero extra fetch cost) is scanned for the same
flags, since a real developer-docs page frequently states pricing or
verification requirements directly. All signals found, from either
source, are collected before precedence is applied -- not "return on the
first hit." TOS_UNVERIFIABLE ranks lowest in that precedence: it fires
only if the dedicated ToS page couldn't be found AND no blocking signal
was found anywhere else either. It is a detection failure ("couldn't
confirm"), not a vendor policy, and must never mask a real
payment/verification/CAPTCHA finding that's already in hand from the docs
page. See DECISIONS.md D-040.

DISCOVERY_INCOMPLETE (DISCOVER crawled a real API's docs but couldn't pin
down every expected field) is deliberately NOT a precondition that routes
to HITL, unlike DISCOVERY_FAILED. It's carried forward as a `completeness`
gap list instead -- a missing field in prose is not something a human
needs to unblock; see DECISIONS.md D-029.
"""

from ..enums import PipelineStage, ReasonCode, Status
from ..models.state import ClassifyResult, CompletenessGap, DiscoveryResult, EvidenceItem, GateResult
from ..providers.fetch import FetchException, FetchProvider
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


def _completeness_gaps(extraction: DiscoveryExtraction) -> list[CompletenessGap]:
    return [
        CompletenessGap(field=field_name, reason=reason)
        for field_name, reason in _EXPECTED_DISCOVERY_FIELDS
        if getattr(extraction, field_name) is None
    ]

_TOS_URL_GUESSES = (
    "/developers/terms",
    "/developer-terms",
    "/legal/developer-terms",
    "/terms-of-service",
    "/terms",
    "/tos",
    "/legal/terms",
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


async def _find_tos_page(domain: str, *, fetch: FetchProvider) -> tuple[str, str] | None:
    for path in _TOS_URL_GUESSES:
        url = f"https://{domain}{path}"
        try:
            result = await fetch.fetch(url, method="GET")
        except FetchException:
            continue
        if (
            200 <= result.status_code < 300
            and result.text
            and len(result.text) >= _MIN_USABLE_TEXT_LENGTH
            and _looks_like_a_real_page(result.text)
        ):
            return url, result.text
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
    completeness_gaps = _completeness_gaps(discovery.extraction)
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

    if discovery.docs_text and discovery.docs_url:
        docs_signals = await extractor.extract_tos_gate_signals(
            tos_text=discovery.docs_text, tos_url=discovery.docs_url
        )
        signal_sources.append(("developer docs", discovery.docs_url, docs_signals))

    tos_found = await _find_tos_page(identity_key, fetch=fetch)
    if tos_found is not None:
        tos_url, tos_text = tos_found
        tos_signals = await extractor.extract_tos_gate_signals(tos_text=tos_text, tos_url=tos_url)
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
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message=f"blocked by gate check: {blocked.reason_code}",
            )
        )
        return blocked

    if tos_found is None:
        # No blocking signal anywhere we looked, but the dedicated ToS
        # page specifically was never found -- TOS_UNVERIFIABLE ranks
        # lowest in precedence (see _blocking_gate_result), so it only
        # ever fires here, after every other signal source came back
        # clean. A clean docs-page scan is not sufficient evidence of a
        # clean ToS -- D-021's principle still holds.
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.GATE,
                identity_key=identity_key,
                message="no blocking signal found, but the dedicated ToS page is unverifiable -- cannot confirm automation is permitted",
            )
        )
        return GateResult(
            status=Status.HITL,
            reason_code=ReasonCode.TOS_UNVERIFIABLE,
            completeness_gaps=completeness_gaps,
        )

    tos_url, tos_text = tos_found
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
        evidence=[
            EvidenceItem(
                claim="ToS reviewed; no automation prohibition or gating signal found",
                source_url=tos_url,
                snippet=tos_text[:200].strip(),
            )
        ],
        completeness_gaps=completeness_gaps,
    )
