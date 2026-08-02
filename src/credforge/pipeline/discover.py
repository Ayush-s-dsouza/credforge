"""DISCOVER: crawl developer docs and extract structured API facts.

Approach: take RESOLVE's ranked, content-verified docs-URL candidates
(D-027) and try them in order; if a candidate is unreachable, or the real
extractor concludes there's no public API on that page after all, move to
the next candidate rather than failing outright (D-028) -- RESOLVE's own
cheap heuristic filtering can still be wrong about a specific vendor, and
a single bad pick shouldn't end the whole run. DISCOVER's own conventional
subdomain guesses are appended as a last-resort fallback, in case RESOLVE
passed nothing usable at all.

Exhausting every candidate has two distinct outcomes, not one (D-037):
if at least one candidate was actually fetched and read (a real
extraction happened, it just said has_public_api=False), that's a
genuine "no public API" verdict -- returned as such, not discarded. Only
when NOT ONE candidate ever yielded readable content at all does DISCOVER
give up with DISCOVERY_FAILED. Conflating these two was a real bug: it
made GATE's UNSUPPORTED status unreachable through the real pipeline,
since every genuinely-no-API vendor would exhaust every candidate with
has_public_api=False and fall through to DISCOVERY_FAILED (-> HITL)
instead of a clean, correct UNSUPPORTED.

DISCOVER only reports facts -- it's GATE's job to decide what an
incomplete or missing result means for the final status, not this stage's
(see DECISIONS.md D-029).

Whichever docs_url this stage actually settles on, its source tier
(D-049) and API style (D-055, REST/GraphQL/unknown) are computed exactly
once, here, and carried on the result -- CLASSIFY and EMIT both read
these fields instead of recomputing them from a possibly-different URL.
See DECISIONS.md D-054 for the real bug this replaced.
"""

from ..enums import ApiStyle, PipelineStage, ReasonCode
from ..models.state import DiscoveryResult
from ..providers.fetch import FetchException, FetchProvider
from ..providers.llm import Extractor
from .explain import NULL_EXPLAIN, ExplainEvent, ExplainSink
from .source_authority import classify_source_tier, describe_tier_match

_DOCS_SUBDOMAIN_GUESSES = ("docs", "developer", "developers")
_MIN_USABLE_TEXT_LENGTH = 200

# D-055: cheap, deterministic keyword signals -- not an LLM call, matching
# every other cheap-heuristic-first pattern in this stage (soft-404
# detection, negative-signal filtering). Checked against the URL and the
# first 5000 chars of crawled text, which is where a real vendor states
# its API's shape (in a title, an intro paragraph, or a getting-started
# code sample) if it states it at all. UNKNOWN (neither list matches) is
# the deliberately conservative default -- it changes nothing about how
# completeness gaps are read, unlike a confirmed GRAPHQL classification
# (gate.py suppresses REST-specific gaps only for a confirmed GRAPHQL,
# never for UNKNOWN).
_GRAPHQL_MARKERS = ("/graphql", "graphql api", "graphql endpoint", "graphql schema")
_REST_MARKERS = ("rest api", "restful api", "/openapi", "swagger")
_STYLE_TEXT_SAMPLE_LENGTH = 5000


def _detect_api_style(docs_url: str, docs_text: str) -> ApiStyle:
    haystack = f"{docs_url} {docs_text[:_STYLE_TEXT_SAMPLE_LENGTH]}".lower()
    if any(marker in haystack for marker in _GRAPHQL_MARKERS):
        return ApiStyle.GRAPHQL
    if any(marker in haystack for marker in _REST_MARKERS):
        return ApiStyle.REST
    return ApiStyle.UNKNOWN


async def _try_fetch_text(url: str, *, fetch: FetchProvider) -> str | None:
    try:
        result = await fetch.fetch(url, method="GET")
    except FetchException:
        return None
    if not (200 <= result.status_code < 300) or not result.text:
        return None
    if len(result.text) < _MIN_USABLE_TEXT_LENGTH:
        return None
    return result.text


