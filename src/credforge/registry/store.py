"""Append-only registry of pipeline stage completions and provisioned accounts.

Every stage completion (and every revoke) is one appended JSONL line --
never a rewrite of an existing line. This is what makes resumability safe:
a crash mid-write can, at worst, leave one truncated trailing line, and an
append-only log makes that line trivially identifiable and skippable
without touching anything written before it. See DECISIONS.md D-006.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from ..models.registry_entities import RegistryEntry

logger = logging.getLogger("credforge.registry")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AppendOnlyRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: RegistryEntry) -> None:
        line = entry.model_dump_json()
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def load_all(self) -> list[RegistryEntry]:
        with self._lock:
            raw_lines = self._path.read_text(encoding="utf-8").splitlines()

        entries: list[RegistryEntry] = []
        for line_number, raw in enumerate(raw_lines, start=1):
            if not raw.strip():
                continue
            try:
                entries.append(RegistryEntry.model_validate_json(raw))
            except (json.JSONDecodeError, ValueError):
                logger.warning(
                    "skipping corrupted registry line",
                    extra={"line_number": line_number, "raw_excerpt": raw[:120]},
                )
                continue
        return entries

    def latest_by_stage(self) -> dict[tuple[str, str], RegistryEntry]:
        latest: dict[tuple[str, str], RegistryEntry] = {}
        for entry in self.load_all():
            key = (entry.identity_key, entry.stage.value)
            existing = latest.get(key)
            if existing is None or entry.recorded_at >= existing.recorded_at:
                latest[key] = entry
        return latest

    def find_open_provision(self, identity_key: str) -> RegistryEntry | None:
        # Append-only means both the original "completed, closed=False" entry
        # and a later "closed=True" entry (written by close()) coexist in the
        # log forever. The *latest* one by recorded_at is the current state --
        # filtering on `not e.closed` across all of history would wrongly
        # resurrect an account that was already closed.
        provision_entries = [
            e
            for e in self.load_all()
            if e.identity_key == identity_key
            and e.stage.value == "provision"
            and e.stage_status.value == "completed"
        ]
        if not provision_entries:
            return None
        latest = max(provision_entries, key=lambda e: e.recorded_at)
        return None if latest.closed else latest

    def close(self, identity_key: str, *, run_id: str) -> RegistryEntry:
        entry = self.find_open_provision(identity_key)
        if entry is None:
            raise LookupError(f"no open provisioned account found for identity_key={identity_key!r}")
        closed_entry = entry.model_copy(update={"closed": True, "run_id": run_id, "recorded_at": _now()})
        self.append(closed_entry)
        return closed_entry
