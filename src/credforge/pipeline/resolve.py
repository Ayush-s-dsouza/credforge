"""RESOLVE: app name -> canonical vendor.

Approach: heuristic confidence scoring over web search results, not an LLM
call -- RESOLVE's job (find/confirm a domain, decide if there's a single
confident answer) is well served by a small, fully deterministic, free
scoring function; the LLM-assisted extraction budget (Stage 2/3) is
reserved for the genuinely unstructured task of reading arbitrary docs
pages, which this isn't.

Flow:
1. Validate the input isn't malformed (empty/too long/control characters)
   -- reject before making a single provider call.
2. Search "<app> official website", group results by registrable domain,
   drop known aggregator/reference domains from candidacy entirely
   (DECISIONS.md D-025), merge same-brand TLD variants (e.g. github.com /
   github.blog) into one candidate rather than letting them compete
   (D-026), and score each remaining candidate.
3. Ambiguity is decided on the *unboosted* scores only -- if the top two
   candidates are too close, stop there without even attempting docs-URL
   discovery on a candidate we're not going to trust anyway.
4. Only once a single candidate clearly leads does RESOLVE try to find its
   developer-docs URL -- and this is a RANKING problem, not a first-match
   lookup (D-027): collect every plausible URL (search hits + conventional
   guesses), reject anything with a help/support/community/status
   signal outright, rank the rest by developer-signal strength, and accept
   only a candidate whose fetched content actually looks like API
   documentation. The full ranked, verified list is carried forward (not
   just the winner) so DISCOVER can fall back to the next-best candidate
   if the top one doesn't pan out (D-028). A confirmed docs URL is real
   corroborating evidence, so finding one raises that candidate's
   confidence before the final threshold check.
"""

import asyncio
import re
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from ..enums import PipelineStage, ReasonCode
from ..models.state import ResolveCandidate, ResolveResult
from ..providers.fetch import FetchException, FetchProvider
from ..providers.search import SearchProvider, SearchProviderError, SearchResult
from ..registry.identity import slugify, unresolved_identity_key
from ..utils.domains import parse_bare_domain, registrable_domain, subdomain_of
from .explain import NULL_EXPLAIN, ExplainEvent, ExplainSink
from .source_authority import describe_tier_match, tier_sort_key

# A domain below this confidence isn't worth listing as an alternate at all.
MIN_PLAUSIBLE_CONFIDENCE = 0.35

# If the top two candidates are within this margin of each other, treat it
# as ambiguous rather than picking a winner. Applied to *unboosted* scores
# only -- see the module docstring and DECISIONS.md D-026.
AMBIGUITY_MARGIN = 0.15

# Applied once, to the (already unambiguous) top candidate, if a real
# developer-docs page is found, ranked, and content-verified -- "material"
# per the spec's own language: large enough to rescue a borderline-low-
# confidence but otherwise-unambiguous top candidate, safe because it's
# applied strictly after the ambiguity check, never before it.
DOCS_URL_CONFIDENCE_BOOST = 0.15

_MAX_INPUT_LENGTH = 200

# Reference/aggregator sites that legitimately rank well for "<company>
# official website" queries without being a candidate identity themselves.
# Their hits are dropped from candidacy but stay in the result corpus
# (still counted in consensus-fraction denominators for real candidates),
# so they aren't simply discarded -- see DECISIONS.md D-025.
_AGGREGATOR_BLOCKLIST = frozenset(
    {
        "wikipedia.org",
        "archive.org",  # Wayback Machine snapshots surface the same reference-page problem as Wikipedia
        "crunchbase.com",
        "g2.com",
        "capterra.com",
        "linkedin.com",
        "producthunt.com",
        "glassdoor.com",
        "trustpilot.com",
        "medium.com",
        "github.io",
        "apps.apple.com",
        "play.google.com",
        "saashub.com",  # SaaS-comparison directory; same reference-page problem as g2.com/capterra.com
    }
)

# Preferred TLD when multiple same-brand domain variants are merged into
# one candidate (github.com vs github.blog) -- see DECISIONS.md D-026.
_PREFERRED_SUFFIXES = ("com",)

