"""CLASSIFY: assign the final auth-scheme enum value.

Approach: if DISCOVER already determined no public API exists, the auth
scheme question doesn't apply -- short-circuit to NO_PUBLIC_API with full
confidence, no extractor call needed (this is a deterministic rule, not a
classification, and saves an LLM call on every app that's going to be
UNSUPPORTED anyway). Otherwise, hand the docs text to whichever Extractor
the factory chose and coerce its answer into the AuthScheme enum, flagging
low confidence rather than silently trusting a shaky guess.

Source-authority weighting (D-049): the extractor's raw confidence
measures how clearly the *page* could be read, not whether the *page* was
authoritative -- a Trailhead tutorial can be extracted just as confidently
as a real API reference. `source_tier` (computed here, from whichever
docs_url DISCOVER actually used -- may not be RESOLVE's top pick, D-028)
adjusts the raw number materially before it's compared against
CONFIDENCE_THRESHOLD, and is recorded on the result so a HIGH-tier-derived
0.6 and a LOW-tier-derived 0.6 are no longer indistinguishable.
"""

from ..enums import AuthScheme, PipelineStage, ReasonCode, SourceTier
from ..models.state import ClassifyResult
from ..providers.llm import DiscoveryExtraction, Extractor
from .explain import NULL_EXPLAIN, ExplainEvent, ExplainSink
from .source_authority import classify_source_tier

# Below this, a classification is treated as a guess, not a decision --
# GATE routes it to HITL for human confirmation rather than auto-trusting
# it. The heuristic extractor always reports exactly 0.5 (see DECISIONS.md
# D-015), so with no ANTHROPIC_API_KEY configured, essentially every real
# auth-scheme classification lands here -- deliberately, not a bug.
CONFIDENCE_THRESHOLD = 0.6

# Applied to the extractor's raw confidence, additively, before the
# threshold check -- deliberately asymmetric: a HIGH-tier source earns
# only a small reward (it was probably going to be trusted anyway), but a
# LOW-tier source takes a real, threshold-crossing penalty, because that's
# the actual failure mode this exists to fix (a confidently-read tutorial
# passing the same bar as a confidently-read reference). See DECISIONS.md
# D-049 for why these specific values and what was rejected.
_TIER_CONFIDENCE_ADJUSTMENT: dict[SourceTier, float] = {
    SourceTier.HIGH: 0.05,
    SourceTier.MEDIUM: 0.0,
    SourceTier.LOW: -0.15,
}


def _coerce_auth_scheme(raw: str) -> AuthScheme | None:
    try:
        return AuthScheme(raw)
    except ValueError:
        return None


async def classify(
    identity_key: str,
    *,
    docs_text: str,
    docs_url: str,
    discovery: DiscoveryExtraction,
    extractor: Extractor,
    explain: ExplainSink = NULL_EXPLAIN,
) -> ClassifyResult:
    if not discovery.has_public_api:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.CLASSIFY,
                identity_key=identity_key,
                message="DISCOVER found no public API -- auth scheme doesn't apply",
            )
        )
        return ClassifyResult(auth_scheme=AuthScheme.NO_PUBLIC_API, confidence=1.0)

    extraction = await extractor.extract_classification(
        docs_text=docs_text, docs_url=docs_url, discovery=discovery
    )
    auth_scheme = _coerce_auth_scheme(extraction.auth_scheme)

    source_tier = classify_source_tier(docs_url)
    adjusted_confidence = round(
        min(max(extraction.confidence + _TIER_CONFIDENCE_ADJUSTMENT[source_tier], 0.0), 1.0), 4
    )

    reason_code = None
    if auth_scheme is None or adjusted_confidence < CONFIDENCE_THRESHOLD:
        reason_code = ReasonCode.CLASSIFY_LOW_CONFIDENCE

    explain.emit(
        ExplainEvent(
            stage=PipelineStage.CLASSIFY,
            identity_key=identity_key,
            message=(
                f"classified auth_scheme={auth_scheme} raw_confidence={extraction.confidence} "
                f"source_tier={source_tier.value} adjusted_confidence={adjusted_confidence}"
            ),
        )
    )

    return ClassifyResult(
        reason_code=reason_code,
        auth_scheme=auth_scheme,
        redirect_uris_required=extraction.redirect_uris_required,
        scopes_available=extraction.scopes_available,
        confidence=adjusted_confidence,
        source_tier=source_tier,
    )
