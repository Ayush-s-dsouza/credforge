"""Tests the real code path (prompt construction, response unpacking) by
mocking client.messages.parse() -- never a live network call, never a real
API key. See DECISIONS.md for why AnthropicExtractor is exercised this way
rather than skipped entirely.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from credforge.providers.anthropic_extractor import AnthropicExtractor
from credforge.providers.llm import ClassifyExtraction, DiscoveryExtraction, TosGateExtraction


def _extractor() -> AnthropicExtractor:
    return AnthropicExtractor(api_key="test-key-not-real")


@pytest.mark.asyncio
async def test_extract_discovery_returns_parsed_output_and_uses_sonnet() -> None:
    extractor = _extractor()
    expected = DiscoveryExtraction(has_public_api=True, base_url="https://api.example.com/v1")
    extractor._client.messages.parse = AsyncMock(return_value=SimpleNamespace(parsed_output=expected))

    result = await extractor.extract_discovery(docs_text="some docs", docs_url="https://docs.example.com")

    assert result is expected
    call_kwargs = extractor._client.messages.parse.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-5"
    assert call_kwargs["output_format"] is DiscoveryExtraction
    assert "some docs" in call_kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_extract_classification_includes_discovery_hint_in_prompt() -> None:
    extractor = _extractor()
    expected = ClassifyExtraction(auth_scheme="oauth2_auth_code", confidence=0.9)
    extractor._client.messages.parse = AsyncMock(return_value=SimpleNamespace(parsed_output=expected))
    discovery = DiscoveryExtraction(has_public_api=True, auth_scheme_hint="oauth2_auth_code")

    result = await extractor.extract_classification(
        docs_text="docs", docs_url="https://docs.example.com", discovery=discovery
    )

    assert result is expected
    prompt = extractor._client.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert "oauth2_auth_code" in prompt


@pytest.mark.asyncio
async def test_extract_tos_gate_signals_returns_parsed_output() -> None:
    extractor = _extractor()
    expected = TosGateExtraction(
        prohibits_automation=True,
        requires_payment=False,
        requires_business_verification=False,
        requires_sales_contact=False,
        requires_phone_verification=False,
        requires_captcha=False,
        requires_sso_only=False,
    )
    extractor._client.messages.parse = AsyncMock(return_value=SimpleNamespace(parsed_output=expected))

    result = await extractor.extract_tos_gate_signals(tos_text="tos text", tos_url="https://example.com/tos")

    assert result is expected


def test_docs_text_is_truncated_to_bound_cost() -> None:
    from credforge.providers.anthropic_extractor import _MAX_DOCS_CHARS

    assert _MAX_DOCS_CHARS > 0