# --- Docs-URL ranking (DECISIONS.md D-027) -----------------------------

# A URL whose subdomain or first path segment matches one of these is
# never accepted as a docs candidate, full stop -- a support/help/status
# page is not developer documentation no matter how confidently it
# resolves. This is what stopped RESOLVE from picking help.salesforce.com.
_DOCS_NEGATIVE_SIGNALS = ("help", "support", "community", "status", "knowledgebase", "kb")

_DOCS_SUBDOMAIN_GUESSES = ("docs", "developer", "api")
_DOCS_PATH_GUESSES = ("/docs", "/developers")
_DOCS_SEARCH_QUERY_TEMPLATES = (
    "{app} developer API documentation",
    "{app} developer docs",
)

# --- Search-provider-failure fallback -----------------------------------
#
# "The search provider raised" is a categorically different failure from
# "the search succeeded and found nothing" (D-024's SearchProviderError
# split) -- and it must not just crash the whole app's pipeline run. Real
# batch data showed this happening at a genuinely bad rate (2 of 20 seed
# apps, 10%). The fallback: guess the app's slug under a few conventional
# TLDs and verify each by a real fetch, same "conventional guess, verified
# by content, never assumed" pattern already used everywhere else in this
# codebase (RESOLVE's own docs-URL guessing, GATE's ToS-URL guessing).
_FALLBACK_TLDS = (".com", ".io", ".app", ".dev")
_FALLBACK_MIN_TEXT_LENGTH = 200
# Deliberately below a typical real search-corroborated score (no rank, no
# consensus signal exists here) but high enough that a genuine hit with a
# real, verified docs page (+DOCS_URL_CONFIDENCE_BOOST) can still clear
# the default confidence_threshold -- the fallback can fully resolve a
# well-established vendor, not just produce a permanent HITL.
_FALLBACK_CONFIDENCE = 0.6

# A page must match at least this many *distinct* categories below to be
# accepted as real API documentation, not just a page that happens to
# mention "API" once in passing. Checked against fetched visible text
# (already HTML-stripped -- see D-023), so patterns are chosen to survive
# that conversion (no reliance on literal <code> tags, for instance).
_MIN_API_MARKER_HITS = 2
_API_MARKER_PATTERNS = (
    re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+/\S+"),
    re.compile(r"\bapi[\s_-]?reference\b", re.IGNORECASE),
    re.compile(r"\bapi[\s_-]?documentation\b", re.IGNORECASE),
    re.compile(r"\bauthenticat(e|ion)\b", re.IGNORECASE),
    re.compile(r"\bauthoriz(e|ation)\b", re.IGNORECASE),
    re.compile(r"\bopenapi\b", re.IGNORECASE),
    re.compile(r"\bswagger\b", re.IGNORECASE),
    re.compile(r"\bendpoints?\b", re.IGNORECASE),
    re.compile(r"\bcurl\s+-", re.IGNORECASE),
    re.compile(r"```"),
)


def _validate_input(app_name: str) -> str | None:
    cleaned = app_name.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_INPUT_LENGTH:
        return None
    if any(ord(ch) < 0x20 for ch in cleaned):
        return None
    return cleaned


def _domain_label(domain: str) -> str:
    return domain.split(".")[0]


def _pick_canonical_domain(domain_variants: list[str], hits_by_domain: dict[str, list[SearchResult]]) -> str:
    def _best_rank(domain: str) -> int:
        return min(r.rank for r in hits_by_domain[domain])

    for suffix in _PREFERRED_SUFFIXES:
        matches = [d for d in domain_variants if d.endswith(f".{suffix}")]
        if matches:
            return min(matches, key=_best_rank)
    return min(domain_variants, key=_best_rank)


