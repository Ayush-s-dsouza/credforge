"""Structured JSONL logging with mandatory secret redaction.

Every handler credforge attaches gets both JSONLFormatter and a
RedactionFilter -- the two are meant to be inseparable. The filter runs
*before* formatting (it's a logging.Filter, not part of the Formatter), so
by the time JSONLFormatter.format() runs, record.msg/args/exc_text have
already been scrubbed. See redaction.py for the scrubbing logic itself.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .redaction import RedactionFilter

# Attribute names present on every plain LogRecord -- used to separate
# "standard" fields (handled explicitly below) from caller-supplied extras
# (e.g. extra={"stage": "resolve", "identity_key": "github.com"}), which get
# passed through into the JSON payload verbatim.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
}


class JSONLFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)

        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value

        return json.dumps(payload, default=str)


class RunLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that *merges* bound extras with per-call extras.

    The stdlib LoggerAdapter.process() overwrites kwargs["extra"] with
    self.extra entirely, silently dropping any extra= passed at the call
    site. That would mean a run_id bound once at startup wipes out a
    stage=/identity_key= passed on an individual log call. This subclass
    merges instead, call-site keys winning on conflict.
    """

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        extra = {**(self.extra or {}), **kwargs.get("extra", {})}
        kwargs["extra"] = extra
        return msg, kwargs


def configure_logging(*, run_id: str, log_dir: Path, level: int = logging.INFO) -> RunLoggerAdapter:
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("credforge")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = JSONLFormatter()
    redaction_filter = RedactionFilter()

    file_handler = logging.FileHandler(log_dir / "logs.jsonl", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction_filter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redaction_filter)
    logger.addHandler(stream_handler)

    return RunLoggerAdapter(logger, {"run_id": run_id})