def _candidate_list(identity_key: str, docs_url_candidates: list[str]) -> list[str]:
    seen = set(docs_url_candidates)
    ordered = list(docs_url_candidates)
    for subdomain in _DOCS_SUBDOMAIN_GUESSES:
        guess = f"https://{subdomain}.{identity_key}"
        if guess not in seen:
            ordered.append(guess)
            seen.add(guess)
    # Last-resort fallback: the bare marketing domain itself. A consumer
    # product with no dedicated docs/developer subdomain at all (no public
    # API to document) still usually has a real, readable homepage --
    # trying it is what actually lets DISCOVER report a genuine
    # has_public_api=False instead of DISCOVERY_FAILED when every
    # docs-shaped guess is unreachable (robots-disallowed, DNS failure,
    # or simply doesn't exist). Found live, investigating why Superhuman
    # -- seeded as the UNSUPPORTED test case -- was landing on
    # DISCOVERY_FAILED instead: all three subdomain guesses are
    # robots-disallowed, but https://superhuman.com itself returns a real,
    # substantial page. See DECISIONS.md.
    bare_domain = f"https://{identity_key}"
    if bare_domain not in seen:
        ordered.append(bare_domain)
        seen.add(bare_domain)
    return ordered


async def discover(
    identity_key: str,
    *,
    docs_url_candidates: list[str],
    fetch: FetchProvider,
    extractor: Extractor,
    explain: ExplainSink = NULL_EXPLAIN,
) -> DiscoveryResult:
    candidates = _candidate_list(identity_key, docs_url_candidates)
    tried: list[str] = []
    last_no_api_result: DiscoveryResult | None = None

    for docs_url in candidates:
        tried.append(docs_url)
        docs_text = await _try_fetch_text(docs_url, fetch=fetch)
        if docs_text is None:
            continue  # unreachable/too short -- try the next candidate

        extraction = await extractor.extract_discovery(docs_text=docs_text, docs_url=docs_url)
        if not extraction.has_public_api:
            # This specific candidate didn't pan out -- RESOLVE's cheap
            # filter can still be wrong about *this page*. Try the next
            # one rather than concluding "no public API" from one bad
            # pick. But remember this result: if every remaining candidate
            # also comes back has_public_api=False, that consistent real
            # verdict is what we report, not a DISCOVERY_FAILED that
            # implies nothing was ever actually read (D-037).
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.DISCOVER,
                    identity_key=identity_key,
                    message=f"{docs_url} did not yield a usable API page -- trying next candidate",
                )
            )
            last_no_api_result = DiscoveryResult(
                reason_code=None,
                docs_url=docs_url,
                docs_text=docs_text,
                extraction=extraction,
                source_tier=classify_source_tier(docs_url),
                source_tier_reason=describe_tier_match(docs_url),
                api_style=_detect_api_style(docs_url, docs_text),
            )
            continue

        reason_code = None
        if not extraction.base_url and not extraction.validation_endpoint:
            reason_code = ReasonCode.DISCOVERY_INCOMPLETE

        explain.emit(
            ExplainEvent(
                stage=PipelineStage.DISCOVER,
                identity_key=identity_key,
                message=(
                    f"crawled {docs_url} (candidate {len(tried)}/{len(candidates)}); "
                    f"has_public_api=True, base_url={extraction.base_url}"
                ),
            )
        )
        return DiscoveryResult(
            reason_code=reason_code,
            docs_url=docs_url,
            docs_text=docs_text,
            extraction=extraction,
            source_tier=classify_source_tier(docs_url),
            source_tier_reason=describe_tier_match(docs_url),
            api_style=_detect_api_style(docs_url, docs_text),
        )

    if last_no_api_result is not None:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.DISCOVER,
                identity_key=identity_key,
                message=(
                    f"exhausted all {len(tried)} candidate(s); at least one was actually read and had no "
                    "public API -- reporting has_public_api=False, not DISCOVERY_FAILED"
                ),
            )
        )
        return last_no_api_result

    explain.emit(
        ExplainEvent(
            stage=PipelineStage.DISCOVER,
            identity_key=identity_key,
            message=f"exhausted all {len(tried)} candidate docs URL(s), none were even readable",
        )
    )
    return DiscoveryResult(
        reason_code=ReasonCode.DISCOVERY_FAILED,
        detail=f"tried {len(tried)} candidate docs URL(s), none yielded a usable API page",
    )