def _score_candidates(app_name: str, results: list[SearchResult]) -> list[ResolveCandidate]:
    hits_by_domain: dict[str, list[SearchResult]] = {}
    for result in results:
        domain = registrable_domain(result.url)
        if domain in _AGGREGATOR_BLOCKLIST:
            continue  # never its own candidate; still counted in `total` below
        hits_by_domain.setdefault(domain, []).append(result)

    # Merge same-brand TLD variants (github.com / github.blog) into one
    # family, keyed by the shared second-level label -- corroboration for
    # a single candidate, not a competing one.
    families: dict[str, list[str]] = {}
    for domain in hits_by_domain:
        families.setdefault(_domain_label(domain), []).append(domain)

    app_slug = slugify(app_name)
    total = len(results)
    candidates: list[ResolveCandidate] = []

    for label, domain_variants in families.items():
        canonical_domain = _pick_canonical_domain(domain_variants, hits_by_domain)
        merged_hits = sorted(
            (hit for d in domain_variants for hit in hits_by_domain[d]), key=lambda r: r.rank
        )
        top_hit = merged_hits[0]

        rank_weight = 1.0 / top_hit.rank
        consensus_fraction = len(merged_hits) / total
        name_similarity = SequenceMatcher(None, app_slug, label).ratio()

        confidence = 0.5 * name_similarity + 0.3 * rank_weight + 0.2 * consensus_fraction
        candidates.append(
            ResolveCandidate(
                domain=canonical_domain,
                confidence=round(min(confidence, 1.0), 4),
                evidence_url=top_hit.url,
                evidence_snippet=top_hit.snippet,
            )
        )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def _url_signal_tokens(url: str) -> tuple[str, str]:
    """(subdomain first label, path first segment), both lowercased."""
    subdomain_label = subdomain_of(url).split(".")[0].lower()
    path = urlsplit(url).path.strip("/")
    path_first_segment = path.split("/")[0].lower() if path else ""
    return subdomain_label, path_first_segment


def _is_negative_docs_signal(url: str) -> bool:
    subdomain_label, path_first_segment = _url_signal_tokens(url)
    return subdomain_label in _DOCS_NEGATIVE_SIGNALS or path_first_segment in _DOCS_NEGATIVE_SIGNALS


def _looks_like_api_docs(text: str) -> bool:
    distinct_hits = sum(1 for pattern in _API_MARKER_PATTERNS if pattern.search(text))
    return distinct_hits >= _MIN_API_MARKER_HITS


def _conventional_docs_guesses(domain: str) -> list[str]:
    """The no-search-required half of docs-candidate collection: pure
    conventional subdomain/path guesses off a known domain. Split out so
    the search-provider-failure fallback (which by definition can't issue
    another search call) can still attempt docs discovery."""
    seen: set[str] = set()
    urls: list[str] = []
    for sub in _DOCS_SUBDOMAIN_GUESSES:
        guess = f"https://{sub}.{domain}"
        if guess not in seen:
            urls.append(guess)
            seen.add(guess)
    for path in _DOCS_PATH_GUESSES:
        guess = f"https://{domain}{path}"
        if guess not in seen:
            urls.append(guess)
            seen.add(guess)
    return urls


async def _collect_docs_candidate_urls(
    app_name: str, domain: str, *, search: SearchProvider, explain: ExplainSink = NULL_EXPLAIN
) -> list[str]:
    """Every plausible docs URL from search + conventional guesses, deduped.
    Order here doesn't matter -- ranking happens in the caller.

    The identification search (`resolve()`'s own "<app> official website"
    call) already has a SearchProviderError fallback (D-024) -- this,
    the *second*, later search (docs-URL discovery) did not, and a real
    provider failure here used to propagate all the way out of resolve()
    uncaught, crashing the whole app's batch iteration with no artifact at
    all. Degrading to conventional guesses only (never raising) is the
    same "provider failure isn't fatal" principle D-024 already
    established, applied to the call site that was missing it.
    """
    seen: set[str] = set()
    urls: list[str] = []

    for template in _DOCS_SEARCH_QUERY_TEMPLATES:
        query = template.format(app=app_name)
        try:
            results = await search.search(query, count=8)
        except SearchProviderError as exc:
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.RESOLVE,
                    identity_key=domain,
                    message=f"docs-URL search failed ({exc}) -- continuing with conventional guesses only",
                )
            )
            continue
        for r in results:
            if registrable_domain(r.url) == domain and r.url not in seen:
                urls.append(r.url)
                seen.add(r.url)

    for guess in _conventional_docs_guesses(domain):
        if guess not in seen:
            urls.append(guess)
            seen.add(guess)

    return urls


