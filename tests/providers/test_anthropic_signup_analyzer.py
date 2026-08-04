"""Tests the real code path (prompt construction, timing, call-log
wiring) by mocking client.messages.parse() -- never a live network call,
never a real API key. Same pattern as test_anthropic_extractor.py.
"""

import json
from unittest.mock import AsyncMock

import pytest

from credforge.pipeline.llm_call_log import LlmCallLog
from credforge.providers.anthropic_signup_analyzer import AnthropicSignupFormAnalyzer
from credforge.providers.signup_generation import (
    CredentialLocationFinding,
    FormElement,
    RevealCandidate,
    RevealTriggerClassification,
    SignupFormClassification,
)


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, *, parsed_output, input_tokens: int, output_tokens: int) -> None:
        self.parsed_output = parsed_output
        self.usage = _FakeUsage(input_tokens, output_tokens)
        self.content = [_FakeTextBlock(parsed_output.model_dump_json())]


def _analyzer(call_log: LlmCallLog) -> AnthropicSignupFormAnalyzer:
    return AnthropicSignupFormAnalyzer(api_key="test-key-not-real", call_log=call_log)


@pytest.mark.asyncio
async def test_identify_reveal_trigger_writes_a_redacted_jsonl_entry(tmp_path) -> None:
    path = tmp_path / "llm_calls.jsonl"
    analyzer = _analyzer(LlmCallLog(path))
    parsed = RevealTriggerClassification(selector="a[href='/register']", reasoning="Register link", confidence=0.97)
    analyzer._client.messages.parse = AsyncMock(
        return_value=_FakeResponse(parsed_output=parsed, input_tokens=3046, output_tokens=98)
    )

    result = await analyzer.identify_reveal_trigger(
        url="https://finnhub.io/signup",
        candidates=[RevealCandidate(selector="a[href='/register']", tag="a", text="Register", href="/register")],
    )

    assert result is parsed
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["kind"] == "identify_reveal_trigger"
    assert "Register" in entry["prompt"]
    assert entry["confidence"] == 0.97
    assert entry["input_tokens"] == 3046
    assert entry["output_tokens"] == 98
    assert entry["duration_ms"] >= 0
    assert "a[href='/register']" in entry["raw_response"]


@pytest.mark.asyncio
async def test_classify_signup_form_writes_a_call_log_entry(tmp_path) -> None:
    path = tmp_path / "llm_calls.jsonl"
    analyzer = _analyzer(LlmCallLog(path))
    parsed = SignupFormClassification(field_map=[], submit_selector="button", confidence=0.75)
    analyzer._client.messages.parse = AsyncMock(
        return_value=_FakeResponse(parsed_output=parsed, input_tokens=1550, output_tokens=94)
    )

    await analyzer.classify_signup_form(
        url="https://resend.com/signup",
        elements=[FormElement(selector="[name='email']", tag="input", type="email")],
    )

    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["kind"] == "classify_signup_form"
    assert "resend.com/signup" in entry["prompt"]
    assert entry["confidence"] == 0.75


@pytest.mark.asyncio
async def test_locate_credential_redacts_a_credential_shaped_value_from_the_prompt(tmp_path) -> None:
    # This is the call most likely to carry a live, unregistered credential
    # straight in its prompt (the real post-submit page/email text) --
    # the exact case D-078/D-079's shape heuristic exists for.
    path = tmp_path / "llm_calls.jsonl"
    analyzer = _analyzer(LlmCallLog(path))
    parsed = CredentialLocationFinding(found=True, location="page", anchor_text="Your API key is:")
    analyzer._client.messages.parse = AsyncMock(
        return_value=_FakeResponse(parsed_output=parsed, input_tokens=500, output_tokens=40)
    )

    await analyzer.locate_credential(
        context_label="the signup page",
        content="Your API key is: aB3dE7fG9hJ2kL4mN6pQ8r -- keep it secret",
    )

    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "aB3dE7fG9hJ2kL4mN6pQ8r" not in entry["prompt"]
    assert "***REDACTED-SHAPED***" in entry["prompt"]


@pytest.mark.asyncio
async def test_default_call_log_is_a_safe_no_op_when_not_configured() -> None:
    analyzer = AnthropicSignupFormAnalyzer(api_key="test-key-not-real")
    parsed = RevealTriggerClassification(selector=None, reasoning=None, confidence=0.1)
    analyzer._client.messages.parse = AsyncMock(
        return_value=_FakeResponse(parsed_output=parsed, input_tokens=10, output_tokens=5)
    )

    result = await analyzer.identify_reveal_trigger(url="https://example.com", candidates=[])

    assert result is parsed  # no exception, nothing written anywhere
