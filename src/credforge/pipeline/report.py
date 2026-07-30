"""REPORT: aggregates a run's already-emitted HandoffArtifacts into one
summary -- counts by status/reason_code, and a HITL attention list a human
can act on without opening every artifact individually.

REPORT never re-runs anything. Everything it needs is already on disk from
EMIT (`.credforge/runs/<run_id>/artifacts/*.json`); REPORT reads and
aggregates, nothing more. See DECISIONS.md D-034.

Split the same way EMIT is (D-033): `build_report()` is pure (artifacts
in, report out, no I/O), separable from `load_artifacts()`/`generate_report()`
which do the actual file I/O -- the aggregation logic is fully testable
without ever touching a filesystem.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..enums import ReasonCode, Status
from ..models.artifact import HandoffArtifact

logger = logging.getLogger("credforge.report")


class AttentionItem(BaseModel):
    identity_key: str
    app_name: str
    reason_code: ReasonCode
    completeness_gap_fields: list[str] = Field(default_factory=list)


class RunReport(BaseModel):
    run_id: str
    generated_at: datetime
    total_apps: int
    skipped_files: int  # artifact files present but unreadable -- see load_artifacts()
    status_counts: dict[str, int]
    reason_code_counts: dict[str, int]
    needs_attention: list[AttentionItem]  # HITL apps only, sorted by reason_code for scanability
    auto_apps: list[str]  # identity_keys
    unsupported_apps: list[str]  # identity_keys


def build_report(run_id: str, artifacts: list[HandoffArtifact], *, skipped_files: int = 0) -> RunReport:
    status_counts: dict[str, int] = {}
    reason_code_counts: dict[str, int] = {}
    needs_attention: list[AttentionItem] = []
    auto_apps: list[str] = []
    unsupported_apps: list[str] = []

    for artifact in artifacts:
        status_counts[artifact.status.value] = status_counts.get(artifact.status.value, 0) + 1
        reason_code_counts[artifact.reason_code.value] = reason_code_counts.get(artifact.reason_code.value, 0) + 1

        if artifact.status == Status.AUTO:
            auto_apps.append(artifact.app.identity_key)
        elif artifact.status == Status.UNSUPPORTED:
            unsupported_apps.append(artifact.app.identity_key)
        else:  # HITL
            needs_attention.append(
                AttentionItem(
                    identity_key=artifact.app.identity_key,
                    app_name=artifact.app.app_name,
                    reason_code=artifact.reason_code,
                    completeness_gap_fields=[g.field for g in artifact.completeness_gaps],
                )
            )

    needs_attention.sort(key=lambda item: (item.reason_code.value, item.identity_key))

    return RunReport(
        run_id=run_id,
        generated_at=datetime.now(timezone.utc),
        total_apps=len(artifacts),
        skipped_files=skipped_files,
        status_counts=status_counts,
        reason_code_counts=reason_code_counts,
        needs_attention=needs_attention,
        auto_apps=auto_apps,
        unsupported_apps=unsupported_apps,
    )


def load_artifacts(artifacts_dir: Path) -> tuple[list[HandoffArtifact], int]:
    """A corrupted/unreadable artifact file is skipped, not fatal to the
    whole report -- same "one bad entry doesn't sink the batch" principle
    as AppendOnlyRegistry.load_all()'s corrupted-line handling (D-006)."""
    if not artifacts_dir.exists():
        return [], 0

    artifacts: list[HandoffArtifact] = []
    skipped = 0
    for path in sorted(artifacts_dir.glob("*.json")):
        try:
            artifacts.append(HandoffArtifact.model_validate_json(path.read_text(encoding="utf-8")))
        except (ValidationError, ValueError, OSError):
            logger.warning("skipping unreadable artifact file", extra={"path": str(path)})
            skipped += 1
    return artifacts, skipped


def generate_report(run_id: str, *, data_dir: Path) -> RunReport:
    artifacts_dir = data_dir / "runs" / run_id / "artifacts"
    artifacts, skipped = load_artifacts(artifacts_dir)
    report = build_report(run_id, artifacts, skipped_files=skipped)

    report_path = data_dir / "runs" / run_id / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    return report
