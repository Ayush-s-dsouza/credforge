"""JSONL audit log for every LLM call made during signup-recipe
generation (DISCOVER_SIGNUP). One line per call: prompt, raw response
text, parsed result, confidence, timing, token usage.

This is the only surviving record of a generation run's LLM calls once
the process exits. Part 1 of this session's audit found tonight's
original three generation runs left no trace at all -- nothing in the
codebase logged a request or response anywhere, so a past run could
never be audited after the fact, only re-derived by calling the LLM
again against the same (possibly-changed) live page. This closes that
gap, and doubles as the only data that could ever let the generator's
prompts be improved from real usage rather than guesswork.

Anything credential-shaped is redacted before a line is written, via two
independent layers (same reasoning as redaction.py's own two-layer
design, D-004): `scrub_secrets()` catches values already confirmed and
registered as real credentials; `_CREDENTIAL_SHAPED_RE` is a broader
heuristic that also catches long token-looking runs the model may quote
in an unconfirmed context (e.g. `locate_credential`'s reasoning about a
value it found, before that value has been validated or registered) --
scrub_secrets() alone can't protect a value it doesn't know about yet.
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..redaction import scrub_secrets

# Defense in depth alongside scrub_secrets(), not a replacement for it:
# 16+ characters of letters/digits/-/_ with at least one of each is the
# shape of essentially every API key/token/secret this project has seen
# (generated passwords are 24+ chars; every real key seen so far is
# 16+ -- the same threshold redaction.py's register_secret() reasons
# from). Requiring both a letter AND a digit keeps a plain English
# sentence or URL path from tripping it.
_CREDENTIAL_SHAPED_RE = re.compile(
    r"\b(?=[A-Za-z0-9_-]{16,}\b)(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{16,}\b"
)
_SHAPE_PLACEHOLDER = "***REDACTED-SHAPED***"


def _redact(text: str) -> str:
    return _CREDENTIAL_SHAPED_RE.sub(_SHAPE_PLACEHOLDER, scrub_secrets(text))


class LlmCallLog:
    """Appends one redacted JSONL line per LLM call to
    `<data_dir>/runs/<run_id>/llm_calls.jsonl`. A no-op instance
    (`path=None`) is used wherever a call isn't part of a real run (unit
    tests, ad-hoc scratch scripts) so call sites never need an `if
    logger:` branch -- `.record()` is always safe to call."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        kind: str,
        prompt: str,
        raw_response: str,
        parsed_result: BaseModel | None,
        duration_ms: float,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        if self._path is None:
            return
        parsed_dict: dict[str, Any] | None = parsed_result.model_dump() if parsed_result is not None else None
        confidence = getattr(parsed_result, "confidence", None)
        line = {
            "ts": time.time(),
            "kind": kind,
            "prompt": _redact(prompt),
            "raw_response": _redact(raw_response),
            "parsed_result": _redact(json.dumps(parsed_dict)) if parsed_dict is not None else None,
            "confidence": confidence,
            "duration_ms": round(duration_ms, 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


NULL_LLM_CALL_LOG = LlmCallLog(path=None)


def llm_call_log_path(data_dir: Path, run_id: str) -> Path:
    return data_dir / "runs" / run_id / "llm_calls.jsonl"
