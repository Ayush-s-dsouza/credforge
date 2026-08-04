import json

from credforge.pipeline.llm_call_log import LlmCallLog, llm_call_log_path
from credforge.providers.signup_generation import RevealTriggerClassification
from credforge.redaction import register_secret


def test_null_instance_is_a_safe_no_op(tmp_path) -> None:
    log = LlmCallLog(path=None)
    log.record(
        kind="identify_reveal_trigger",
        prompt="p",
        raw_response="r",
        parsed_result=None,
        duration_ms=1.0,
        input_tokens=1,
        output_tokens=1,
    )
    # nothing should have been created anywhere
    assert list(tmp_path.iterdir()) == []


def test_record_writes_one_jsonl_line_with_expected_fields(tmp_path) -> None:
    path = tmp_path / "runs" / "run-1" / "llm_calls.jsonl"
    log = LlmCallLog(path)
    result = RevealTriggerClassification(selector="a[href='/register']", reasoning="why", confidence=0.97)

    log.record(
        kind="identify_reveal_trigger",
        prompt="the prompt text",
        raw_response='{"selector": "a[href=\'/register\']"}',
        parsed_result=result,
        duration_ms=123.4,
        input_tokens=3046,
        output_tokens=153,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "identify_reveal_trigger"
    assert entry["prompt"] == "the prompt text"
    assert entry["confidence"] == 0.97
    assert entry["duration_ms"] == 123.4
    assert entry["input_tokens"] == 3046
    assert entry["output_tokens"] == 153
    assert "a[href='/register']" in entry["parsed_result"]


def test_record_appends_rather_than_overwrites(tmp_path) -> None:
    path = tmp_path / "llm_calls.jsonl"
    log = LlmCallLog(path)
    for i in range(3):
        log.record(
            kind="classify_signup_form",
            prompt=f"prompt {i}",
            raw_response="{}",
            parsed_result=None,
            duration_ms=1.0,
            input_tokens=None,
            output_tokens=None,
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["prompt"] for line in lines] == ["prompt 0", "prompt 1", "prompt 2"]


def test_registered_secret_is_redacted_from_prompt_and_response(tmp_path) -> None:
    path = tmp_path / "llm_calls.jsonl"
    log = LlmCallLog(path)
    register_secret("SUPER-SECRET-REGISTERED-VALUE-XYZ123")

    log.record(
        kind="locate_credential",
        prompt="the page said: SUPER-SECRET-REGISTERED-VALUE-XYZ123 is your key",
        raw_response='{"anchor_text": "Your key is:"} SUPER-SECRET-REGISTERED-VALUE-XYZ123',
        parsed_result=None,
        duration_ms=1.0,
        input_tokens=None,
        output_tokens=None,
    )

    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "SUPER-SECRET-REGISTERED-VALUE-XYZ123" not in entry["prompt"]
    assert "SUPER-SECRET-REGISTERED-VALUE-XYZ123" not in entry["raw_response"]
    assert "***REDACTED***" in entry["prompt"]


def test_credential_shaped_value_is_redacted_even_if_never_registered(tmp_path) -> None:
    # This is the case that matters most: locate_credential's prompt is the
    # REAL post-submit page text, which may contain the actual live
    # credential value before it has ever been extracted or registered as
    # a known secret (register_secret() only happens after extraction
    # succeeds downstream). The shape heuristic has to catch it anyway.
    path = tmp_path / "llm_calls.jsonl"
    log = LlmCallLog(path)
    unregistered_looking_key = "aB3dE7fG9hJ2kL4mN6pQ8r"  # 22 chars, letters+digits, never registered

    log.record(
        kind="locate_credential",
        prompt=f"Your dedicated access key is: {unregistered_looking_key}",
        raw_response="{}",
        parsed_result=None,
        duration_ms=1.0,
        input_tokens=None,
        output_tokens=None,
    )

    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert unregistered_looking_key not in entry["prompt"]
    assert "***REDACTED-SHAPED***" in entry["prompt"]


def test_plain_english_prompt_is_not_touched_by_the_shape_heuristic(tmp_path) -> None:
    path = tmp_path / "llm_calls.jsonl"
    log = LlmCallLog(path)
    prompt = "Which single element, if clicked, is MOST likely to reveal a signup form?"

    log.record(
        kind="identify_reveal_trigger",
        prompt=prompt,
        raw_response="{}",
        parsed_result=None,
        duration_ms=1.0,
        input_tokens=None,
        output_tokens=None,
    )

    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["prompt"] == prompt


def test_llm_call_log_path_matches_the_runs_directory_layout(tmp_path) -> None:
    assert llm_call_log_path(tmp_path, "run-123") == tmp_path / "runs" / "run-123" / "llm_calls.jsonl"
