"""CLASSIFY: assign the final auth-scheme enum value.

Approach: if DISCOVER already determined no public API exists, the auth
scheme question doesn't apply -- short-circuit to NO_PUBLIC_API with full
confidence, no extractor call needed (this is a deterministic rule, not a
classification, and saves an LLM call on every app that's going to be
UNSUPPORTED anyway). Otherwise, hand the docs text to whichever Extractor
the factory chose and coerce its answer into the AuthScheme enum, flagging
low confidence rather than silently trusting a shaky guess.
"""

from ..enums import AuthScheme, PipelineStage, ReasonCode
from ..models.state import ClassifyResult
from ..providers.llm import DiscoveryExtraction, Extractor
from .explain import NULL_EXPLAIN, ExplainEvent, ExplainSink

# Below this, a classification is treated as a guess, not a decision --
# GATE routes it to HITL for human confirmation rather than auto-trusting
# it. The heuristic extractor always reports exactly 0.5 (see DECISIONS.md
# D-015), so with no ANTHROPIC_API_KEY configured, essentially every real
# auth-scheme classification lands here -- deliberately, not a bug.
CONFIDENCE_THRESHOLD = 0.6


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

    reason_code = None
    if auth_scheme is None or extraction.confidence < CONFIDENCE_THRESHOLD:
        reason_code = ReasonCode.CLASSIFY_LOW_CONFIDENCE

    explain.emit(
        ExplainEvent(
            stage=PipelineStage.CLASSIFY,
            identity_key=identity_key,
            message=f"classified auth_scheme={auth_scheme} confidence={extraction.confidence}",
        )
    )

    return ClassifyResult(
        reason_code=reason_code,
        auth_scheme=auth_scheme,
        redirect_uris_required=extraction.redirect_uris_required,
        scopes_available=extraction.scopes_available,
        confidence=extraction.confidence,
    )