# Bounded, not unbounded: every candidate in this list targets the same
# app's registrable domain (search hits filtered to it, plus conventional
# same-domain guesses), so the shared per-domain TokenBucket
# (net/rate_limiter.py, keyed by registrable domain) still serializes most
# of these regardless of how many run "concurrently" -- this cap exists to
# avoid firing an unbounded burst of coroutines at once, not because it
# unlocks true parallel network I/O for the common single-domain case. See
# DECISIONS.md D-056 for the measured effect, which is real but smaller
# than the number of candidates would suggest.
_MAX_CONCURRENT_DOCS_PROBES = 5


async def _verify_one_candidate(url: str, *, fetch: FetchProvider) -> tuple[str, str] | None:
    try:
        fetched = await fetch.fetch(url, method="GET")
    except FetchException:
        return None
    if not (200 <= fetched.status_code < 300) or not fetched.text:
        return None
    if not _looks_like_api_docs(fetched.text):
        return None

    # D-054: describe the tier of `fetched.final_url` -- the URL actually
    # returned and used downstream -- never the pre-redirect `url` a
    # conventional guess started from. A real, live case: the guess
    # "https://developers.linear.app" (a HIGH-tier subdomain) redirects to
    # "https://linear.app/developers" (a HIGH-tier *path*, once D-054 also
    # fixed the path/subdomain asymmetry -- but LOW under the old rule).
    # Computing the reason on `url` and storing it against `final_url`
    # would silently describe a URL that isn't the one anything downstream
    # actually reads.
    tier_desc = describe_tier_match(fetched.final_url)
    marker_hits = [p.pattern for p in _API_MARKER_PATTERNS if p.search(fetched.text)]
    reason = f"{tier_desc}; matched content markers: {', '.join(marker_hits[:4])}"
    return (fetched.final_url, reason)


async def _rank_and_verify_docs_candidates(
    candidate_urls: list[str], *, fetch: FetchProvider
) -> list[tuple[str, str]]:
    """Filters out negative-signal URLs, ranks the rest by three-tier
    source authority (D-049: official reference > general docs/guide >
    everything else -- blogs, tutorials, forums), then fetches every
    ranked candidate (bounded concurrency, D-056) and keeps only the ones
    whose *content* actually looks like API documentation. A stable sort
    means within a tier, candidates keep whatever relative order they
    arrived in (search rank, then conventional guesses); `asyncio.gather`
    preserves that same order in its results regardless of which fetch
    actually completes first. Returns (url, reason) pairs, best first --
    `reason` names the specific tier and matched signal, for
    evidence/auditability.
    """
    filtered = [u for u in candidate_urls if not _is_negative_docs_signal(u)]
    filtered.sort(key=tier_sort_key)

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOCS_PROBES)

    async def _bounded(url: str) -> tuple[str, str] | None:
        async with semaphore:
            return await _verify_one_candidate(url, fetch=fetch)

    results = await asyncio.gather(*(_bounded(url) for url in filtered))
    return [r for r in results if r is not None]


async def _discover_docs_candidates(
    app_name: str, domain: str, *, search: SearchProvider, fetch: FetchProvider, explain: ExplainSink = NULL_EXPLAIN
) -> list[tuple[str, str]]:
    candidate_urls = await _collect_docs_candidate_urls(app_name, domain, search=search, explain=explain)
    return await _rank_and_verify_docs_candidates(candidate_urls, fetch=fetch)


