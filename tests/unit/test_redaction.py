"""The spec-mandated test: a known secret must never appear in log output,
however it reaches the logger."""

import io
import json
import logging

from credforge.logging_setup import JSONLFormatter
from credforge.redaction import RedactedSecret, RedactionFilter, register_secret, scrub_secrets


def _make_logger(stream: io.StringIO) -> logging.Logger:
    logger = logging.getLogger("credforge.test.redaction")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONLFormatter())
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    return logger


def test_known_secret_never_appears_in_log_output() -> None:
    secret_value = "sk_live_ULTRA_SECRET_TOKEN_39fa2"
    register_secret(secret_value)

    stream = io.StringIO()
    logger = _make_logger(stream)

    # (1) secret passed as a %s logging argument
    logger.info("issued credential %s for app", secret_value)

    # (2) secret baked into an f-string before it ever reaches the logger
    logger.info(f"raw header: Authorization: Bearer {secret_value}")

    # (3) secret embedded in an exception message / traceback
    try:
        raise ValueError(f"upstream rejected token {secret_value}")
    except ValueError:
        logger.exception("validation call failed")

    # (4) secret embedded inside a logged dict's repr
    logger.info("credential payload: %s", {"api_key": secret_value})

    output = stream.getvalue()
    assert secret_value not in output
    assert "***REDACTED***" in output

    # sanity check: this isn't a no-op test that happens to log nothing
    lines = [json.loads(line) for line in output.strip().splitlines()]
    assert len(lines) == 4


def test_numeric_args_survive_the_filter_unmodified() -> None:
    # Regression guard: the filter must not coerce every arg to a string --
    # only ones that actually contain a secret. Otherwise ordinary %d
    # logging (e.g. latency_ms) would break once any secret is registered.
    register_secret("some-other-secret-not-used-here")

    stream = io.StringIO()
    logger = _make_logger(stream)
    logger.info("request took %d ms", 42)

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "request took 42 ms"


def test_redacted_secret_wrapper_never_reveals_value_directly() -> None:
    wrapped = RedactedSecret("another-secret-value")
    assert "another-secret-value" not in repr(wrapped)
    assert "another-secret-value" not in str(wrapped)
    assert wrapped.reveal() == "another-secret-value"


def test_scrub_secrets_protects_surfaces_that_never_touch_logging() -> None:
    # --explain's console output goes straight to rich.Console.print(),
    # never through the `logging` module -- RedactionFilter (a
    # logging.Filter) never sees it. scrub_secrets() is the same
    # scrubbing logic, exposed directly for exactly this surface.
    secret_value = "fake-example-secret-value-not-real-000111"
    register_secret(secret_value)

    message = f"receive_credential_email succeeded using password {secret_value}"
    scrubbed = scrub_secrets(message)

    assert secret_value not in scrubbed
    assert "***REDACTED***" in scrubbed
