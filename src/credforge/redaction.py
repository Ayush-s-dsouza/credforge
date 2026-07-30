"""Secret redaction: two independent layers.

1. RedactedSecret -- a wrapper type whose repr/str always print a fixed
   placeholder. Anything that stores a live secret in a RedactedSecret
   instead of a bare str is structurally safe to accidentally log or print.
2. RedactionFilter -- a logging.Filter that scrubs any value previously
   registered via register_secret() out of a LogRecord before it is
   formatted, whether it arrived as the message, a %-style arg, or an
   exception's message/traceback. This is the safety net for the (common)
   case where a secret reaches the logger as a bare str, not wrapped.

Both layers key off the same process-wide registry so that calling
register_secret() once (right after a credential is minted) protects every
subsequent log call for the lifetime of the process. See DECISIONS.md
D-004 for why both layers exist rather than just one.
"""

import logging
import threading

_PLACEHOLDER = "***REDACTED***"


class RedactedSecret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return _PLACEHOLDER

    def __str__(self) -> str:
        return _PLACEHOLDER

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RedactedSecret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


_MIN_REGISTERABLE_LENGTH = 8  # see register_secret()'s docstring


class _SecretRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()

    def register(self, value: str | None) -> None:
        if not value or len(value) < _MIN_REGISTERABLE_LENGTH:
            return
        with self._lock:
            self._secrets.add(value)

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            # Longest-first so a secret that happens to be a substring of
            # another registered secret still gets fully masked.
            return tuple(sorted(self._secrets, key=len, reverse=True))


_registry = _SecretRegistry()


def register_secret(value: "str | RedactedSecret | None") -> None:
    """Register a value so every subsequent log line / --explain message /
    scrub_secrets() call masks it.

    Values shorter than _MIN_REGISTERABLE_LENGTH are silently ignored, not
    registered -- found live, via a test whose fixture credential was the
    single character "t": registering a value that short turns every
    occurrence of the letter "t" in *any* subsequent log line into
    "***REDACTED***", corrupting unrelated text ("connection_error"
    becomes "connec***REDACTED***ion_error"). No real credential this
    project generates or accepts is anywhere near this short (generated
    account passwords are 24+ characters; every real API key/token seen
    so far is 16+); a value this short reaching register_secret() is
    either test data or not actually a usable secret, and either way
    should not be able to mangle unrelated output. See DECISIONS.md D-045.
    """
    if isinstance(value, RedactedSecret):
        value = value.reveal()
    _registry.register(value)


def scrub_secrets(text: str) -> str:
    """Scrub every registered secret out of a plain string directly --
    for surfaces that never go through the `logging` module at all (e.g.
    the CLI's `--explain` console output, printed straight via
    `rich.console.Console.print()`). `RedactionFilter` only protects
    `logging`-based output; this is the same scrubbing logic exposed for
    everything else."""
    return _scrub(text, _registry.snapshot())


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, _PLACEHOLDER)
    return text


def _scrub_arg(value: object, secrets: tuple[str, ...]) -> object:
    """Scrub one logging arg, preserving its original type if nothing matched.

    Coercing every arg to str unconditionally would break %d/%r-style
    formatting for ordinary numeric args (`"%d" % "5"` raises TypeError).
    Only args that actually contain a registered secret get replaced --
    and only then does their type change to str, which is safe because a
    secret-bearing value was never going to be validly used as %d anyway.
    """
    text = str(value)
    scrubbed = _scrub(text, secrets)
    return scrubbed if scrubbed != text else value


class RedactionFilter(logging.Filter):
    """Attach to every handler that could write secrets to disk or stdout."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _registry.snapshot()
        if not secrets:
            return True

        record.msg = _scrub(str(record.msg), secrets)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_arg(v, secrets) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub_arg(a, secrets) for a in record.args)

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _scrub(record.exc_text, secrets)

        return True