async def _search_failure_fallback(app_name: str, *, fetch: FetchProvider) -> list[ResolveCandidate]:
    """Guess the app's slug under a few conventional TLDs and verify each
    by a real fetch -- only reached when the search provider itself
    failed (raised), not when it succeeded and found nothing."""
    slug = slugify(app_name).replace("-", "")
    candidates: list[ResolveCandidate] = []
    for tld in _FALLBACK_TLDS:
        domain = f"{slug}{tld}"
        url = f"https://{domain}"
        try:
            result = await fetch.fetch(url, method="GET")
        except FetchException:
            continue
        if not (200 <= result.status_code < 300) or not result.text:
            continue
        if len(result.text) < _FALLBACK_MIN_TEXT_LENGTH:
            continue
        candidates.append(
            ResolveCandidate(
                domain=domain,
                confidence=_FALLBACK_CONFIDENCE,
                evidence_url=url,
                evidence_snippet=result.text[:200].strip(),
            )
        )
    return candidates


async def _resolve_via_search_failure_fallback(
    app_name: str,
    identity_key: str,
    *,
    fetch: FetchProvider,
    confidence_threshold: float,
    explain: ExplainSink,
) -> ResolveResult:
    candidates = await _search_failure_fallback(app_name, fetch=fetch)
    if not candidates:
        return ResolveResult(resolved=False, reason_code=ReasonCode.RESOLVE_NOT_FOUND)

    if len(candidates) > 1:
        # Multiple conventional-TLD domains all responding is itself
        # ambiguous -- there's no rank or consensus signal here to break
        # the tie, unlike the normal search-driven path. Never guess.
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.RESOLVE,
                identity_key=identity_key,
                message=(
                    f"{len(candidates)} conventional-TLD domains all responded "
                    f"({', '.join(c.domain for c in candidates)}) -- ambiguous without search corroboration"
                ),
            )
        )
        return ResolveResult(resolved=False, reason_code=ReasonCode.RESOLVE_AMBIGUOUS, alternates=candidates)

    top = candidates[0]

    # Docs discovery here is search-free by necessity -- search is known
    # to be down, so only the conventional subdomain/path guesses apply.
    verified_docs = await _rank_and_verify_docs_candidates(_conventional_docs_guesses(top.domain), fetch=fetch)
    docs_url_candidates = [url for url, _ in verified_docs]
    docs_url = docs_url_candidates[0] if docs_url_candidates else None
    docs_url_reason = verified_docs[0][1] if verified_docs else None

    effective_confidence = top.confidence
    if docs_url:
        effective_confidence = round(min(top.confidence + DOCS_URL_CONFIDENCE_BOOST, 1.0), 4)
    top.confidence = effective_confidence

    if effective_confidence < confidence_threshold:
        return ResolveResult(resolved=False, reason_code=ReasonCode.RESOLVE_LOW_CONFIDENCE, alternates=[top])

    top.docs_url = docs_url
    top.docs_url_candidates = docs_url_candidates
    top.docs_url_reason = docs_url_reason
    explain.emit(
        ExplainEvent(
            stage=PipelineStage.RESOLVE,
            identity_key=top.domain,
            message=(
                f"resolved via conventional-domain fallback to {top.domain} "
                f"(confidence {effective_confidence}); docs_url={docs_url or 'not confirmed'}"
            ),
        )
    )
    return ResolveResult(resolved=True, chosen=top, alternates=[])


# --- Recipe-pinned identity (DECISIONS.md D-048) -----------------------
#
# A registered SignupRecipe already encodes vendor identity: someone read
# that vendor's real signup page and its real docs page to build it, so
# the domain and the docs URL are already known, verified facts -- not
# something RESOLVE should re-derive via search. This pins IDENTITY ONLY:
# DISCOVER/CLASSIFY/GATE still run for real against the pinned docs_url,
# and GATE's verdict is still fully binding. See _resolve_bare_domain and
# `resolve()` for where that boundary is enforced (a pinned candidate is
# handed to the exact same downstream stages as any other RESOLVE result,
# nothing about GATE's own logic is touched).


