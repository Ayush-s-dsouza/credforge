import pytest

from credforge.enums import AuthScheme, ReasonCode, SourceTier
from credforge.pipeline.classify import classify
from credforge.providers.llm import ClassifyExtraction, DiscoveryExtraction


class FakeExtractor:
    def __init__(self, result: ClassifyExtraction) -> None:
        self._result = result
        self.calls = 0

    async def extract_discovery(self, **kwargs):
        raise NotImplementedError

    async def extract_classification(self, *, docs_text, docs_url, discovery) -> ClassifyExtraction:
        self.calls += 1
        return self._result

    async def extract_tos_gate_signals(self, **kwargs):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_no_public_api_short_circuits_without_calling_the_extractor() -> None:
    discovery = DiscoveryExtraction(has_public_api=False)
    extractor = FakeExtractor(ClassifyExtraction(auth_scheme="api_key", confidence=0.9))

    result = await classify(
        "example.com", docs_text="x", docs_url="https://example.com", discovery=discovery, extractor=extractor
    )

    assert result.auth_scheme == AuthScheme.NO_PUBLIC_API
    assert result.confidence == 1.0
    assert result.reason_code is None
    assert extractor.calls == 0


@pytest.mark.parametrize("scheme", list(AuthScheme))
@pytest.mark.asyncio
async def test_classifies_every_auth_scheme_value_with_high_confidence(scheme: AuthScheme) -> None:
    discovery = DiscoveryExtraction(has_public_api=True)
    extractor = FakeExtractor(ClassifyExtraction(auth_scheme=scheme.value, confidence=0.95))

    result = await classify(
        "example.com", docs_text="x", docs_url="https://example.com", discovery=discovery, extractor=extractor
    )

    assert result.auth_scheme == scheme
    assert result.reason_code is None
    assert extractor.calls == 1


@pytest.mark.asyncio
async def test_low_confidence_is_flagged_not_silently_trusted() -> None:
    # This is the expected default with the heuristic extractor (always 0.5).
    discovery = DiscoveryExtraction(has_public_api=True)
    extractor = FakeExtractor(ClassifyExtraction(auth_scheme="oauth2_auth_code", confidence=0.5))

    result = await classify(
        "example.com", docs_text="x", docs_url="https://example.com", discovery=discovery, extractor=extractor
    )

    assert result.auth_scheme == AuthScheme.OAUTH2_AUTH_CODE
    assert result.reason_code == ReasonCode.CLASSIFY_LOW_CONFIDENCE


@pytest.mark.asyncio
async def test_unparseable_auth_scheme_string_is_flagged_not_crashed() -> None:
    # Failure drill: a malformed/unexpected string from the extractor (LLM
    # hallucination, or a heuristic bug) must degrade to "needs a human,"
    # never raise and never silently default to a specific scheme.
    discovery = DiscoveryExtraction(has_public_api=True)
    extractor = FakeExtractor(ClassifyExtraction(auth_scheme="totally_not_a_real_scheme", confidence=0.95))

    result = await classify(
        "example.com", docs_text="x", docs_url="https://example.com", discovery=discovery, extractor=extractor
    )

    assert result.auth_scheme is None
    assert result.reason_code == ReasonCode.CLASSIFY_LOW_CONFIDENCE


@pytest.mark.asyncio
async def test_source_tier_is_recorded_on_the_result() -> None:
    discovery = DiscoveryExtraction(has_public_api=True)
    extractor = FakeExtractor(ClassifyExtraction(auth_scheme="api_key", confidence=0.7))

    result = await classify(
        "example.com", docs_text="x", docs_url="https://developer.example.com/api/reference",
        discovery=discovery, extractor=extractor,
    )

    assert result.source_tier == SourceTier.HIGH


@pytest.mark.asyncio
async def test_the_same_raw_confidence_is_distinguishable_by_source_tier() -> None:
    # D-049, the actual bug: a 0.6 from an official reference and a 0.6
    # from a tutorial used to be indistinguishable in the artifact. Same
    # raw extractor confidence, three different docs_url shapes -- the
    # *adjusted* confidence and reason_code must differ.
    discovery = DiscoveryExtraction(has_public_api=True)

    high = await classify(
        "example.com", docs_text="x", docs_url="https://example.com/api/reference",
        discovery=discovery, extractor=FakeExtractor(ClassifyExtraction(auth_scheme="api_key", confidence=0.6)),
    )
    medium = await classify(
        "example.com", docs_text="x", docs_url="https://docs.example.com/getting-started",
        discovery=discovery, extractor=FakeExtractor(ClassifyExtraction(auth_scheme="api_key", confidence=0.6)),
    )
    low = await classify(
        "example.com", docs_text="x", docs_url="https://trailhead.example.com/some-tutorial",
        discovery=discovery, extractor=FakeExtractor(ClassifyExtraction(auth_scheme="api_key", confidence=0.6)),
    )

    assert high.source_tier == SourceTier.HIGH
    assert medium.source_tier == SourceTier.MEDIUM
    assert low.source_tier == SourceTier.LOW

    assert high.confidence == 0.65  # 0.6 + 0.05
    assert medium.confidence == 0.6  # unchanged
    assert low.confidence == 0.45  # 0.6 - 0.15

    # The actual, material consequence: the exact same raw 0.6 clears the
    # threshold from an official reference, and is flagged from a tutorial.
    assert high.reason_code is None
    assert medium.reason_code is None
    assert low.reason_code == ReasonCode.CLASSIFY_LOW_CONFIDENCE


@pytest.mark.asyncio
async def test_low_tier_penalty_is_capped_at_zero_not_negative() -> None:
    discovery = DiscoveryExtraction(has_public_api=True)
    extractor = FakeExtractor(ClassifyExtraction(auth_scheme="api_key", confidence=0.05))

    result = await classify(
        "example.com", docs_text="x", docs_url="https://forum.example.com/thread",
        discovery=discovery, extractor=extractor,
    )

    assert result.confidence == 0.0
    assert result.reason_code == ReasonCode.CLASSIFY_LOW_CONFIDENCE


@pytest.mark.asyncio
async def test_scopes_and_redirect_uri_flag_are_passed_through() -> None:
    discovery = DiscoveryExtraction(has_public_api=True)
    extractor = FakeExtractor(
        ClassifyExtraction(
            auth_scheme="oauth2_auth_code",
            confidence=0.9,
            redirect_uris_required=True,
            scopes_available=["read:user", "repo"],
        )
    )

    result = await classify(
        "example.com", docs_text="x", docs_url="https://example.com", discovery=discovery, extractor=extractor
    )

    assert result.redirect_uris_required is True
    assert result.scopes_available == ["read:user", "repo"]
