import pytest

from credforge.enums import AuthScheme, ReasonCode
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