def _match_recipe_identity(app_name: str) -> tuple[str, str] | None:
    """(domain, docs_url) if `app_name` clearly identifies a vendor with a
    registered live SignupRecipe -- either the exact domain, or the
    domain's own label appearing in the slugified input (`"NASA API"` ->
    `"nasa-api"` contains `"nasa"`, the label of `"nasa.gov"`). Lazy
    import: signup_recipes.py itself has no forced dependency on the
    `live` extra (playwright is only ever imported inside a method body),
    but keeping this import local to the one function that needs it keeps
    that guarantee visible at the call site, not just true by accident."""
    from ..providers.signup_recipes import LIVE_SIGNUP_RECIPES

    bare = parse_bare_domain(app_name)
    slug = slugify(app_name)
    for domain, recipe in LIVE_SIGNUP_RECIPES.items():
        if bare == domain or _domain_label(domain) in slug:
            return domain, recipe.docs_url
    return None


def _pinned_candidate(domain: str, docs_url: str | None, *, evidence_snippet: str, docs_url_reason: str) -> ResolveCandidate:
    return ResolveCandidate(
        domain=domain,
        docs_url=docs_url,
        docs_url_candidates=[docs_url] if docs_url else [],
        docs_url_reason=docs_url_reason if docs_url else None,
        confidence=1.0,
        evidence_url=docs_url or f"https://{domain}",
        evidence_snippet=evidence_snippet,
    )


# --- Bare domain trust (DECISIONS.md D-047) -----------------------------


async def _resolve_bare_domain(
    domain: str,
    *,
    search: SearchProvider,
    fetch: FetchProvider,
    explain: ExplainSink,
) -> ResolveResult:
    """The input already IS a domain -- searching for "who this vendor is"
    would be searching for an answer already in hand. Skips candidate
    scoring entirely; docs-URL discovery still runs for real (a bare
    domain says nothing about where its docs live)."""
    explain.emit(
        ExplainEvent(
            stage=PipelineStage.RESOLVE,
            identity_key=domain,
            message=f"input is an exact domain ({domain}) -- trusting it directly, skipping candidate search",
        )
    )

    verified_docs = await _discover_docs_candidates(domain, domain, search=search, fetch=fetch, explain=explain)
    docs_url_candidates = [url for url, _ in verified_docs]
    docs_url = docs_url_candidates[0] if docs_url_candidates else None
    docs_url_reason = verified_docs[0][1] if verified_docs else None

    chosen = ResolveCandidate(
        domain=domain,
        docs_url=docs_url,
        docs_url_candidates=docs_url_candidates,
        docs_url_reason=docs_url_reason,
        confidence=1.0,
        evidence_url=f"https://{domain}",
        evidence_snippet="exact domain supplied as input",
    )
    explain.emit(
        ExplainEvent(
            stage=PipelineStage.RESOLVE,
            identity_key=domain,
            message=f"resolved to {domain} (confidence 1.0, exact input); docs_url={docs_url or 'not confirmed'}",
        )
    )
    return ResolveResult(resolved=True, chosen=chosen, alternates=[])


async def resolve(
    app_name: str,
    *,
    search: SearchProvider,
    fetch: FetchProvider,
    confidence_threshold: float = 0.7,
    explain: ExplainSink = NULL_EXPLAIN,
) -> ResolveResult:
    cleaned = _validate_input(app_name)
    if cleaned is None:
        return ResolveResult(resolved=False, reason_code=ReasonCode.MALFORMED_INPUT)

    identity_key = unresolved_identity_key(cleaned)

    recipe_match = _match_recipe_identity(cleaned)
    if recipe_match:
        domain, docs_url = recipe_match
        chosen = _pinned_candidate(
            domain,
            docs_url,
            evidence_snippet=f"identity pinned by registered SignupRecipe for {domain}",
            docs_url_reason="pinned by registered SignupRecipe -- known, real developer-docs URL, not re-derived",
        )
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.RESOLVE,
                identity_key=domain,
                message=(
                    f"recipe-pinned identity: {domain} (docs_url={docs_url}) -- "
                    "identity only, GATE still evaluates independently and its verdict is still binding"
                ),
            )
        )
        return ResolveResult(resolved=True, chosen=chosen, alternates=[])

    bare_domain = parse_bare_domain(cleaned)
    if bare_domain:
        return await _resolve_bare_domain(bare_domain, search=search, fetch=fetch, explain=explain)

    try:
        results = await search.search(f"{cleaned} official website", count=8)
    except SearchProviderError as exc:
        # The search provider itself failed (raised) -- a categorically
        # different failure from "searched successfully, found nothing"
        # (D-024). Real batch data showed this at a genuinely bad rate
        # (2 of 20 seed apps, 10% -- the worst number in OPS.md), and
        # previously this simply crashed the app's whole pipeline run
        # with no artifact at all. Fall back to conventional domain
        # construction, verified by fetch, before giving up.
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.RESOLVE,
                identity_key=identity_key,
                message=f"search provider failed ({exc}) -- falling back to conventional domain construction",
            )
        )
        return await _resolve_via_search_failure_fallback(
            cleaned, identity_key, fetch=fetch, confidence_threshold=confidence_threshold, explain=explain
        )

    if not results:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.RESOLVE,
                identity_key=identity_key,
                message="search returned no results at all",
            )
        )
        return ResolveResult(resolved=False, reason_code=ReasonCode.RESOLVE_NOT_FOUND)

    candidates = _score_candidates(cleaned, results)
    top = candidates[0]
    rest = candidates[1:]

    explain.emit(
        ExplainEvent(
            stage=PipelineStage.RESOLVE,
            identity_key=identity_key,
            message=f"scored {len(candidates)} candidate domain(s); top={top.domain} ({top.confidence})",
            detail={"candidates": [(c.domain, c.confidence) for c in candidates[:5]]},
        )
    )

    if top.confidence < MIN_PLAUSIBLE_CONFIDENCE:
        return ResolveResult(
            resolved=False, reason_code=ReasonCode.RESOLVE_NOT_FOUND, alternates=candidates[:3]
        )

    # Ambiguity is decided on unboosted scores -- a docs-URL boost must
    # never be able to manufacture a false top-two tie. See D-026.
    is_ambiguous = (
        bool(rest)
        and rest[0].confidence >= MIN_PLAUSIBLE_CONFIDENCE
        and (top.confidence - rest[0].confidence) < AMBIGUITY_MARGIN
    )
    if is_ambiguous:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.RESOLVE,
                identity_key=identity_key,
                message=(
                    f"top two candidates too close ({top.confidence} vs "
                    f"{rest[0].confidence}) -- not guessing"
                ),
            )
        )
        return ResolveResult(
            resolved=False, reason_code=ReasonCode.RESOLVE_AMBIGUOUS, alternates=candidates[:3]
        )

    # Only reachable once a single candidate clearly leads -- now it's
    # worth spending real search+fetch calls to rank and verify its docs.
    verified_docs = await _discover_docs_candidates(cleaned, top.domain, search=search, fetch=fetch, explain=explain)
    docs_url_candidates = [url for url, _ in verified_docs]
    docs_url = docs_url_candidates[0] if docs_url_candidates else None
    docs_url_reason = verified_docs[0][1] if verified_docs else None

    effective_confidence = top.confidence
    if docs_url:
        effective_confidence = round(min(top.confidence + DOCS_URL_CONFIDENCE_BOOST, 1.0), 4)
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.RESOLVE,
                identity_key=identity_key,
                message=(
                    f"confirmed docs_url={docs_url} ({docs_url_reason}); confidence boosted "
                    f"{top.confidence} -> {effective_confidence}; "
                    f"{len(docs_url_candidates)} verified candidate(s) total"
                ),
            )
        )
    top.confidence = effective_confidence

    if effective_confidence < confidence_threshold:
        return ResolveResult(
            resolved=False, reason_code=ReasonCode.RESOLVE_LOW_CONFIDENCE, alternates=candidates[:3]
        )

    top.docs_url = docs_url
    top.docs_url_candidates = docs_url_candidates
    top.docs_url_reason = docs_url_reason
    explain.emit(
        ExplainEvent(
            stage=PipelineStage.RESOLVE,
            identity_key=top.domain,
            message=f"resolved to {top.domain} (confidence {effective_confidence}); docs_url={docs_url or 'not confirmed'}",
        )
    )
    return ResolveResult(resolved=True, chosen=top, alternates=rest[:2])
